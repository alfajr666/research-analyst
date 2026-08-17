import json
import time
import sys
import argparse
from uuid import uuid4
from datetime import datetime, timezone, timedelta
import config
from alpha_outbox import OUTBOX_DIR
from ingest_coinalyze import ingest_coinalyze
from ingest_deribit import ingest_deribit
from analyze import update_daily_summary, get_profile_summary

# Startup tracking for grace period & hourly scanner
STARTUP_TIME = time.time()
DAEMON_MODE = False

def prune_db(conn, futures_retention_days: int, auxiliary_retention_days: int = 30):
    """Prunes auxiliary data and optionally retains longer futures history for research."""
    auxiliary_limit = datetime.now(timezone.utc) - timedelta(days=auxiliary_retention_days)
    futures_limit = (
        datetime.now(timezone.utc) - timedelta(days=futures_retention_days)
        if futures_retention_days > 0 else None
    )
    print(f"Pruning auxiliary records older than {auxiliary_limit.strftime('%Y-%m-%d %H:%M:%S')} UTC...")
    try:
        # Prune option_chains
        res_opt = conn.execute("DELETE FROM option_chains WHERE timestamp < ?", (auxiliary_limit,)).rowcount
        res_brain = conn.execute("DELETE FROM brain_outputs WHERE timestamp < ?", (auxiliary_limit,)).rowcount
        if futures_limit is None:
            res_fut = 0
            print("  Futures history pruning disabled.")
        else:
            res_fut = conn.execute("DELETE FROM futures_data WHERE timestamp < ?", (futures_limit,)).rowcount
        
        conn.commit()
        print(f"  Pruned: {res_opt} option chains, {res_fut} futures rows, {res_brain} brain outputs.")
        
        # Vacuum database to reclaim disk space (only once a day between 00:00 and 01:00 UTC)
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour == 0:
            print("  Running daily DuckDB VACUUM to reclaim space...")
            conn.execute("VACUUM;")
            print("  Database vacuum completed.")
        else:
            print("  Skipping DuckDB VACUUM (runs daily at 00:00 UTC).")
    except Exception as e:
        print(f"Error during database pruning: {e}", file=sys.stderr)

