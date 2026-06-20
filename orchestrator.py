import json
import time
import sys
import argparse
import httpx
from datetime import datetime, timezone, timedelta
import config
from ingest_coinalyze import ingest_coinalyze
from ingest_deribit import ingest_deribit
from analyze import update_daily_summary, get_profile_summary

# Startup tracking for grace period & hourly scanner
STARTUP_TIME = time.time()
DAEMON_MODE = False

def prune_db(conn, retention_days: int = 30):
    """Removes historical data older than retention_days to keep the database size bounded."""
    limit_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
    print(f"Pruning database records older than {limit_date.strftime('%Y-%m-%d %H:%M:%S')} UTC...")
    try:
        # Prune option_chains
        res_opt = conn.execute("DELETE FROM option_chains WHERE timestamp < ?", (limit_date,)).rowcount
        # Prune futures_data
        res_fut = conn.execute("DELETE FROM futures_data WHERE timestamp < ?", (limit_date,)).rowcount
        # Prune brain_outputs
        res_brain = conn.execute("DELETE FROM brain_outputs WHERE timestamp < ?", (limit_date,)).rowcount
        
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
    """Checks all active assets for High Confluence Entry alerts and sends notifications to Telegram with 1h cooldown."""
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
                # Check conviction threshold before proceeding
                min_conviction = config.MIN_CONVICTION.upper()
                alert_conviction = prof.get("conviction", "LOW")
                conv_order = {"LOW": 0, "MODERATE": 1, "HIGH": 2}
                if conv_order.get(alert_conviction, 0) < conv_order.get(min_conviction, 0):
                    print(f"  Skipping {alert_conviction} conviction alert for {underlying} (min: {min_conviction})")
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
                stop_str = fmt_price(prof.get('stop_anchor'))
                t1l = fmt_price(prof.get('t1_long'))
                t2l = fmt_price(prof.get('t2_long'))
                t1s = fmt_price(prof.get('t1_short'))
                t2s = fmt_price(prof.get('t2_short'))

                directional_bias = prof.get("directional_bias", "neutral")
                conviction = prof.get("conviction", "LOW")
                confidence_score = prof.get("confidence_score", 0)
                confirmations_list = prof.get("confirmations", [])

                conv_icons = {"HIGH": "🔥 HIGH CONVICTION", "MODERATE": "✅ MODERATE", "LOW": "⚠️ LOW"}

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
                        f"{anchor_str}\n\n"
                        f"• *Signal Details:*\n"
                        f"  _{prof.get('ta_desc')}_\n\n"
                        f"_Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
                    )
                else:
                    dir_label = "LONG" if directional_bias == "long" else "SHORT"
                    conv_label = conv_icons.get(conviction, conviction)
                    title = f"🔔 *{conv_label} — {dir_label} SETUP* 🔔"
                    ema_dir = "Bullish (26>99)" if directional_bias == "long" else "Bearish (26<99)"
                    conf_count = len(confirmations_list)

                    if directional_bias == "long":
                        entry_line = f"▫️ *Entry:* Close above {trig_long}{vol_conf}"
                        targets_line = f"▫️ *Targets:* T1 {t1l} | T2 {t2l} | {fmt_rr(prof.get('rr_long_t2'))}"
                    else:
                        entry_line = f"▫️ *Entry:* Close below {trig_short}{vol_conf}"
                        targets_line = f"▫️ *Targets:* T1 {t1s} | T2 {t2s} | {fmt_rr(prof.get('rr_short_t2'))}"

                    conf_summary = f" | ✅ {conf_count} confirmations" if conf_count > 0 else ""

                    alert_msg = (
                        f"{title}\n\n"
                        f"• *Asset:* #{underlying} | *Confidence:* {conv_label}{conf_summary}\n"
                        f"• *Current Price:* {price_str}\n\n"
                        f"{entry_line}\n"
                        f"▫️ *Stop anchor:* POC {stop_str}\n\n"
                        f"{targets_line}\n\n"
                        f"▫️ *Trend:* EMA26({ema26_str}) {'>' if directional_bias == 'long' else '<'} EMA99({ema99_str}) — {ema_dir}\n"
                        f"▫️ *Bias:* {prof.get('bias_assessment', 'N/A')}\n"
                        f"▫️ *Profile:* {shape} → {shape_ctx}\n"
                        f"▫️ *Staleness:* last 15m close ({prof.get('staleness_mins', '?')}m ago)\n\n"
                        f"• *Volume POC:* {poc_str} | *VWAP:* {vwap_str}\n"
                        f"• *Value Area:* {val_str} – {vah_str}\n"
                        f"• *EMA26:* {ema26_str} | *EMA99:* {ema99_str}\n"
                        f"• *HVNs:* {hvns_str} | *LVNs:* {lvns_str}\n\n"
                        f"{anchor_str}\n\n"
                        f"• *Signal Details:*\n"
                        f"  _{prof.get('ta_desc')}_\n\n"
                        f"_Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC_"
                    )
                
                # Send the Telegram alert
                token = config.TELEGRAM_BOT_TOKEN
                chat_id = config.TELEGRAM_CHAT_ID
                if token and chat_id:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    payload = {
                        "chat_id": chat_id,
                        "text": alert_msg,
                        "parse_mode": "Markdown"
                    }
                    resp = httpx.post(url, json=payload, timeout=10)
                    if resp.status_code == 200:
                        print(f"  Alert sent successfully for {underlying}")
                        # Record alert to database
                        conn.execute(
                            "INSERT INTO confluence_alerts (underlying, price, poc, ema26, ema99, val, vah, hvns, lvns) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (underlying, prof['close'], prof['volume_poc'], prof['ema26'], prof['ema99'],
                             prof.get('val'), prof.get('vah'),
                             json.dumps(prof.get('hvns', [])),
                             json.dumps(prof.get('lvns', [])))
                        )
                        conn.commit()
                    else:
                        print(f"  Failed to send Telegram alert for {underlying}: {resp.text}")
                else:
                    print(f"  Telegram credentials not configured; alert skipped for {underlying}")
        except Exception as e:
            print(f"  Error checking alert for {underlying}: {e}")

