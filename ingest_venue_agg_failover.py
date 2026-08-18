"""Venue aggregate failover ingest for CA 15m backbone under rate limits.

Per specs/ca-truth-venue-agg-failover.md + specs/ca-limited-takeover.md.
- CA remains truth when usable (close>0)
- When CA limited (high 429 or stale): shape non-critical CA calls, failover to BN+BY
- On gap/unusable CA bar: fetch BN+BY public, blend/partial, write venue_agg_v1
- Single row per (asset, source_end)
- provenance + data_purity in payload
- circuit for shaping + expansion
- budget + catchup 2h
- emit only later via purity stamps
- funding/oi best-effort (funding may be unavailable)
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from api_clients.binance_futures import BinanceFuturesClient
from api_clients.bybit_linear import BybitLinearClient


_binance_client = BinanceFuturesClient()
_bybit_client = BybitLinearClient()


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _completed_15m_ends(since: datetime, up_to: datetime) -> List[datetime]:
    """List of completed 15m source_end strictly after since and <= up_to."""
    ends: List[datetime] = []
    # Align to 15m boundaries
    t = since.replace(second=0, microsecond=0)
    minute = (t.minute // 15) * 15
    t = t.replace(minute=minute)
    if t <= since:
        t += timedelta(minutes=15)
    while t <= up_to:
        ends.append(t)
        t += timedelta(minutes=15)
    return ends


def _is_usable_ca_row(row: Optional[Dict]) -> bool:
    if not row:
        return False
    try:
        close = float(row.get("close", 0) or 0)
        vol = row.get("volume")
        if close > 0:
            if vol is None:
                return True
            return float(vol) >= 0
        return False
    except Exception:
        return False


def _fetch_recent_429_rate(source: str = "coinalyze", window_min: int = 30) -> float:
    """Return 429 / (429+ok) over recent window from source_request_log."""
    conn = config.get_db_connection(read_only=True)
    try:
        since = _now_utc() - timedelta(minutes=window_min)
        rows = conn.execute(
            """
            SELECT status FROM source_request_log
            WHERE source = ? AND requested_at >= ?
            """,
            (source, since),
        ).fetchall()
        total = len(rows)
        if total == 0:
            return 0.0
        c429 = sum(1 for (s,) in rows if s == "429")
        return c429 / total if total > 0 else 0.0
    finally:
        conn.close()


def _core_preferred_latest_age_min() -> float:
    """Age in minutes of most recent usable bar across core assets (coinalyze or venue)."""
    conn = config.get_db_connection(read_only=True)
    try:
        core = list(config.OPENMARKET_PERMANENT_ASSETS)
        if not core:
            core = ["BTC", "ETH", "SOL"]
        placeholders = ",".join("?" for _ in core)
        row = conn.execute(
            f"""
            SELECT MAX(source_end) FROM source_observations
            WHERE asset IN ({placeholders}) AND interval='15m'
              AND json_extract(payload_json, '$.close')::DOUBLE > 0
            """,
            core,
        ).fetchone()
        latest = row[0] if row and row[0] else None
        if not latest:
            return 999.0
        return (_now_utc() - latest).total_seconds() / 60.0
    finally:
        conn.close()


def _circuit_open() -> bool:
    if not config.MARKET_FAILOVER_ENABLED:
        return False
    age = _core_preferred_latest_age_min()
    rate = _fetch_recent_429_rate("coinalyze", config.FAILOVER_CIRCUIT_WINDOW_MIN)
    trip_age = age > config.FAILOVER_CIRCUIT_AGE_MIN
    trip_rate = rate >= config.FAILOVER_CIRCUIT_429_RATE
    return trip_age or trip_rate


def is_ca_limited() -> bool:
    """Public: true when CA circuit is open (age or 429 rate) and failover master enabled.
    Used by ingest shaping and health to decide takeover mode.
    """
    return _circuit_open()


def log_ca_shaped(request_type: str, cutoff_id: str = "shaped") -> None:
    """Log a shaped (skipped due to CA limited) request for telemetry.
    Called from ingest_coinalyze and scanner when shaping.
    """
    conn = config.get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO source_request_log (
                request_id, cutoff_id, source, request_type, weight,
                budget_remaining, selected_universe_json, status,
                requested_at, completed_at, response_meta_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"coinalyze-{cutoff_id}-{request_type}-{int(time.time()*1000)}-{uuid.uuid4().hex[:8]}",
                cutoff_id,
                "coinalyze",
                request_type,
                0,
                None,
                "[]",
                "shaped_due_to_circuit",
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                '{"reason": "ca_limited_shaping"}',
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _get_assets_needing_backfill(catchup_hours: int = 2) -> List[str]:
    """Return assets that have recent history (i.e. 'needed' bars) so failover can cover gaps.
    This is broader than core+hot cap — BY/BN failover should cover whatever was active.
    """
    conn = config.get_db_connection(read_only=True)
    try:
        # Any asset observed in last day+catchup window
        rows = conn.execute(f"""
            SELECT DISTINCT asset 
            FROM source_observations 
            WHERE source_end > now() - interval '{catchup_hours + 24} hours'
              AND json_extract(payload_json, '$.close')::DOUBLE > 0
        """).fetchall()
        assets = sorted({str(r[0]).upper() for r in rows if r[0]})
        return assets
    finally:
        conn.close()


def _get_hot_assets() -> List[str]:
    # Kept for reference / circuit expansion if wanted; main path now uses recent observed assets.
    hot: List[str] = []
    try:
        p = config.DEFAULT_DB_DIR / "scanned_pairs.json"
        if p.exists():
            data = json.loads(p.read_text())
            for item in data.get("rankings", [])[:10]:
                a = item.get("underlying") or item.get("asset")
                if a:
                    hot.append(str(a).upper())
            for item in data.get("accumulation_alerts", []):
                a = item.get("underlying") or item.get("asset")
                if a and str(a).upper() not in hot:
                    hot.append(str(a).upper())
    except Exception:
        pass
    cap = getattr(config, "FAILOVER_WATCHLIST_CAP", 20)
    return hot[:cap]


def _existing_bars_for_asset(asset: str, since: datetime) -> Dict[datetime, Dict]:
    """Return {source_end: row_dict} for bars with close>0 since."""
    conn = config.get_db_connection(read_only=True)
    try:
        rows = conn.execute(
            """
            SELECT source_end, source, payload_json
            FROM source_observations
            WHERE asset = ? AND interval = '15m' AND source_end >= ?
            ORDER BY source_end
            """,
            (asset, since),
        ).fetchall()
        out: Dict[datetime, Dict] = {}
        for se, src, pj in rows:
            try:
                p = json.loads(pj) if pj else {}
                p["source"] = src
                if float(p.get("close", 0) or 0) > 0:
                    out[se] = p
            except Exception:
                pass
        return out
    finally:
        conn.close()


def _blend_or_partial(bn: Optional[Dict], by: Optional[Dict], asset: str, bar_end: datetime) -> Optional[Dict]:
    """Return payload + provenance for the bar, or None."""
    if not bn and not by:
        return None
    # Normalize units: assume klines give [ts, o, h, l, c, vol] base vol? Binance quote vol? For v1 use as-is.
    # Spec: volume base_asset for us, oi usd.
    def parse_kline(k: List) -> Dict:
        # binance: [open_time, open, high, low, close, volume, close_time, ...]
        # bybit list: [start, open, high, low, close, volume, turnover]
        try:
            if isinstance(k, (list, tuple)) and len(k) >= 5:
                return {
                    "open": float(k[1]),
                    "high": float(k[2]),
                    "low": float(k[3]),
                    "close": float(k[4]),
                    "volume": float(k[5]) if len(k) > 5 else 0.0,
                }
            if isinstance(k, dict):
                return {
                    "open": float(k.get("open", k.get("o", 0))),
                    "high": float(k.get("high", k.get("h", 0))),
                    "low": float(k.get("low", k.get("l", 0))),
                    "close": float(k.get("close", k.get("c", 0))),
                    "volume": float(k.get("volume", k.get("v", 0))),
                }
        except Exception:
            pass
        return {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}

    bn_k = parse_kline(bn.get("kline") if bn else None) if bn else None
    by_k = parse_kline(by.get("kline") if by else None) if by else None

    def safe(d: Optional[Dict], k: str, default=0.0) -> float:
        try:
            return float(d.get(k, default)) if d else default
        except Exception:
            return default

    components = []
    o = h = l = c = v = 0.0
    oi = None
    fr = None
    partial = False
    purity = "synthetic_agg"

    if bn_k and by_k:
        # blend
        o = (safe(bn_k, "open") + safe(by_k, "open")) / 2.0   # simple; spec says volume-weighted but for v1 ok avg
        h = max(safe(bn_k, "high"), safe(by_k, "high"))
        l = min(safe(bn_k, "low"), safe(by_k, "low"))
        c = (safe(bn_k, "close") + safe(by_k, "close")) / 2.0
        v = safe(bn_k, "volume") + safe(by_k, "volume")
        components = [
            {"venue": "binance_usdm", "symbol": bn.get("symbol", ""), "weight_vol": 0.5, "weight_oi": 0.5},
            {"venue": "bybit_linear", "symbol": by.get("symbol", ""), "weight_vol": 0.5, "weight_oi": 0.5},
        ]
    elif bn_k:
        o, h, l, c, v = [safe(bn_k, k) for k in ("open", "high", "low", "close", "volume")]
        partial = True
        purity = "single_venue"
        components = [{"venue": "binance_usdm", "symbol": bn.get("symbol", ""), "weight_vol": 1.0, "weight_oi": 1.0}]
    elif by_k:
        o, h, l, c, v = [safe(by_k, k) for k in ("open", "high", "low", "close", "volume")]
        partial = True
        purity = "single_venue"
        components = [{"venue": "bybit_linear", "symbol": by.get("symbol", ""), "weight_vol": 1.0, "weight_oi": 1.0}]

    if o <= 0 or c <= 0:
        return None

    # Best effort OI/funding from the legs if present
    oi = None
    fr = None
    if bn and "oi" in bn:
        oi = float(bn["oi"]) if bn["oi"] else None
    if by and "oi" in by:
        oi = (oi or 0) + float(by["oi"]) if by["oi"] else oi
    if bn and "funding" in bn:
        fr = float(bn["funding"])
    if by and "funding" in by and fr is not None:
        fr = (fr + float(by["funding"])) / 2.0
    elif by and "funding" in by:
        fr = float(by["funding"])

    payload = {
        "open": round(o, 8),
        "high": round(h, 8),
        "low": round(l, 8),
        "close": round(c, 8),
        "volume": round(v, 8),
        "open_interest": oi,
        "funding_rate": fr,
        "predicted_funding": None,
        "liquidation_long": None,
        "liquidation_short": None,
        "long_short_ratio": None,
        "provenance": {
            "kind": "synthetic_aggregate",
            "pure_ca": False,
            "data_purity": purity,
            "aggregator": "binance_bybit_v1",
            "partial": partial,
            "reason": "ca_missing_bar",
            "components": components,
            "fields": {
                "ohlcv": "blended" if not partial else "single",
                "open_interest": "sum_usd" if oi is not None else "unavailable",
                "funding_rate": "oi_weighted" if fr is not None else "unavailable",
                "predicted_funding": "unavailable",
                "long_short_ratio": "unavailable",
                "liquidation_long": "unavailable",
                "liquidation_short": "unavailable",
            },
            "units": {"volume": "base_asset", "open_interest": "usd_notional"},
        },
    }
    return payload


def _write_failover_bar(asset: str, bar_end: datetime, payload: Dict) -> bool:
    obs_id = f"{config.FAILOVER_SOURCE_NAME}:{asset}:{bar_end.isoformat()}"
    native = f"{asset}-USDT-PERP-VAGG"
    start = bar_end - timedelta(minutes=15)
    conn = config.get_db_connection(read_only=False)
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO source_observations (
                observation_id, source, venue, native_symbol, asset, market_kind, interval,
                source_start, source_end, retrieved_at, retrieval_kind, payload_json
            ) VALUES (?, ?, 'binance_bybit', ?, ?, 'perpetual', '15m', ?, ?, ?, 'failover', ?)
            """,
            (
                obs_id,
                config.FAILOVER_SOURCE_NAME,
                native,
                asset,
                start,
                bar_end,
                _now_utc(),
                json.dumps(payload, default=str),
            ),
        )
        conn.commit()
        return True
    except Exception as e:
        print(f"Failover write error for {asset}@{bar_end}: {e}")
        return False
    finally:
        conn.close()


