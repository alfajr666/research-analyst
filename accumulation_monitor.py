#!/usr/bin/env python3
"""Accumulation Monitor — zero-API accumulation detection from DuckDB.

Reads 15-min candles already stored in futures_data by the ingestion pipeline,
aggregates them into 1-hour windows, runs the same detection logic as scanner.py
(volume spike >= 1.5x, |price change| <= 3%), and sends Telegram alerts
immediately on new detection.

Runs via PM2 cron (every 15 min). Zero new API calls — all data from local DB.
"""

import json
import os
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import httpx

import config

STATE_FILE: Path = config.DEFAULT_DB_DIR / "accumulation_state.json"
VOL_THRESHOLD: float = float(os.getenv("VOLUME_SPIKE_THRESHOLD", "1.5"))
PRICE_THRESHOLD: float = float(os.getenv("PRICE_SILENT_THRESHOLD", "3.0"))
LOOKBACK_HOURS: int = 30


def get_hourly_buckets(conn, symbol: str) -> list[dict]:
    """Fetch 15-min candles and aggregate into 1-hour buckets in Python.

    Returns a list of dicts ordered oldest→newest, one per hour bucket
    that has at least one data point.
    """
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
    """Run accumulation detection on hourly buckets.

    Returns a metadata dict if accumulating, None otherwise.
    """
    if len(hourly) < 25:
        return None

    prev_volumes = [h["volume"] for h in hourly[-25:-1]]
    avg_vol = sum(prev_volumes) / len(prev_volumes)

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
    """Send an immediate Telegram alert for a newly detected accumulation."""
    vol_spike = meta["vol_spike"]
    price_change = meta["price_change_1h"]
    vol_7d = meta.get("vol_7d_usd", 0.0)
    oi_usd = meta.get("oi_usd", 0.0)
    clean_sym = symbol.split("_")[0]

    msg = (
        f"\U0001F6A8 *ACCUMULATION DETECTED* \U0001F6A8\n"
        f"\U0001F4C5 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n\n"
        f"\U0001F538 *#{underlying}* ({clean_sym})\n"
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

        current_accumulating: set[str] = set()
        new_accumulations: list[tuple[str, str, dict]] = []

        for sym_row in symbols:
            symbol: str = sym_row[0]
            underlying: str = sym_row[1]

            hourly = get_hourly_buckets(conn, symbol)
            meta = check_accumulation(hourly)

            if meta is None:
                continue

            current_accumulating.add(symbol)

            if symbol in alerted:
                continue

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

        stale = [sym for sym in alerted if sym not in current_accumulating]
        for sym in stale:
            print(f"  {sym} no longer accumulating — removed from state")
            del alerted[sym]

        state["last_check"] = now_iso
        state["alerted"] = alerted
        save_state(state)

        print(
            f"  Accumulating: {len(current_accumulating)} | "
            f"New alerts: {len(new_accumulations)} | "
            f"Cleared: {len(stale)}"
        )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
