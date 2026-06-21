#!/usr/bin/env python3
"""Accumulation Monitor — dual-source accumulation detection + Telegram alerts.

Has two data sources:
  1) DuckDB (zero-API): reads 15-min candles from futures_data, aggregates
     into 1-hour windows, runs detection (volume spike >= 1.5x, |price| <= 3%).
  2) Scanner feed: reads data/scanner_pending_accums.json written by the hourly
     scanner (scanner.py) to detect accumulation on symbols that lack 25+ hours
     of DuckDB history (e.g. freshly discovered altcoins).

When a symbol newly enters accumulation from either source, it sends a
dedicated Telegram alert. State is tracked in data/accumulation_state.json
with a "source" field ("db" vs "scanner") for independent dedup and staleness.

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
                    meta_for_alert = {
                        "vol_spike": meta["vol_spike"],
                        "price_change_1h": meta["price_change_1h"],
                        "vol_7d_usd": meta["vol_7d_usd"],
                        "oi_usd": meta["oi_usd"],
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
                        print(f"  Scanner-fed alert sent for {sym}")
                    else:
                        remaining[sym] = meta
                if remaining:
                    with open(pending_path, "w") as f:
                        json.dump({"scanner_timestamp": pending["scanner_timestamp"], "symbols": remaining}, f, indent=2)
                else:
                    pending_path.unlink(missing_ok=True)
            except Exception as e:
                print(f"  Error processing scanner pending accums: {e}")

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
