"""Binance USD-M perpetual open-interest rotation discovery.

This module owns Binance-native discovery only.  It never changes the legacy
CoinAnalyze scanner artifact and publishes a venue-neutral, atomic feed.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, pstdev

import httpx

import config


SOURCE = "binance_usdm"

_STATIC_SEED_CACHE: set[str] | None = None


def _load_static_seed_bases() -> set[str]:
    """Bases that must not get entered/active membership (ADR-013 P1). Fail-open empty."""
    global _STATIC_SEED_CACHE
    if _STATIC_SEED_CACHE is not None:
        return _STATIC_SEED_CACHE
    out: set[str] = set()
    if not getattr(config, "BINANCE_OI_STATIC_MEMBERSHIP_SKIP", False):
        _STATIC_SEED_CACHE = out
        return out
    path = (getattr(config, "BINANCE_OI_STATIC_SEED_PATH", "") or "").strip()
    if path:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(raw, list):
                for item in raw:
                    s = str(item).strip().upper()
                    if s.endswith("USDT"):
                        s = s[:-4]
                    if s:
                        out.add(s)
            elif isinstance(raw, dict) and isinstance(raw.get("bases"), list):
                for item in raw["bases"]:
                    s = str(item).strip().upper()
                    if s.endswith("USDT"):
                        s = s[:-4]
                    if s:
                        out.add(s)
        except Exception as exc:
            print(f"[oi-static-seed] path load err: {exc}")
    if not out:
        try:
            import sys

            sys.path.insert(0, "/home/ubuntu/propr-trading-agent")
            from propr_python.tradeable_assets import TRADEABLE_ASSETS

            for b, info in TRADEABLE_ASSETS.items():
                if getattr(info, "type", "") == "CRYPTO":
                    out.add(str(b).upper())
        except Exception:
            pass
    _STATIC_SEED_CACHE = out
    if out:
        print(f"[oi-static-seed] membership skip bases={len(out)}")
    return out


def _asset_is_static_covered(asset: str, seed: set[str] | None = None) -> bool:
    if seed is None:
        seed = _load_static_seed_bases()
    if not seed:
        return False
    a = str(asset).strip().upper()
    if a.endswith("USDT"):
        a = a[:-4]
    return a in seed


def completed_bar(now: datetime | None = None, bar_minutes: int = 60) -> datetime:
    """Return the UTC bar open time for the most recently *completed* bar of given size."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    # Align to bar boundary then back one full bar.
    minutes = int((now - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() // 60)
    boundary = minutes - (minutes % bar_minutes)
    bar_end = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=boundary)
    return bar_end - timedelta(minutes=bar_minutes)


def completed_hour(now: datetime | None = None) -> datetime:
    """Return the UTC hour whose OHLCV bar has fully closed. (compat for 1h path)"""
    return completed_bar(now, bar_minutes=60)


def _percentile(value: float, values: list[float]) -> float | None:
    if not values:
        return None
    return sum(item <= value for item in values) / len(values)


def _volume_anomaly(current: float, history: list[float]) -> float | None:
    if len(history) < 2:
        return None
    deviation = pstdev(history)
    if deviation == 0:
        return 0.0
    return (current - mean(history)) / deviation


def _latest_at_or_before(rows: list[dict], timestamp_ms: int) -> dict | None:
    """OI history timestamps are observations, not guaranteed candle-open times."""
    matching = [row for row in rows if int(row["timestamp"]) <= timestamp_ms]
    return max(matching, key=lambda row: int(row["timestamp"])) if matching else None


def _latest_in_window(rows: list[dict], start_ms: int, end_ms: int) -> dict | None:
    matching = [row for row in rows if start_ms <= int(row["timestamp"]) <= end_ms]
    return max(matching, key=lambda row: int(row["timestamp"])) if matching else None


def coinalyze_symbol_from_binance(symbol: str) -> str:
    """Keep the known Binance-to-CoinAnalyze aliases in one producer mapping."""
    aliases = {"SHIB1000USDT": "1000SHIBUSDT_PERP.A", "1000FLOKIUSDT": "FLOKIUSDT_PERP.3"}
    return aliases.get(symbol, f"{symbol}_PERP.A")


def build_observation(
    market: dict,
    ticker: dict | None,
    oi_history: list[dict] | None,
    candles: list[list] | None,
    interval: datetime,
    bar_minutes: int = 60,
    history_hours: int = config.BINANCE_OI_ROTATION_HISTORY_HOURS,
    history_bars: int | None = None,
) -> dict:
    """Build one auditable discovery observation from completed Binance inputs for a bar of any size.

    Legacy metric keys (*_1h_*) are populated with the values from the *active discovery bar*
    so the published feed contract is unchanged. bar_* additive fields clarify the grain.
    """
    symbol = market["symbol"]
    asset = market["baseAsset"]
    base = {
        "source": SOURCE,
        "symbol": symbol,
        "asset": asset,
        "quote": market["quoteAsset"],
        "contract_type": market["contractType"].lower(),
        "volume_24h_usd": 0.0,
        "is_eligible": False,
        "rejection_reason": None,
    }
    if not oi_history or not candles:
        return {**base, "is_eligible": False, "rejection_reason": "missing_history"}

    delta = timedelta(minutes=bar_minutes)
    interval_ms = int(interval.timestamp() * 1000)
    prior_ms = int((interval - delta).timestamp() * 1000)
    window_end_ms = interval_ms + int(delta.total_seconds() * 1000) - 1
    current_oi = _latest_in_window(oi_history, interval_ms, window_end_ms)
    prior_oi = _latest_at_or_before(oi_history, interval_ms - 1)
    candle_by_open = {int(candle[0]): candle for candle in candles}
    current_candle = candle_by_open.get(interval_ms)
    prior_candle = candle_by_open.get(prior_ms)
    if not current_oi or not prior_oi or not current_candle or not prior_candle:
        return {**base, "is_eligible": False, "rejection_reason": "incomplete_completed_bar"}

    # 24h liquidity uses ~24h closed-candle window regardless of discovery bar size.
    day_ms = 24 * 3_600_000
    volume_24h = sum(
        float(candle[7]) for candle in candles
        if interval_ms - day_ms <= int(candle[0]) <= interval_ms
    )
    base = {**base, "volume_24h_usd": volume_24h}

    # For 10m path we apply the 10m-specific floor later in tier filtering / qualify;
    # hourly path continues to gate here with its floor.
    min_24h = config.BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD
    if bar_minutes != 60:
        min_24h = getattr(config, "BINANCE_OI_10M_MIN_24H_VOLUME_USD", min_24h)
    if volume_24h < min_24h:
        return {**base, "rejection_reason": "below_liquidity_floor"}
    base = {**base, "is_eligible": True}

    oi_values = [(int(row["timestamp"]), float(row["sumOpenInterestValue"])) for row in oi_history]
    current_oi_usd = float(current_oi["sumOpenInterestValue"])
    prior_oi_usd = float(prior_oi["sumOpenInterestValue"])
    if prior_oi_usd <= 0:
        return {**base, "is_eligible": False, "rejection_reason": "invalid_prior_oi"}
    oi_pct_changes = [
        (value / previous - 1)
        for (_, previous), (_, value) in zip(oi_values, oi_values[1:])
        if previous > 0
    ]
    current_oi_pct = current_oi_usd / prior_oi_usd - 1
    prior_close = float(prior_candle[4])
    current_close = float(current_candle[4])
    if prior_close <= 0:
        return {**base, "is_eligible": False, "rejection_reason": "invalid_prior_close"}
    volumes = [(int(candle[0]), float(candle[7])) for candle in candles]
    current_volume = float(current_candle[7])

    # Trailing baseline length depends on cadence.
    if bar_minutes == 60:
        trail = history_hours
    else:
        trail = history_bars if history_bars is not None else getattr(config, "BINANCE_OI_10M_HISTORY_BARS", 672)
    trailing_volumes = [value for timestamp, value in volumes if timestamp < interval_ms][-trail:]
    history_changes = oi_pct_changes[-trail:]
    percentile = _percentile(current_oi_pct, history_changes)
    anomaly = _volume_anomaly(current_volume, trailing_volumes)
    if percentile is None or anomaly is None:
        return {**base, "is_eligible": False, "rejection_reason": "insufficient_history"}

    bar_delta_usd = current_oi_usd - prior_oi_usd
    bar_px = current_close / prior_close - 1 if prior_close > 0 else 0.0
    rec = {
        **base,
        "open_interest_usd": current_oi_usd,
        # Legacy keys kept for feed contract; values reflect the discovery bar (documented via bar_minutes).
        "oi_change_1h_pct": current_oi_pct,
        "oi_change_1h_usd": bar_delta_usd,
        "price_change_1h": bar_px,
        "volume_1h_usd": current_volume,
        "volume_anomaly": anomaly,
        "oi_spike_percentile": percentile,
        # Additive for clarity (harmless to old consumers)
        "bar_minutes": bar_minutes,
        "oi_change_bar_pct": current_oi_pct,
        "oi_change_bar_usd": bar_delta_usd,
        "price_change_bar": bar_px,
        "volume_bar_usd": current_volume,
    }
    return rec


def qualify_and_rank(
    observations: list[dict],
    *,
    min_oi_delta_usd: float | None = None,
    min_percentile: float | None = None,
    min_volume_anomaly: float | None = None,
) -> list[dict]:
    """Apply configuration gates and deterministic, direction-neutral ranking.

    Defaults are the hourly knobs; pass 10m-specific for the fast path.
    """
    min_delta = min_oi_delta_usd if min_oi_delta_usd is not None else config.BINANCE_OI_ROTATION_MIN_OI_DELTA_USD
    min_pct = min_percentile if min_percentile is not None else config.BINANCE_OI_ROTATION_MIN_OI_PERCENTILE
    min_vol = min_volume_anomaly if min_volume_anomaly is not None else config.BINANCE_OI_ROTATION_MIN_VOLUME_ANOMALY
    candidates = [
        record for record in observations
        if record.get("is_eligible")
        and record.get("oi_change_1h_usd", 0) >= min_delta
        and record.get("oi_spike_percentile", 0) >= min_pct
        and record.get("volume_anomaly", 0) >= min_vol
    ]
    ordered = sorted(
        candidates,
        key=lambda item: (-item["oi_spike_percentile"], -item["oi_change_1h_usd"], -item["volume_anomaly"], item["symbol"]),
    )
    return [{**record, "rank": index} for index, record in enumerate(ordered, 1)]


def build_feed(
    interval: datetime,
    candidates: list[dict],
    generated_at: datetime | None = None,
    bar_minutes: int = 60,
) -> dict:
    generated_at = generated_at or datetime.now(timezone.utc)
    expiry = interval + timedelta(hours=config.BINANCE_OI_ROTATION_FEED_EXPIRY_HOURS)
    cadence = "1h_full" if bar_minutes == 60 else "10m_liquid" if bar_minutes == 10 else f"{bar_minutes}m_liquid"
    feed = {
        "schema_version": 1,
        "source": SOURCE,
        "scanner_version": config.BINANCE_OI_ROTATION_SCANNER_VERSION,
        "generated_at": generated_at.isoformat(),
        "completed_interval_at": interval.isoformat(),
        "expires_at": expiry.isoformat(),
        "bar_minutes": bar_minutes,
        "discovery_cadence": cadence,
        "candidates": [],
    }
    for candidate in candidates:
        c = {
            key: candidate[key] for key in (
                "asset", "symbol", "quote", "contract_type", "rank", "volume_24h_usd",
                "open_interest_usd", "oi_change_1h_pct", "oi_change_1h_usd", "price_change_1h",
                "volume_1h_usd", "volume_anomaly", "oi_spike_percentile",
            ) if key in candidate
        }
        # ensure additive are present (short bar populates bar_* and bar_minutes on candidate too)
        for k in ("bar_minutes", "oi_change_bar_pct", "oi_change_bar_usd", "discovery_cadence"):
            if k in candidate:
                c[k] = candidate[k]
        if "bar_minutes" not in c:
            c["bar_minutes"] = bar_minutes
        if "discovery_cadence" not in c:
            c["discovery_cadence"] = cadence
        feed["candidates"].append(c)
    return feed


def publish_feed_atomic(feed: dict, path: Path | None = None) -> None:
    """Replace the feed only after a fully flushed JSON document exists."""
    path = Path(path or config.BINANCE_OI_ROTATION_FEED_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(feed, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class BinanceClient:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=config.BINANCE_FUTURES_BASE_URL, timeout=20.0)

    def get(self, path: str, params: dict | None = None):
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def eligible_markets(self) -> tuple[list[dict], dict[str, dict]]:
        exchange_info = self.get("/fapi/v1/exchangeInfo")
        tickers = {item["symbol"]: item for item in self.get("/fapi/v1/ticker/24hr")}
        markets = [
            item for item in exchange_info["symbols"]
            if item["status"] == "TRADING" and item["contractType"] == "PERPETUAL"
            and item["quoteAsset"] == "USDT" and item["marginAsset"] == "USDT"
        ]
        return markets, tickers

    def history(self, symbol: str, interval: datetime, bar_minutes: int = 60) -> tuple[list[dict], list[list]]:
        if bar_minutes == 60:
            hist_bars = config.BINANCE_OI_ROTATION_HISTORY_HOURS
            period = "1h"
            kline_interval = "1h"
            start = interval - timedelta(hours=hist_bars + 2)
            end = interval + timedelta(hours=1) - timedelta(milliseconds=1)
            limit = hist_bars + 3
        else:
            hist_bars = getattr(config, "BINANCE_OI_10M_HISTORY_BARS", 672)
            period = f"{bar_minutes}m"
            kline_interval = period
            delta = timedelta(minutes=bar_minutes)
            start = interval - timedelta(minutes=bar_minutes * (hist_bars + 2))
            end = interval + delta - timedelta(milliseconds=1)
            limit = hist_bars + 3
        params = {"symbol": symbol, "period": period, "startTime": int(start.timestamp() * 1000), "endTime": int(end.timestamp() * 1000), "limit": limit}
        oi_history = self.get("/futures/data/openInterestHist", params)
        candles = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": kline_interval, "startTime": params["startTime"], "endTime": params["endTime"], "limit": params["limit"]})
        return oi_history, candles


def _persist(conn, interval: datetime, observations: list[dict], candidates: list[dict], raw_oi_history: dict[str, list[dict]], observed_at: datetime, scan_complete: bool, bar_minutes: int = 60) -> None:
    version = config.BINANCE_OI_ROTATION_SCANNER_VERSION
    rows = [(
        SOURCE, interval, version, item["symbol"], item["asset"], item["quote"], item["contract_type"],
        item["is_eligible"], item.get("rejection_reason"), item["volume_24h_usd"], item.get("open_interest_usd"),
        item.get("oi_change_1h_pct"), item.get("oi_change_1h_usd"), item.get("price_change_1h"),
        item.get("volume_1h_usd"), item.get("volume_anomaly"), item.get("oi_spike_percentile"), observed_at,
        bar_minutes,
    ) for item in observations]
    if rows:
        conn.executemany("""INSERT INTO binance_oi_rotation_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", rows)
    raw_rows = [
        (SOURCE, symbol, datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=timezone.utc), float(row["sumOpenInterestValue"]), interval, bar_minutes)
        for symbol, rows in raw_oi_history.items()
        for row in rows
    ]
    if raw_rows:
        conn.executemany("""INSERT INTO binance_oi_rotation_raw_oi_history VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", raw_rows)
    for item in candidates:
        conn.execute("""INSERT INTO binance_oi_rotation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", (SOURCE, item["asset"], interval, version, item["symbol"], item["rank"], json.dumps(item, sort_keys=True), observed_at, bar_minutes))
    conn.execute("""INSERT INTO binance_oi_rotation_scans VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (source, completed_interval_at, scanner_version, bar_minutes) DO UPDATE SET
            status = excluded.status, completed_at = excluded.completed_at""",
        (SOURCE, interval, version, "complete" if scan_complete else "incomplete", observed_at, bar_minutes))


def _update_watchlist_oi_only(oi_conn, interval: datetime, candidates: list[dict]) -> None:
    """Persist rotation watchlist rows without touching market_data (no overlap/backfill)."""
    expires_at = interval + timedelta(hours=config.BINANCE_OI_ROTATION_WATCHLIST_HOURS)
    static_seed = _load_static_seed_bases()
    selected_assets = {item["asset"] for item in candidates if not _asset_is_static_covered(item["asset"], static_seed)}
    current = oi_conn.execute("""
        SELECT asset, symbol, state, expires_at
        FROM binance_oi_rotation_watchlist_history
        WHERE source = ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY source, asset ORDER BY observed_at DESC) = 1
    """, (SOURCE,)).fetchall()
    for asset, symbol, state, expiry in current:
        if state != "expired" and asset not in selected_assets and interval >= expiry:
            oi_conn.execute("""INSERT INTO binance_oi_rotation_watchlist_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING""", (SOURCE, asset, symbol, interval, "expired", expiry, False, False))
    for item in candidates:
        # P1: still keep events/observations (caller); skip entered/active for static forever bases
        if _asset_is_static_covered(item["asset"], static_seed):
            continue
        prior = next((row for row in current if row[0] == item["asset"]), None)
        new_entry = prior is None or prior[2] == "expired"
        oi_conn.execute("""INSERT INTO binance_oi_rotation_watchlist_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", (
            SOURCE, item["asset"], item["symbol"], interval,
            "entered" if new_entry else "active", expires_at, False, False,
        ))


def _update_watchlist(oi_conn, main_conn, interval: datetime, candidates: list[dict]) -> None:
    """Rotation watchlist + cross-pool overlap using dedicated oi_conn for its tables.
    main_conn used only briefly for discovery_watchlist/deep_backfill (shared state).
    """
    expires_at = interval + timedelta(hours=config.BINANCE_OI_ROTATION_WATCHLIST_HOURS)
    static_seed = _load_static_seed_bases()
    selected_assets = {item["asset"] for item in candidates if not _asset_is_static_covered(item["asset"], static_seed)}
    current = oi_conn.execute("""
        SELECT asset, symbol, state, expires_at
        FROM binance_oi_rotation_watchlist_history
        WHERE source = ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY source, asset ORDER BY observed_at DESC) = 1
    """, (SOURCE,)).fetchall()
    for asset, symbol, state, expiry in current:
        if state != "expired" and asset not in selected_assets and interval >= expiry:
            oi_conn.execute("""INSERT INTO binance_oi_rotation_watchlist_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT DO NOTHING""", (SOURCE, asset, symbol, interval, "expired", expiry, False, False))
    for item in candidates:
        if _asset_is_static_covered(item["asset"], static_seed):
            continue
        overlap = main_conn.execute("""
            SELECT 1 FROM (
                SELECT state FROM discovery_watchlist_history
                WHERE asset = ?
                QUALIFY ROW_NUMBER() OVER (PARTITION BY pool, symbol ORDER BY observed_at DESC) = 1
            ) WHERE state != 'expired'
            LIMIT 1
        """, (item["asset"],)).fetchone() is not None
        prior = next((row for row in current if row[0] == item["asset"]), None)
        new_entry = prior is None or prior[2] == "expired"
        deep_backfill_required = new_entry and not overlap
        oi_conn.execute("""INSERT INTO binance_oi_rotation_watchlist_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING""", (SOURCE, item["asset"], item["symbol"], interval, "entered" if new_entry else "active", expires_at, deep_backfill_required, overlap))
        if deep_backfill_required:
            coinalyze_symbol = coinalyze_symbol_from_binance(item["symbol"])
            job_exists = main_conn.execute("SELECT 1 FROM deep_backfill_jobs WHERE symbol = ?", (coinalyze_symbol,)).fetchone()
            if not job_exists:
                from two_pool_discovery import enqueue_deep_backfill_jobs
                enqueue_deep_backfill_jobs(main_conn, interval, [coinalyze_symbol])


def _select_liquid_tier(markets: list[dict], tickers: dict, bar_minutes: int, now: datetime) -> list[dict]:
    """Return capped list for fast path using floor | carry | (simple heat)."""
    if not markets:
        return []
    min_vol = getattr(config, "BINANCE_OI_10M_MIN_24H_VOLUME_USD", config.BINANCE_OI_ROTATION_MIN_24H_VOLUME_USD)
    max_c = getattr(config, "BINANCE_OI_10M_MAX_CONTRACTS", 100)
    # Carry set: recent watchlist non-expired + current feed candidates (if unexpired)
    carry_assets: set[str] = set()
    config.init_binance_oi_db()
    oi_conn = config.get_db_connection(read_only=True, db_path=config.BINANCE_OI_DB_PATH)
    try:
        rows = oi_conn.execute(
            "SELECT asset FROM binance_oi_rotation_watchlist_history WHERE source=? AND state IN ('entered','active') AND expires_at > ?",
            (SOURCE, now),
        ).fetchall()
        carry_assets.update(r[0] for r in rows)
    finally:
        oi_conn.close()
    try:
        feed_path = config.BINANCE_OI_ROTATION_FEED_PATH
        if feed_path.exists():
            with open(feed_path) as f:
                live = json.load(f)
            exp = parse_feed_expires(live)
            if exp and exp > now:
                for c in live.get("candidates", []):
                    if c.get("asset"):
                        carry_assets.add(c["asset"])
    except Exception:
        pass

    # annotate vol and sort by vol desc
    annotated = []
    for m in markets:
        vol = float(tickers.get(m["symbol"], {}).get("quoteVolume", 0) or 0)
        annotated.append((vol, m))
    annotated.sort(key=lambda t: t[0], reverse=True)

    selected = []
    for vol, m in annotated:
        asset = m["baseAsset"]
        is_floor = vol >= min_vol
        is_carry = asset in carry_assets
        # heat stub: allow if very high vol relative (top slice already), or config can extend
        is_heat = False  # future: acceleration vs prior window
        if is_floor or is_carry or is_heat:
            selected.append(m)
        if max_c > 0 and len(selected) >= max_c:
            break
    if max_c > 0:
        selected = selected[:max_c]
    return selected


def parse_feed_expires(feed: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(feed.get("expires_at", "")).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def run_scanner(now: datetime | None = None, client: BinanceClient | None = None, bar_minutes: int | None = None) -> dict:
    """Fetch closed bar (1h full or liquid short), persist, (maybe) publish feed."""
    bar = bar_minutes if bar_minutes is not None else 60
    interval = completed_bar(now, bar_minutes=bar)
    observed_at = now or datetime.now(timezone.utc)
    client = client or BinanceClient()
    markets, tickers = client.eligible_markets()
    if bar == 60:
        # full universe, sorted by vol (existing behavior)
        markets.sort(key=lambda market: float(tickers.get(market["symbol"], {}).get("quoteVolume", 0)), reverse=True)
        to_scan = markets
        max_contracts = config.BINANCE_OI_ROTATION_MAX_CONTRACTS
        use_10m_floor_in_obs = False
    else:
        # liquid tier only
        tiered = _select_liquid_tier(markets, tickers, bar, observed_at)
        # keep original sort order for determinism (already vol sorted inside select)
        to_scan = tiered
        max_contracts = getattr(config, "BINANCE_OI_10M_MAX_CONTRACTS", 100)
        use_10m_floor_in_obs = True

    observations = []
    raw_oi_history: dict[str, list[dict]] = {}
    fetched_contracts = 0
    for market in to_scan:
        ticker = tickers.get(market["symbol"])
        if max_contracts > 0 and fetched_contracts >= max_contracts:
            observations.append({
                "source": SOURCE, "symbol": market["symbol"], "asset": market["baseAsset"],
                "quote": market["quoteAsset"], "contract_type": market["contractType"].lower(),
                "volume_24h_usd": 0.0, "is_eligible": False, "rejection_reason": "api_budget_exceeded"
            })
            continue
        try:
            fetched_contracts += 1
            oi_history, candles = client.history(market["symbol"], interval, bar_minutes=bar)
            raw_oi_history[market["symbol"]] = oi_history
            obs = build_observation(market, ticker, oi_history, candles, interval, bar_minutes=bar)
            observations.append(obs)
        except httpx.HTTPError:
            observations.append({
                "source": SOURCE, "symbol": market["symbol"], "asset": market["baseAsset"],
                "quote": market["quoteAsset"], "contract_type": market["contractType"].lower(),
                "volume_24h_usd": 0.0, "is_eligible": False, "rejection_reason": "market_data_request_failed"
            })

    # qualify with cadence-specific gates
    if bar == 60:
        candidates = qualify_and_rank(observations)
    else:
        candidates = qualify_and_rank(
            observations,
            min_oi_delta_usd=getattr(config, "BINANCE_OI_10M_MIN_OI_DELTA_USD", 250000),
            min_percentile=getattr(config, "BINANCE_OI_10M_MIN_OI_PERCENTILE", 0.95),
            min_volume_anomaly=getattr(config, "BINANCE_OI_10M_MIN_VOLUME_ANOMALY", 1.0),
        )

    config.init_binance_oi_db()
    oi_conn = config.get_db_connection(db_path=config.BINANCE_OI_DB_PATH)
    short_qualifiers = candidates if bar != 60 else []
    try:
        scan_complete = not any(item.get("rejection_reason") == "market_data_request_failed" for item in observations)
        _persist(oi_conn, interval, observations, candidates, raw_oi_history, observed_at, scan_complete, bar_minutes=bar)
        # Watchlist + backfill side effects only on *new qualifiers* (same as before)
        effective_cands_for_watch = candidates
        try:
            main_conn = config.get_db_connection(read_only=False)
            try:
                _update_watchlist(oi_conn, main_conn, interval, effective_cands_for_watch)
                main_conn.commit()
            finally:
                main_conn.close()
        except Exception as error:
            print(f"Binance OI watchlist update deferred (market_data lock): {error}")
            _update_watchlist_oi_only(oi_conn, interval, effective_cands_for_watch)
        oi_conn.commit()
    finally:
        oi_conn.close()

    # Feed publish policy
    do_publish = True
    if bar != 60:
        # short bar: only publish if has qualifiers (after possible merge)
        if not short_qualifiers:
            do_publish = False
        elif getattr(config, "BINANCE_OI_10M_FEED_MERGE_HOURLY", True):
            # merge with unexpired hourly candidates
            merged = _merge_with_unexpired_hourly(candidates, observed_at)
            candidates = qualify_and_rank(  # re-apply sort only; gates already passed
                merged,
                min_oi_delta_usd=getattr(config, "BINANCE_OI_10M_MIN_OI_DELTA_USD", 250000),
                min_percentile=getattr(config, "BINANCE_OI_10M_MIN_OI_PERCENTILE", 0.95),
                min_volume_anomaly=getattr(config, "BINANCE_OI_10M_MIN_VOLUME_ANOMALY", 1.0),
            )
            # after merge we may still have some; publish
            do_publish = len(candidates) > 0

    feed = build_feed(interval, candidates, observed_at, bar_minutes=bar)
    if do_publish:
        publish_feed_atomic(feed)
        try:
            from oi_discord_notify import notify_oi_feed
            notify_result = notify_oi_feed(feed)
            print(f"Binance OI Discord notify: {notify_result}")
        except Exception as error:
            print(f"Binance OI Discord notify error: {error}")
    else:
        print(f"Binance OI 10m: no qualifiers; feed left unchanged for {interval.isoformat()}")
    return feed


def _merge_with_unexpired_hourly(short_candidates: list[dict], now: datetime) -> list[dict]:
    """Union short qualifiers with any still-valid hourly candidates from last 1h scan/feed."""
    assets_seen: set[str] = {c["asset"] for c in short_candidates}
    merged = list(short_candidates)
    # Try current feed first (fast)
    try:
        fp = config.BINANCE_OI_ROTATION_FEED_PATH
        if fp.exists():
            with open(fp) as f:
                live = json.load(f)
            exp = parse_feed_expires(live)
            live_bar = int(live.get("bar_minutes", 60))
            if exp and exp > now and live_bar == 60:
                for c in live.get("candidates", []):
                    if c.get("asset") and c["asset"] not in assets_seen:
                        merged.append(c)
                        assets_seen.add(c["asset"])
                return merged
    except Exception:
        pass
    # Fallback: query last hourly events whose interval still unexpired
    try:
        config.init_binance_oi_db()
        conn = config.get_db_connection(read_only=True, db_path=config.BINANCE_OI_DB_PATH)
        try:
            expiry_h = config.BINANCE_OI_ROTATION_FEED_EXPIRY_HOURS
            cutoff = now - timedelta(hours=expiry_h + 1)
            rows = conn.execute(
                """SELECT metrics_json FROM binance_oi_rotation_events
                   WHERE source=? AND bar_minutes=60 AND completed_interval_at >= ?
                   ORDER BY completed_interval_at DESC, rank ASC LIMIT 200""",
                (SOURCE, cutoff),
            ).fetchall()
            for (mj,) in rows:
                try:
                    m = json.loads(mj) if isinstance(mj, str) else (mj or {})
                    a = m.get("asset")
                    if a and a not in assets_seen:
                        # ensure minimal keys
                        merged.append(m)
                        assets_seen.add(a)
                except Exception:
                    continue
        finally:
            conn.close()
    except Exception:
        pass
    return merged
