import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from continuation_breakout_v2 import ContV2Config, STRATEGY_ID, evaluate_symbol
from strategy_v2_context import has_active_event


def _trend_then_flag(
    *,
    days: int = 16,
    base: float = 100.0,
    breach: bool = False,
    deep_retrace: bool = False,
    parabolic: bool = False,
) -> tuple[pl.DataFrame, datetime]:
    """Uptrend on 4h scale, then tight 1h flag under the high."""
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    n = days * 24 * 4
    rows = []
    price = base * 0.75
    for i in range(n):
        ts = start + timedelta(minutes=15 * i)
        phase = i / n
        # Long trend, short flag (~last 8% of series ≈ 1–1.5d)
        if phase < 0.92:
            progress = phase / 0.92
            price = base * (0.75 + 0.30 * progress)
            if parabolic:
                price = base * (0.75 + 0.55 * (progress ** 1.5))
            half = price * 0.004
        else:
            lid = base * 1.05
            if deep_retrace:
                price = lid * (0.88 + 0.02 * ((i % 6) / 6))
            else:
                price = lid * (0.985 + 0.012 * ((i % 6) / 6))
            half = price * 0.001
        rows.append(
            {
                "timestamp": ts,
                "open": price - half * 0.2,
                "high": price + half,
                "low": price - half,
                "close": price + half * 0.15,
                "volume": 1200.0 if phase < 0.92 else 700.0,
                "open_interest": 1e6 * (1.0 + 0.1 * phase),
                "funding_rate": 0.00005,
            }
        )
    bars = pl.DataFrame(rows, strict=False)
    tail = bars.tail(64)
    flag_high = float(tail["high"].max())
    flag_low = float(tail["low"].min())
    last = bars.tail(1).to_dicts()[0]
    if breach:
        last["close"] = flag_high * 1.01
        last["high"] = flag_high * 1.015
    else:
        last["close"] = flag_high - (flag_high - flag_low) * 0.08
        last["high"] = flag_high
        last["low"] = last["close"] - (flag_high - flag_low) * 0.1
        last["open"] = last["close"] - (flag_high - flag_low) * 0.02
    body = bars.head(bars.height - 1).to_dicts()
    body.append(last)
    bars = pl.DataFrame(body, strict=False)
    cutoff = bars["timestamp"][-1] + timedelta(minutes=15)
    if hasattr(cutoff, "to_pydatetime"):
        cutoff = cutoff.to_pydatetime()
    return bars, cutoff


class ContinuationBreakoutV2Tests(unittest.TestCase):
    def test_emits_flag_breakout_identity(self):
        bars, cutoff = _trend_then_flag()
        zones = [
            {
                "type": "fvg",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "active",
                "low": 95.0,
                "high": 110.0,
            }
        ]
        cfg = ContV2Config(
            n=12,
            k=20.0,
            p=8,
            t_min=0.1,
            retr_max=0.9,
            e=5.0,
            r_max=20.0,
            g=5.0,
            x_max=50.0,
            s_min=0.0,
        )
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=zones,
            cfg=cfg,
        )
        self.assertIsNotNone(event, "expected armed flag breakout")
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["setup_class"], "continuation_breakout")
        self.assertEqual(event["phase"], "armed_flag_breakout")
        self.assertEqual(event["entry_condition"]["type"], "breakout_above")
        self.assertEqual(event["confidence_status"], "uncalibrated")
        self.assertEqual(event["horizon_minutes"], 240)
        self.assertEqual(len(event["targets"]), 1)
        self.assertEqual(event["feature_snapshot"].get("weight_profile"), "balanced")
        entry = event["entry_condition"]["price"]
        inv = event["invalidation_price"]
        self.assertGreater(entry, inv)
        risk = entry - inv
        self.assertAlmostEqual(event["targets"][0], entry + 1.5 * risk, places=5)
        self.assertIn("flag_high", event["feature_snapshot"])
        self.assertIn("trend_norm_4h", event["feature_snapshot"])

    def test_breach_fails(self):
        bars, cutoff = _trend_then_flag(breach=True)
        zones = [
            {
                "type": "fvg",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "partial",
                "low": 90.0,
                "high": 120.0,
            }
        ]
        cfg = ContV2Config(n=12, k=50.0, p=8, t_min=0.1, retr_max=0.95, e=50.0, r_max=50.0, x_max=50.0)
        self.assertIsNone(
            evaluate_symbol(bars, asset="SOL", symbol="X", cutoff=cutoff, zones=zones, cfg=cfg)
        )

    def test_deep_retrace_fails(self):
        bars, cutoff = _trend_then_flag(deep_retrace=True)
        zones = [
            {
                "type": "order_block",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "active",
                "low": 80.0,
                "high": 120.0,
            }
        ]
        cfg = ContV2Config(
            n=12, k=50.0, p=8, t_min=0.05, retr_max=0.25, e=50.0, r_max=50.0, x_max=50.0, s_min=0.0
        )
        self.assertIsNone(
            evaluate_symbol(bars, asset="SOL", symbol="X", cutoff=cutoff, zones=zones, cfg=cfg)
        )

    def test_extension_cap_hard_fails(self):
        bars, cutoff = _trend_then_flag(parabolic=True)
        zones = [
            {
                "type": "fvg",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "active",
                "low": 90.0,
                "high": 130.0,
            }
        ]
        # tiny x_max forces extension fail if move is large
        cfg = ContV2Config(
            n=12, k=50.0, p=6, t_min=0.05, retr_max=0.95, e=50.0, r_max=50.0, x_max=0.05, x_bars=64, s_min=0.0
        )
        self.assertIsNone(
            evaluate_symbol(bars, asset="SOL", symbol="X", cutoff=cutoff, zones=zones, cfg=cfg)
        )

    def test_no_mutex_with_sibling_families(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        outbox = Path(directory.name) / "outbox"
        outbox.mkdir()
        now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        for sid in ("accumulation-base-v2", "impulse-ignition-v2"):
            (outbox / f"{sid}.json").write_text(
                json.dumps(
                    {
                        "strategy_id": sid,
                        "asset": "SOL",
                        "direction": "long",
                        "valid_until": (now + timedelta(hours=2)).isoformat(),
                    }
                ),
                encoding="utf-8",
            )
        self.assertFalse(
            has_active_event(STRATEGY_ID, "SOL", "long", outbox_dir=outbox, now=now)
        )


if __name__ == "__main__":
    unittest.main()
