"""Shared market context for confluence v2 strategy plugins."""

from __future__ import annotations

import hashlib
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
from polars_indicators import wilder_atr_series


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


_INTERVAL_MINUTES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def completed_cycle_for(now: datetime | None, interval: str) -> datetime:
    """Floor `now` to the most recent completed `interval` bar boundary."""
    if interval == "1m":
        raise ValueError("1m evaluation is retired; use 5m")
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
               payload_json, observation_id, retrieval_kind, retrieved_at
          FROM source_observations
          WHERE asset = ? AND interval=?
             AND source_end <= ? AND source_end >= ?
             {validity_filter}
           ORDER BY source_end ASC
        """,
        (asset, interval, cutoff, start),
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
            "retrieval_kind": r[11],
            "retrieved_at": r[12],
        })
    return out


def _prefer_rows(raw_rows: List[Dict]) -> List[Dict]:
    """For each timestamp prefer live WS data when duplicates exist."""
    from collections import defaultdict
    by_ts: Dict[datetime, List[Dict]] = defaultdict(list)
    for r in raw_rows:
        by_ts[_normalise_bar_end(r["timestamp"])].append(r)
    preferred = []
    for ts, lst in sorted(by_ts.items()):
        ws = [x for x in lst if str(x["source"]).endswith("_ws")]
        if ws:
            preferred.append(ws[0])
            continue
        preferred.append(max(
            lst,
            key=lambda row: (
                1 if row.get("retrieval_kind") == "stream" else 0,
                str(row.get("retrieved_at") or ""),
            ),
        ))
    return preferred


def _source_high_water(conn: Any, asset: str, interval: str, cutoff: datetime,
                       start: datetime) -> tuple[Any, ...]:
    """Return a bounded source identity used to validate incremental reuse."""
    try:
        row = conn.execute(
            """
            SELECT COUNT(*), MAX(source_end), MAX(retrieved_at),
                   COALESCE(SUM(LENGTH(payload_json)), 0),
                   COALESCE(GROUP_CONCAT(DISTINCT source), '')
              FROM source_observations
             WHERE asset = ? AND interval = ? AND source_end >= ? AND source_end <= ?
            """,
            (asset, interval, start, cutoff),
        ).fetchone()
    except Exception:
        return ("unavailable",)
    return tuple(row or ("unavailable",))


def _feed_identity() -> str:
    """Identify the configured market-source contract for cache reuse."""
    payload = {
        "ws_source": getattr(config, "BYBIT_WS_SOURCE", "bybit_ws"),
        "purity": getattr(config, "WS_DATA_PURITY", "pure_ws"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


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


DIRECT_HTF_DATA_CONTRACT_VERSION = "direct-htf-v1"
SHARED_COMPUTATION_CACHE_VERSION = "shared-computation-cache-v2"
_DIRECT_HTF_CONTEXT: ContextVar["DirectHTFContext | None"] = ContextVar(
    "direct_htf_context", default=None
)
_SHARED_COMPUTATION_CONTEXT: ContextVar["SharedComputationContext | None"] = ContextVar(
    "shared_computation_context", default=None
)
_SEQUENTIAL_FRAME_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
_SEQUENTIAL_FRAME_CACHE_LIMIT = 128


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
    canonical_rows: dict[datetime, dict[str, Any]] = {}
    for end, values in grouped.items():
        if len(values) == 1:
            canonical_rows[end] = values[0]
            continue
        raw_ends = [_ensure_utc(value["timestamp"]) for value in values]
        aliases = (
            len(values) == 2
            and end in raw_ends
            and any(value != end for value in raw_ends)
            and all(_normalise_bar_end(value) == end for value in raw_ends)
        )
        if not aliases:
            return [], "canonical_tail_duplicate"
        signatures = {
            tuple(float(value.get(field) or 0.0) for field in ("open", "high", "low", "close", "volume"))
            for value in values
        }
        if len(signatures) != 1:
            return [], "canonical_tail_duplicate"
        canonical_rows[end] = _prefer_rows(values)[0]
    by_end = {
        end: row for end, row in canonical_rows.items()
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


class DirectHTFContext:
    """Invocation-scoped loader for native regime-owned HTF frames."""

    def __init__(self, market_conn: Any, regime_conn: Any | None, cutoff: datetime,
                 evaluation_cutoff: datetime | None = None,
                 market_db_path: str | Path | None = None,
                 regime_db_path: str | Path | None = None):
        self.market_conn = market_conn
        self.regime_conn = regime_conn
        self.cutoff = _ensure_utc(cutoff)
        self.evaluation_cutoff = (
            _ensure_utc(evaluation_cutoff) if evaluation_cutoff is not None else self.cutoff
        )
        self.market_db_path = str(market_db_path or config.MARKET_DB_PATH)
        self.regime_db_path = str(regime_db_path or getattr(config, "REGIME_DB_PATH", ""))
        self.last_reused = False
        self._frames: dict[tuple[str, str, int], pl.DataFrame] = {}
        self._diagnostics: dict[str, dict[str, dict[str, Any]]] = {}

    def summary(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {
            asset: {interval: dict(details) for interval, details in intervals.items()}
            for asset, intervals in self._diagnostics.items()
        }

    def _record(self, asset: str, interval: str, **details: Any) -> None:
        self._diagnostics.setdefault(asset, {})[interval] = {
            "data_contract_version": DIRECT_HTF_DATA_CONTRACT_VERSION,
            "cutoff_at": self.cutoff.isoformat(),
            "evaluation_cutoff_at": self.evaluation_cutoff.isoformat(),
            **details,
        }

    def load(self, symbol: str, interval: str, lookback_days: int) -> pl.DataFrame:
        if interval not in {"1h", "4h"}:
            raise ValueError(f"unsupported direct HTF interval: {interval}")
        asset = _asset_from_symbol(symbol)
        key = (asset, interval, int(lookback_days))
        if key in self._frames:
            return self._frames[key]
        required = int(getattr(
            config, f"DIRECT_HTF_{'1H' if interval == '1h' else '4H'}_SEED_BARS", 240
        ))
        seconds = 3600 if interval == "1h" else 14400
        if self.regime_conn is None:
            self._record(asset, interval, availability="unavailable",
                         direct_readiness="not_ready", source_mode="direct_rest",
                         reason="direct_history_unavailable")
            self._frames[key] = pl.DataFrame()
            return self._frames[key]
        try:
            from regime_history import load_regime_1h_bars, load_regime_4h_bars
            loader = load_regime_1h_bars if interval == "1h" else load_regime_4h_bars
            frame = loader(self.regime_conn, asset, self.cutoff, limit=required)
            if frame.is_empty():
                reason = "direct_history_missing"
            elif not _direct_seed_is_contiguous(
                frame, interval, required, _floor_boundary(self.cutoff, seconds)
            ):
                reason = "direct_history_incomplete"
            else:
                reason = None
            if reason:
                self._record(
                    asset, interval, availability="unavailable",
                    direct_readiness="not_ready", source_mode="direct_rest",
                    reason=reason,
                    direct_bar_ids=[str(value) for value in frame["bar_id"].to_list()]
                    if "bar_id" in frame.columns else [],
                )
                self._frames[key] = pl.DataFrame()
                return self._frames[key]
            direct_ids = [str(value) for value in frame["bar_id"].to_list()]
            versions = [str(value) for value in frame["bar_version"].unique().to_list()]
            frame = frame.with_columns(
                pl.lit("direct_rest").alias("source_mode"),
                pl.lit("direct_rest").alias("data_purity"),
            )
            self._record(
                asset, interval, availability="ready", direct_readiness="ready",
                source_mode="direct_rest", direct_bar_ids=direct_ids,
                direct_bar_versions=versions, direct_source="bybit_rest",
                direct_venue="bybit",
            )
        except Exception as exc:
            self._record(
                asset, interval, availability="unavailable",
                direct_readiness="not_ready", source_mode="direct_rest",
                reason="direct_history_invalid", error=type(exc).__name__,
            )
            frame = pl.DataFrame()
        self._frames[key] = frame
        return frame

@contextmanager
def direct_htf_context(regime_db_path: str | Path | None, cutoff: datetime,
                       *, evaluation_cutoff: datetime | None = None):
    """Install one read-only direct HTF context for an evaluation."""
    regime_conn = None
    try:
        try:
            regime_conn = config.get_db_connection(
                read_only=True, db_path=regime_db_path or config.REGIME_DB_PATH
            )
        except Exception:
            regime_conn = None
        context = DirectHTFContext(
            None, regime_conn, cutoff, evaluation_cutoff=evaluation_cutoff,
            regime_db_path=regime_db_path or getattr(config, "REGIME_DB_PATH", None),
        )
        token = _DIRECT_HTF_CONTEXT.set(context)
        try:
            yield context
        finally:
            _DIRECT_HTF_CONTEXT.reset(token)
            if regime_conn is not None:
                regime_conn.close()
    finally:
        pass


def direct_htf_provenance(asset: str) -> dict[str, dict[str, Any]]:
    context = _DIRECT_HTF_CONTEXT.get()
    if context is None:
        return {}
    return context.summary().get(_asset_from_symbol(asset), {})


def direct_htf_context_active() -> bool:
    return _DIRECT_HTF_CONTEXT.get() is not None


def direct_htf_context_cutoff() -> datetime | None:
    context = _DIRECT_HTF_CONTEXT.get()
    return context.cutoff if context is not None else None


def direct_htf_context_evaluation_cutoff() -> datetime | None:
    context = _DIRECT_HTF_CONTEXT.get()
    return context.evaluation_cutoff if context is not None else None


def _load_bars_for_interval_uncached(conn, symbol: str, interval: str, cutoff: datetime,
                                     lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Load direct HTF bars or canonical execution/auxiliary bars without sharing.

    Within an invocation-scoped direct context, 1h/4h frames come only from
    the regime-owned native history. There is deliberately no canonical 5m
    fallback for those setup frames.
    """
    cutoff = _ensure_utc(cutoff)
    context = _DIRECT_HTF_CONTEXT.get()
    if context is not None and interval in {"1h", "4h"}:
        if (context.evaluation_cutoff != cutoff
                and completed_cycle_for(cutoff, "5m") != context.cutoff):
            raise ValueError(
                f"direct HTF context evaluation cutoff {context.evaluation_cutoff.isoformat()} "
                f"does not match requested cutoff {cutoff.isoformat()}"
            )
        return context.load(symbol, interval, lookback_days)
    asset = _asset_from_symbol(symbol)
    if interval in {"1h", "4h"}:
        return pl.DataFrame()
    if interval == "15m":
        base_lookback = max(lookback_days, 16)
        start = cutoff - timedelta(days=base_lookback)
        raw = _prefer_rows(_load_raw_observations_for_asset(conn, asset, cutoff, start, interval="5m"))
        return resample_ohlcv(_rows_to_frame(raw), interval)
    start = cutoff - timedelta(days=lookback_days)
    raw = _load_raw_observations_for_asset(conn, asset, cutoff, start, interval=interval)
    rows = _prefer_rows(raw)
    return _rows_to_frame(rows)