def run_pipeline():
    """Runs the full sequential ingestion, scanning, and alerts pipeline."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n==========================================")
    print(f"PIPELINE RUN: {now_str} UTC")
    print(f"==========================================")
    
    # 1. Ensure DB schemas are initialized
    config.init_db()
    
    # 2. Run hourly scanner if 1 hour has elapsed (check scanner_history table for last run)
    conn = config.get_db_connection()
    last_scan = conn.execute("SELECT MAX(timestamp) FROM scanner_history").fetchone()[0]
    conn.close()
    elapsed = (datetime.now(timezone.utc) - last_scan).total_seconds() if last_scan else 3601
    if elapsed >= 3600:
        print("Running hourly market scanner...")
        try:
            from scanner import run_scanner, format_telegram_scanner_message
            json_data, accumulating_all = run_scanner()
            
            # Send Telegram alert for the hourly rotation
            if json_data:
                msg = format_telegram_scanner_message(json_data, accumulating_all)
                token = config.TELEGRAM_BOT_TOKEN
                chat_id = config.TELEGRAM_CHAT_ID
                if token and chat_id:
                    url = f"https://api.telegram.org/bot{token}/sendMessage"
                    payload = {
                        "chat_id": chat_id,
                        "text": msg,
                        "parse_mode": "Markdown"
                    }
                    resp = httpx.post(url, json=payload, timeout=15)
                    if resp.status_code == 200:
                        print("Scanner Telegram alert sent successfully.")
                    else:
                        print(f"Failed to send scanner Telegram alert: {resp.text}")
                else:
                    print("Telegram credentials not configured; scanner alert skipped.")
        except Exception as e:
            print(f"Error during hourly scan: {e}", file=sys.stderr)
            
    # 3. Ingest futures data (this now dynamically loads scanned symbols)
    try:
        ingest_coinalyze()
    except Exception as e:
        print(f"Error during CoinAnalyze ingestion: {e}", file=sys.stderr)
        
    # 3. Ingest options data
    try:
        ingest_deribit()
    except Exception as e:
        print(f"Error during Deribit ingestion: {e}", file=sys.stderr)
        
    # 4. Update daily ATM IV lookback table
    try:
        # Get write connection and save today's stats
        conn = config.get_db_connection(read_only=False)
        try:
            update_daily_summary(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"Error updating daily summary: {e}", file=sys.stderr)
        
    # 5. Prune database records older than 30 days
    try:
        conn = config.get_db_connection(read_only=False)
        try:
            prune_db(conn, retention_days=30)
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
        
    print(f"Pipeline run completed.")

def main():
    parser = argparse.ArgumentParser(description="BTC/ETH Options and Futures Research Ingestion Orchestrator")
    parser.add_argument("--once", action="store_true", help="Run the pipeline once and exit immediately.")
    args = parser.parse_args()
    
    if args.once:
        run_pipeline()
    else:
        global DAEMON_MODE
        DAEMON_MODE = True
        interval_secs = config.INGEST_INTERVAL_MINS * 60
        print(f"Starting orchestrator daemon. Loop interval: {config.INGEST_INTERVAL_MINS} minutes ({interval_secs}s)...")
        while True:
            start_time = time.time()
            try:
                run_pipeline()
            except Exception as e:
                print(f"Critical error in orchestrator pipeline: {e}", file=sys.stderr)
                
            # Calculate sleep time, taking execution time into account
            elapsed = time.time() - start_time
            sleep_time = max(1, interval_secs - elapsed)
            print(f"Sleeping for {sleep_time:.1f} seconds until next run...")
            time.sleep(sleep_time)

if __name__ == "__main__":
    main()