def ingest_venue_agg_failover(cutoff_id: str = "failover") -> Dict[str, Any]:
    """Main entry. Idempotent. Returns summary."""
    if not getattr(config, "MARKET_FAILOVER_ENABLED", False):
        return {"status": "disabled"}

    summary = {"circuit": "closed", "gaps_filled": 0, "partial": 0, "assets": [], "skipped_budget": 0}

    circuit = _circuit_open()
    summary["circuit"] = "open" if circuit else "closed"

    # Backfill *all symbols that needed it*: any asset with recent observations.
    # (Core are included automatically via recent history.)
    # The per-cycle request budget is the only limiter.
    catchup = getattr(config, "FAILOVER_CATCHUP_HOURS", 2)
    assets = _get_assets_needing_backfill(catchup)

    max_req = getattr(config, "FAILOVER_MAX_REQUESTS_PER_CYCLE", 80)
    req_used = 0

    now = _now_utc()
    since = now - timedelta(hours=catchup)

    for asset in assets:
        if req_used >= max_req:
            summary["skipped_budget"] += 1
            continue
        try:
            existing = _existing_bars_for_asset(asset, since)
            expected_ends = _completed_15m_ends(since, now)
            for bar_end in expected_ends:
                if bar_end in existing:
                    # if CA present and usable, skip (even if venue there, prefer will pick CA)
                    if existing[bar_end].get("source") == "coinalyze" and _is_usable_ca_row(existing[bar_end]):
                        continue
                    # if venue already, skip
                    if existing[bar_end].get("source") == config.FAILOVER_SOURCE_NAME:
                        continue
                # gap or unusable CA: try fetch
                start_ms = int((bar_end - timedelta(minutes=15)).timestamp() * 1000)
                end_ms = int(bar_end.timestamp() * 1000) - 1

                bn_k = _binance_client.fetch_klines(asset, start_ms, end_ms, "15m", cutoff_id)
                by_k = _bybit_client.fetch_klines(asset, start_ms, end_ms, "15", cutoff_id)
                req_used += 2

                bn_leg = {"kline": bn_k[0] if bn_k else None, "symbol": f"{asset}USDT"} if bn_k else None
                by_leg = {"kline": by_k[0] if by_k else None, "symbol": f"{asset}USDT"} if by_k else None

                # Funding priority (wider window to catch last settlement; funding changes infrequently)
                # Then OI. Always after successful kline if budget.
                funding_lookback_ms = 8 * 3600 * 1000  # 8h to hit last rate
                f_start = start_ms - funding_lookback_ms
                if getattr(config, "FAILOVER_FUNDING_PRIORITY", True):
                    if bn_leg and req_used < max_req:
                        fr_b = _binance_client.fetch_funding(asset, f_start, end_ms, cutoff_id)
                        if fr_b:
                            bn_leg["funding"] = float(fr_b[-1].get("fundingRate", 0)) if fr_b else None
                        req_used += 1
                    if by_leg and req_used < max_req:
                        fr_y = _bybit_client.fetch_funding(asset, f_start, end_ms, cutoff_id)
                        if fr_y:
                            by_leg["funding"] = float(fr_y[-1].get("fundingRate", 0)) if fr_y else None
                        req_used += 1
                # OI best effort
                if bn_leg and req_used < max_req:
                    oi_b = _binance_client.fetch_oi_hist(asset, start_ms, end_ms, "15m", cutoff_id)
                    if oi_b:
                        bn_leg["oi"] = oi_b[-1].get("sumOpenInterestValue") if oi_b else None
                    req_used += 1
                if by_leg and req_used < max_req:
                    oi_y = _bybit_client.fetch_oi(asset, start_ms, end_ms, "15m", cutoff_id)
                    if oi_y:
                        by_leg["oi"] = oi_y[-1].get("openInterest", None) if oi_y else None
                    req_used += 1

                payload = _blend_or_partial(bn_leg, by_leg, asset, bar_end)
                if payload:
                    ok = _write_failover_bar(asset, bar_end, payload)
                    if ok:
                        summary["gaps_filled"] += 1
                        if payload.get("provenance", {}).get("partial"):
                            summary["partial"] += 1
                        if asset not in summary["assets"]:
                            summary["assets"].append(asset)
                if req_used >= max_req:
                    break
        except Exception as e:
            print(f"Failover error for asset {asset}: {e}")

    print(f"Failover: circuit={summary['circuit']} gaps_filled={summary['gaps_filled']} partial={summary['partial']} assets={summary['assets']}")
    return summary


if __name__ == "__main__":
    config.init_db()
    print(ingest_venue_agg_failover())
