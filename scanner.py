import time
import os
import json
import statistics
from datetime import datetime, timedelta, timezone
import httpx
import duckdb
import config
from alpha_research import classify_liquidity_tier, record_universe_snapshot
from ingest_coinalyze import fetch_coinalyze_data, fetch_coinalyze_data_batched
import bootstrap_trend_history
import two_pool_discovery

def map_binance_to_coinalyze(symbol: str) -> str:
    """Maps Binance symbol to Coinalyze symbol format."""
    if symbol == "1000FLOKIUSDT":
        return "FLOKIUSDT_PERP.3"  # Binance-specific FLOKI aggregate perp
    if symbol in ["1000SHIBUSDT", "SHIB1000USDT"]:
        return "1000SHIBUSDT_PERP.A"
    return f"{symbol}_PERP.A"


def _history_values(history: list[dict]) -> list[float]:
    """Extract CoinAnalyze history values across its endpoint-specific shapes."""
    values = []
    for item in history:
        for key in ("c", "value", "openInterest"):
            if item.get(key) is not None:
                try:
                    values.append(float(item[key]))
                except (TypeError, ValueError):
                    pass
                break
    return values


def build_discovery_record(binance_meta: dict, coinalyze_symbol: str, ohlcv_history: list[dict],
                           oi_history: list[dict], current_oi: float, funding_rate: float,
                           now: datetime | None = None) -> dict:
    """Normalize one hourly CoinAnalyze snapshot for the two discovery pools."""
    volume_24h = float(binance_meta["vol_24h_usd"])
    asset = binance_meta["binance_symbol"].removesuffix("USDT")
    record = {
        "symbol": coinalyze_symbol,
        "asset": asset,
        "liquidity_tier": classify_liquidity_tier(volume_24h),
        "eligible": volume_24h >= config.SCANNER_MIN_24H_VOLUME_USD,
        "data_fresh": False,
        "history_warmed": False,
        "volume_24h_usd": volume_24h,
        "open_interest_usd": 0.0,
        "volume_zscore": 0.0,
        "oi_change_1h": 0.0,
        "price_change_1h": 0.0,
        "price_change_24h": 0.0,
        "price_range_percentile": 0.5,
        "funding_rate": funding_rate,
        "funding_zscore": 0.0,
        "long_short_ratio_change": 0.0,
        "fresh_breakout": False,
        "post_breakout_pullback": False,
        "exhausted_expansion": False,
    }
    try:
        candles = sorted(ohlcv_history, key=lambda candle: float(candle.get("t", 0.0)))
        closes = [float(candle["c"]) for candle in candles]
        highs = [float(candle["h"]) for candle in candles]
        lows = [float(candle["l"]) for candle in candles]
        volumes = [float(candle["v"]) for candle in candles]
        oi_values = _history_values(oi_history)
        if len(closes) < 49 or len(oi_values) < 2 or closes[-1] <= 0 or closes[-2] <= 0:
            return record

        latest_timestamp = float(candles[-1]["t"])
        if latest_timestamp > 1e12:
            latest_timestamp /= 1000
        latest_at = datetime.fromtimestamp(latest_timestamp, tz=timezone.utc)
        now = now or datetime.now(timezone.utc)
        if latest_at < now - timedelta(hours=2) or latest_at > now + timedelta(minutes=5):
            return record

        current_close = closes[-1]
        current_oi = current_oi if current_oi > 0 else oi_values[-1]
        record["open_interest_usd"] = current_oi * current_close
        record["data_fresh"] = True
        record["history_warmed"] = True
        record["price_change_1h"] = current_close / closes[-2] - 1
        record["price_change_24h"] = current_close / closes[-25] - 1 if closes[-25] > 0 else 0.0
        record["oi_change_1h"] = current_oi / oi_values[-2] - 1 if oi_values[-2] > 0 else 0.0

        baseline = volumes[-25:-1]
        mean_volume = statistics.mean(baseline)
        volume_stdev = statistics.pstdev(baseline)
        record["volume_zscore"] = (volumes[-1] - mean_volume) / volume_stdev if volume_stdev > 0 else 0.0

        prior_high = max(highs[-25:-1])
        prior_low = min(lows[-25:-1])
        range_width = prior_high - prior_low
        if range_width > 0:
            record["price_range_percentile"] = max(0.0, min(1.0, (current_close - prior_low) / range_width))

        upward_breakout = current_close > prior_high and record["price_change_1h"] >= 0.005
        downward_breakout = current_close < prior_low and record["price_change_1h"] <= -0.005
        record["fresh_breakout"] = (upward_breakout or downward_breakout) and record["volume_zscore"] >= 1.0

        previous_high = max(highs[-49:-25])
        previous_low = min(lows[-49:-25])
        recent_high = max(highs[-25:-1])
        recent_low = min(lows[-25:-1])
        upward_pullback = recent_high >= previous_high * 1.005 and current_close <= recent_high * 0.99
        downward_pullback = recent_low <= previous_low * 0.995 and current_close >= recent_low * 1.01
        record["post_breakout_pullback"] = not record["fresh_breakout"] and (upward_pullback or downward_pullback)
        at_range_edge = record["price_range_percentile"] >= 0.95 or record["price_range_percentile"] <= 0.05
        record["exhausted_expansion"] = (
            abs(record["price_change_24h"]) >= 0.10
            and at_range_edge
            and record["volume_zscore"] >= 2.0
            and record["oi_change_1h"] <= 0.0
        )
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return record
    return record


