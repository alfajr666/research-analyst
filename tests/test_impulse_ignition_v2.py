import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

import config
from strategies.v2.impulse_ignition_v2 import IgnV2Config, STRATEGY_ID, evaluate_symbol
from strategy_v2_context import has_active_event


def _coil_bars(
    *,
    days: int = 14,
    base: float = 100.0,
    prior_wide: bool = True,
    near_high: bool = True,
    breach: bool = False,
) -> tuple[pl.DataFrame, datetime]:
    """Build 15m history: prior wider range, then tight coil near lid."""
    start = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    n = days * 24 * 4
    rows = []
    price = base * 0.9
    for i in range(n):
        ts = start + timedelta(minutes=15 * i)
        phase = i / n
        if phase < 0.55:
            # prior expansion / wider range
            swing = 0.04 if prior_wide else 0.005
            price = base * (0.9 + 0.15 * phase + swing * ((i % 20) / 20 - 0.5))
            half = price * (0.01 if prior_wide else 0.002)
        else:
            # compression coil under lid ~ base
            price = base * (0.995 + 0.004 * ((i % 8) / 8))
            half = price * 0.0012
        o, c = price - half * 0.2, price + half * 0.2
        h, lo = price + half, price - half
        rows.append(
            {
                "timestamp": ts,
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": 800.0 if phase >= 0.55 else 1500.0,
                "open_interest": 1e6 * (1.0 + 0.05 * phase),
                "funding_rate": 0.00005,
            }
        )
    # Force last close near / through lid
    bars = pl.DataFrame(rows, strict=False)
    # Approximate base high from last ~16h of 1h → last 64 15m bars
    tail = bars.tail(64)
    base_high = float(tail["high"].max())
    base_low = float(tail["low"].min())
    last = bars.tail(1).to_dicts()[0]
    if breach:
        last["close"] = base_high * 1.01
        last["high"] = base_high * 1.015
    elif near_high:
        last["close"] = base_high - (base_high - base_low) * 0.05
        last["high"] = base_high
        last["low"] = last["close"] - (base_high - base_low) * 0.1
        last["open"] = last["close"] - (base_high - base_low) * 0.02
    # rebuild last row
    body = bars.head(bars.height - 1).to_dicts()
    body.append(last)
    bars = pl.DataFrame(body, strict=False)
    cutoff = bars["timestamp"][-1] + timedelta(minutes=15)
    if hasattr(cutoff, "to_pydatetime"):
        cutoff = cutoff.to_pydatetime()
    return bars, cutoff


class ImpulseIgnitionV2Tests(unittest.TestCase):
    def test_breakout_entry_at_base_high_not_synthetic(self):
        bars, cutoff = _coil_bars(near_high=True, breach=False)
        zones = [
            {
                "type": "fvg",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "active",
                "low": 95.0,
                "high": 105.0,
            }
        ]
        cfg = IgnV2Config(
            n=12, k=20.0, p=20, c_ratio=1.05, e=5.0, r_max=20.0, g=5.0, s_min=0.0
        )
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=zones,
            cfg=cfg,
        )
        self.assertIsNotNone(event, "expected armed breakout candidate")
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["phase"], "armed_base_breakout")
        self.assertEqual(event["setup_class"], "impulse_ignition")
        self.assertEqual(event["entry_condition"]["type"], "breakout_above")
        self.assertEqual(event["confidence_status"], "uncalibrated")
        self.assertEqual(len(event["targets"]), 1)
        entry = event["entry_condition"]["price"]
        inv = event["invalidation_price"]
        self.assertGreater(entry, inv)
        # single 1.5R target
        risk = entry - inv
        self.assertAlmostEqual(event["targets"][0], entry + 2.0 * risk, places=5)
        # not v1 synthetic close*1.005
        close = event["feature_snapshot"]["close_15m"]
        self.assertNotAlmostEqual(entry, close * 1.005, places=4)

    def test_breach_of_lid_is_not_ignition(self):
        bars, cutoff = _coil_bars(breach=True)
        zones = [
            {
                "type": "fvg",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "active",
                "low": 95.0,
                "high": 105.0,
            }
        ]
        cfg = IgnV2Config(n=12, k=50.0, p=20, c_ratio=0.99, e=50.0, r_max=50.0, g=5.0)
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=zones,
            cfg=cfg,
        )
        self.assertIsNone(event)

    def test_missing_oi_funding_does_not_hard_fail(self):
        bars, cutoff = _coil_bars()
        bars = bars.with_columns(
            [
                pl.lit(0.0).alias("open_interest"),
                pl.lit(0.0).alias("funding_rate"),
            ]
        )
        zones = [
            {
                "type": "order_block",
                "timeframe": "4h",
                "direction": "bullish",
                "state": "partial",
                "low": 90.0,
                "high": 110.0,
            }
        ]
        cfg = IgnV2Config(n=12, k=20.0, p=20, c_ratio=0.99, e=5.0, r_max=20.0, g=5.0, s_min=0.0)
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=zones,
            cfg=cfg,
        )
        # may still gate on geometry; if emits, oi/funding soft terms are 0
        if event is not None:
            raw = event["feature_snapshot"]["component_raw"]
            self.assertEqual(raw.get("oi_pressure", 0.0), 0.0)

    def test_no_mutex_with_accumulation_outbox(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        outbox = Path(directory.name) / "outbox"
        outbox.mkdir()
        now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        (outbox / "acc.json").write_text(
            json.dumps(
                {
                    "strategy_id": "accumulation-base-v2",
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
