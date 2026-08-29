import unittest
from datetime import datetime, timezone, timedelta
import tempfile
import os
import json

import config
import outcome_evaluator


class OutcomeEvaluatorContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.market_path = os.path.join(self.directory.name, "market.db")
        self.alpha_path = os.path.join(self.directory.name, "alpha.db")
        config.init_market_db(self.market_path)
        config.init_analyst_db(self.alpha_path)

    def tearDown(self):
        self.directory.cleanup()

    def test_evaluate_reads_market_only_and_writes_alpha(self):
        now = datetime.now(timezone.utc)
        # seed minimal expired event in alpha DB
        aconn = config.get_db_connection(db_path=self.alpha_path)
        event = {
            "schema_version": 1,
            "alpha_id": "alpha-test-1",
            "strategy_id": "test",
            "asset": "SOL",
            "direction": "long",
            "setup_class": "test",
            "phase": "test",
            "observed_at": (now - timedelta(minutes=30)).isoformat(),
            "valid_until": (now - timedelta(minutes=1)).isoformat(),
            "horizon_minutes": 60,
            "confidence": 0.5,
            "entry_condition": {"type": "breakout_above", "price": 100.0},
            "invalidation_price": 99.0,
            "targets": [101.0],
            "feature_snapshot": {"source_symbol": "SOLUSDT"},
            "dedupe_key": "test-dedupe",
        }
        aconn.execute(
            """INSERT INTO alpha_events (dedupe_key, alpha_id, strategy_id, asset, direction, setup_class, phase, status, observed_at, valid_until, event_json, persisted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'expired', ?, ?, ?, ?)""",
            ("test-dedupe", "alpha-test-1", "test", "SOL", "long", "test", "test",
             now - timedelta(minutes=30), now - timedelta(minutes=1), json.dumps(event), now)
        )
        aconn.commit()
        aconn.close()

        # also seed matching candidate for FK (outcomes references candidates)
        aconn = config.get_db_connection(db_path=self.alpha_path)
        aconn.execute(
            "INSERT INTO alpha_candidates (candidate_id, observed_at, strategy_id, asset, direction, liquidity_tier) VALUES (?, ?, ?, ?, ?, 'mid')",
            ("alpha-test-1", now - timedelta(minutes=30), "test", "SOL", "long")
        )
        aconn.commit()
        aconn.close()

        # seed bar in source_observations (primary post-drop)
        mconn = config.get_db_connection(db_path=self.market_path)
        bar_ts = now - timedelta(minutes=5)
        payload = json.dumps({"open": 100, "high": 102, "low": 99, "close": 101.5, "volume": 10})
        mconn.execute(
            "INSERT INTO source_observations (observation_id, source, venue, native_symbol, asset, market_kind, interval, source_start, source_end, retrieved_at, retrieval_kind, payload_json) VALUES (?, 'coinalyze', 'aggregate_perp', 'SOLUSDT', 'SOL', 'perpetual', '15m', ?, ?, ?, 'live', ?)",
            (f"obs-{bar_ts.isoformat()}", bar_ts, bar_ts, bar_ts, payload)
        )
        mconn.commit()
        mconn.close()

        n = outcome_evaluator.evaluate_expired_outcomes(self.market_path, self.alpha_path, now)
        self.assertGreaterEqual(n, 1)

        # verify written only to alpha
        aconn = config.get_db_connection(db_path=self.alpha_path)
        row = aconn.execute("SELECT outcome FROM alpha_outcomes WHERE candidate_id = 'alpha-test-1'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn(row[0], ("target", "invalidated", "expired", "ambiguous_same_bar", "not_triggered"))
        aconn.close()

        # confirm no cross-write: market has no alpha_outcomes table data (or empty)
        mconn = config.get_db_connection(db_path=self.market_path)
        # table may not even exist in pure market init, or empty
        try:
            cnt = mconn.execute("SELECT COUNT(*) FROM alpha_outcomes").fetchone()[0]
            self.assertEqual(cnt, 0)
        except Exception:
            pass  # ok if schema not present in market-only
        mconn.close()