def check_and_alert_confluences(conn):
    """Checks and records High Confluence Entry events with a one-hour cooldown."""
    global DAEMON_MODE, STARTUP_TIME
    if DAEMON_MODE:
        elapsed = time.time() - STARTUP_TIME
        if elapsed < 1800: # 30 minutes grace period
            print(f"Skipping alerts during startup grace period (elapsed: {elapsed/60:.1f}/30.0 mins).")
            return
            
    print("Checking for High Confluence Entry alerts...")
    # Fetch distinct underlyings from the database
    underlyings_rows = conn.execute("SELECT DISTINCT underlying FROM futures_data").fetchall()
    underlyings = [row[0] for row in underlyings_rows if row[0]]
    
    if not underlyings:
        print("  No underlyings found in database.")
        return
        
    for underlying in underlyings:
        try:
            # We look for a 1-day (24h) profile for daily confluence
            prof = get_profile_summary(conn, underlying, lookback_days=1)
            if not prof or prof.get("status") == "Insufficient data":
                continue
                
            # If it's a High Confluence Entry
            if prof.get("ta_signal") == "🔥 HIGH CONFLUENCE ENTRY":
                directional_bias = prof.get("directional_bias", "neutral")

                # Daily Regime as BOOSTER/DAMPENER (not a hard gate)
                # Hard gates are: dual VWAP split (neutral) + 4h structural conflict.
                # HMM regime only modifies the conviction score — never vetoes.
                daily_sig = conn.execute("""
                    SELECT signal, conviction, regime, regime_conf
                    FROM regime_signals
                    WHERE underlying = ?
                    ORDER BY date DESC LIMIT 1
                """, (underlying,)).fetchone()

                daily_regime_info = {"regime": "unknown", "regime_conf": 0.0, "daily_conv": None, "signal": "no_signal"}

                if daily_sig:
                    daily_direction, daily_conv, daily_regime, daily_regime_conf = daily_sig
                    daily_regime_info = {
                        "regime": daily_regime or "unknown",
                        "regime_conf": daily_regime_conf or 0.0,
                        "daily_conv": daily_conv,
                        "signal": daily_direction or "no_signal",
                    }

                # 6-factor conviction scoring (5 confluence factors + 1 HMM regime booster)
                confidence_score = prof.get("confidence_score", 0)
                # HMM regime: booster if aligned, dampener if opposing/ranging, neutral if unknown
                regime = daily_regime_info["regime"]
                regime_conf = daily_regime_info["regime_conf"]
                if regime in ("trending_up", "trending_down") and regime_conf >= 0.50:
                    # aligned only if HMM direction matches 15m bias
                    hmm_aligned = (regime == "trending_up" and directional_bias == "long") or \
                                  (regime == "trending_down" and directional_bias == "short")
                    hmm_factor = 2 if hmm_aligned else -2   # boost aligned, dampen opposite trend
                elif regime in ("ranging", "high_vol"):
                    hmm_factor = -1                          # dampen choppy regimes
                else:
                    hmm_factor = 0                           # unknown / low conf → neutral
                total_score = confidence_score + hmm_factor

                if total_score < 5:
                    print(f"  Suppressed 15m alert for {underlying}: conviction score {total_score}/6 (5-factor: {confidence_score}, HMM factor: {hmm_factor}, regime: {regime}) — below threshold.")
                    continue

                # 4h Structural Trend Filter
                from analyze import get_structural_trend
                structure = get_structural_trend(conn, underlying, timeframe="4h")
                struct_dir = structure.get("direction", "no_signal")
                if struct_dir != "no_signal" and directional_bias != "neutral" and struct_dir != directional_bias:
                    print(f"  Suppressed 15m alert for {underlying}: 4h structural trend ({struct_dir}) conflicts with 15m alert ({directional_bias}).")
                    continue

                # Check when we last sent an alert for this asset (1 hour cooldown)
                cooldown_time = datetime.now(timezone.utc) - timedelta(hours=1)
                last_alert = conn.execute(
                    "SELECT alert_time FROM confluence_alerts WHERE underlying = ? AND alert_time >= ? ORDER BY alert_time DESC LIMIT 1",
                    (underlying, cooldown_time)
                ).fetchone()
                
                if last_alert:
                    # An alert was already sent within the last hour
                    continue
                    
                # Format price values depending on price magnitude
                def fmt_price(val):
                    if val is None:
                        return "N/A"
                    if val < 1.0:
                        return f"${val:.6f}"
                    return f"${val:,.2f}" if val < 10000.0 else f"${round(val):,.0f}"

                price_str = fmt_price(prof['close'])
                poc_str = fmt_price(prof['volume_poc'])
                vwap_str = fmt_price(prof.get('vwap'))
                ema26_str = fmt_price(prof['ema26'])
                ema99_str = fmt_price(prof['ema99'])

                val_str = fmt_price(prof.get('val'))
                vah_str = fmt_price(prof.get('vah'))

                hvn_list = [prof['volume_poc']] + prof.get('hvns', [])
                hvns_str = ", ".join([fmt_price(x) for x in hvn_list[:3]])
                lvns_list = prof.get('lvns', [])
                lvns_str = ", ".join([fmt_price(x) for x in lvns_list[:2]]) if lvns_list else "N/A"

                data_start = prof.get('data_start')
                data_end = prof.get('data_end')
                candle_count = prof.get('candle_count', 0)
                if data_start and data_end:
                    anchor_str = (
                        f"• *Anchored from:* {data_start.strftime('%Y-%m-%d %H:%M')} → {data_end.strftime('%Y-%m-%d %H:%M')} UTC\n"
                        f"  ({candle_count} × 15m candles — CoinAnalyze perps)"
                    )
                else:
                    anchor_str = "• *Anchored from:* N/A"

                # --- Build the enhanced alert message (directional) ---
                shape = prof.get('profile_shape', '')
                shape_ctx = "no directional edge until VA break" if shape and shape.startswith("D") else prof.get('profile_shape_desc', '')

                vol_conf = " + volume" if prof.get('volume_surge') else ""

                def fmt_rr(val):
                    return f"R:R {val}" if val is not None else "R:R N/A"

                trig_long = fmt_price(prof.get('trigger_long'))
                trig_short = fmt_price(prof.get('trigger_short'))
                
                if directional_bias == "long":
                    stop_str = fmt_price(prof.get('stop_anchor_long') or prof.get('stop_anchor'))
                elif directional_bias == "short":
                    stop_str = fmt_price(prof.get('stop_anchor_short') or prof.get('stop_anchor'))
                else:
                    stop_str = fmt_price(prof.get('stop_anchor_long') or prof.get('stop_anchor'))

                t1l = fmt_price(prof.get('t1_long'))
                t2l = fmt_price(prof.get('t2_long'))
                t1s = fmt_price(prof.get('t1_short'))
                t2s = fmt_price(prof.get('t2_short'))

                directional_bias = prof.get("directional_bias", "neutral")
                confirmations_list = prof.get("confirmations", [])
                conf_count = len(confirmations_list)

                # Build HMM regime line for the message
                dr = daily_regime_info
                regime_pct = f"{dr['regime_conf']*100:.0f}%" if dr['regime_conf'] else "?"
                daily_conv_label = f" ({dr['daily_conv']})" if dr['daily_conv'] else ""
                hmm_line = f"▫️ *Daily Regime:* {dr['regime']} ({regime_pct} confidence{daily_conv_label})"

                if directional_bias == "neutral":
                    alert_msg = (
                        f"🔔 *HIGH CONFLUENCE ENTRY ALERT* 🔔\n\n"
                        f"• *Asset:* #{underlying}\n"
                        f"• *Current Price:* {price_str}\n\n"
                        f"▫️ *Trigger Long:*  Close above {trig_long}{vol_conf}\n"
                        f"▫️ *Trigger Short:* Close below {trig_short}{vol_conf}\n"
                        f"▫️ *Stop anchor:*   POC {stop_str}\n\n"
                        f"▫️ *If Long:*  T1 {t1l} | T2 {t2l} | {fmt_rr(prof.get('rr_long_t2'))}\n"
                        f"▫️ *If Short:* T1 {t1s} | T2 {t2s} | {fmt_rr(prof.get('rr_short_t2'))}\n\n"
                        f"▫️ *Bias:* {prof.get('bias_assessment', 'N/A')}\n"
                        f"▫️ *Profile shape:* {shape} → {shape_ctx}\n"
                        f"▫️ *Staleness:* levels as of last 15m close ({prof.get('staleness_mins', '?')}m ago)\n\n"
                        f"• *Volume POC:* {poc_str} | *VWAP:* {vwap_str}\n"
                        f"• *Value Area:* {val_str} – {vah_str}\n"
                        f"• *EMA26:* {ema26_str} | *EMA99:* {ema99_str}\n"
                        f"• *HVNs:* {hvns_str} | *LVNs:* {lvns_str}\n\n"
                        f"{hmm_line}\n\n"
                        f"{anchor_str}\n\n"
                        f"• *Signal Details:*\n"
                        f"  _{prof.get('ta_desc')}_\n\n"
                        f"_Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
                    )
                else:
                    dir_label = "LONG" if directional_bias == "long" else "SHORT"
                    title = f"🔔 *🔥 HIGH CONVICTION — {dir_label} SETUP* 🔔"
                    ema_dir = "Bullish (26>99)" if directional_bias == "long" else "Bearish (26<99)"

                    if directional_bias == "long":
                        entry_line = f"▫️ *Entry:* Close above {trig_long}{vol_conf}"
                        targets_line = f"▫️ *Targets:* T1 {t1l} | T2 {t2l} | {fmt_rr(prof.get('rr_long_t2'))}"
                    else:
                        entry_line = f"▫️ *Entry:* Close below {trig_short}{vol_conf}"
                        targets_line = f"▫️ *Targets:* T1 {t1s} | T2 {t2s} | {fmt_rr(prof.get('rr_short_t2'))}"

                    conf_summary = f" | ✅ {conf_count} confirmations" if conf_count > 0 else ""
                    sizing_str = "Dynamic Volatility (ATR-based TP/SL)" if prof.get('latest_atr') else "Static VA Width"

                    alert_msg = (
                        f"{title}\n\n"
                        f"• *Asset:* #{underlying} | *Confidence:* 🔥 HIGH CONVICTION{conf_summary}\n"
                        f"• *Current Price:* {price_str}\n\n"
                        f"{entry_line}\n"
                        f"▫️ *Stop anchor:* {stop_str}\n\n"
                        f"{targets_line}\n\n"
                        f"▫️ *Trend:* 15m {ema_dir} | 4h Structure: {struct_dir.upper()}\n"
                        f"▫️ *Momentum:* RSI({prof.get('latest_rsi', 0):.1f})\n"
                        f"▫️ *Sizing:* {sizing_str}\n"
                        f"▫️ *Bias:* {prof.get('bias_assessment', 'N/A')}\n"
                        f"▫️ *Profile:* {shape} → {shape_ctx}\n"
                        f"▫️ *Staleness:* last 15m close ({prof.get('staleness_mins', '?')}m ago)\n\n"
                        f"• *Volume POC:* {poc_str} | *VWAP:* {vwap_str}\n"
                        f"• *Value Area:* {val_str} – {vah_str}\n"
                        f"• *EMA26:* {ema26_str} | *EMA99:* {ema99_str}\n"
                        f"• *HVNs:* {hvns_str} | *LVNs:* {lvns_str}\n\n"
                        f"{hmm_line}\n\n"
                        f"{anchor_str}\n\n"
                        f"• *Signal Details:*\n"
                        f"  _{prof.get('ta_desc')}_\n\n"
                        f"_Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
                    )
                
                # Preserve cooldown state; signal_publisher is the sole automated sender.
                conn.execute(
                    "INSERT INTO confluence_alerts (underlying, price, poc, ema26, ema99, val, vah, hvns, lvns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (underlying, prof['close'], prof['volume_poc'], prof['ema26'], prof['ema99'],
                     prof.get('val'), prof.get('vah'),
                     json.dumps(prof.get('hvns', [])),
                     json.dumps(prof.get('lvns', [])))
                )
                conn.commit()
                print(f"  High confluence alert recorded for {underlying}; Telegram delivery is disabled here.")
        except Exception as e:
            print(f"  Error checking alert for {underlying}: {e}")

