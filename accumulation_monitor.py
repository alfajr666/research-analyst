#!/usr/bin/env python3
"""Accumulation Monitor — dual-source confluence detection + Telegram alerts.

Has two data sources:
  1) DuckDB (zero-API): reads 15-min candles from futures_data, aggregates
     into 1-hour windows, runs detection (volume spike >= 1.5x, |price| <= 3%).
  2) Scanner feed: reads data/scanner_pending_accums.json written by the hourly
     scanner (scanner.py).

For symbols identified as accumulating from either source, it runs the confluence pipeline:
  - 15m EMA 99 Pullback (within 1% threshold).
  - 15m Green/Red candle execution trigger.

When a confluence newly triggers, it sends a Telegram alert with the Entry Zone.
State is tracked in data/accumulation_state.json.
Runs as a continuous PM2 daemon, checking every 15 minutes.
"""

import json
import os
import statistics
import sys
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx
import polars as pl

import config

STATE_FILE: Path = config.DEFAULT_DB_DIR / "accumulation_state.json"
VOL_THRESHOLD: float = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
PRICE_THRESHOLD: float = float(os.getenv("PRICE_SILENT_THRESHOLD", "3.0"))
LOOKBACK_HOURS: int = 30


def get_hourly_buckets(conn, symbol: str) -> list[dict]:
    """Fetch 15-min candles and aggregate into 1-hour buckets in Python."""
    rows = conn.execute("""
        SELECT timestamp, volume, close, open_interest
        FROM futures_data
        WHERE symbol = ?
          AND timestamp >= NOW() - INTERVAL '30 hours'
        ORDER BY timestamp ASC
    """, [symbol]).fetchall()

    if not rows:
        return []

    buckets: OrderedDict[str, dict] = OrderedDict()

    for row in rows:
        ts = row[0]
        vol = float(row[1] or 0.0)
        close = float(row[2] or 0.0)
        oi_raw = float(row[3] or 0.0)

        hour_key = ts.replace(minute=0, second=0, microsecond=0)

        if hour_key not in buckets:
            buckets[hour_key] = {
                "hour": hour_key,
                "volume": 0.0,
                "close": 0.0,
                "oi_raw": 0.0,
            }

        buckets[hour_key]["volume"] += vol
        buckets[hour_key]["close"] = close
        buckets[hour_key]["oi_raw"] = oi_raw

    return list(buckets.values())


def check_accumulation(hourly: list[dict]) -> dict | None:
    """Run accumulation detection on hourly buckets."""
    if len(hourly) < 25:
        return None

    prev_volumes = [h["volume"] for h in hourly[-25:-1]]
    avg_vol = statistics.median(prev_volumes)

    if avg_vol <= 0.0:
        return None

    current = hourly[-1]
    previous = hourly[-2]

    vol_spike = current["volume"] / avg_vol
    price_change_1h = (
        ((current["close"] - previous["close"]) / previous["close"]) * 100.0
        if previous["close"] > 0.0 else 0.0
    )

    is_acc = vol_spike >= VOL_THRESHOLD and abs(price_change_1h) <= PRICE_THRESHOLD

    if not is_acc:
        return None

    return {
        "vol_spike": round(vol_spike, 2),
        "price_change_1h": round(price_change_1h, 2),
        "hour_volume": round(current["volume"], 2),
        "close_price": current["close"],
        "oi_raw": current["oi_raw"],
    }


def get_15m_ema_99(conn, symbol: str) -> tuple[float, float, str] | None:
    """Fetch 15-min candles and calculate EMA 99 using Polars.

    Returns (latest_close, latest_ema, trend) or None.
    """
    rows = conn.execute("""
        SELECT timestamp, close
        FROM futures_data
        WHERE symbol = ?
          AND timestamp >= NOW() - INTERVAL '7 days'
        ORDER BY timestamp ASC
    """, [symbol]).fetchall()

    if len(rows) < 100:  # Require enough 15m candles for EMA 99
        return None

    # Create Polars DataFrame directly from 15m candles
    df = pl.DataFrame({
        "timestamp": [r[0] for r in rows],
        "close": [float(r[1] or 0.0) for r in rows]
    })

    df = df.with_columns(
        pl.col("close").ewm_mean(span=99, adjust=False).alias("ema_99")
    )

    latest = df.tail(1).to_dicts()[0]
    latest_close = latest["close"]
    latest_ema = latest["ema_99"]

    trend = "long" if latest_close > latest_ema else "short"
    return latest_close, latest_ema, trend


