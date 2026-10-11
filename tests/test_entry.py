from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from test_team import Fixture, FakeCodex, FakeClaude
from agent_team.entry import inspect_project, main, prepare_run, read_tests
from agent_team.store import Store
from agent_team.workflow import DEFAULTS, Workflow
from agent_team.safety import TeamError
from agent_team.adapters import CodexAdapter, Answer


class EntryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo()
        self.profiles = self.root / "profiles"

    def configure(self):
        f = self.root / "approved-tests.json"
        f.write_text(json.dumps([["python", "test.py"]]), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--cwd", str(self.repo_path), "--profiles-dir", str(self.profiles), "configure", "--tests-file", str(f)]), 0)

    def test_nested_directory_detects_git_root(self):
        sub = self.repo_path / "nested"
        sub.mkdir()
        info = inspect_project(sub, profiles=self.profiles)
        self.assertEqual(Path(info["repo"]), self.repo_path)

    def test_profiles_are_per_repository_not_shared_defaults(self):
        self.configure()
        other = self.root / "other"
        other.mkdir()
        self.flow_git = __import__('agent_team.gitwork', fromlist=['GitWorktrees']).GitWorktrees(self.root / "git")
        self.flow_git.git(other, "init", "-b", "main")
        self.flow_git.git(other, "config", "user.name", "Fixture")
        self.flow_git.git(other, "config", "user.email", "fixture@example.invalid")
        (other / "README.md").write_text("other", encoding="utf-8")
        self.flow_git.git(other, "add", ".")
        self.flow_git.git(other, "commit", "-m", "base")
        first, second = inspect_project(self.repo_path, profiles=self.profiles), inspect_project(other, profiles=self.profiles)
        self.assertTrue(first["tests_ready"])
        self.assertFalse(second["tests_ready"])
        self.assertNotEqual(first["project_id"], second["project_id"])

    def test_local_profile_writes_no_project_file(self):
        before = sorted(p.name for p in self.repo_path.iterdir())
        self.configure()
        self.assertEqual(before, sorted(p.name for p in self.repo_path.iterdir()))

    def test_project_config_takes_precedence(self):
        self.configure()
        (self.repo_path / ".agent-team.json").write_text(json.dumps({"version": 1, "test_commands": [["python", "-m", "unittest"]]}))
        info = inspect_project(self.repo_path, profiles=self.profiles)
        self.assertEqual(info["test_commands"], [["python", "-m", "unittest"]])

    def test_project_config_cannot_authorize_paid_calls(self):
        file = self.repo_path / ".agent-team.json"
        file.write_text(json.dumps({"test_commands": [["python", "test.py"]], "allow_estimated_budget": True}))
        with self.assertRaises(TeamError):
            read_tests(file)

    def test_missing_tests_blocks_dispatch(self):
        info = inspect_project(self.repo_path, profiles=self.profiles)
        with self.assertRaises(TeamError) as ctx:
            prepare_run(info, "fix", "selective")
        self.assertEqual(ctx.exception.kind, "TEST_CONFIG_REQUIRED")

    def test_missing_or_uncommitted_test_script_is_not_ready(self):
        file = self.repo_path / ".agent-team.json"
        file.write_text(json.dumps({"test_commands": [["python", "missing.py"]]}))
        self.assertFalse(inspect_project(self.repo_path, profiles=self.profiles)["tests_ready"])
        (self.repo_path / "missing.py").write_text("print('uncommitted')")
        info = inspect_project(self.repo_path, profiles=self.profiles)
        self.assertFalse(info["tests_ready"])
        self.assertTrue(info["unavailable_test_files"][0]["absent_from_base_commit"])

    def test_explicit_no_tests_does_not_run_cached_commands(self):
        self.configure()
        m = prepare_run(inspect_project(self.repo_path, profiles=self.profiles), "static review", "review", no_tests=True)
        self.assertIn("--no-tests", m["cli_args"])
        self.assertNotIn("--test-command", m["cli_args"])

    def test_explicit_no_tests_can_be_previewed(self):
        info = inspect_project(self.repo_path, profiles=self.profiles)
        self.assertIn("--no-tests", prepare_run(info, "review", "review", no_tests=True)["cli_args"])

    def test_gpt_only_and_selective_never_implicitly_pay(self):
        self.configure()
        info = inspect_project(self.repo_path, profiles=self.profiles)
        for mode in ("gpt_only", "selective", "review"):
            m = prepare_run(info, "task", mode)
            self.assertFalse(m["paid_claude_authorized"])
            self.assertNotIn("--allow-estimated-budget", m["cli_args"])

    def test_only_explicit_per_task_budget_authorizes(self):
        self.configure()
        m = prepare_run(inspect_project(self.repo_path, profiles=self.profiles), "review", "review", approved_budget=.3)
        self.assertTrue(m["paid_claude_authorized"])
        self.assertIn("--review-only", m["cli_args"])
        self.assertIn("--allow-estimated-budget", m["cli_args"])

    def test_gpt_only_refuses_paid_flag(self):
        self.configure()
        with self.assertRaises(TeamError):
            prepare_run(inspect_project(self.repo_path, profiles=self.profiles), "task", "gpt_only", approved_budget=.2)

    def test_agent_environment_blocks_recursion(self):
        self.configure()
        with patch.dict(os.environ, {"AGENT_TEAM_CHILD": "1"}):
            info = inspect_project(self.repo_path, profiles=self.profiles)
            with self.assertRaises(TeamError) as ctx:
                prepare_run(info, "task", "gpt_only")
            self.assertEqual(ctx.exception.kind, "RECURSION_BLOCKED")

    def test_managed_branch_blocks_recursion_without_environment_flag(self):
        from agent_team.gitwork import GitWorktrees
        g = GitWorktrees(self.root / "git")
        g.git(self.repo_path, "checkout", "-b", "codex/team/1234567890abcdef/codex")
        self.assertTrue(inspect_project(self.repo_path, profiles=self.profiles)["nested_orchestrator_blocked"])

    def test_real_cli_queue_reuses_current_project_and_no_fee(self):
        self.configure()
        state = self.root / "state"
        args = ["--cwd", str(self.repo_path), "--profiles-dir", str(self.profiles), "--state-dir", str(state),
                "run", "--mode", "selective", "--request", "fixture task", "--queue"]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
        store = Store(state)
        task = store.task(store.tasks()[0]["id"])
        self.assertEqual(Path(task["repo"]), self.repo_path)
        self.assertFalse(task["config"]["allow_estimated_budget"])
        self.assertEqual(store.calls(task["id"]), [])
        self.assertTrue(task["data"]["queued_only"])

    def test_queue_resume_authority_never_changes_paid_review_identity(self):
        from agent_team.gitwork import GitWorktrees
        store = Store(self.root / "state")
        flow = Workflow(store, trusted_test_fixture=True)
        task_id = flow.create(self.repo_path, "只排队，随后继续开发", DEFAULTS)
        task = store.task(task_id)
        old_review = flow.prompt(task, "review", "same diff")
        task["data"]["queued_execution_resumed"] = True
        self.assertEqual(old_review, flow.prompt(task, "review", "same diff"))
        self.assertNotEqual(flow.prompt(store.task(task_id), "develop"), flow.prompt(task, "develop"))

    def test_project_override_file_is_reported(self):
        (self.repo_path / "AGENTS.md").write_text("base")
        (self.repo_path / "AGENTS.override.md").write_text("override")
        info = inspect_project(self.repo_path, profiles=self.profiles)
        self.assertEqual(info["instruction_files"], [str(self.repo_path / "AGENTS.override.md")])

    def test_readonly_adapter_restricts_developer(self):
        argv = CodexAdapter(binary="codex.exe").command(self.repo_path, "develop", self.root / "s", self.root / "a",
                                                         dict(DEFAULTS, review_only=True))
        self.assertEqual(argv[argv.index("--sandbox") + 1], "read-only")


class ReadOnlyWorkflowTests(Fixture):
    def test_confirmed_review_findings_and_failed_tests_never_trigger_fix(self):
        repo = self.repo()
        (repo / "value.py").write_text("VALUE = 1\n")
        from agent_team.gitwork import GitWorktrees
        g = GitWorktrees(self.root / "git")
        g.git(repo, "add", "value.py")
        g.git(repo, "commit", "-m", "fixture existing defect")
        store = Store(self.root / "state")
        class ReadCodex(FakeCodex):
            def call(self, cwd, role, prompt, artifact_dir, config, **kw):
                if role == "develop":
                    self.calls.append((role, str(cwd), prompt))
                    return Answer({"summary": "read-only existing code audit", "changed_files": [], "test_notes": "inspect", "remaining": []}, {}, .01)
                return super().call(cwd, role, prompt, artifact_dir, config, **kw)
        codex, claude = ReadCodex(high=True), FakeClaude(findings=True)
        flow = Workflow(store, codex=codex, claude=claude, trusted_test_fixture=True)
        task = flow.create(repo, "audit only", dict(DEFAULTS, review_only=True, allow_estimated_budget=True,
                                                   test_commands=[[__import__('sys').executable, "test.py"]]))
        with redirect_stdout(io.StringIO()):
            result = flow.run(task)
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertNotIn("fix", [c[0] for c in codex.calls])
        self.assertTrue(result["data"]["review_only_test_failures"])
        self.assertEqual((Path(result["data"]["codex_worktree"]) / "value.py").read_text(), "VALUE = 1\n")
        self.assertEqual(len(result["data"]["review_only_findings"]), 1)
