import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

import config
from strategies.v2.accumulation_base_v2 import AccV2Config, STRATEGY_ID, evaluate, evaluate_symbol
from strategy_v2_context import has_active_event, resolve_bias


def _bars_15m(
    n: int,
    *,
    base_close: float = 100.0,
    start: datetime | None = None,
    range_pct: float = 0.002,
    trend: float = 0.0,
    near_ema_noise: float = 0.0,
) -> pl.DataFrame:
    """Synthetic 15m series with mild noise (enough history for 1h EMA99 + 4h EMA48)."""
    start = start or datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    price = base_close
    for i in range(n):
        ts = start + timedelta(minutes=15 * i)
        # slow drift then flat coil
        if i < n - 80:
            price = base_close * (1.0 + trend * (i / max(n, 1)))
        else:
            price = base_close * (1.0 + near_ema_noise)
        half = price * range_pct
        o = price - half * 0.2
        c = price + half * 0.2
        h = price + half
        lo = price - half
        rows.append(
            {
                "timestamp": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": 1000.0 + (i % 7) * 10,
                "open_interest": 1_000_000.0,
                "funding_rate": 0.0001,
            }
        )
    return pl.DataFrame(rows, strict=False)


class AccumulationBaseV2Tests(unittest.TestCase):
    def test_resolve_bias_agree_or_abstain(self):
        self.assertEqual(resolve_bias("long", "missing"), "long")
        self.assertEqual(resolve_bias("missing", "short"), "short")
        self.assertEqual(resolve_bias("long", "long"), "long")
        self.assertIsNone(resolve_bias("long", "short"))
        self.assertIsNone(resolve_bias("missing", "missing"))

    def test_emits_limit_at_ema_with_uncalibrated_confidence(self):
        # Slow grind higher so 4h close > EMA48, then coil near last close.
        start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
        n = 14 * 24 * 4
        rows = []
        price = 90.0
        for i in range(n):
            ts = start + timedelta(minutes=15 * i)
            if i < n - 100:
                price = 90.0 + 15.0 * (i / (n - 100))
                half = price * 0.003
            else:
                price = 105.0 + 0.02 * ((i % 5) - 2)
                half = price * 0.001
            rows.append(
                {
                    "timestamp": ts,
                    "open": price - half * 0.2,
                    "high": price + half,
                    "low": price - half,
                    "close": price + half * 0.1,
                    "volume": 1000.0,
                    "open_interest": 1e6,
                    "funding_rate": 0.0001,
                }
            )
        bars = pl.DataFrame(rows, strict=False)
        cutoff = bars["timestamp"][-1] + timedelta(minutes=15)
        if hasattr(cutoff, "to_pydatetime"):
            cutoff = cutoff.to_pydatetime()
        # Structure alone (no zone) → long from EMA48_4h
        cfg = AccV2Config(n=12, k=8.0, d_max=8.0, r_max=20.0, s_min=0.0, g=2.0)
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=[],
            cfg=cfg,
        )
        self.assertIsNotNone(event, "expected a gated candidate")
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["setup_class"], "accumulation_base")
        self.assertEqual(event["phase"], "armed_compression_pullback")
        self.assertEqual(event["entry_condition"]["type"], "limit_at_ema_context")
        self.assertEqual(event["confidence_status"], "uncalibrated")
        self.assertEqual(len(event["targets"]), 1)
        self.assertEqual(event["horizon_minutes"], 240)
        self.assertIn("confluence_score", event["feature_snapshot"])
        self.assertIn("ema99_1h", event["feature_snapshot"])

    def test_hard_fail_without_resolved_bias(self):
        bars = _bars_15m(14 * 24 * 4, base_close=100.0)
        cutoff = bars["timestamp"][-1] + timedelta(minutes=15)
        if hasattr(cutoff, "to_pydatetime"):
            cutoff = cutoff.to_pydatetime()
        # No zones and flat enough that structure may still form; use empty zones
        # and bars that won't create clear EMA48 separation — force missing via short series on 4h
        short = bars.tail(48)  # not enough for EMA48_4h
        event = evaluate_symbol(
            short,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=[],
            cfg=AccV2Config(n=8, k=50.0, d_max=50.0),
        )
        self.assertIsNone(event)

    def test_stretched_from_ema_fails_d_max(self):
        bars = _bars_15m(14 * 24 * 4, base_close=100.0, trend=0.05)
        # Spike last bars far from mean
        closes = bars["close"].to_list()
        closes[-1] = closes[-1] * 1.2
        bars = bars.with_columns(pl.Series("close", closes))
        cutoff = bars["timestamp"][-1] + timedelta(minutes=15)
        if hasattr(cutoff, "to_pydatetime"):
            cutoff = cutoff.to_pydatetime()
        zones = [
            {
                "type": "fvg",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "active",
                "low": 90.0,
                "high": 95.0,
            }
        ]
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=zones,
            cfg=AccV2Config(n=12, k=50.0, d_max=0.05, r_max=50.0, g=5.0),
        )
        self.assertIsNone(event)

    def test_rearm_blocks_second_active(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        outbox = Path(directory.name) / "outbox"
        outbox.mkdir()
        now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        payload = {
            "strategy_id": STRATEGY_ID,
            "asset": "SOL",
            "direction": "long",
            "valid_until": (now + timedelta(hours=2)).isoformat(),
        }
        (outbox / "live.json").write_text(json.dumps(payload), encoding="utf-8")
        self.assertTrue(
            has_active_event(STRATEGY_ID, "SOL", "long", outbox_dir=outbox, now=now)
        )
        self.assertFalse(
            has_active_event(STRATEGY_ID, "SOL", "short", outbox_dir=outbox, now=now)
        )

    def test_db_evaluate_top_n_and_identity(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db = Path(directory.name) / "m.db"
        config.init_market_db(db)
        cutoff = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        start = cutoff - timedelta(days=14)
        conn = config.get_db_connection(db_path=db)
        try:
            price = 100.0
            ts = start
            idx = 0
            while ts < cutoff:
                half = price * 0.001
                payload = json.dumps(
                    {
                        "open": price - half * 0.1,
                        "high": price + half,
                        "low": price - half,
                        "close": price + half * 0.1,
                        "volume": 1000.0,
                        "open_interest": 1e6,
                        "funding_rate": 0.0001,
                    }
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO source_observations
                    (observation_id, source, venue, native_symbol, asset, market_kind, interval,
                     source_start, source_end, retrieved_at, retrieval_kind, payload_json)
                    VALUES (?, 'coinalyze', 'agg', 'SOLUSDT_PERP.A', 'SOL', 'perpetual', '15m', ?, ?, ?, 'live', ?)
                    """,
                    (f"sol-{idx}", ts, ts, ts, payload),
                )
                # gentle uptrend early so 4h close > EMA48
                if idx < 500:
                    price *= 1.0003
                ts += timedelta(minutes=15)
                idx += 1
            conn.commit()
            cfg = AccV2Config(n=12, k=20.0, d_max=10.0, r_max=20.0, s_min=0.0, n_top=3, g=5.0)
            zones = [
                {
                    "asset": "SOL",
                    "type": "fvg",
                    "timeframe": "4h",
                    "direction": "bullish",
                    "state": "active",
                    "low": price * 0.98,
                    "high": price * 1.02,
                }
            ]
            events = evaluate(
                conn,
                cutoff,
                cfg=cfg,
                snapshot={"zones": zones},
                outbox_dir=Path(directory.name) / "empty_outbox",
            )
            for ev in events:
                self.assertEqual(ev["strategy_id"], STRATEGY_ID)
                self.assertEqual(ev["plugin_version"], "v2")
                self.assertEqual(ev["confidence_status"], "uncalibrated")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