def load_state() -> dict:
    """Load accumulation state from JSON file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_check": None, "alerted": {}}


def save_state(state: dict):
    """Save accumulation state to JSON file."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_alert(symbol: str, underlying: str, meta: dict) -> bool:
    """Send an immediate Telegram alert for a newly detected confluence."""
    vol_spike = meta["vol_spike"]
    price_change = meta["price_change_1h"]
    vol_7d = meta.get("vol_7d_usd", 0.0)
    oi_usd = meta.get("oi_usd", 0.0)
    clean_sym = symbol.split("_")[0]

    trend = meta["trend"]
    ema_val = meta["ema_val"]
    ema_dist_pct = meta["ema_dist"] * 100.0

    if trend == "long":
        zone_min = ema_val
        zone_max = ema_val * 1.01
        emoji = "🔸"
        setup_type = "15m EMA 99 Pullback + Accumulation"
        title = "🎯 *HOLY GRAIL LONG DETECTED* 🎯"
    else:
        zone_min = ema_val * 0.99
        zone_max = ema_val
        emoji = "🔻"
        setup_type = "15m EMA 99 Pullback + Distribution"
        title = "🎯 *HOLY GRAIL SHORT DETECTED* 🎯"

    def format_price(p):
        if p >= 1000:
            return f"${p:,.0f}"
        elif p >= 1:
            return f"${p:,.2f}"
        else:
            return f"${p:,.6f}"

    entry_zone_str = f"{format_price(zone_min)} - {format_price(zone_max)}"

    msg = (
        f"{title}\n"
        f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        f"{emoji} *#{underlying}* ({clean_sym})\n"
        f"   \u2022 Setup: *{setup_type}*\n"
        f"   \u2022 Entry Zone: *{entry_zone_str}* (Dist: {ema_dist_pct:.2f}%)\n"
        f"   \u2022 Vol Spike: *{vol_spike:.2f}x* | 1h Price: *{price_change:+.2f}%*\n"
        f"   \u2022 7D Vol: ${vol_7d / 1e6:.1f}M | OI: ${oi_usd / 1e6:.1f}M"
    )

    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("  Telegram credentials not configured; alert skipped.")
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
        resp = httpx.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            print(f"  Alert sent for {symbol}")
            return True
        else:
            print(f"  Failed to send alert: {resp.text}")
            return False
    except Exception as e:
        print(f"  Error sending alert: {e}")
        return False


