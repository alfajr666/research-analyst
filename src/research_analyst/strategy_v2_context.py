"""Shared market context for confluence v2 strategy plugins."""

from __future__ import annotations

import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import polars as pl

import config
from alpha_outbox import OUTBOX_DIR
from structure_zones import compute_atr, detect_fvg, detect_order_blocks


MAX_BAR_AGE = timedelta(minutes=20)
LOOKBACK_DAYS = 16


def _asset_from_symbol(symbol: str) -> str:
    s = str(symbol or "").strip()
    upper = s.upper()
    if "_PERP" in s or "_PERP.A" in s or s.endswith(".A"):
        base = s.split("_")[0].split("USDT")[0].split("USD")[0]
        return base.upper() or "BTC"
    if "-USDT-PERP" in upper:
        return upper.split("-")[0]
    # fallback guess
    for c in ("BTC", "ETH", "SOL", "PAXG", "XAUT"):
        if c in upper:
            return c
    for suffix in ("USDT", "USD"):
        if upper.endswith(suffix) and len(upper) > len(suffix):
            return upper[:-len(suffix)].rstrip("_-") or "BTC"
    return upper or "BTC"


def completed_cycle(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)


_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def completed_cycle_for(now: datetime | None, interval: str) -> datetime:
    """Floor `now` to the most recent completed `interval` bar boundary."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minutes = _INTERVAL_MINUTES.get(interval, 15)
    if minutes >= 60:
        hours = minutes // 60
        return now.replace(hour=now.hour - now.hour % hours, minute=0, second=0, microsecond=0)
    return now.replace(minute=now.minute - now.minute % minutes, second=0, microsecond=0)


def cutoff_from_id(cutoff_id: str, fallback: datetime | None = None) -> datetime:
    """Parse an evaluator cutoff ID or explicit cutoff without using wall time."""
    text = str(cutoff_id or "")
    if text[:4].isdigit():
        pass
    elif ":" in text:
        text = text.split(":", 1)[1]
    elif "20" in text:
        text = text[text.find("20"):]
    try:
        return _ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except (TypeError, ValueError):
        if fallback is None:
            raise
        return _ensure_utc(fallback)


def _ensure_utc(ts: datetime) -> datetime:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _load_raw_observations_for_asset(conn, asset: str, cutoff: datetime, start: datetime,
                                     interval: str = "15m", include_invalid: bool = False) -> List[Dict]:
    """Internal: raw rows with source for prefer logic."""
    cutoff = _ensure_utc(cutoff)
    validity_filter = "" if include_invalid else "AND CAST(json_extract(payload_json, '$.close') AS REAL) > 0"
    query_cutoff = cutoff + timedelta(minutes=5) if include_invalid else cutoff
    rows = conn.execute(
        f"""
        SELECT source_end, source,
               CAST(json_extract(payload_json, '$.open') AS REAL),
               CAST(json_extract(payload_json, '$.high') AS REAL),
               CAST(json_extract(payload_json, '$.low') AS REAL),
               CAST(json_extract(payload_json, '$.close') AS REAL),
               COALESCE(CAST(json_extract(payload_json, '$.volume') AS REAL), 0.0),
               CAST(json_extract(payload_json, '$.open_interest') AS REAL),
               CAST(json_extract(payload_json, '$.funding_rate') AS REAL),
               payload_json, observation_id
          FROM source_observations
          WHERE asset = ? AND interval=?
             AND source_end <= ? AND source_end >= ?
             {validity_filter}
           ORDER BY source_end ASC
        """,
        (asset, interval, query_cutoff, start),
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "timestamp": _ensure_utc(r[0]),
            "source": r[1],
            "open": float(r[2] or 0),
            "high": float(r[3] or 0),
            "low": float(r[4] or 0),
            "close": float(r[5] or 0),
            "volume": float(r[6] or 0),
            "open_interest": float(r[7]) if r[7] is not None else None,
            "funding_rate": float(r[8]) if r[8] is not None else None,
            "payload": r[9],
            "source_observation_ids": [str(r[10])] if r[10] else [],
        })
    return out


def _prefer_rows(raw_rows: List[Dict]) -> List[Dict]:
    """For each timestamp prefer configured live WS data, then failover."""
    from collections import defaultdict
    by_ts: Dict[datetime, List[Dict]] = defaultdict(list)
    for r in raw_rows:
        by_ts[r["timestamp"]].append(r)
    preferred = []
    for ts, lst in sorted(by_ts.items()):
        if getattr(config, "COINANALYZE_EVAL_ENABLED", False):
            ca = [x for x in lst if x["source"] == "coinalyze"]
            if ca:
                preferred.append(ca[0])
                continue
        ws = [x for x in lst if str(x["source"]).endswith("_ws")]
        if ws:
            preferred.append(ws[0])
            continue
        vagg = [x for x in lst if x["source"] == getattr(config, "FAILOVER_SOURCE_NAME", "venue_agg_v1")]
        if vagg:
            preferred.append(vagg[0])
            continue
        # fallback any
        preferred.append(lst[0])
    return preferred


def load_preferred_15m_bars(conn, asset: Optional[str] = None, native_symbol: Optional[str] = None,
                            cutoff: Optional[datetime] = None, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Canonical preferred loader: usable CA wins over venue_agg_v1 for same bar end.
    If native_symbol given and looks CA, resolve to asset.
    """
    if cutoff is None:
        cutoff = _ensure_utc(datetime.now(timezone.utc))
    else:
        cutoff = _ensure_utc(cutoff)
    start = cutoff - timedelta(days=lookback_days)
    if asset is None and native_symbol:
        asset = _asset_from_symbol(native_symbol)
    if not asset:
        asset = "BTC"
    return load_bars_for_interval(conn, asset, "15m", cutoff, lookback_days)


