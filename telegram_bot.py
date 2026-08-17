import logging
import sys
from datetime import time, datetime, timezone
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest
import config

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def is_authorized_sender(update: Update) -> bool:
    """Allow commands only from explicitly configured chats and users."""
    chat_id = str(update.effective_chat.id) if update.effective_chat else None
    user_id = str(update.effective_user.id) if update.effective_user else None
    chat_allowed = not config.TELEGRAM_ALLOWED_CHAT_IDS or chat_id in config.TELEGRAM_ALLOWED_CHAT_IDS
    user_allowed = not config.TELEGRAM_ALLOWED_USER_IDS or user_id in config.TELEGRAM_ALLOWED_USER_IDS
    return chat_allowed and user_allowed


def allowlisted(handler):
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not is_authorized_sender(update):
            logger.warning("Rejected Telegram command from chat=%s user=%s", update.effective_chat, update.effective_user)
            return
        await handler(update, context)
    return wrapped

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Greets the user and displays available commands."""
    welcome_text = (
        "📊 *BTC/ETH Options & Futures Research Analyst Bot*\n\n"
        "Available commands:\n"
        "/brief - Generate and send the latest comprehensive market brief\n"
        "/futures - Show futures metrics (OI change, funding, liquidations)\n"
        "/options - Show options metrics (ATM IV, IV Rank, skew, term structure)\n"
        "/profile - Show 7d volume & market profiles with POC, VA, and LVN levels\n"
        "/scanner - Show latest results of the hourly volume/OI scanner & alerts\n"
        "/regime [SYMBOL] - Show HMM + dual VWAP regime signal (default: BTC, ETH, SOL)\n\n"
        "Daily briefs are scheduled to send at *08:00 WITA*."
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def brief_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the latest comprehensive market brief with brain one-liner appended."""
    await update.message.reply_chat_action(action="typing")
    try:
        from analyze import generate_market_brief
        from brain import generate_brain_brief
        brief_text = generate_market_brief()
        try:
            brain_text = generate_brain_brief()
            brief_text = brief_text + brain_text
        except Exception as brain_err:
            logger.error(f"Brain generation failed (continuing with brief only): {brain_err}")
        await update.message.reply_text(brief_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error generating brief: {e}")
        await update.message.reply_text("❌ Error generating brief. Check logs.")

async def futures_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the futures-only report."""
    await update.message.reply_chat_action(action="typing")
    try:
        from analyze import get_futures_summary
        conn = config.get_db_connection(read_only=True)
        try:
            reports = []
            for currency in ["BTC", "ETH", "SOL"]:
                fut = get_futures_summary(conn, currency)
                if not fut:
                    continue
                
                # Format price values depending on price magnitude
                def fmt_p(val):
                    if val is None:
                        return "N/A"
                    if val < 1.0:
                        return f"${val:.6f}"
                    return f"${val:,.2f}" if val < 10000.0 else f"${round(val):,.0f}"

                price_str = fmt_p(fut['price'])
                range_str = f"{fmt_p(fut['low_24h'])} - {fmt_p(fut['high_24h'])}"
                
                reports.append(
                    f"📊 *{currency} Futures Context*\n"
                    f"• Price: {price_str} ({fut['price_change_24h']:+.2f}% 24h) | *24h Range:* {range_str}\n"
                    f"• Open Interest: ${fut['open_interest']/1e9:.2f}B ({fut['open_interest_change_24h']:+.2f}% 24h)\n"
                    f"• Funding: {fut['funding_rate']:.4f}% | Pred: {fut['predicted_funding']:.4f}%\n"
                    f"• 24h Liqs: Long ${fut['liq_long_24h']/1e6:.1f}M / Short ${fut['liq_short_24h']/1e6:.1f}M\n"
                    f"• L/S Ratio: {fut['long_short_ratio']:.2f}"
                )
            if not reports:
                await update.message.reply_text("❌ No futures data available.")
            else:
                await update.message.reply_text("\n\n".join(reports), parse_mode="Markdown")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error generating futures report: {e}")
        await update.message.reply_text("❌ Error fetching futures data.")

async def options_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the options-only report."""
    await update.message.reply_chat_action(action="typing")
    try:
        from analyze import get_options_summary
        conn = config.get_db_connection(read_only=True)
        try:
            reports = []
            for currency in ["BTC", "ETH", "SOL"]:
                opt = get_options_summary(conn, currency)
                if not opt:
                    continue
                ts_str = ", ".join([f"{k}: {v:.1f}%" for k, v in list(opt["term_structure"].items())[:3]])
                reports.append(
                    f"📈 *{currency} Options Context*\n"
                    f"• ATM IV: {opt['atm_iv']:.1f}% | IV Rank: {opt['iv_rank']:.1f}%\n"
                    f"• 25d Skew: {opt['skew_25d']:+.2f}%\n"
                    f"• Put/Call OI: {opt['put_call_ratio']:.2f}\n"
                    f"• Max Pain (Next Expiry): ${opt['max_pain']:,.0f}\n"
                    f"• Term Structure: {ts_str}"
                )
            if not reports:
                await update.message.reply_text("❌ No options data available.")
            else:
                await update.message.reply_text("\n\n".join(reports), parse_mode="Markdown")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error generating options report: {e}")
        await update.message.reply_text("❌ Error fetching options data.")

async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the volume and market profile report with ASCII charts."""
    await update.message.reply_chat_action(action="typing")
    
    # Check if specific symbols were requested as arguments
    requested_symbols = []
    if context.args:
        for arg in context.args:
            requested_symbols.append(arg.upper())
    else:
        requested_symbols = ["BTC", "ETH", "SOL"]
        
    try:
        from analyze import get_profile_summary
        conn = config.get_db_connection(read_only=True)
        try:
            reports = []
            for currency in requested_symbols:
                prof = get_profile_summary(conn, currency, lookback_days=7)
                if not prof:
                    if context.args:
                        reports.append(f"❌ *{currency}*: No profile data found in database. Make sure it's ingested and has active trading data.")
                    continue
                    
                if prof.get("status") == "Insufficient data":
                    reports.append(
                        f"❌ *{currency} 7d Profile Context*\n"
                        f"_Status: Insufficient historical data ({prof.get('candles_count')}/{prof.get('required_candles')} candles required)._\n"
                        f"Please wait for the background daemon to ingest more data."
                    )
                    continue
                
                # Format price values depending on price magnitude
                def fmt_price(val):
                    if val is None:
                        return "N/A"
                    if val < 1.0:
                        return f"${val:.6f}"
                    return f"${val:,.2f}" if val < 10000.0 else f"${round(val):,.0f}"

                vol_poc_str = fmt_price(prof['volume_poc'])
                tpo_poc_str = fmt_price(prof['tpo_poc'])
                
                vol_val_str = fmt_price(prof.get('val'))
                vol_vah_str = fmt_price(prof.get('vah'))
                tpo_val_str = fmt_price(prof.get('tpo_val'))
                tpo_vah_str = fmt_price(prof.get('tpo_vah'))
                
                # High Volume Nodes (including primary POC + secondary HVNs)
                hvns = prof.get("hvns", [])
                hvn_list = [prof['volume_poc']] + hvns
                hvns_str = ", ".join([fmt_price(x) for x in hvn_list[:3]])
                
                # Low Volume Nodes
                lvns = prof.get("lvns", [])
                if lvns:
                    lvns_str = ", ".join([fmt_price(x) for x in lvns])
                else:
                    lvns_str = "N/A (Requires more history to identify valleys)"
                
                reports.append(
                    f"📊 *{currency} 7d Profile Context*\n"
                    f"_Type: 7-Day Rolling Profile (Floating anchor, recalculated from rolling lookback, not fixed sessions)_\n\n"
                    f"*📈 Volume Profile Levels:*\n"
                    f"• *Point of Control (POC):* {vol_poc_str}\n"
                    f"• *Value Area High (VAH):* {vol_vah_str}\n"
                    f"• *Value Area Low (VAL):* {vol_val_str}\n"
                    f"• *High Volume Nodes (HVNs):* {hvns_str}\n"
                    f"• *Low Volume Nodes (LVNs):* {lvns_str}\n\n"
                    f"*🕒 TPO (Time) Profile Levels:*\n"
                    f"• *TPO POC:* {tpo_poc_str}\n"
                    f"• *TPO VAH:* {tpo_vah_str}\n"
                    f"• *TPO VAL:* {tpo_val_str}\n\n"
                    f"*📐 Profile Shape & Technical Confluence:*\n"
                    f"• *EMA26:* {fmt_price(prof.get('ema26'))} | *EMA99:* {fmt_price(prof.get('ema99'))} | *VWAP:* {fmt_price(prof.get('vwap'))}\n"
                    f"• *Profile Shape:* *{prof.get('profile_shape', 'N/A')}*\n"
                    f"  _{prof.get('profile_shape_desc', '')}_\n"
                    f"• *TA Confluence:* *{prof.get('ta_signal', 'Neutral')}*\n"
                    f"  _{prof.get('ta_desc', '')}_"
                )
            if not reports:
                await update.message.reply_text("❌ No profile data available.")
            else:
                combined_report = "\n\n".join(reports)
                if len(combined_report) > 4000:
                    for rep in reports:
                        await update.message.reply_text(rep, parse_mode="Markdown")
                else:
                    await update.message.reply_text(combined_report, parse_mode="Markdown")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error generating profile report: {e}")
        await update.message.reply_text("❌ Error fetching profile data.")

async def scanner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sends the latest results of the hourly volume/OI scanner."""
    await update.message.reply_chat_action(action="typing")
    try:
        import json
        json_path = config.DEFAULT_DB_DIR / "scanned_pairs.json"
        if not json_path.exists():
            await update.message.reply_text("⏳ Scanner has not run yet. Data will populate on the next hourly cycle.")
            return
            
        with open(json_path, "r") as f:
            json_data = json.load(f)
            
        from scanner import format_telegram_scanner_message
        accumulating_all = json_data.get("accumulation_alerts", [])
        msg = format_telegram_scanner_message(json_data, accumulating_all)
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error in scanner command: {e}")
        await update.message.reply_text("❌ Error reading scanner data.")

async def regime_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows the latest HMM + dual VWAP regime signal for requested symbols."""
    await update.message.reply_chat_action(action="typing")

    # Parse optional symbol argument(s)
    if context.args:
        requested = [a.upper() for a in context.args]
    else:
        requested = ["BTC", "ETH", "SOL"]

    def fmt_p(v):
        if v is None:
            return "N/A"
        v = float(v)
        if v < 1.0:    return f"${v:.6f}"
        if v < 10000:  return f"${v:,.2f}"
        return f"${round(v):,.0f}"

    try:
        conn = config.get_db_connection(read_only=True)
        try:
            reports = []
            for sym in requested:
                row = conn.execute("""
                    SELECT date, signal, no_signal_reason, conviction, conviction_score,
                           regime, regime_conf, weekly_vwap, monthly_vwap,
                           ema12, ema25, ema_aligned, acceptance, close_price,
                           sl, tp1, tp2
                    FROM regime_signals
                    WHERE underlying = ?
                    ORDER BY date DESC LIMIT 1
                """, (sym,)).fetchone()

                if not row:
                    reports.append(f"❌ *{sym}*: No regime signal data yet. Run the pipeline first.")
                    continue

                (sig_date, signal, reason, conviction, score,
                 regime, regime_conf, w_vwap, m_vwap,
                 ema12, ema25, ema_aligned, acceptance, close,
                 sl, tp1, tp2) = row

                if signal == "no_signal":
                    reports.append(
                        f"📡 *Regime Signal — #{sym}*\n"
                        f"• Date: {sig_date}\n"
                        f"• Signal: ⏸ NO SIGNAL\n"
                        f"• Reason: `{reason}`\n"
                        f"• Close: {fmt_p(close)}\n"
                        f"• Weekly VWAP: {fmt_p(w_vwap)} | Monthly VWAP: {fmt_p(m_vwap)}"
                    )
                else:
                    direction = "LONG 🟢" if signal == "long" else "SHORT 🔴"
                    icon = {"HIGH": "🔥", "MODERATE": "✅", "LOW": "⚠️"}.get(conviction, "")
                    ema_ok = "✅" if ema_aligned else "❌"
                    regime_str = f"{regime} ({(regime_conf or 0)*100:.0f}%)" if regime else "unknown"
                    reports.append(
                        f"📡 *Regime Signal — #{sym}*\n"
                        f"• Date: {sig_date}\n"
                        f"• Signal: {direction}\n"
                        f"• Conviction: {icon} {conviction} (score: {score}/6)\n"
                        f"• Close: {fmt_p(close)}\n\n"
                        f"🎯 *Levels:*\n"
                        f"  ▫️ *Entry:* {fmt_p(close)} (Market)\n"
                        f"  ▫️ *Stop Loss:* {fmt_p(sl)}\n"
                        f"  ▫️ *Target 1 (1.5R):* {fmt_p(tp1)}\n"
                        f"  ▫️ *Target 2 (3.0R):* {fmt_p(tp2)}\n\n"
                        f"*Setup:*\n"
                        f"  ▫️ Weekly VWAP: {fmt_p(w_vwap)} — price {'above' if signal == 'long' else 'below'} ✅\n"
                        f"  ▫️ Monthly VWAP: {fmt_p(m_vwap)} ✅\n"
                        f"  ▫️ Acceptance: {acceptance}/{5} closes ✅\n\n"
                        f"*Confluences:*\n"
                        f"  ▫️ Regime: {regime_str}\n"
                        f"  ▫️ EMA12/25: {fmt_p(ema12)} / {fmt_p(ema25)} {ema_ok}"
                    )

            if not reports:
                await update.message.reply_text("❌ No data found.")
            else:
                for rep in reports:
                    await update.message.reply_text(rep, parse_mode="Markdown")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Error in regime command: {e}")
        await update.message.reply_text("❌ Error fetching regime signal data.")

async def scheduled_brief_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job callback to send the daily brief to the configured channel/chat."""
    chat_id = config.TELEGRAM_CHAT_ID
    if not chat_id:
        logger.warning("TELEGRAM_CHAT_ID not configured. Scheduled brief skipped.")
        return
        
    logger.info("Triggering scheduled daily brief...")
    try:
        from analyze import generate_market_brief
        from brain import generate_brain_brief
        brief_text = generate_market_brief()
        try:
            brain_text = generate_brain_brief()
            brief_text = brief_text + brain_text
        except Exception as brain_err:
            logger.error(f"Brain generation failed (continuing with brief only): {brain_err}")
        await context.bot.send_message(chat_id=chat_id, text=brief_text, parse_mode="Markdown")
        logger.info("Scheduled brief sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send scheduled brief: {e}")

def main() -> None:
    """Runs the Telegram Bot listener and schedules daily briefs."""
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN is not configured in .env. Bot cannot start.", file=sys.stderr)
        sys.exit(1)
    try:
        config.secure_secret_file()
        config.validate_telegram_allowlist()
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
        
    custom_request = HTTPXRequest(
        connection_pool_size=5,
        pool_timeout=10.0,
        read_timeout=20.0,
        write_timeout=20.0,
        connect_timeout=10.0
    )
    app = (
        ApplicationBuilder()
        .token(token)
        .request(custom_request)
        .concurrent_updates(1)
        .build()
    )

    # Register commands
    app.add_handler(CommandHandler("start", allowlisted(start)))
    app.add_handler(CommandHandler("brief", allowlisted(brief_command)))
    app.add_handler(CommandHandler("futures", allowlisted(futures_command)))
    app.add_handler(CommandHandler("options", allowlisted(options_command)))
    app.add_handler(CommandHandler("profile", allowlisted(profile_command)))
    app.add_handler(CommandHandler("scanner", allowlisted(scanner_command)))
    app.add_handler(CommandHandler("regime", allowlisted(regime_command)))

    # Schedule the daily brief in Asia/Makassar timezone (WITA)
    tz = pytz.timezone("Asia/Makassar")
    try:
        hour, minute = map(int, config.DAILY_BRIEF_TIME_WITA.split(":"))
        brief_time = time(hour=hour, minute=minute, tzinfo=tz)
        
        job_queue = app.job_queue
        # Scheduled to run daily at the specified time
        job_queue.run_daily(scheduled_brief_job, time=brief_time)
        print(f"Scheduled daily brief at {config.DAILY_BRIEF_TIME_WITA} WITA (Asia/Makassar).")
    except Exception as e:
        print(f"Error scheduling daily brief: {e}. Scheduled brief not active.", file=sys.stderr)

    # Start the bot
    print("Starting Telegram Bot listener...")
    app.run_polling()

if __name__ == "__main__":
    main()
