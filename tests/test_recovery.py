from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import sys
import time

from test_team import Fixture, FakeCodex, FakeClaude, PLAN
from agent_team.gitwork import GitWorktrees
from agent_team.process import run_process
from agent_team.safety import TeamError, pid_alive
from agent_team.store import Store
from agent_team.workflow import DEFAULTS, Workflow


class RecoveryTests(Fixture):
    def setup_flow(self, *, high=False, bug=False, findings=False, **config):
        repo = self.repo(expected=1 if findings else 2)
        self.store = Store(self.root / "state")
        self.codex = FakeCodex(high=high, bug=bug)
        self.claude = FakeClaude(findings)
        self.flow = Workflow(self.store, codex=self.codex, claude=self.claude, trusted_test_fixture=True)
        self.id = self.flow.create(repo, "set value", {**DEFAULTS, "allow_estimated_budget": True,
            "test_commands": [[sys.executable, "test.py"]], **config})

    def run_flow(self):
        with redirect_stdout(io.StringIO()):
            return self.flow.run(self.id)

    def interrupt_commit(self, phase):
        original = self.flow.git.commit
        def interrupted(cwd, expected, task_id, current_phase, **kw):
            answer = original(cwd, expected, task_id, current_phase, **kw)
            if current_phase == phase:
                raise TeamError("PAUSED", "simulated crash after durable commit")
            return answer
        self.flow.git.commit = interrupted
        return original

    def test_development_commit_recovery_reuses_model_result(self):
        self.setup_flow()
        original = self.interrupt_commit("develop")
        first = self.run_flow()
        self.assertEqual(first["status"], "PAUSED")
        self.assertIsNotNone(first["data"]["pending_commit"])
        self.flow.git.commit = original
        final = self.run_flow()
        self.assertEqual(final["status"], "READY_FOR_MERGE")
        self.assertEqual(sum(c[0] == "develop" for c in self.codex.calls), 1)
        self.assertEqual(len(final["data"]["commits"]), 1)

    def test_repair_commit_recovery_preserves_round_limit(self):
        self.setup_flow(bug=True)
        original = self.interrupt_commit("repair_tests")
        self.assertEqual(self.run_flow()["status"], "PAUSED")
        self.flow.git.commit = original
        final = self.run_flow()
        self.assertEqual(final["status"], "READY_FOR_MERGE")
        self.assertEqual(final["data"]["repair_rounds"], 1)
        self.assertEqual(sum(c[0] == "fix" for c in self.codex.calls), 1)

    def test_claude_commit_recovery_integrates_without_repaying(self):
        self.setup_flow(high=True, claude_max_calls=2, claude_developer={"request": "algorithm",
            "files": ["algorithm.py"], "acceptance": "returns 2"})
        original = self.interrupt_commit("claude_develop")
        self.assertEqual(self.run_flow()["status"], "PAUSED")
        self.flow.git.commit = original
        final = self.run_flow()
        self.assertEqual(final["status"], "READY_FOR_MERGE")
        self.assertEqual(sum(c[0] == "claude_develop" for c in self.claude.calls), 1)
        self.assertTrue((Path(final["data"]["integration_worktree"]) / "algorithm.py").exists())

    def test_no_paid_developer_dispatch_when_codex_globally_exhausted(self):
        self.setup_flow(high=True, claude_developer={"request": "algorithm", "files": ["algorithm.py"], "acceptance": "returns 2"})
        task = self.store.task(self.id)
        cwd = self.flow.git.ensure(task["repo"], self.id, "codex", task["base"])
        self.store.update(self.id, phase="claude_develop", data={"plan": dict(PLAN, critical_logic=True,
            expected_claude_gain="invariant", claude_developer_gain="independent algorithm"), "codex_worktree": str(cwd),
            "codex_head": task["base"], "repair_rounds": 0, "review_rounds": 0})
        self.store.set_health("codex", "QUOTA_EXHAUSTED")
        self.assertEqual(self.run_flow()["status"], "PAUSED_QUOTA")
        self.assertEqual(self.claude.calls, [])

    def test_repeated_problem_at_new_commit_stops(self):
        self.setup_flow(high=True, bug=True, findings=True, claude_max_calls=2, claude_max_reviews=2)
        original = self.codex.call
        def verified(cwd, role, *args, **kw):
            answer = original(cwd, role, *args, **kw)
            if role == "verify":
                answer.data["assessments"][0]["evidence_excerpt"] = (cwd / "value.py").read_text().strip()
            return answer
        self.codex.call = verified
        result = self.run_flow()
        self.assertEqual(result["status"], "NEEDS_ATTENTION")
        self.assertEqual(result["data"]["last_error"]["kind"], "REPEATED_ISSUE")
        self.assertEqual(len(self.claude.calls), 2)

    def test_no_second_review_after_default_budget_count(self):
        self.setup_flow(high=True, bug=True, findings=True)
        result = self.run_flow()
        self.assertEqual(result["status"], "AWAITING_REVIEW")
        self.assertEqual(len(self.claude.calls), 1)
        self.assertEqual(result["data"]["repair_rounds"], 1)

    def test_parent_exit_kills_descendants(self):
        if os.name != "nt":
            self.skipTest("Windows Job Object test")
        code = "import subprocess,sys; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); Path('child-pid').write_text(str(p.pid))"
        result = run_process([sys.executable, "-c", code], self.root, timeout=5)
        self.assertEqual(result.code, 0)
        child = int((self.root / "child-pid").read_text())
        for _ in range(10):
            if not pid_alive(child):
                break
            time.sleep(.1)
        self.assertFalse(pid_alive(child))

    def test_filter_attribute_rejected_before_checkout(self):
        repo = self.repo()
        g = GitWorktrees(self.root / "git")
        (repo / ".gitattributes").write_text("value.py filter=fixture\n", encoding="utf-8")
        g.git(repo, "add", ".gitattributes")
        g.git(repo, "commit", "-m", "filter fixture")
        with self.assertRaises(TeamError) as ctx:
            g.inspect(repo)
        self.assertEqual(ctx.exception.kind, "UNSUPPORTED_REPO")

    def test_orphaned_active_child_blocks_resume(self):
        self.setup_flow()
        self.store.update(self.id, data={"active_pid": os.getpid()})
        with self.assertRaises(TeamError) as ctx:
            with self.store.claim(self.id):
                pass
        self.assertEqual(ctx.exception.kind, "ORPHANED_PROCESS")
