import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from rsi_reclaim_v1 import (
    STRATEGY_ID,
    RsiReclaimConfig,
    evaluate_symbol,
    rsi_series,
)
from strategy_v2_context import has_active_event, ema_last, atr_last
from strategy_plugins import KNOWN_STRATEGIES, load_enabled_plugins
import config


def _cutoff_from(bars: pl.DataFrame) -> datetime:
    cutoff = bars["timestamp"][-1] + timedelta(minutes=15)
    if hasattr(cutoff, "to_pydatetime"):
        cutoff = cutoff.to_pydatetime()
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    return cutoff


def _grind_bars(
    n: int = 16 * 24 * 4,
    *,
    start_price: float = 90.0,
    end_price: float = 110.0,
    start: datetime | None = None,
    range_pct: float = 0.002,
) -> pl.DataFrame:
    """Slow grind higher — enough history for 1h EMA200 + 4h EMA48."""
    start = start or datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        ts = start + timedelta(minutes=15 * i)
        t = i / max(n - 1, 1)
        price = start_price + (end_price - start_price) * t
        half = price * range_pct
        rows.append(
            {
                "timestamp": ts,
                "open": price - half * 0.1,
                "high": price + half,
                "low": price - half,
                "close": price + half * 0.05,
                "volume": 1000.0,
                "open_interest": 1e6,
                "funding_rate": 0.0001,
            }
        )
    return pl.DataFrame(rows, strict=False)


def _with_long_reclaim(bars: pl.DataFrame) -> pl.DataFrame:
    """Overwrite last bars: shallow dip then green reclaim of 15m EMA20 (stack stays bull)."""
    rows = bars.to_dicts()
    # 5-bar shallow dip into the prior grind so EMA20 remains above EMA50.
    base = float(rows[-6]["close"])
    for j, offset in enumerate(range(-5, -1)):
        px = base * (1.0 - 0.0004 * (j + 1))
        half = px * 0.0015
        rows[offset]["open"] = px + half * 0.05
        rows[offset]["high"] = px + half
        rows[offset]["low"] = px - half
        rows[offset]["close"] = px - half * 0.1
    soft = pl.DataFrame(rows, strict=False)
    ema20 = ema_last(soft["close"].to_list(), 20)
    ema50 = ema_last(soft["close"].to_list(), 50)
    assert ema20 is not None and ema50 is not None
    atr = atr_last(soft, 14) or (ema20 * 0.01)
    # Reclaim: tag EMA20 from below-ish, close above with quality body; stay near EMA200 band.
    body = max(0.40 * atr, ema20 * 0.002)
    o = min(ema20 - 0.02 * atr, ema20 * 0.9995)
    c = max(ema20 + body, ema50 + 0.05 * atr)
    lo = min(o, ema20) - 0.05 * atr
    hi = c + 0.05 * atr
    rows[-1]["open"] = o
    rows[-1]["high"] = hi
    rows[-1]["low"] = lo
    rows[-1]["close"] = c
    return pl.DataFrame(rows, strict=False)


def _loose_cfg(**overrides) -> RsiReclaimConfig:
    base = dict(
        rsi_max=80.0,
        rsi_min=20.0,
        sep_min=0.0,
        sep_max=0.50,
        body_atr_min=0.05,
        r_max=50.0,
        s_min=0.0,
        pullback_tol=0.01,
    )
    base.update(overrides)
    return RsiReclaimConfig(**base)


class RsiReclaimV1Tests(unittest.TestCase):
    def test_known_strategy_registered(self):
        self.assertIn("rsi-reclaim-v1", KNOWN_STRATEGIES)

    def test_rsi_series_warm_and_bounded(self):
        closes = [100.0 + i * 0.5 for i in range(40)]
        vals = rsi_series(closes, 14)
        self.assertIsNone(vals[13])
        self.assertIsNotNone(vals[14])
        self.assertTrue(0.0 <= float(vals[-1]) <= 100.0)

    def test_emits_confirmed_reclaim_identity(self):
        bars = _with_long_reclaim(_grind_bars())
        cutoff = _cutoff_from(bars)
        cfg = _loose_cfg()
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=[],
            cfg=cfg,
        )
        self.assertIsNotNone(event, "expected gated long reclaim candidate")
        self.assertEqual(event["strategy_id"], STRATEGY_ID)
        self.assertEqual(event["setup_class"], "continuation_pullback")
        self.assertEqual(event["phase"], "confirmed_rsi_reclaim")
        self.assertEqual(event["direction"], "long")
        self.assertEqual(event["entry_condition"]["type"], "breakout_above")
        self.assertEqual(event["confidence_status"], "uncalibrated")
        self.assertEqual(len(event["targets"]), 1)
        self.assertEqual(event["horizon_minutes"], 240)
        self.assertIn("rsi_15m", event["feature_snapshot"])
        self.assertIn("ema200_1h", event["feature_snapshot"])
        self.assertIn("sep_1h", event["feature_snapshot"])
        self.assertIn("confluence_score", event["feature_snapshot"])
        entry = event["entry_condition"]["price"]
        inv = event["invalidation_price"]
        risk = abs(entry - inv)
        self.assertAlmostEqual(event["targets"][0], entry + 2.0 * risk, places=5)

    def test_hard_fail_without_reclaim(self):
        bars = _grind_bars()
        # Force last bar red and below last close (no reclaim geometry)
        rows = bars.to_dicts()
        px = float(rows[-1]["close"])
        rows[-1]["open"] = px + 0.5
        rows[-1]["close"] = px - 0.5
        rows[-1]["high"] = px + 0.6
        rows[-1]["low"] = px - 0.6
        bars = pl.DataFrame(rows, strict=False)
        cutoff = _cutoff_from(bars)
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=[],
            cfg=_loose_cfg(),
        )
        self.assertIsNone(event)

    def test_hard_fail_sep_too_extended(self):
        bars = _with_long_reclaim(_grind_bars(start_price=50.0, end_price=200.0))
        cutoff = _cutoff_from(bars)
        # sep_max tiny → parabolic grind fails extension band
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=[],
            cfg=_loose_cfg(sep_min=0.0, sep_max=0.001),
        )
        self.assertIsNone(event)

    def test_hard_fail_short_history(self):
        bars = _grind_bars(n=80)
        cutoff = _cutoff_from(bars)
        event = evaluate_symbol(
            bars,
            asset="SOL",
            symbol="SOLUSDT_PERP.A",
            cutoff=cutoff,
            zones=[],
            cfg=_loose_cfg(),
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

    def test_plugin_loads_when_enabled(self):
        prev = config.STRATEGY_ENABLED_IDS
        try:
            config.STRATEGY_ENABLED_IDS = ("rsi-reclaim-v1",)
            plugins = load_enabled_plugins()
            self.assertEqual(len(plugins), 1)
            self.assertEqual(plugins[0].id, "rsi-reclaim-v1")
            self.assertEqual(plugins[0].version, "v1")
        finally:
            config.STRATEGY_ENABLED_IDS = prev


if __name__ == "__main__":
    unittest.main()
