import unittest
from agent_team.quota import parse_snapshot
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
from test_team import Fixture, FakeCodex, FakeClaude
from agent_team.store import Store
from agent_team.workflow import DEFAULTS, Workflow
from agent_team.safety import TeamError


class QuotaTests(unittest.TestCase):
    def test_official_usage_and_reset_recorded_without_guess(self):
        result = parse_snapshot({"rateLimits": {"limitId": "codex", "primary": {
            "usedPercent": 27, "windowDurationMins": 300, "resetsAt": 1780000000}}})
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertEqual(result["windows"][0]["used_percent"], 27)
        self.assertEqual(result["windows"][0]["resets_at_unix"], 1780000000)

    def test_exhausted_window_records_reliable_recovery(self):
        result = parse_snapshot({"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": 1780000000}}})
        self.assertEqual(result["status"], "QUOTA_EXHAUSTED")
        self.assertIsNotNone(result["recovery_at_utc"])

    def test_missing_or_invalid_data_remains_unknown(self):
        self.assertEqual(parse_snapshot({})["status"], "UNKNOWN")
        self.assertEqual(parse_snapshot({"rateLimits": {"primary": {"usedPercent": float("nan")}}})["status"], "UNKNOWN")

    def test_other_bucket_does_not_assert_codex_available(self):
        self.assertEqual(parse_snapshot({"rateLimits": {"limitId": "another-product", "primary": {"usedPercent": 5}}})["status"], "UNKNOWN")

    def test_codex_multibucket_takes_precedence(self):
        result = parse_snapshot({"rateLimits": {"limitId": "another-product"}, "rateLimitsByLimitId": {
            "codex": {"primary": {"usedPercent": 50, "resetsAt": None}}}})
        self.assertEqual(result["status"], "AVAILABLE")
        self.assertIsNone(result["windows"][0]["resets_at_utc"])


class QuotaWorkflowTests(Fixture):
    def prepare(self):
        self.codex = FakeCodex()
        self.claude = FakeClaude()
        self.store = Store(self.root / "state")
        self.flow = Workflow(self.store, codex=self.codex, claude=self.claude, trusted_test_fixture=True)
        self.id = self.flow.create(self.repo(), "set value", {**DEFAULTS, "test_commands": [[sys.executable, "test.py"]]})

    def test_exhausted_official_window_stops_before_any_model_dispatch(self):
        self.prepare()
        self.codex.quota = lambda *a, **kw: parse_snapshot({"rateLimits": {"primary": {"usedPercent": 100, "resetsAt": 1791547527}}})
        with redirect_stdout(io.StringIO()):
            result = self.flow.run(self.id)
        self.assertEqual(result["status"], "PAUSED_QUOTA")
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.claude.calls, [])
        self.assertIsNotNone(result["data"]["codex_quota_snapshot"]["recovery_at_utc"])

    def test_unavailable_probe_uses_conservative_execution_state(self):
        self.prepare()
        def unavailable(*a, **kw):
            raise TeamError("QUOTA_PROBE_UNAVAILABLE", "mock timeout")
        self.codex.quota = unavailable
        with redirect_stdout(io.StringIO()):
            result = self.flow.run(self.id)
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertEqual(result["data"]["codex_quota_snapshot"]["status"], "UNKNOWN")