def load_bars_for_interval(conn, symbol: str, interval: str, cutoff: datetime,
                           lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
    """Load a cutoff-bound frame, reusing the active evaluation context."""
    if interval == "1m":
        raise ValueError("1m market data is retired from the strategy engine")
    shared = _SHARED_COMPUTATION_CONTEXT.get()
    if shared is not None:
        return shared.load_bars(conn, symbol, interval, cutoff, lookback_days)
    return _load_bars_for_interval_uncached(conn, symbol, interval, cutoff, lookback_days)


class SharedComputationContext:
    """One immutable frame and feature cache for a cutoff-bound evaluation."""

    def __init__(self, market_conn: Any, evaluation_cutoff: datetime,
                 market_db_path: str | Path | None = None):
        self.market_conn = market_conn
        self.evaluation_cutoff = _ensure_utc(evaluation_cutoff)
        self.market_db_path = str(market_db_path or config.MARKET_DB_PATH)
        direct = _DIRECT_HTF_CONTEXT.get()
        self.feed_id = _feed_identity()
        self.source_contract = f"{SHARED_COMPUTATION_CACHE_VERSION}:{DIRECT_HTF_DATA_CONTRACT_VERSION}"
        self.htf_cutoff = direct.cutoff if direct is not None else None
        self._frames: dict[tuple[str, str, int], pl.DataFrame] = {}
        self._features: dict[tuple[str, str, tuple], pl.DataFrame] = {}
        self._dmi: dict[tuple[str, str, int, int], tuple[list[float | None], float | None, float | None]] = {}
        self.stats = {
            "frame_hits": 0,
            "frame_misses": 0,
            "feature_hits": 0,
            "feature_misses": 0,
            "dmi_hits": 0,
            "dmi_misses": 0,
            "sequential_hits": 0,
            "sequential_misses": 0,
            "cache_invalidations": 0,
            "cache_invalidation_reasons": {},
        }

    @staticmethod
    def _feature_key(spec: dict[str, Any]) -> tuple:
        def freeze(value: Any) -> Any:
            if isinstance(value, dict):
                return tuple(sorted((str(key), freeze(item)) for key, item in value.items()))
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            return value

        return tuple(sorted((str(key), freeze(value)) for key, value in spec.items()))

    def load_bars(self, conn: Any, symbol: str, interval: str, cutoff: datetime,
                  lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
        cutoff = _ensure_utc(cutoff)
        if cutoff != self.evaluation_cutoff and not (
            interval in {"1h", "4h"}
            and completed_cycle_for(cutoff, "5m") == completed_cycle_for(self.evaluation_cutoff, "5m")
        ):
            raise ValueError(
                f"shared computation cutoff {self.evaluation_cutoff.isoformat()} "
                f"does not match requested cutoff {cutoff.isoformat()}"
            )
        asset = _asset_from_symbol(symbol)
        key = (asset, interval, int(lookback_days))
        cached = self._frames.get(key)
        if cached is not None:
            self.stats["frame_hits"] += 1
            return cached
        self.stats["frame_misses"] += 1
        frame = self._load_sequential_frame(asset, interval, cutoff, lookback_days)
        self._frames[key] = frame
        return frame

    def _load_sequential_frame(self, asset: str, interval: str, cutoff: datetime,
                               lookback_days: int) -> pl.DataFrame:
        """Extend a validated base frame instead of rebuilding every cutoff."""
        direct = _DIRECT_HTF_CONTEXT.get()
        if direct is not None and interval in {"1h", "4h"}:
            self.stats["sequential_misses"] += 1
            return direct.load(asset, interval, lookback_days)
        cache_key = (
            self.market_db_path, self.feed_id, self.source_contract, asset, interval,
            int(lookback_days), self.htf_cutoff if interval in {"1h", "4h"} else None,
        )
        previous = _SEQUENTIAL_FRAME_CACHE.get(cache_key)
        period_minutes = {"5m": 5}.get(interval)
        if previous is not None and period_minutes is not None:
            previous_cutoff = previous["cutoff"]
            previous_frame = previous["frame"]
            if previous_cutoff < cutoff and not previous_frame.is_empty():
                previous_start = previous_cutoff - timedelta(days=lookback_days)
                current_prefix_identity = _source_high_water(
                    self.market_conn, asset, interval, previous_cutoff, previous_start
                )
                if current_prefix_identity != previous.get("source_high_water"):
                    self._invalidate_cache("source_identity_changed")
                    previous = None
            if previous is not None and previous_cutoff < cutoff and not previous_frame.is_empty():
                repair_start = max(
                    cutoff - timedelta(days=lookback_days),
                    previous_cutoff - timedelta(minutes=period_minutes * 2),
                )
                raw = _load_raw_observations_for_asset(
                    self.market_conn,
                    asset,
                    cutoff,
                    repair_start,
                    interval=interval,
                )
                tail = _rows_to_frame(_prefer_rows(raw))
                prefix = previous_frame.filter(pl.col("timestamp") < repair_start)
                frame = None
                if not tail.is_empty():
                    frame = pl.concat([prefix, tail], how="diagonal_relaxed").sort("timestamp")
                    duplicate_timestamps = frame.group_by("timestamp").len().filter(pl.col("len") > 1)
                    if not duplicate_timestamps.is_empty():
                        self._invalidate_cache("conflicting_incremental_rows")
                        frame = None
                    else:
                        frame = frame.unique(subset=["timestamp"], keep="last", maintain_order=True)
                if frame is not None and not tail.is_empty():
                    frame = frame.filter(pl.col("timestamp") <= cutoff)
                    self.stats["sequential_hits"] += 1
                    _SEQUENTIAL_FRAME_CACHE[cache_key] = {
                        "cutoff": cutoff,
                        "frame": frame,
                        "feed_id": self.feed_id,
                        "source_contract": self.source_contract,
                        "source_high_water": _source_high_water(
                            self.market_conn, asset, interval, cutoff,
                            cutoff - timedelta(days=lookback_days),
                        ),
                    }
                    return frame
            elif previous is not None and previous_cutoff >= cutoff:
                self._invalidate_cache("non_monotonic_cutoff")
        self.stats["sequential_misses"] += 1
        frame = _load_bars_for_interval_uncached(
            self.market_conn, asset, interval, cutoff, lookback_days
        )
        _SEQUENTIAL_FRAME_CACHE[cache_key] = {
            "cutoff": cutoff,
            "frame": frame,
            "feed_id": self.feed_id,
            "source_contract": self.source_contract,
            "source_high_water": _source_high_water(
                self.market_conn, asset, interval, cutoff,
                cutoff - timedelta(days=lookback_days),
            ),
        }
        while len(_SEQUENTIAL_FRAME_CACHE) > _SEQUENTIAL_FRAME_CACHE_LIMIT:
            _SEQUENTIAL_FRAME_CACHE.pop(next(iter(_SEQUENTIAL_FRAME_CACHE)))
        return frame

    def _invalidate_cache(self, reason: str) -> None:
        self.stats["cache_invalidations"] += 1
        reasons = self.stats["cache_invalidation_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1

    def features(self, symbol: str, interval: str, spec: dict[str, Any],
                 cutoff: datetime | None = None, lookback_days: int = LOOKBACK_DAYS) -> pl.DataFrame:
        """Build one Polars feature frame for a normalized feature specification."""
        from strategy_features import build_feature_frame

        cutoff = self.evaluation_cutoff if cutoff is None else _ensure_utc(cutoff)
        key = (_asset_from_symbol(symbol), interval, self._feature_key(spec))
        cached = self._features.get(key)
        if cached is not None:
            self.stats["feature_hits"] += 1
            return cached
        self.stats["feature_misses"] += 1
        frame = self.load_bars(None, symbol, interval, cutoff, lookback_days)
        result = build_feature_frame(frame, **spec)
        self._features[key] = result
        return result

    def dmi_adx(self, symbol: str, interval: str, length: int, smoothing: int,
                cutoff: datetime | None = None, lookback_days: int = LOOKBACK_DAYS):
        """Compute and cache one ADX/DMI contract for this cutoff."""
        from polars_indicators import dmi_adx_series

        cutoff = self.evaluation_cutoff if cutoff is None else _ensure_utc(cutoff)
        key = (_asset_from_symbol(symbol), interval, int(length), int(smoothing))
        cached = self._dmi.get(key)
        if cached is not None:
            self.stats["dmi_hits"] += 1
            return cached
        self.stats["dmi_misses"] += 1
        frame = self.load_bars(None, symbol, interval, cutoff, lookback_days)
        result = dmi_adx_series(frame, length, smoothing)
        self._dmi[key] = result
        return result


@contextmanager
def shared_computation_context(market_db_path: str | Path | None,
                               evaluation_cutoff: datetime):
    """Install one shared read-only computation context for an evaluation."""
    current = _SHARED_COMPUTATION_CONTEXT.get()
    if current is not None:
        yield current
        return
    direct = _DIRECT_HTF_CONTEXT.get()
    owns_connection = True
    conn = config.get_db_connection(
        read_only=True,
        db_path=market_db_path or config.MARKET_DB_PATH,
    )
    context = SharedComputationContext(conn, evaluation_cutoff, market_db_path)
    token = _SHARED_COMPUTATION_CONTEXT.set(context)
    try:
        yield context
    finally:
        _SHARED_COMPUTATION_CONTEXT.reset(token)
        if owns_connection:
            conn.close()


def shared_computation_context_active() -> bool:
    return _SHARED_COMPUTATION_CONTEXT.get() is not None


def get_shared_computation_context() -> SharedComputationContext | None:
    return _SHARED_COMPUTATION_CONTEXT.get()


def strategy_market_connection(db_path: str | Path | None = None) -> tuple[Any, bool]:
    """Return the shared market connection, or an owned fallback for direct calls."""
    context = get_shared_computation_context()
    if context is not None:
        return context.market_conn, False
    return config.get_db_connection(read_only=True, db_path=db_path), True


def shared_computation_stats() -> dict[str, int]:
    context = _SHARED_COMPUTATION_CONTEXT.get()
    return dict(context.stats) if context is not None else {}


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
    # The gateway and evaluator consume the same durable effective-universe
    # snapshot. A stale feed may retain unexpired watchlist entries, but
    # strategies must not re-rank or extend that state from local bars.
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
    # Normalize only the two representations accepted by the source contract.
    try:
        normalized_timestamps = [
            _normalise_bar_end(value) for value in bars["timestamp"].to_list()
        ]
        for row in bars.to_dicts():
            prices = [float(row[name]) for name in ("open", "high", "low", "close")]
            volume = float(row.get("volume") or 0.0)
            if (not all(math.isfinite(value) and value > 0 for value in prices)
                    or prices[1] < max(prices[0], prices[3])
                    or prices[2] > min(prices[0], prices[3])
                    or not math.isfinite(volume) or volume < 0):
                return pl.DataFrame()
    except (KeyError, TypeError, ValueError, OverflowError):
        return pl.DataFrame()
    normalized = bars.with_columns(
        pl.Series("timestamp", normalized_timestamps, dtype=pl.Datetime("us", time_zone="UTC"))
    ).sort("timestamp")
    duplicate_times = normalized.group_by("timestamp").len().filter(pl.col("len") > 1)["timestamp"].to_list()
    for timestamp in duplicate_times:
        duplicate_rows = normalized.filter(pl.col("timestamp") == timestamp)
        signatures = {
            tuple(row.get(name) for name in ("open", "high", "low", "close", "volume"))
            for row in duplicate_rows.to_dicts()
        }
        if len(signatures) > 1:
            return pl.DataFrame()
    normalized = normalized.unique(subset=["timestamp"], keep="last", maintain_order=True).sort("timestamp")
    deltas = normalized.select(
        pl.col("timestamp").diff().dt.total_seconds().alias("delta_seconds")
    )["delta_seconds"].drop_nulls()
    positive_deltas = deltas.filter(deltas > 0)
    base_seconds = int(positive_deltas.min()) if positive_deltas.len() else 300
    if base_seconds <= 0 or seconds % base_seconds:
        return pl.DataFrame()
    required = seconds // base_seconds
    if "source" not in normalized.columns:
        normalized = normalized.with_columns(pl.lit("unknown").alias("source"))
    else:
        normalized = normalized.with_columns(pl.col("source").fill_null("unknown"))
    if "volume" not in normalized.columns:
        normalized = normalized.with_columns(pl.lit(0.0).alias("volume"))
    else:
        normalized = normalized.with_columns(pl.col("volume").fill_null(0.0))
    if "open_interest" not in normalized.columns:
        normalized = normalized.with_columns(pl.lit(0.0).alias("open_interest"))
    else:
        normalized = normalized.with_columns(pl.col("open_interest").fill_null(0.0))
    if "funding_rate" not in normalized.columns:
        normalized = normalized.with_columns(pl.lit(0.0).alias("funding_rate"))
    else:
        normalized = normalized.with_columns(pl.col("funding_rate").fill_null(0.0))
    if "source_observation_ids" not in normalized.columns:
        normalized = normalized.with_columns(
            pl.Series("source_observation_ids", [[] for _ in range(normalized.height)], dtype=pl.List(pl.String))
        )

    result = normalized.group_by_dynamic(
        "timestamp", every=every, closed="right", label="right"
    ).agg([
        pl.len().alias("_bucket_count"),
        pl.col("timestamp").sort().alias("_timestamps"),
        pl.col("open").first().cast(pl.Float64).alias("open"),
        pl.col("high").max().cast(pl.Float64).alias("high"),
        pl.col("low").min().cast(pl.Float64).alias("low"),
        pl.col("close").last().cast(pl.Float64).alias("close"),
        pl.col("volume").sum().cast(pl.Float64).alias("volume"),
        pl.col("open_interest").last().cast(pl.Float64).alias("open_interest"),
        pl.col("funding_rate").last().cast(pl.Float64).alias("funding_rate"),
        pl.col("source").last().alias("source"),
        pl.col("source").unique().sort().alias("source_provenance"),
        pl.col("source").str.ends_with("_ws").all().alias("_pure_ws"),
        pl.col("source_observation_ids").explode().drop_nulls().unique().sort().alias(
            "source_observation_ids"
        ),
    ]).filter(pl.col("_bucket_count") == required).sort("timestamp")
    exact_bucket_mask = []
    for row in result.select("timestamp", "_timestamps").to_dicts():
        bucket_end = int(_ensure_utc(row["timestamp"]).timestamp())
        actual = sorted(int(_ensure_utc(value).timestamp()) for value in row["_timestamps"])
        expected = [bucket_end - seconds + base_seconds * (index + 1) for index in range(required)]
        exact_bucket_mask.append(actual == expected)
    if result.height:
        result = result.filter(pl.Series("_exact_bucket", exact_bucket_mask))
    if result.is_empty():
        return pl.DataFrame()
    return result.select([
        "timestamp", "open", "high", "low", "close", "volume", "open_interest",
        "funding_rate", "source", "source_provenance",
        pl.when(pl.col("_pure_ws")).then(pl.lit("pure_ws"))
        .otherwise(pl.lit("unknown")).alias("data_purity"),
        "source_observation_ids",
    ])


def ema_last(closes: Sequence[float], span: int) -> float | None:
    if len(closes) < span:
        return None
    return ema_series(closes, span)[-1]


def _polars_seeded_ewm(values: pl.Series, length: int, alpha: float) -> pl.Series:
    """Run an EWMA from an explicit arithmetic seed in Polars."""
    values = values.cast(pl.Float64)
    if length <= 0 or values.len() < length:
        return pl.Series("ewm", [None] * values.len(), dtype=pl.Float64)
    seed = values.head(length).mean()
    seeded = pl.concat([
        pl.Series("values", [None] * (length - 1), dtype=pl.Float64),
        pl.Series("values", [seed], dtype=pl.Float64),
        values.slice(length),
    ])
    return seeded.ewm_mean(alpha=alpha, adjust=False, min_samples=1).alias("ewm")


def ema_series(values: Sequence[float], span: int) -> List[float | None]:
    """TradingView-style EMA with an SMA seed at the declared warmup point."""
    series = pl.Series("values", [float(value) for value in values], dtype=pl.Float64)
    return _polars_seeded_ewm(series, span, 2.0 / (span + 1.0)).to_list()


def wilder_rsi(values: Sequence[float], length: int = 14) -> List[float | None]:
    """Wilder RMA RSI, equivalent to TradingView ``ta.rsi``."""
    closes = pl.Series("close", [float(value) for value in values], dtype=pl.Float64)
    if length <= 0 or closes.len() <= length:
        return [None] * closes.len()
    changes = closes.diff().slice(1)
    gains = changes.clip(lower_bound=0.0)
    losses = (-changes).clip(lower_bound=0.0)
    gain_rma = _polars_seeded_ewm(gains, length, 1.0 / length)
    loss_rma = _polars_seeded_ewm(losses, length, 1.0 / length)
    frame = pl.DataFrame({
        "gain": pl.concat([pl.Series([None], dtype=pl.Float64), gain_rma]),
        "loss": pl.concat([pl.Series([None], dtype=pl.Float64), loss_rma]),
    }).with_columns(
        pl.when(pl.col("loss").is_null() | pl.col("gain").is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .when(pl.col("loss") == 0)
        .then(pl.when(pl.col("gain") > 0).then(100.0).otherwise(0.0))
        .otherwise(100.0 - 100.0 / (1.0 + pl.col("gain") / pl.col("loss")))
        .alias("rsi")
    )
    return frame["rsi"].to_list()


def wilder_atr(bars: pl.DataFrame, length: int = 14) -> float | None:
    """Return the final Wilder ATR after its declared warmup."""
    if bars.is_empty() or length <= 0 or bars.height < length:
        return None
    atr = wilder_atr_series(bars, length)[-1]
    return float(atr) if atr is not None and math.isfinite(atr) and atr > 0 else None


def stoch_rsi(values: Sequence[float], rsi_length: int = 14, stoch_length: int = 14,
              k_smoothing: int = 3, d_smoothing: int = 3) -> tuple[List[float | None], ...]:
    """Return raw StochRSI, SMA K, SMA D using explicit zero-denominator rules."""
    if min(rsi_length, stoch_length, k_smoothing, d_smoothing) <= 0:
        return ([None] * len(values),) * 3
    rsi = pl.Series("rsi", wilder_rsi(values, rsi_length), dtype=pl.Float64)
    frame = pl.DataFrame({"rsi": rsi}).with_columns(
        rsi_low=pl.col("rsi").rolling_min(stoch_length, min_samples=stoch_length),
        rsi_high=pl.col("rsi").rolling_max(stoch_length, min_samples=stoch_length),
    ).with_columns(
        pl.when(pl.col("rsi_low").is_null() | pl.col("rsi_high").is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .when(pl.col("rsi_high") == pl.col("rsi_low"))
        .then(0.0)
        .otherwise(100.0 * (pl.col("rsi") - pl.col("rsi_low")) /
                   (pl.col("rsi_high") - pl.col("rsi_low")))
        .alias("raw")
    ).with_columns(
        pl.col("raw").rolling_mean(k_smoothing, min_samples=k_smoothing).alias("k")
    ).with_columns(
        pl.col("k").rolling_mean(d_smoothing, min_samples=d_smoothing).alias("d")
    )
    return frame["raw"].to_list(), frame["k"].to_list(), frame["d"].to_list()


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