def _retry_delay(attempts: int) -> timedelta:
    return timedelta(minutes=config.DEEP_BACKFILL_RETRY_BASE_MINUTES * 2 ** max(attempts - 1, 0))


def claim_due_deep_backfill_jobs(conn, now: datetime, batch_size: int = config.DEEP_BACKFILL_BATCH_SIZE) -> list[str]:
    """Lease a bounded set of due jobs; expired leases are safe to retry."""
    lease_expired_at = now - timedelta(minutes=config.DEEP_BACKFILL_LEASE_MINUTES)
    jobs = conn.execute("""
        SELECT symbol
        FROM deep_backfill_jobs
        WHERE (status IN ('pending', 'failed') AND next_retry_at <= ?)
           OR (status = 'running' AND started_at <= ?)
        ORDER BY next_retry_at, created_at
        LIMIT ?
    """, (now, lease_expired_at, batch_size)).fetchall()
    symbols = [symbol for symbol, in jobs]
    if symbols:
        conn.execute(f"""
            UPDATE deep_backfill_jobs
            SET status = 'running', attempts = attempts + 1, started_at = ?, updated_at = ?
            WHERE symbol IN ({', '.join('?' for _ in symbols)})
        """, (now, now, *symbols))
        conn.commit()
    return symbols


def finish_deep_backfill_job(conn, symbol: str, now: datetime, error: Exception | None = None) -> None:
    if error is None:
        conn.execute("""
            UPDATE deep_backfill_jobs
            SET status = 'completed', last_error = NULL, completed_at = ?, updated_at = ?
            WHERE symbol = ?
        """, (now, now, symbol))
    else:
        attempts = conn.execute("SELECT attempts FROM deep_backfill_jobs WHERE symbol = ?", (symbol,)).fetchone()[0]
        conn.execute("""
            UPDATE deep_backfill_jobs
            SET status = 'failed', next_retry_at = ?, last_error = ?, updated_at = ?
            WHERE symbol = ?
        """, (now + _retry_delay(attempts), str(error), now, symbol))
    conn.commit()