def _run_pipeline():
    """Runs the full sequential ingestion, scanning, and alerts pipeline."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==========================================")
    print(f"PIPELINE RUN: {now_str} UTC")
    print(f"==========================================")
    
    # 1. Ensure DB schemas are initialized
    config.init_db()

    # Binance-native discovery is independent of the rate-limited CoinAnalyze
    # scanner and must run first at each completed-hour boundary.
    if config.BINANCE_OI_ROTATION_ENABLED:
        try:
            from binance_oi_rotation_scanner import completed_hour, run_scanner as run_binance_oi_rotation_scanner
            interval = completed_hour()
            conn = config.get_db_connection(read_only=True)
            try:
                existing = conn.execute(
                    """SELECT 1 FROM binance_oi_rotation_scans
                    WHERE source = 'binance_usdm' AND completed_interval_at = ?
                      AND scanner_version = ? AND status = 'complete' LIMIT 1""",
                    (interval, config.BINANCE_OI_ROTATION_SCANNER_VERSION),
                ).fetchone()
            finally:
                conn.close()
            if existing:
                print(f"Binance OI rotation scan already recorded for {interval.isoformat()}.")
            else:
                feed = run_binance_oi_rotation_scanner()
                print(f"Binance OI rotation feed published with {len(feed['candidates'])} candidates.")
        except Exception as e:
            print(f"Error during Binance OI rotation scan: {e}", file=sys.stderr)
    
    # 2. Run hourly scanner if 1 hour has elapsed (check scanner_history table for last run)
    conn = config.get_db_connection()
    last_scan = conn.execute("SELECT MAX(timestamp) FROM scanner_history").fetchone()[0]
    conn.close()
    elapsed = (datetime.now(timezone.utc) - last_scan).total_seconds() if last_scan else 3601
    if elapsed >= 3600:
        print("Running hourly market scanner...")
        try:
            from scanner import run_scanner
            json_data, accumulating_all = run_scanner()
            
            # Scanner results remain available to the accumulation monitor, but this
            # legacy rotation no longer owns Telegram delivery.
            if json_data:
                print("Scanner rotation completed; Telegram delivery is disabled here.")

            # Feed scanner-detected accumulations to the accumulation monitor
            if accumulating_all:
                pending_path = config.DEFAULT_DB_DIR / "scanner_pending_accums.json"
                pending = {
                    "scanner_timestamp": datetime.now(timezone.utc).isoformat(),
                    "symbols": {
                        r["symbol"]: {
                            "underlying": r["underlying"],
                            "vol_spike": r["volume_spike_multiple"],
                            "price_change_1h": r["price_change_1h"],
                            "vol_7d_usd": r["volume_7d_usd"],
                            "oi_usd": r["open_interest_usd"],
                        }
                        for r in accumulating_all
                    }
                }
                with open(pending_path, "w") as f:
                    json.dump(pending, f, indent=2)
                print(f"  Fed {len(accumulating_all)} scanner accumulations to monitor.")
        except Exception as e:
            print(f"Error during hourly scan: {e}", file=sys.stderr)

    # 3. Ingest futures data (this now dynamically loads scanned symbols)
    try:
        ingest_coinalyze()
    except Exception as e:
        print(f"Error during CoinAnalyze ingestion: {e}", file=sys.stderr)
        
    # 3. Ingest options data (DISABLED)
    # try:
    #     ingest_deribit()
    # except Exception as e:
    #     print(f"Error during Deribit ingestion: {e}", file=sys.stderr)
        
    # 4. Update daily ATM IV lookback table (DISABLED)
    # try:
    #     # Get write connection and save today's stats
    #     conn = config.get_db_connection(read_only=False)
    #     try:
    #         update_daily_summary(conn)
    #     finally:
    #         conn.close()
    # except Exception as e:
    #     print(f"Error updating daily summary: {e}", file=sys.stderr)
        
    # 5. Retain futures history long enough for alpha research.
    try:
        conn = config.get_db_connection(read_only=False)
        try:
            prune_db(conn, futures_retention_days=config.FUTURES_RETENTION_DAYS)
        finally:
            conn.close()
    except Exception as e:
        print(f"Error executing database pruning: {e}", file=sys.stderr)
        
    # 6. Check for High Confluence Entry Alerts
    try:
        conn = config.get_db_connection(read_only=False)
        try:
            check_and_alert_confluences(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"Error checking confluence alerts: {e}", file=sys.stderr)

    # The scheduled runtime has one DuckDB owner. Run context and evaluators
    # sequentially after ingestion so they see the just-closed local data.
    try:
        from regime_evaluator import run_once as run_regime_evaluator
        run_regime_evaluator()
    except Exception as e:
        print(f"Error running regime evaluator: {e}", file=sys.stderr)
    from accumulation_evaluator import run_once as run_accumulation_evaluator
    from acceleration_evaluator import run_once as run_acceleration_evaluator
    from ignition_evaluator import run_once as run_ignition_evaluator
    for name, evaluator in (
        ("accumulation", run_accumulation_evaluator),
        ("ignition", run_ignition_evaluator),
        ("acceleration", run_acceleration_evaluator),
    ):
        try:
            evaluator()
        except Exception as e:
            print(f"Error running {name} evaluator: {e}", file=sys.stderr)

    print(f"Pipeline run completed.")


def _start_pipeline_run(run_id: str, started_at: datetime) -> None:
    connection = config.get_db_connection()
    try:
        connection.execute("""
            INSERT INTO pipeline_runs (run_id, started_at, status, details_json)
            VALUES (?, ?, 'running', '{}')
        """, (run_id, started_at))
    finally:
        connection.close()


def _finish_pipeline_run(run_id: str, status: str, error: Exception | None = None) -> None:
    """Persist health data without letting metrics failure delay the next cycle."""
    try:
        completed_at = datetime.now(timezone.utc)
        connection = config.get_db_connection()
        try:
            latest_data_at = connection.execute("SELECT MAX(timestamp) FROM futures_data").fetchone()[0]
            freshness = (completed_at - latest_data_at).total_seconds() if latest_data_at else None
            connection.execute("""
                UPDATE pipeline_runs
                SET completed_at = ?, status = ?, data_freshness_seconds = ?,
                    outbox_depth = ?, error_message = ?, details_json = ?
                WHERE run_id = ?
            """, (
                completed_at, status, freshness, len(list(OUTBOX_DIR.glob("*.json"))),
                str(error)[:500] if error else None,
                json.dumps({"data_latest_at": latest_data_at.isoformat() if latest_data_at else None}),
                run_id,
            ))
        finally:
            connection.close()
    except Exception as metrics_error:
        print(f"Error recording pipeline metrics: {metrics_error}", file=sys.stderr)


def run_pipeline():
    """Run the deterministic pipeline and record its durable operational state."""
    config.init_db()
    run_id = str(uuid4())
    _start_pipeline_run(run_id, datetime.now(timezone.utc))
    try:
        _run_pipeline()
    except Exception as error:
        _finish_pipeline_run(run_id, "failed", error)
        raise
    _finish_pipeline_run(run_id, "completed")


def publish_events():
    """Persist and deliver queued events after each sequential pipeline cycle."""
    from signal_publisher import SignalPublisher
    print(f"Signal publisher: {SignalPublisher().run_once()}")

def main():
    parser = argparse.ArgumentParser(description="BTC/ETH Options and Futures Research Ingestion Orchestrator")
    parser.add_argument("--once", action="store_true", help="Run the pipeline once and exit immediately.")
    args = parser.parse_args()
    config.secure_secret_file()
    
    if args.once:
        run_pipeline()
        publish_events()
    else:
        global DAEMON_MODE
        DAEMON_MODE = True
        interval_secs = config.INGEST_INTERVAL_MINS * 60
        print(f"Starting orchestrator daemon. Loop interval: {config.INGEST_INTERVAL_MINS} minutes ({interval_secs}s)...")
        next_pipeline_at = 0.0
        while True:
            now = time.monotonic()
            if now >= next_pipeline_at:
                start_time = time.monotonic()
                try:
                    run_pipeline()
                except Exception as e:
                    print(f"Critical error in orchestrator pipeline: {e}", file=sys.stderr)
                next_pipeline_at = start_time + interval_secs
            time.sleep(min(30, max(1, next_pipeline_at - time.monotonic())))

if __name__ == "__main__":
    main()