def load_15m_bars(conn, symbol: str, cutoff: datetime, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Backward compat: delegate to preferred by asset."""
    asset = _asset_from_symbol(symbol)
    return load_preferred_15m_bars(conn, asset=asset, cutoff=cutoff, lookback_days=lookback_days)


HYBRID_HTF_DATA_CONTRACT_VERSION = "hybrid-htf-v1"
_HYBRID_HTF_CONTEXT: ContextVar["HybridHTFContext | None"] = ContextVar(
    "hybrid_htf_context", default=None
)


def _normalise_bar_end(value: Any) -> datetime:
    """Normalize exact and exchange boundary-minus-one-millisecond ends."""
    timestamp = _ensure_utc(value)
    if timestamp.microsecond == 0:
        return timestamp
    if timestamp.microsecond == 999000:
        return (timestamp + timedelta(milliseconds=1)).replace(microsecond=0)
    raise ValueError(f"bar end is not an exact or millisecond boundary: {timestamp!s}")


def _floor_boundary(value: datetime, seconds: int) -> datetime:
    epoch = int(_ensure_utc(value).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, timezone.utc)


def _contiguous_canonical_tail(rows: list[dict[str, Any]], cutoff: datetime) -> tuple[list[dict[str, Any]], str | None]:
    grouped: dict[datetime, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            end = _normalise_bar_end(row["timestamp"])
            prices = [float(row[name]) for name in ("open", "high", "low", "close")]
            volume = float(row.get("volume") or 0.0)
        except (KeyError, TypeError, ValueError, OverflowError):
            return [], "canonical_tail_invalid"
        if (end.timestamp() % 300 != 0
                or not all(math.isfinite(value) and value > 0 for value in prices)
                or prices[1] < max(prices[0], prices[3])
                or prices[2] > min(prices[0], prices[3])
                or not math.isfinite(volume) or volume < 0):
            return [], "canonical_tail_invalid"
        if end > cutoff:
            return [], "canonical_tail_future"
        grouped.setdefault(end, []).append(row)
    if any(len(values) > 1 for values in grouped.values()):
        return [], "canonical_tail_duplicate"
    by_end = {
        _normalise_bar_end(row["timestamp"]): row
        for row in _prefer_rows(rows)
        if _normalise_bar_end(row["timestamp"]) <= cutoff
    }
    expected = _floor_boundary(cutoff, 300)
    if expected not in by_end:
        return [], "canonical_tail_missing"
    tail = []
    cursor = expected
    while cursor in by_end:
        tail.append(by_end[cursor])
        cursor -= timedelta(minutes=5)
    tail.reverse()
    return tail, None


def _direct_seed_is_contiguous(frame: pl.DataFrame, interval: str, required: int,
                               handoff: datetime) -> bool:
    if frame.is_empty() or frame.height < required:
        return False
    try:
        ends = [_ensure_utc(value) for value in frame["timestamp"].to_list()]
        for row in frame.to_dicts():
            prices = [float(row[name]) for name in ("open", "high", "low", "close")]
            volume = float(row.get("volume") or 0.0)
            if (not all(math.isfinite(value) and value > 0 for value in prices)
                    or prices[1] < max(prices[0], prices[3])
                    or prices[2] > min(prices[0], prices[3])
                    or not math.isfinite(volume) or volume < 0):
                return False
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    seconds = {"1h": 3600, "4h": 14400}[interval]
    return (
        len(ends) >= required
        and ends[-1] == handoff
        and all(int((right - left).total_seconds()) == seconds
                for left, right in zip(ends, ends[1:]))
    )


class HybridHTFContext:
    """Invocation-scoped engine source selection for strategy HTF frames."""

    def __init__(self, market_conn: Any, regime_conn: Any | None, cutoff: datetime):
        self.market_conn = market_conn
        self.regime_conn = regime_conn
        self.cutoff = _ensure_utc(cutoff)
        self._frames: dict[tuple[str, str, int], pl.DataFrame] = {}
        self._diagnostics: dict[str, dict[str, dict[str, Any]]] = {}

    def summary(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            asset: {interval: dict(details) for interval, details in intervals.items()}
            for asset, intervals in self._diagnostics.items()
        }

    def _record(self, asset: str, interval: str, **details: Any) -> None:
        self._diagnostics.setdefault(asset, {})[interval] = {
            "data_contract_version": HYBRID_HTF_DATA_CONTRACT_VERSION,
            "cutoff_at": self.cutoff.isoformat(),
            **details,
        }

    def load(self, symbol: str, interval: str, lookback_days: int) -> pl.DataFrame:
        asset = _asset_from_symbol(symbol)
        key = (asset, interval, int(lookback_days))
        if key in self._frames:
            return self._frames[key]

        base_lookback = max(int(lookback_days), 60 if interval == "4h" else 16)
        start = self.cutoff - timedelta(days=base_lookback)
        try:
            raw = _load_raw_observations_for_asset(
                self.market_conn, asset, self.cutoff, start, interval="5m", include_invalid=True
            )
        except Exception as exc:
            self._record(asset, interval, availability="unavailable",
                         hybrid_readiness="not_ready", reason="canonical_tail_unavailable",
                         error=type(exc).__name__)
            self._frames[key] = pl.DataFrame()
            return self._frames[key]
        tail, tail_reason = _contiguous_canonical_tail(raw, self.cutoff)
        if not tail:
            self._record(asset, interval, availability="unavailable",
                         hybrid_readiness="not_ready", reason=tail_reason)
            self._frames[key] = pl.DataFrame()
            return self._frames[key]

        seconds = {"1h": 3600, "4h": 14400}.get(interval)
        if seconds is None:
            raise ValueError(f"unsupported hybrid interval: {interval}")
        seed_required = int(getattr(
            config,
            f"HYBRID_HTF_{'1H' if interval == '1h' else '4H'}_SEED_BARS",
            240,
        ))
        retain_days = int(getattr(
            config,
            f"HYBRID_HTF_{'1H' if interval == '1h' else '4H'}_RETAIN_DAYS",
            14 if interval == "1h" else 45,
        ))
        interval_seconds = seconds
        seed_reserve = max(300, retain_days * 86400 - seed_required * interval_seconds)
        desired_handoff = _floor_boundary(
            self.cutoff - timedelta(seconds=seed_reserve), seconds
        )
        earliest_handoff = _floor_boundary(_normalise_bar_end(tail[0]["timestamp"]), seconds)
        handoff = max(desired_handoff, earliest_handoff)
        tail_after_handoff = [
            row for row in tail
            if _normalise_bar_end(row["timestamp"]) > handoff
        ]
        if (not tail_after_handoff
                or _normalise_bar_end(tail_after_handoff[0]["timestamp"])
                != handoff + timedelta(minutes=5)):
            self._record(asset, interval, availability="unavailable",
                         hybrid_readiness="not_ready", reason="canonical_tail_gap",
                         handoff_at=handoff.isoformat())
            self._frames[key] = pl.DataFrame()
            return self._frames[key]

        direct = pl.DataFrame()
        direct_ids: list[str] = []
        direct_versions: list[str] = []
        direct_error: str | None = None
        if self.regime_conn is not None:
            try:
                from regime_history import load_regime_1h_bars, load_regime_4h_bars
                loader = load_regime_1h_bars if interval == "1h" else load_regime_4h_bars
                direct = loader(self.regime_conn, asset, handoff, limit=seed_required)
                if not direct.is_empty() and not _direct_seed_is_contiguous(
                    direct, interval, seed_required, handoff
                ):
                    self._record(asset, interval, availability="unavailable",
                                 hybrid_readiness="not_ready",
                                 reason="direct_seed_incomplete", handoff_at=handoff.isoformat(),
                                 direct_bar_ids=[str(value) for value in direct["bar_id"].to_list()]
                                 if "bar_id" in direct.columns else [])
                    self._frames[key] = pl.DataFrame()
                    return self._frames[key]
                direct_ids = [str(value) for value in direct["bar_id"].to_list()]
                direct_versions = [str(value) for value in direct["bar_version"].unique().to_list()]
            except Exception as exc:
                direct_error = type(exc).__name__
                direct = pl.DataFrame()
                if direct_error in {"KeyError", "TypeError", "ValueError", "OverflowError"}:
                    self._record(asset, interval, availability="unavailable",
                                 hybrid_readiness="not_ready", reason="direct_seed_invalid",
                                 error=direct_error, handoff_at=handoff.isoformat())
                    self._frames[key] = pl.DataFrame()
                    return self._frames[key]
        if direct.is_empty() and getattr(config, "HYBRID_HTF_MODE", "shadow") != "shadow":
            self._record(asset, interval, availability="unavailable",
                         hybrid_readiness="not_ready",
                         reason="direct_seed_missing", error=direct_error,
                         handoff_at=handoff.isoformat())
            self._frames[key] = pl.DataFrame()
            return self._frames[key]

        local = resample_ohlcv(_rows_to_frame(tail_after_handoff), interval)
        if local.is_empty() and direct.is_empty():
            self._record(asset, interval, availability="unavailable",
                         hybrid_readiness="not_ready", reason="insufficient_htf_tail",
                         handoff_at=handoff.isoformat())
            self._frames[key] = pl.DataFrame()
            return self._frames[key]
        canonical_ids = sorted({
            str(observation_id)
            for row in local.to_dicts()
            for observation_id in row.get("source_observation_ids", [])
            if observation_id
        })

        by_end: dict[datetime, dict[str, Any]] = {}
        if not direct.is_empty():
            for row in direct.to_dicts():
                row["source_provenance"] = [str(row.get("source") or "bybit_rest")]
                row["data_purity"] = "direct_rest"
                by_end[_ensure_utc(row["timestamp"])] = row
        for row in local.to_dicts():
            by_end[_ensure_utc(row["timestamp"])] = row
        merged = pl.DataFrame(
            [by_end[end] for end in sorted(by_end)], strict=False
        )
        if "open_interest" not in merged.columns:
            merged = merged.with_columns(pl.lit(0.0).alias("open_interest"))
        if "funding_rate" not in merged.columns:
            merged = merged.with_columns(pl.lit(0.0).alias("funding_rate"))
        self._record(
            asset, interval, availability="ready",
            hybrid_readiness="ready" if not direct.is_empty() else "not_ready",
            source_mode="hybrid" if not direct.is_empty() else "canonical_only",
            handoff_at=handoff.isoformat(), direct_bar_ids=direct_ids,
            direct_bar_versions=direct_versions,
            canonical_5m_observation_ids=canonical_ids,
        )
        self._frames[key] = merged
        return merged


@contextmanager
def hybrid_htf_context(market_db_path: str | Path | None, regime_db_path: str | Path | None,
                       cutoff: datetime):
    """Install one read-only hybrid context for an engine evaluation."""
    if (not getattr(config, "HYBRID_HTF_ENABLED", True)
            or getattr(config, "HYBRID_HTF_MODE", "shadow") == "off"):
        yield None
        return
    market_conn = config.get_db_connection(read_only=True, db_path=market_db_path or config.MARKET_DB_PATH)
    regime_conn = None
    try:
        if regime_db_path or getattr(config, "REGIME_DB_PATH", None):
            try:
                regime_conn = config.get_db_connection(
                    read_only=True, db_path=regime_db_path or config.REGIME_DB_PATH
                )
            except Exception:
                regime_conn = None
        context = HybridHTFContext(market_conn, regime_conn, cutoff)
        token = _HYBRID_HTF_CONTEXT.set(context)
        try:
            yield context
        finally:
            _HYBRID_HTF_CONTEXT.reset(token)
            if regime_conn is not None:
                regime_conn.close()
    finally:
        market_conn.close()


def hybrid_htf_provenance(asset: str) -> dict[str, dict[str, Any]]:
    context = _HYBRID_HTF_CONTEXT.get()
    if context is None:
        return {}
    return context.summary().get(_asset_from_symbol(asset), {})


def hybrid_htf_context_active() -> bool:
    return _HYBRID_HTF_CONTEXT.get() is not None


def hybrid_htf_context_cutoff() -> datetime | None:
    context = _HYBRID_HTF_CONTEXT.get()
    return context.cutoff if context is not None else None


def load_bars_for_interval(conn, symbol: str, interval: str, cutoff: datetime,
                           lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Load engine-context HTF bars or canonical market bars.

    Within an engine hybrid context, 1h/4h frames use direct historical seeds
    followed by the canonical 5m-derived tail. Outside that context, higher
    timeframes remain derived from canonical 5m rows.
    """
    cutoff = _ensure_utc(cutoff)
    context = _HYBRID_HTF_CONTEXT.get()
    if context is not None and interval in {"1h", "4h"}:
        if context.cutoff != cutoff:
            raise ValueError(
                f"hybrid HTF context cutoff {context.cutoff.isoformat()} "
                f"does not match requested cutoff {cutoff.isoformat()}"
            )
        return context.load(symbol, interval, lookback_days)
    asset = _asset_from_symbol(symbol)
    if interval in {"15m", "1h", "4h"}:
        base_lookback = max(lookback_days, 60 if interval == "4h" else 16)
        start = cutoff - timedelta(days=base_lookback)
        raw = _prefer_rows(_load_raw_observations_for_asset(conn, asset, cutoff, start, interval="5m"))
        return resample_ohlcv(_rows_to_frame(raw), interval)
    start = cutoff - timedelta(days=lookback_days)
    raw = _load_raw_observations_for_asset(conn, asset, cutoff, start, interval=interval)
    rows = _prefer_rows(raw)
    return _rows_to_frame(rows)


def _rows_to_frame(rows: List[Dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame()
    data = {
            "timestamp": [r["timestamp"] for r in rows],
            "open": [r["open"] for r in rows],
            "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows],
            "close": [r["close"] for r in rows],
            "volume": [r["volume"] for r in rows],
            "open_interest": [r["open_interest"] for r in rows],
            "funding_rate": [r["funding_rate"] for r in rows],
            "source": [r["source"] for r in rows],
        }
    if "source_provenance" in rows[0]:
        data["source_provenance"] = [r.get("source_provenance", []) for r in rows]
    if "data_purity" in rows[0]:
        data["data_purity"] = [r.get("data_purity", "unknown") for r in rows]
    if "source_observation_ids" in rows[0]:
        data["source_observation_ids"] = [r.get("source_observation_ids", []) for r in rows]
    return pl.DataFrame(data, strict=False).with_columns(
        pl.col("open_interest").fill_null(0.0),
        pl.col("funding_rate").fill_null(0.0),
    )


def load_btc_15m(conn, cutoff: datetime, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """BTC preferred loader (delegates)."""
    cutoff = _ensure_utc(cutoff)
    df = load_preferred_15m_bars(conn, asset="BTC", cutoff=cutoff, lookback_days=lookback_days)
    if df.is_empty():
        return df
    return df.select(["timestamp", "close"])


def list_candidate_symbols(conn, cutoff: datetime, *, apply_rotation: bool = False,
                           assets: Iterable[str] | None = None) -> list[tuple[str, str]]:
    """Return every symbol in the upstream subscription universe.

    Strategies are intentionally unaware of rotation policy. The optional
    ``assets`` argument is retained for non-strategy callers and tests only.
    """
    cutoff = _ensure_utc(cutoff)
    if assets is not None:
        bases = sorted({str(asset).strip().upper() for asset in assets if str(asset).strip()})
        candidates = list(zip(config.expand_perp_symbols(bases, "bybit"), bases))
        if apply_rotation:
            from symbol_rotation import select_symbols
            return select_symbols(conn, candidates, cutoff)
        return candidates
    from symbol_rotation import subscription_assets
    bases, feed = subscription_assets(cutoff)
    candidates = list(zip(config.expand_perp_symbols(bases, "bybit"), bases))
    # The gateway and the evaluator consume the same durable rotation snapshot.
    # A missing/expired feed is fail-closed to permanent assets; strategies must
    # not re-rank that fallback from local bars.
    return candidates


def evaluation_symbols(conn, cutoff: datetime, snapshot: dict | None = None) -> list[tuple[str, str]]:
    """Use the evaluator-supplied scope; retain the loader for direct callers."""
    supplied = (snapshot or {}).get("subscription_symbols")
    if supplied is not None:
        return [(str(symbol), str(asset)) for symbol, asset in supplied]
    return list_candidate_symbols(conn, cutoff)


def resample_ohlcv(bars: pl.DataFrame, every: str) -> pl.DataFrame:
    """Resample closed end-stamped bars into complete UTC-aligned buckets.

    Input timestamps are exclusive bar ends. A bucket ending at ``12:00`` thus
    expects base bars ending at ``11:50``, ``11:55`` and ``12:00``. Incomplete
    buckets are deliberately omitted rather than persisted or evaluated.
    """
    if bars.is_empty():
        return bars
    seconds = {"15m": 900, "1h": 3600, "4h": 14400}.get(every)
    if seconds is None:
        raise ValueError(f"unsupported resampling interval: {every}")
    rows = bars.sort("timestamp").to_dicts()
    # Exchange feeds may encode closed bar ends one millisecond before the boundary.
    def normalize_bar_end(value: Any) -> datetime:
        return (_ensure_utc(value) + timedelta(milliseconds=1)).replace(microsecond=0)

    timestamps = [normalize_bar_end(row["timestamp"]) for row in rows]
    deltas = [int((timestamps[i] - timestamps[i - 1]).total_seconds())
              for i in range(1, len(timestamps))
              if timestamps[i] > timestamps[i - 1]]
    base_seconds = min(deltas) if deltas else 300
    if base_seconds <= 0 or seconds % base_seconds:
        return pl.DataFrame()
    required = seconds // base_seconds
    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for row, timestamp in zip(rows, timestamps):
        epoch = int(timestamp.timestamp())
        bucket_end = ((epoch + seconds - 1) // seconds) * seconds
        grouped.setdefault(bucket_end, []).append(row)
    output: List[Dict[str, Any]] = []
    for bucket_end, bucket_rows in sorted(grouped.items()):
        by_end = {normalize_bar_end(row["timestamp"]): row for row in bucket_rows}
        expected = [datetime.fromtimestamp(bucket_end - base_seconds * i, timezone.utc)
                    for i in range(required - 1, -1, -1)]
        if any(end not in by_end for end in expected):
            continue
        ordered = [by_end[end] for end in expected]
        output.append({
            "timestamp": datetime.fromtimestamp(bucket_end, timezone.utc),
            "open": float(ordered[0]["open"]),
            "high": max(float(row["high"]) for row in ordered),
            "low": min(float(row["low"]) for row in ordered),
            "close": float(ordered[-1]["close"]),
            "volume": sum(float(row.get("volume") or 0.0) for row in ordered),
            "open_interest": ordered[-1].get("open_interest", 0.0),
            "funding_rate": ordered[-1].get("funding_rate", 0.0),
            "source": ordered[-1].get("source", "resampled"),
            "source_provenance": sorted({str(row.get("source", "unknown")) for row in ordered}),
            "data_purity": "pure_ws" if all(str(row.get("source", "")).endswith("_ws") for row in ordered) else "unknown",
            "source_observation_ids": sorted({
                str(observation_id)
                for row in ordered
                for observation_id in row.get("source_observation_ids", [])
                if observation_id
            }),
        })
    return _rows_to_frame(output)


def ema_last(closes: Sequence[float], span: int) -> float | None:
    if len(closes) < span:
        return None
    return ema_series(closes, span)[-1]


def ema_series(values: Sequence[float], span: int) -> List[float | None]:
    """TradingView-style EMA with an SMA seed at the declared warmup point."""
    out: List[float | None] = [None] * len(values)
    if span <= 0 or len(values) < span:
        return out
    out[span - 1] = sum(float(value) for value in values[:span]) / span
    alpha = 2.0 / (span + 1.0)
    for index in range(span, len(values)):
        out[index] = alpha * float(values[index]) + (1.0 - alpha) * out[index - 1]
    return out


def wilder_rsi(values: Sequence[float], length: int = 14) -> List[float | None]:
    """Wilder RMA RSI, equivalent to TradingView ``ta.rsi``."""
    out: List[float | None] = [None] * len(values)
    if length <= 0 or len(values) <= length:
        return out
    gains = [max(float(values[i]) - float(values[i - 1]), 0.0) for i in range(1, len(values))]
    losses = [max(float(values[i - 1]) - float(values[i]), 0.0) for i in range(1, len(values))]
    gain = sum(gains[:length]) / length
    loss = sum(losses[:length]) / length

    def value() -> float:
        if loss == 0:
            return 100.0 if gain > 0 else 0.0
        return 100.0 - 100.0 / (1.0 + gain / loss)

    out[length] = value()
    for index in range(length + 1, len(values)):
        gain = (gain * (length - 1) + gains[index - 1]) / length
        loss = (loss * (length - 1) + losses[index - 1]) / length
        out[index] = value()
    return out


def wilder_atr(bars: pl.DataFrame, length: int = 14) -> float | None:
    """Return the final Wilder ATR after its declared warmup."""
    if bars.is_empty() or length <= 0 or bars.height < length:
        return None
    highs = [float(value) for value in bars["high"].to_list()]
    lows = [float(value) for value in bars["low"].to_list()]
    closes = [float(value) for value in bars["close"].to_list()]
    true_ranges = [highs[0] - lows[0]]
    true_ranges.extend(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                           abs(lows[i] - closes[i - 1])) for i in range(1, len(closes)))
    atr = sum(true_ranges[:length]) / length
    for current in true_ranges[length:]:
        atr = (atr * (length - 1) + current) / length
    return atr if math.isfinite(atr) and atr > 0 else None


def stoch_rsi(values: Sequence[float], rsi_length: int = 14, stoch_length: int = 14,
              k_smoothing: int = 3, d_smoothing: int = 3) -> tuple[List[float | None], ...]:
    """Return raw StochRSI, SMA K, SMA D using explicit zero-denominator rules."""
    if min(rsi_length, stoch_length, k_smoothing, d_smoothing) <= 0:
        return ([None] * len(values),) * 3
    rsi = wilder_rsi(values, rsi_length)
    raw: List[float | None] = [None] * len(values)
    for index in range(len(values)):
        window = rsi[index - stoch_length + 1:index + 1] if index + 1 >= stoch_length else []
        if len(window) != stoch_length or any(value is None for value in window):
            continue
        low, high = min(window), max(window)
        raw[index] = 0.0 if high == low else 100.0 * (rsi[index] - low) / (high - low)
    k: List[float | None] = [None] * len(values)
    d: List[float | None] = [None] * len(values)
    for index in range(len(values)):
        window = raw[index - k_smoothing + 1:index + 1] if index + 1 >= k_smoothing else []
        if len(window) == k_smoothing and all(value is not None for value in window):
            k[index] = sum(window) / k_smoothing
        window_k = k[index - d_smoothing + 1:index + 1] if index + 1 >= d_smoothing else []
        if len(window_k) == d_smoothing and all(value is not None for value in window_k):
            d[index] = sum(window_k) / d_smoothing
    return raw, k, d


def last_completed_bar_fresh(bars_15m: pl.DataFrame, cutoff: datetime) -> bool:
    if bars_15m.is_empty():
        return False
    latest = _ensure_utc(bars_15m["timestamp"][-1])
    cutoff = _ensure_utc(cutoff)
    return latest <= cutoff and cutoff - latest <= MAX_BAR_AGE


def atr_last(bars: pl.DataFrame, period: int = 14) -> float | None:
    return wilder_atr(bars, period)


def structure_bias_4h(bars_4h: pl.DataFrame) -> str:
    """close vs EMA48_4h → long | short | missing."""
    if bars_4h.is_empty() or bars_4h.height < 48:
        return "missing"
    closes = bars_4h["close"].to_list()
    ema48 = ema_last(closes, 48)
    if ema48 is None or ema48 <= 0:
        return "missing"
    close = float(closes[-1])
    if close > ema48:
        return "long"
    if close < ema48:
        return "short"
    return "missing"


def _zone_mid(zone: dict) -> float | None:
    lo, hi = zone.get("low"), zone.get("high")
    if lo is None or hi is None:
        return None
    return (float(lo) + float(hi)) / 2.0


def _zone_direction(zone: dict) -> str | None:
    d = zone.get("direction")
    if d in ("bullish", "long"):
        return "long"
    if d in ("bearish", "short"):
        return "short"
    return None


def zone_bias_4h(zones: Sequence[dict], ref_close: float, atr_4h: float | None) -> tuple[str, dict | None]:
    """Nearest active|partial 4h FVG/OB by midpoint distance → bias + zone."""
    candidates = []
    for z in zones:
        tf = str(z.get("timeframe") or "")
        if tf not in ("4h", "4H"):
            kind = str(z.get("kind") or "")
            if "_4h" not in kind and not kind.endswith("4h"):
                continue
        state = z.get("state", "active")
        if state not in ("active", "partial"):
            continue
        mid = _zone_mid(z)
        direction = _zone_direction(z)
        if mid is None or direction is None:
            continue
        dist = abs(float(ref_close) - mid)
        dist_atr = dist / atr_4h if atr_4h and atr_4h > 0 else dist
        candidates.append((dist_atr, z, direction))
    if not candidates:
        return "missing", None
    candidates.sort(key=lambda item: item[0])
    _, zone, direction = candidates[0]
    return direction, zone


def resolve_bias(structure: str, zone: str) -> str | None:
    """Agree-or-abstain. Returns direction or None (fail)."""
    if structure in ("long", "short") and zone == "missing":
        return structure
    if zone in ("long", "short") and structure == "missing":
        return zone
    if structure in ("long", "short") and structure == zone:
        return structure
    return None


def compute_htf_zones(bars_1h: pl.DataFrame, bars_4h: pl.DataFrame) -> list[dict]:
    zones: list[dict] = []
    if not bars_1h.is_empty() and bars_1h.height >= 5:
        atr1 = compute_atr(bars_1h)
        for z in detect_fvg(bars_1h, atr=atr1, tf="1h"):
            zones.append(z)
        for z in detect_order_blocks(bars_1h, atr=atr1, tf="1h"):
            zones.append(z)
    if not bars_4h.is_empty() and bars_4h.height >= 5:
        atr4 = compute_atr(bars_4h)
        for z in detect_fvg(bars_4h, atr=atr4, tf="4h"):
            zones.append(z)
        for z in detect_order_blocks(bars_4h, atr=atr4, tf="4h"):
            zones.append(z)
    return zones


def compression_ok(bars_1h: pl.DataFrame, n: int, k: float, atr_1h: float) -> tuple[bool, float, float, float]:
    """Full-window range ≤ k·ATR. Returns ok, base_high, base_low, range."""
    if bars_1h.height < n or atr_1h <= 0:
        return False, 0.0, 0.0, 0.0
    window = bars_1h.tail(n)
    base_high = float(window["high"].max())
    base_low = float(window["low"].min())
    rng = base_high - base_low
    return rng <= k * atr_1h, base_high, base_low, rng


def prior_base_expansion_fail(
    bars_1h: pl.DataFrame,
    n: int,
    g: float,
    atr_1h: float,
    direction: str,
) -> bool:
    """True if last 1h close breaks prior (N-1) range by > g·ATR in trade direction."""
    if bars_1h.height < n or atr_1h <= 0:
        return True
    prior = bars_1h.tail(n).head(n - 1)
    if prior.height < 1:
        return True
    prior_high = float(prior["high"].max())
    prior_low = float(prior["low"].min())
    last_close = float(bars_1h["close"][-1])
    grace = g * atr_1h
    if direction == "long" and last_close > prior_high + grace:
        return True
    if direction == "short" and last_close < prior_low - grace:
        return True
    return False


def prior_range_ratio(bars_1h: pl.DataFrame, n: int, p: int) -> float | None:
    if bars_1h.height < n + p:
        return None
    base = bars_1h.tail(n)
    prior = bars_1h.tail(n + p).head(p)
    base_range = float(base["high"].max()) - float(base["low"].min())
    prior_range = float(prior["high"].max()) - float(prior["low"].min())
    if prior_range <= 0:
        return None
    return base_range / prior_range


def zone_stack_and_ltf_scores(
    zones: Sequence[dict],
    ref_price: float,
    atr_ref: float,
    direction: str,
) -> tuple[float, float]:
    """Return (ltf_inside_htf, zone_stack_tightness) in [0,1]."""
    from confluence_scoring import proximity_score

    if atr_ref <= 0:
        return 0.0, 0.0
    wanted = "bullish" if direction == "long" else "bearish"
    htf = [z for z in zones if str(z.get("timeframe") or "") in ("4h", "1h") and z.get("state") in ("active", "partial")]
    if not htf:
        return 0.0, 0.0
    dists = []
    dir_match = 0
    for z in htf:
        mid = _zone_mid(z)
        if mid is None:
            continue
        d = abs(ref_price - mid) / atr_ref
        dists.append(d)
        zd = z.get("direction")
        if zd == wanted or (wanted == "bullish" and zd == "long") or (wanted == "bearish" and zd == "short"):
            dir_match += 1
    if not dists:
        return 0.0, 0.0
    best = min(dists)
    ltf = proximity_score(best)
    stack = min(1.0, dir_match / max(2.0, len(dists) * 0.5)) * proximity_score(best)
    return ltf, stack


def has_active_event(
    strategy_id: str,
    asset: str,
    direction: str,
    *,
    alpha_db_path: str | Path | None = None,
    outbox_dir: Path | None = None,
    now: datetime | None = None,
) -> bool:
    """True if a non-terminal live event exists for asset+direction under strategy_id."""
    now = _ensure_utc(now or datetime.now(timezone.utc))
    alpha_path = Path(alpha_db_path or config.ANALYST_DB_PATH)
    # Single-shot open: re-arm must not block the 15m path on publisher lock contention.
    if alpha_path.exists():
        try:
            conn = config.get_db_connection(read_only=True, db_path=alpha_path)
            try:
                row = conn.execute(
                    """
                    SELECT 1 FROM alpha_events
                    WHERE strategy_id = ? AND asset = ? AND direction = ?
                      AND status = 'active' AND valid_until > ?
                    LIMIT 1
                    """,
                    (strategy_id, asset, direction, now),
                ).fetchone()
                if row:
                    return True
            finally:
                conn.close()
        except Exception:
            pass

    directory = Path(outbox_dir or OUTBOX_DIR)
    if not directory.exists():
        return False
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("strategy_id") != strategy_id:
            continue
        if payload.get("asset") != asset or payload.get("direction") != direction:
            continue
        vu = payload.get("valid_until")
        if not vu:
            continue
        try:
            until = _ensure_utc(datetime.fromisoformat(str(vu).replace("Z", "+00:00")))
        except ValueError:
            continue
        if until > now:
            return True
    return False


def snapshot_zones_for_asset(snapshot: dict, asset: str) -> list[dict]:
    zones = snapshot.get("zones") or []
    return [z for z in zones if not z.get("asset") or z.get("asset") == asset]