def process_deep_backfill_jobs(now: datetime | None = None, bootstrap=None) -> list[str]:
    """Run due bootstrap jobs without allowing failures to interrupt scanning."""
    now = now or datetime.now(timezone.utc)
    bootstrap = bootstrap or bootstrap_trend_history.bootstrap
    conn = config.get_db_connection(read_only=False)
    try:
        symbols = claim_due_deep_backfill_jobs(conn, now)
    finally:
        conn.close()
    for symbol in symbols:
        try:
            bootstrap([symbol], days=14)
        except Exception as error:
            print(f"Failed to bootstrap deep discovery history for {symbol}: {error}")
            conn = config.get_db_connection(read_only=False)
            try:
                finish_deep_backfill_job(conn, symbol, now, error)
            finally:
                conn.close()
        else:
            conn = config.get_db_connection(read_only=False)
            try:
                finish_deep_backfill_job(conn, symbol, now)
            finally:
                conn.close()
    return symbols

def run_scanner():
    """Runs the hourly scan using a hybrid Binance-Coinalyze approach to optimize API rate limits."""
    print(f"[{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC] Starting hourly market scanner...")
    config.init_db()
    
    if not config.COINANALYZE_API_KEY:
        print("API Key not found. Skipping scan.")
        return None, []
        
    # 1. Fetch all 24h ticker data from Binance Futures (public, no rate limits)
    print("Pre-filtering markets via Binance Futures API...")
    binance_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
    try:
        resp = httpx.get(binance_url, timeout=15.0)
        if resp.status_code != 200:
            print(f"Failed to fetch Binance Futures tickers: {resp.status_code}. Aborting scan.")
            return None, []
        binance_data = resp.json()
    except Exception as e:
        print(f"Binance API request error: {e}. Aborting scan.")
        return None, []
        
    # Filter for USDT contracts and convert volume to float
    active_contracts = []
    for item in binance_data:
        sym = item.get("symbol", "")
        if sym.endswith("USDT") and not sym.startswith("10000"):  # Skip hyper-scaled symbols if any
            try:
                vol_24h_usd = float(item.get("quoteVolume", 0.0))
                price_change_24h = float(item.get("priceChangePercent", 0.0))
                last_price = float(item.get("lastPrice", 0.0))
                
                if vol_24h_usd >= config.SCANNER_MIN_24H_VOLUME_USD:
                    active_contracts.append({
                        "binance_symbol": sym,
                        "vol_24h_usd": vol_24h_usd,
                        "price_change_24h": price_change_24h,
                        "last_price": last_price
                    })
            except ValueError:
                continue
                
    print(f"Found {len(active_contracts)} active contracts on Binance with >= ${config.SCANNER_MIN_24H_VOLUME_USD:,.0f} 24h volume.")
    
    # Keep the full eligible universe as a point-in-time research snapshot.
    # Detailed CoinAnalyze requests remain capped to respect API limits.
    active_contracts.sort(key=lambda x: x["vol_24h_usd"], reverse=True)
    top_active = active_contracts[:config.SCANNER_MAX_CONTRACTS]
    
    # Map the whole eligible universe. The detailed top-50 scanner below reuses
    # these broad-fetch maps rather than making a second set of API requests.
    coinalyze_symbols_map = {}  # Coinalyze_symbol -> Binance_metadata
    for item in active_contracts:
        coinalyze_sym = map_binance_to_coinalyze(item["binance_symbol"])
        coinalyze_symbols_map[coinalyze_sym] = item
        
    symbols_list = list(coinalyze_symbols_map.keys())
    symbols_str = ",".join(symbols_list)
    print(f"Querying Coinalyze for {len(symbols_list)} eligible liquid contracts...")

    snapshot_time = datetime.now(timezone.utc)
    snapshot_conn = config.get_db_connection(read_only=False)
    try:
        selected_symbols = {item["binance_symbol"] for item in top_active}
        snapshot_contracts = [
            {**item, "coinalyze_symbol": map_binance_to_coinalyze(item["binance_symbol"])}
            for item in active_contracts
        ]
        record_universe_snapshot(snapshot_conn, snapshot_time, snapshot_contracts, selected_symbols)
        snapshot_conn.commit()
    finally:
        snapshot_conn.close()
    
    # 2. Fetch the broad data required by both discovery and detailed rankings.
    print("Fetching Open Interest from Coinalyze...")
    oi_data = fetch_coinalyze_data_batched("open-interest", {"symbols": symbols_str}, batch_size=20)
    oi_map = {
        item["symbol"]: float(item.get("openInterest", item.get("value", 0.0)))
        for item in oi_data
        if item.get("symbol")
    }
    
    # 3. Fetch 7-day hourly candlestick history from Coinalyze
    print("Fetching 7-day hourly OHLCV and OI history from Coinalyze...")
    now_epoch = int(time.time())
    from_epoch = now_epoch - 3600 * 24 * 7  # 7 days ago
    
    ohlcv_data = fetch_coinalyze_data_batched(
        "ohlcv-history",
        {
            "symbols": symbols_str,
            "interval": "1hour",
            "from": str(from_epoch),
            "to": str(now_epoch)
        },
        batch_size=20
    )
    
    ohlcv_map = {
        item["symbol"]: item.get("history", [])
        for item in ohlcv_data
        if item.get("symbol")
    }
    oi_history_data = fetch_coinalyze_data_batched(
        "open-interest-history",
        {
            "symbols": symbols_str,
            "interval": "1hour",
            "from": str(from_epoch),
            "to": str(now_epoch),
        },
        batch_size=20,
    )
    oi_history_map = {
        item["symbol"]: item.get("history", [])
        for item in oi_history_data
        if item.get("symbol")
    }
    print("Fetching current funding rates from Coinalyze...")
    funding_data = fetch_coinalyze_data_batched("funding-rate", {"symbols": symbols_str}, batch_size=20)
    funding_map = {
        item["symbol"]: float(item.get("value", 0.0))
        for item in funding_data
        if item.get("symbol")
    }

    broad_records = [
        build_discovery_record(
            binance_meta,
            coinalyze_sym,
            ohlcv_map.get(coinalyze_sym, []),
            oi_history_map.get(coinalyze_sym, []),
            oi_map.get(coinalyze_sym, 0.0),
            funding_map.get(coinalyze_sym, 0.0),
            snapshot_time,
        )
        for coinalyze_sym, binance_meta in coinalyze_symbols_map.items()
    ]

    discovery_conn = config.get_db_connection(read_only=False)
    try:
        two_pool_discovery.process_snapshot(discovery_conn, snapshot_time, broad_records)
        discovery_conn.commit()
        print(f"Recorded {len(broad_records)} broad discovery records.")
    except Exception as discovery_err:
        print(f"Failed to process broad discovery snapshot: {discovery_err}")
    finally:
        discovery_conn.close()
    
    # Read thresholds from env/defaults
    vol_threshold = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
    price_threshold = float(os.getenv("PRICE_SILENT_THRESHOLD", "3.0"))
    
    results = []
    
    # 4. Calculate scanner metrics
    for coinalyze_sym, binance_meta in (
        (map_binance_to_coinalyze(item["binance_symbol"]), item) for item in top_active
    ):
        oi = oi_map.get(coinalyze_sym, 0.0)
        if oi <= 0.0:
            continue
            
        history = ohlcv_map.get(coinalyze_sym, [])
        if len(history) < 25:
            continue
            
        try:
            closes = [float(c.get("c", 0.0)) for c in history]
            volumes = [float(c.get("v", 0.0)) for c in history]
            
            current_close = closes[-1]
            if current_close <= 0.0:
                continue
                
            # Average Hourly Volume over last 24 hours (excluding current hour)
            avg_hourly_vol_24h = statistics.median(volumes[-25:-1])
            if avg_hourly_vol_24h <= 0.0:
                continue
                
            # Volume Spike Multiple: last 1h volume vs 24h avg hourly volume
            last_hour_vol = volumes[-1]
            vol_spike_mult = last_hour_vol / avg_hourly_vol_24h
            
            # 1h Price Change (%) - current vs 1 hour ago
            price_1h_ago = closes[-2]
            price_change_1h = ((current_close - price_1h_ago) / price_1h_ago) * 100 if price_1h_ago > 0 else 0.0
            
            # 7d Cumulative USD Volume
            vol_7d_usd = sum(v * c for v, c in zip(volumes, closes))
            
            # USD Open Interest
            oi_usd = oi * current_close
            
            # Volume-to-OI Ratio
            vol_to_oi_ratio = vol_7d_usd / oi_usd if oi_usd > 0 else 0.0
            
            # Check Accumulation Condition
            is_accumulating = (vol_spike_mult >= vol_threshold) and (abs(price_change_1h) <= price_threshold)
            
            # Extract underlying name (e.g. BTCUSDT -> BTC)
            clean_sym = binance_meta["binance_symbol"]
            underlying = clean_sym.split("USDT")[0]
            
            results.append({
                "underlying": underlying,
                "symbol": coinalyze_sym,
                "volume_7d_usd": vol_7d_usd,
                "open_interest_usd": oi_usd,
                "vol_to_oi_ratio": vol_to_oi_ratio,
                "volume_spike_multiple": vol_spike_mult,
                "price_change_1h": price_change_1h,
                "is_accumulating": is_accumulating,
                "close_price": current_close
            })
        except Exception as e:
            print(f"Error processing symbol {coinalyze_sym}: {e}")
            
    if not results:
        print("No valid metrics calculated.")
        process_deep_backfill_jobs()
        return None, []
        
    # Sort all by Volume-to-OI ratio descending
    results.sort(key=lambda x: x["vol_to_oi_ratio"], reverse=True)
    
    # Take top 10 by vol-to-OI ratio
    top_10 = results[:10]
    
    # Extract all currently accumulating assets
    accumulating_all = [r for r in results if r["is_accumulating"]]
    
    current_time = snapshot_time
    
    # 5. Persist top 10 results to DuckDB scanner_history
    db_conn = config.get_db_connection(read_only=False)
    try:
        for i, res in enumerate(top_10, 1):
            db_conn.execute("""
                INSERT INTO scanner_history (
                    timestamp, rank, underlying, symbol, volume_7d_usd,
                    open_interest_usd, vol_to_oi_ratio, volume_spike_multiple,
                    price_change_1h, is_accumulating
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                current_time, i, res["underlying"], res["symbol"], res["volume_7d_usd"],
                res["open_interest_usd"], res["vol_to_oi_ratio"], res["volume_spike_multiple"],
                res["price_change_1h"], res["is_accumulating"]
            ))
        db_conn.commit()
        print("Scanner results persisted to database.")
    except Exception as db_err:
        print(f"Failed to persist scanner results: {db_err}")
    finally:
        db_conn.close()
        
    # 6. Save pairlist to data/scanned_pairs.json
    pairs_clean = []
    pairs_ccxt = []
    
    watchlist_assets = []
    seen = set()
    
    # Accumulating assets are highest priority for other bots to watch
    for r in accumulating_all:
        if r["symbol"] not in seen:
            watchlist_assets.append(r)
            seen.add(r["symbol"])
            
    for r in top_10:
        if r["symbol"] not in seen:
            watchlist_assets.append(r)
            seen.add(r["symbol"])
            
    for r in watchlist_assets:
        clean_sym = r["symbol"].split("_")[0]
        pairs_clean.append(clean_sym)
        base = r["underlying"]
        quote = "USDT" if "USDT" in clean_sym else "USD"
        pairs_ccxt.append(f"{base}/{quote}:{quote}")
        
    json_data = {
        "last_updated": current_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "research_universe": [
            map_binance_to_coinalyze(item["binance_symbol"])
            for item in top_active
        ],
        "pairs_clean": pairs_clean,
        "pairs_ccxt": pairs_ccxt,
        "rankings": [
            {
                "rank": i + 1,
                "underlying": r["underlying"],
                "symbol": r["symbol"],
                "volume_7d_usd": r["volume_7d_usd"],
                "open_interest_usd": r["open_interest_usd"],
                "vol_to_oi_ratio": r["vol_to_oi_ratio"],
                "volume_spike_multiple": r["volume_spike_multiple"],
                "price_change_1h": r["price_change_1h"],
                "is_accumulating": r["is_accumulating"]
            }
            for i, r in enumerate(top_10)
        ],
        "accumulation_alerts": [
            {
                "underlying": r["underlying"],
                "symbol": r["symbol"],
                "volume_7d_usd": r["volume_7d_usd"],
                "open_interest_usd": r["open_interest_usd"],
                "vol_to_oi_ratio": r["vol_to_oi_ratio"],
                "volume_spike_multiple": r["volume_spike_multiple"],
                "price_change_1h": r["price_change_1h"]
            }
            for r in accumulating_all
        ]
    }
    
    json_path = config.DEFAULT_DB_DIR / "scanned_pairs.json"
    try:
        with open(json_path, "w") as f:
            json.dump(json_data, f, indent=2)
        print(f"Scanned pairlist saved to {json_path}.")
    except Exception as json_err:
        print(f"Failed to save JSON pairlist: {json_err}")

    process_deep_backfill_jobs()
    return json_data, accumulating_all

def format_telegram_scanner_message(json_data, accumulating_all) -> str:
    """Formats the scanner results into a beautiful Telegram message."""
    if not json_data:
        return "⚠️ Scanner run failed. Check console logs."
        
    lines = []
    lines.append("🔍 *HOURLY MARKET SCANNER ROTATION* 🔍")
    lines.append(f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
    
    # 1. Accumulation alerts
    lines.append("🔥 *ACCUMULATION ALERTS (Watchlist)*")
    if accumulating_all:
        lines.append("_Volume spike >= 1.5x average with flat price (<= 3.0%):_")
        for i, r in enumerate(accumulating_all, 1):
            clean_sym = r["symbol"].split("_")[0]
            lines.append(
                f"{i}. 🚀 *#{r['underlying']}* ({clean_sym})\n"
                f"   • Vol Spike: *{r['volume_spike_multiple']:.2f}x* | 1h Price: *{r.get('price_change_1h', r.get('price_change_24h', 0.0)):+.2f}%*\n"
                f"   • 7D Vol: ${r['volume_7d_usd']/1e6:.1f}M | Current OI: ${r['open_interest_usd']/1e6:.1f}M"
            )
    else:
        lines.append("_None (No assets with volume spikes and flat price)._")
        
    lines.append("\n🏆 *TOP HIGH VOLUME / LOW OI*")
    lines.append("_Ranked by 7D USD Volume relative to Open Interest:_")
    
    for i, r in enumerate(json_data["rankings"][:10], 1):
        clean_sym = r["symbol"].split("_")[0]
        prefix = "🔥 " if r["is_accumulating"] else f"{i}. "
        lines.append(
            f"{prefix}*#{r['underlying']}* ({clean_sym})\n"
            f"   • Velocity Ratio: *{r['vol_to_oi_ratio']:.2f}x*\n"
            f"   • 7D Vol: ${r['volume_7d_usd']/1e6:.1f}M | Current OI: ${r['open_interest_usd']/1e6:.1f}M\n"
            f"   • Vol Spike: {r['volume_spike_multiple']:.1f}x | 1h Price: {r.get('price_change_1h', r.get('price_change_24h', 0.0)):+.2f}%"
        )
        
    lines.append('\n💡 Watchlist json updated at `data/scanned_pairs.json` for external bots.')
    return "\n".join(lines)

if __name__ == "__main__":
    res_json, acc_all = run_scanner()
    if res_json:
        print("\n--- Telegram Message Output Preview ---")
        print(format_telegram_scanner_message(res_json, acc_all))
