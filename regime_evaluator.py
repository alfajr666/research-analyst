"""Daily wrapper for the existing HMM/VWAP regime evaluator and Telegram alerts."""

import sys
import time
from datetime import datetime, timezone

import config
from regime_signal import run_regime_signals


def run_once():
    conn = config.get_db_connection(read_only=False)
    try:
        last_date = conn.execute("SELECT MAX(date) FROM regime_signals").fetchone()[0]
        today = datetime.now(timezone.utc).date()
        if last_date is not None and last_date >= today:
            print(f"Regime signals already run today ({last_date}) - skipping.")
            return
        print("Running daily regime signals (HMM + dual VWAP)...")
        run_regime_signals(conn)
    finally:
        conn.close()


def main():
    while True:
        try:
            run_once()
        except Exception as error:
            print(f"Regime evaluator error: {error}", file=sys.stderr)
        time.sleep(60 * 60)


if __name__ == "__main__":
    main()
