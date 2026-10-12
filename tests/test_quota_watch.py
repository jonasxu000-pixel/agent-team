from test_team import Fixture
from agent_team.store import Store
from agent_team.quota_watch import wait_for_codex
from agent_team.safety import TeamError


class WatchTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state")
        self.id = self.store.create({"request": "test", "repo": "fixture", "config": {}})
        self.store.update(self.id, status="PAUSED_QUOTA", phase="develop", data={"codex_head": "fixed", "review_done": True})

    def test_recovers_from_fresh_official_snapshot_without_restarting_task(self):
        results = iter([{"status": "QUOTA_EXHAUSTED"}, {"status": "AVAILABLE"}])
        # Allow the real fixture's disk writes on slower CI hosts; only two probes are used.
        self.assertTrue(wait_for_codex(self.store, self.id, reader=lambda: next(results), timeout=5, interval=.001))
        task = self.store.task(self.id)
        self.assertEqual(task["phase"], "develop")
        self.assertEqual(task["data"]["codex_head"], "fixed")
        self.assertTrue(task["data"]["review_done"])
        self.assertEqual(self.store.health("codex")["status"], "AVAILABLE")

    def test_unknown_is_not_treated_as_available(self):
        self.assertFalse(wait_for_codex(self.store, self.id, reader=lambda: {"status": "UNKNOWN"}, timeout=.02, interval=.005))
        self.assertEqual(self.store.task(self.id)["status"], "PAUSED_QUOTA")

    def test_auth_error_stops_immediately(self):
        def reader():
            raise TeamError("AUTH_ERROR", "mock auth failure")
        self.assertFalse(wait_for_codex(self.store, self.id, reader=reader, timeout=1, interval=.001))
        self.assertEqual(self.store.task(self.id)["status"], "PAUSED_AUTH")

    def test_user_cancel_stops_waiting(self):
        def reader():
            self.store.control(self.id, "cancel")
            return {"status": "QUOTA_EXHAUSTED"}
        self.assertFalse(wait_for_codex(self.store, self.id, reader=reader, timeout=1, interval=.001))
        self.assertEqual(self.store.task(self.id)["status"], "CANCELLED")