def main():
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts} UTC] Accumulation monitor check...")

    conn = config.get_db_connection(read_only=True)
    try:
        symbols = conn.execute("""
            SELECT DISTINCT f.symbol, f.underlying
            FROM futures_data f
            WHERE f.timestamp >= NOW() - INTERVAL '28 hours'
        """).fetchall()

        if not symbols:
            print("  No symbols with recent data. Skipping.")
            return

        print(f"  Checking {len(symbols)} symbols...")

        state = load_state()
        alerted: dict = state.get("alerted", {})
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        pending_path: Path = config.DEFAULT_DB_DIR / "scanner_pending_accums.json"

        # ── Process scanner-fed pending accumulations (bypass 25-hour DB req) ──
        scanner_active_set: set[str] = set()
        if pending_path.exists():
            try:
                with open(pending_path) as f:
                    pending = json.load(f)
                scanner_syms = pending.get("symbols", {})
                scanner_active_set = set(scanner_syms.keys())
                remaining: dict = {}
                for sym, meta in scanner_syms.items():
                    if sym in alerted:
                        continue

                    # Confluence check for scanner-fed symbol
                    ema_res = get_15m_ema_99(conn, sym)
                    if not ema_res:
                        remaining[sym] = meta
                        continue

                    latest_close, latest_ema, trend = ema_res

                    latest_15m = conn.execute("""
                        SELECT open, close
                        FROM futures_data
                        WHERE symbol = ?
                        ORDER BY timestamp DESC
                        LIMIT 1
                    """, [sym]).fetchone()

                    if not latest_15m:
                        remaining[sym] = meta
                        continue

                    open_15m = float(latest_15m[0] or 0.0)
                    close_15m = float(latest_15m[1] or 0.0)

                    is_pullback = False
                    ema_dist = 0.0

                    if trend == "long":
                        ema_dist = (latest_close - latest_ema) / latest_ema
                        if 0.0 <= ema_dist <= 0.01:
                            is_pullback = True
                    elif trend == "short":
                        ema_dist = (latest_ema - latest_close) / latest_ema
                        if 0.0 <= ema_dist <= 0.01:
                            is_pullback = True

                    if not is_pullback:
                        remaining[sym] = meta
                        continue

                    # Check 15m execution trigger
                    is_triggered = (trend == "long" and close_15m > open_15m) or (trend == "short" and close_15m < open_15m)
                    if not is_triggered:
                        remaining[sym] = meta
                        continue

                    meta_for_alert = {
                        "vol_spike": meta["vol_spike"],
                        "price_change_1h": meta["price_change_1h"],
                        "vol_7d_usd": meta["vol_7d_usd"],
                        "oi_usd": meta["oi_usd"],
                        "trend": trend,
                        "ema_val": latest_ema,
                        "ema_dist": ema_dist,
                    }
                    underlying = meta["underlying"]
                    ok = send_alert(sym, underlying, meta_for_alert)
                    if ok:
                        alerted[sym] = {
                            "first_detected": now_iso,
                            "last_alerted": now_iso,
                            "vol_spike": meta["vol_spike"],
                            "price_change_1h": meta["price_change_1h"],
                            "underlying": underlying,
                            "source": "scanner",
                        }
                        print(f"  Scanner-fed confluence alert sent for {sym}")
                    else:
                        remaining[sym] = meta
                if remaining:
                    with open(pending_path, "w") as f:
                        json.dump({"scanner_timestamp": pending["scanner_timestamp"], "symbols": remaining}, f, indent=2)
                else:
                    pending_path.unlink(missing_ok=True)
            except Exception as e:
                print(f"  Error processing scanner pending accums: {e}")

        # ── Process DuckDB-sourced accumulations ──
        current_accumulating: set[str] = set()
        new_accumulations: list[tuple[str, str, dict]] = []

        for sym_row in symbols:
            symbol: str = sym_row[0]
            underlying: str = sym_row[1]

            # Gate 1: Check 1h Accumulation (Fast)
            hourly = get_hourly_buckets(conn, symbol)
            meta = check_accumulation(hourly)

            if meta is None:
                continue

            # Gate 2: Check 15m EMA 99 Pullback (Heavy, runs only on accumulating symbols)
            ema_res = get_15m_ema_99(conn, symbol)
            if not ema_res:
                continue

            latest_close, latest_ema, trend = ema_res

            # Gate 3: Check 15m Execution Trigger
            latest_15m = conn.execute("""
                SELECT open, close
                FROM futures_data
                WHERE symbol = ?
                ORDER BY timestamp DESC
                LIMIT 1
            """, [symbol]).fetchone()

            if not latest_15m:
                continue

            open_15m = float(latest_15m[0] or 0.0)
            close_15m = float(latest_15m[1] or 0.0)

            # Check Confluence conditions
            is_pullback = False
            ema_dist = 0.0

            if trend == "long":
                ema_dist = (latest_close - latest_ema) / latest_ema
                if 0.0 <= ema_dist <= 0.01:
                    is_pullback = True
            elif trend == "short":
                ema_dist = (latest_ema - latest_close) / latest_ema
                if 0.0 <= ema_dist <= 0.01:
                    is_pullback = True

            # If it is in pullback, it is considered actively in our setup zone
            if not is_pullback:
                continue

            current_accumulating.add(symbol)

            if symbol in alerted:
                continue

            # Check the 15m trigger to actually send the alert
            is_triggered = (trend == "long" and close_15m > open_15m) or (trend == "short" and close_15m < open_15m)
            if not is_triggered:
                continue

            # Add confluence details to meta
            meta["trend"] = trend
            meta["ema_val"] = latest_ema
            meta["ema_dist"] = ema_dist

            stats = conn.execute("""
                SELECT
                    COALESCE(SUM(volume * close), 0.0) AS vol_7d_usd,
                    COALESCE(AVG(open_interest * close), 0.0) AS oi_usd
                FROM futures_data
                WHERE symbol = ?
                  AND timestamp >= NOW() - INTERVAL '7 days'
            """, [symbol]).fetchone()

            meta["vol_7d_usd"] = float(stats[0])
            meta["oi_usd"] = float(stats[1])
            meta["underlying"] = underlying
            meta["symbol"] = symbol

            new_accumulations.append((symbol, underlying, meta))

        for symbol, underlying, meta in new_accumulations:
            ok = send_alert(symbol, underlying, meta)
            if ok:
                alerted[symbol] = {
                    "first_detected": now_iso,
                    "last_alerted": now_iso,
                    "vol_spike": meta["vol_spike"],
                    "price_change_1h": meta["price_change_1h"],
                    "underlying": underlying,
                }

        # ── Stale cleanup: DB-sourced symbols ──
        stale = [
            sym for sym in alerted
            if sym not in current_accumulating
            and alerted[sym].get("source") != "scanner"
        ]
        for sym in stale:
            print(f"  {sym} no longer accumulating — removed from state")
            del alerted[sym]

        # ── Stale cleanup: scanner-sourced symbols ──
        stale_scanner = [
            sym for sym in alerted
            if alerted[sym].get("source") == "scanner"
            and sym not in scanner_active_set
        ]
        for sym in stale_scanner:
            print(f"  {sym} no longer in scanner accumulations — removed from state")
            del alerted[sym]

        state["last_check"] = now_iso
        state["alerted"] = alerted
        save_state(state)

        print(
            f"  Accumulating: {len(current_accumulating)} | "
            f"New alerts: {len(new_accumulations)} | "
            f"DB cleared: {len(stale)} | "
            f"Scanner cleared: {len(stale_scanner)}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    while True:
        main()
        time.sleep(900)
