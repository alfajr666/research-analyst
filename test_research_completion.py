import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import config
from llm_client import Completion
from research_repository import ResearchCoordinator, queue_event_review
from research_workflow import queue_question, run_approved_experiment
from signal_publisher import format_research_note


NOW = datetime(2026, 8, 16, 12, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0
        self.tasks = []

    def complete(self, system, task, packet):
        self.calls += 1
        self.tasks.append(task)
        if isinstance(self.result, Exception):
            raise self.result
        return Completion(json.dumps(self.result), {"prompt_tokens": 100, "completion_tokens": 50})


def settings(**overrides):
    values = dict(LLM_RESEARCH_ENABLED=True, LLM_TIMEOUT_SECONDS=20, LLM_MAX_REPORTS_PER_CYCLE=2,
                  LLM_MAX_RETRIES=2, LLM_RETRY_BASE_SECONDS=10, LLM_MAX_INPUT_CHARS=24000,
                  LLM_MAX_OUTPUT_CHARS=6000, LLM_MONTHLY_BUDGET_USD=10, LLM_PROVIDER="openai",
                  LLM_MODEL="fake", LLM_API_KEY="fake", LLM_PRICING_VERSION="test-v1",
                  LLM_INPUT_COST_PER_1K_USD=1, LLM_OUTPUT_COST_PER_1K_USD=1)
    values.update(overrides)
    return SimpleNamespace(**values)


def report():
    evidence = "local:alpha_events:a-1"
    return {"schema_version": 1, "verdict": "neutral", "thesis_summary": "Local evidence is mixed.",
            "claims": [{"claim": "The event is recorded locally.", "stance": "uncertain", "evidence_ids": [evidence]}],
            "risks": [{"type": "data_quality", "severity": "low", "detail": "The evidence packet is bounded.", "evidence_ids": [evidence]}],
            "limitations": ["No external sources are included."], "operator_questions": []}


class ResearchCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "research.db"
        config.init_db(self.db)
        self.connection = config.get_db_connection(db_path=self.db)
        event = {"alpha_id": "a-1", "strategy_id": "test", "asset": "SOL", "feature_snapshot": {"source_symbol": "SOLUSDT"}}
        self.connection.execute("""INSERT INTO alpha_events VALUES ('key', 'a-1', 'test', 'SOL', 'long', 'test', 'phase', 'active', ?, ?, ?, ?)""",
                                (NOW - timedelta(minutes=15), NOW + timedelta(hours=1), json.dumps(event), NOW))
        queue_event_review(self.connection, "a-1", NOW - timedelta(minutes=15), True)

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def coordinator(self, client, **overrides):
        return ResearchCoordinator(settings(**overrides), client, now=lambda: NOW)

    def status(self):
        return self.connection.execute("SELECT status, attempt_count, error_code FROM research_requests").fetchone()

    def test_success_persists_cited_artifact_and_usage(self):
        result = self.coordinator(FakeClient(report())).process(self.connection)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.status()[0], "completed")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_artifacts").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_evidence").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT completed_count FROM research_run_metrics").fetchone()[0], 1)

    def test_rendered_note_is_bounded_and_advisory(self):
        rendered = format_research_note(report())
        self.assertIn("Research note (advisory)", rendered)
        self.assertIn("Verdict: neutral", rendered)
        self.assertLessEqual(len(rendered), 900)

    def test_malformed_and_policy_output_fail_without_artifact(self):
        invalid = report()
        invalid["thesis_summary"] = "This outcome is guaranteed."
        self.coordinator(FakeClient(invalid)).process(self.connection)
        self.assertEqual(self.status()[0], "failed")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM research_artifacts").fetchone()[0], 0)

    def test_timeout_retries_then_exhausts(self):
        client = FakeClient(TimeoutError("slow provider"))
        coordinator = self.coordinator(client)
        coordinator.process(self.connection)
        self.assertEqual(self.status()[0], "pending")
        self.connection.execute("UPDATE research_requests SET next_attempt_at = ?", (NOW,))
        coordinator.process(self.connection)
        self.connection.execute("UPDATE research_requests SET next_attempt_at = ?", (NOW,))
        coordinator.process(self.connection)
        self.assertEqual(self.status()[0], "failed")
        self.assertEqual(self.status()[2], "retries_exhausted")

    def test_budget_limit_skips_without_provider_call(self):
        client = FakeClient(report())
        self.coordinator(client, LLM_MONTHLY_BUDGET_USD=0).process(self.connection)
        self.assertEqual(self.status()[0], "skipped")
        self.assertEqual(client.calls, 0)

    def test_stale_running_request_is_recovered(self):
        self.connection.execute("UPDATE research_requests SET status = 'running', started_at = ?, attempt_count = 1", (NOW - timedelta(seconds=30),))
        client = FakeClient(report())
        result = self.coordinator(client).process(self.connection)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(self.status()[0], "completed")

    def test_unknown_citation_fails(self):
        invalid = report()
        invalid["claims"][0]["evidence_ids"] = ["local:invented"]
        self.coordinator(FakeClient(invalid)).process(self.connection)
        self.assertEqual(self.status()[0], "failed")

    def test_human_question_persists_exact_input_and_completes(self):
        created, _ = queue_question(self.connection, "a-1", "What local risks are present?", NOW, True)
        self.assertTrue(created)
        client = FakeClient(report())
        self.coordinator(client).process(self.connection)
        stored = self.connection.execute("SELECT request_input_json FROM research_requests WHERE request_kind = 'research_question'").fetchone()[0]
        self.assertIn("What local risks are present?", stored)
        self.assertTrue(any("What local risks are present?" in task for task in client.tasks))

    def test_sensitive_input_key_is_rejected_before_queueing(self):
        self.connection.execute("UPDATE alpha_events SET event_json = ? WHERE alpha_id = 'a-1'", (json.dumps({"alpha_id": "a-1", "strategy_id": "test", "asset": "SOL", "feature_snapshot": {"api_key": "nope"}}),))
        with self.assertRaisesRegex(ValueError, "sensitive key"):
            queue_question(self.connection, "a-1", "Assess local evidence", NOW, True)

    def test_only_allowlisted_experiment_can_run(self):
        result = run_approved_experiment(self.connection, "descriptive_outcome_summary", "test")
        self.assertEqual(result["sample_count"], 0)
        with self.assertRaises(ValueError):
            run_approved_experiment(self.connection, "arbitrary_sql", "test")


if __name__ == "__main__":
    unittest.main()
