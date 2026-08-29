import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import config
import pm_sidecar


class PMSidecarTests(unittest.TestCase):
    def setUp(self):
        self.prev_enabled = config.PM_SIDECAR_ENABLED
        self.prev_key = os.environ.get("LLM_API_KEY")
        self.prev_llm_key = getattr(config, "LLM_API_KEY", "")
        self.prev_snap = getattr(config, "EXECUTOR_SNAPSHOT_DIR", "")
        self.prev_dec = getattr(config, "EXECUTOR_DECISION_DIR", "")
        config.PM_SIDECAR_ENABLED = False
        config.LLM_API_KEY = ""  # force offline (hold) path; no live LLM calls
        os.environ.pop("LLM_API_KEY", None)
        self.directory = tempfile.TemporaryDirectory()
        self.db = Path(self.directory.name) / "analyst.db"
        self.market_db = Path(self.directory.name) / "market.sqlite3"
        config.init_market_db(self.market_db)
        config.init_analyst_db(self.db)
        config.MARKET_DB_PATH = str(self.market_db)
        conn = config.get_db_connection(read_only=False, db_path=self.db)
        try:
            conn.execute(
                """
                INSERT INTO positions_feed
                    (position_id, symbol, asset, side, entry, size, opened_at,
                     strategy_id, current_pnl, status, updated_at)
                VALUES ('P1', 'BTCUSDT', 'BTC', 'long', 60000.0, 1.0, ?,
                        'rsi-reclaim-v1', 0.02, 'open', ?)
                """,
                (datetime(2026, 1, 1, tzinfo=timezone.utc),
                 datetime(2026, 1, 1, tzinfo=timezone.utc)),
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        config.PM_SIDECAR_ENABLED = self.prev_enabled
        config.LLM_API_KEY = self.prev_llm_key
        config.EXECUTOR_SNAPSHOT_DIR = self.prev_snap
        config.EXECUTOR_DECISION_DIR = self.prev_dec
        if self.prev_key is None:
            os.environ.pop("LLM_API_KEY", None)
        else:
            os.environ["LLM_API_KEY"] = self.prev_key
        self.directory.cleanup()

    def _count_advice(self):
        conn = config.get_db_connection(read_only=True, db_path=self.db)
        try:
            return conn.execute("SELECT count(*) FROM pm_advice").fetchone()[0]
        finally:
            conn.close()

    def test_disabled_is_noop(self):
        res = pm_sidecar.run_once(self.db, now=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc))
        self.assertEqual(res, {"enabled": False, "advices": 0})
        self.assertEqual(self._count_advice(), 0)

    def test_enabled_emits_hold_without_llm_and_dedupes(self):
        config.PM_SIDECAR_ENABLED = True
        now = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
        res = pm_sidecar.run_once(self.db, now=now)
        self.assertTrue(res["enabled"])
        self.assertEqual(res["positions"], 1)
        self.assertEqual(res["advices"], 1)
        self.assertEqual(self._count_advice(), 1)

        # Same 5m cutoff -> second pass must NOT add a duplicate.
        res2 = pm_sidecar.run_once(self.db, now=datetime(2026, 1, 1, 12, 6, tzinfo=timezone.utc))
        self.assertEqual(res2["advices"], 0)
        self.assertEqual(self._count_advice(), 1)

        conn = config.get_db_connection(read_only=True, db_path=self.db)
        try:
            row = conn.execute(
                "SELECT action, reason FROM pm_advice WHERE position_id='P1'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "hold")
        self.assertIn("hold", row[1].lower())

    def test_call_pm_llm_returns_none_without_key(self):
        self.assertIsNone(pm_sidecar.call_pm_llm("ignored prompt"))

    def test_load_snapshot_positions(self):
        snap = Path(self.directory.name) / "snaps"
        acc = snap / "bybit" / "hyro"
        acc.mkdir(parents=True)
        payload = {
            "positions": [{
                "position_id": "X", "symbol": "SOLUSDT", "side": "short",
                "status": "OPEN", "quantity": 2, "entry_price": 100.0,
                "original_json": json.dumps({"strategy_id": "s", "asset": "SOL"}),
            }],
        }
        (acc / "latest.json").write_text(json.dumps(payload))
        out = pm_sidecar._load_open_positions_from_snapshots(str(snap))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["exchange_id"], "bybit")
        self.assertEqual(out[0]["account_id"], "hyro")
        self.assertEqual(out[0]["asset"], "SOL")
        self.assertEqual(out[0]["strategy_id"], "s")
        # closed positions are skipped
        payload["positions"].append({
            "position_id": "Y", "symbol": "ADAUSDT", "side": "long",
            "status": "CLOSED", "quantity": 1, "entry_price": 1.0,
            "original_json": json.dumps({"strategy_id": "s", "asset": "ADA"}),
        })
        (acc / "latest.json").write_text(json.dumps(payload))
        self.assertEqual(
            len(pm_sidecar._load_open_positions_from_snapshots(str(snap))), 1)

    def test_write_decision_file_reduce_fraction(self):
        decision_dir = Path(self.directory.name) / "dec"
        prev = getattr(config, "EXECUTOR_DECISION_DIR", "")
        config.EXECUTOR_DECISION_DIR = str(decision_dir)
        try:
            pos = {"position_id": "P2", "symbol": "ETHUSDT",
                   "exchange_id": "bybit", "account_id": "hyro"}
            cutoff = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
            observed = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
            self.assertTrue(
                pm_sidecar._write_decision_file(pos, "reduce", "trim", cutoff, observed))
            files = list(decision_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
            self.assertEqual(data["action"], "REDUCE")
            self.assertEqual(data["reduce_fraction"], 0.5)
            self.assertEqual(data["exchange_id"], "bybit")
        finally:
            config.EXECUTOR_DECISION_DIR = prev

    def test_snapshot_source_and_decision_file(self):
        config.PM_SIDECAR_ENABLED = True
        os.environ.pop("LLM_API_KEY", None)
        prev_snap = getattr(config, "EXECUTOR_SNAPSHOT_DIR", "")
        prev_dec = getattr(config, "EXECUTOR_DECISION_DIR", "")
        snap = Path(self.directory.name) / "snaps"
        acc_dir = snap / "bybit" / "hyro"
        acc_dir.mkdir(parents=True)
        payload = {
            "schema_version": 1, "exchange_id": "bybit", "account_id": "hyro",
            "positions": [{
                "position_id": "POS1", "symbol": "BTCUSDT", "side": "long",
                "status": "OPEN", "quantity": 1.0, "entry_price": 60000.0,
                "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                "original_json": json.dumps({"strategy_id": "rsi-reclaim-v1", "asset": "BTC"}),
            }],
        }
        (acc_dir / "latest.json").write_text(json.dumps(payload))
        decision_dir = Path(self.directory.name) / "decisions"
        config.EXECUTOR_SNAPSHOT_DIR = str(snap)
        config.EXECUTOR_DECISION_DIR = str(decision_dir)
        try:
            now = datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc)
            res = pm_sidecar.run_once(self.db, now=now)
            self.assertEqual(res["positions"], 1)
            self.assertEqual(res["advices"], 1)
            self.assertEqual(res["decisions_written"], 1)
            files = list(decision_dir.glob("*.json"))
            self.assertEqual(len(files), 1)
            data = json.loads(files[0].read_text())
            self.assertEqual(data["action"], "HOLD")
            self.assertEqual(data["exchange_id"], "bybit")
            self.assertEqual(data["account_id"], "hyro")
            self.assertEqual(data["symbol"], "BTCUSDT")
            self.assertEqual(data["position_id"], "POS1")
            self.assertEqual(data["schema_version"], 1)
            self.assertIsNone(data["reduce_fraction"])
        finally:
            config.PM_SIDECAR_ENABLED = self.prev_enabled
            config.EXECUTOR_SNAPSHOT_DIR = prev_snap
            config.EXECUTOR_DECISION_DIR = prev_dec


if __name__ == "__main__":
    unittest.main()
