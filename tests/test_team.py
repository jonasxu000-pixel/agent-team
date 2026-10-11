from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_team.adapters import Answer, ClaudeAdapter, CodexAdapter
from agent_team.cli import main
from agent_team.gitwork import GitWorktrees
from agent_team.policy import assess, estimate_cost
from agent_team.process import Result, run_process
from agent_team.safety import TeamError, classify_error, clean_env, in_root, relative_file, scrub
from agent_team.schemas import SCHEMAS, validate
from agent_team.store import Store
from agent_team.workflow import DEFAULTS, Workflow


PLAN = {"summary": "set VALUE to two", "steps": ["edit", "test"], "complexity": "low",
        "critical_logic": False, "security": False, "concurrency": False, "independent_review": False,
        "expected_claude_gain": "", "expected_files": ["value.py"], "claude_developer_gain": ""}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-team-")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, expected=2):
        repo = self.root / "项目 repo"
        repo.mkdir()
        g = GitWorktrees(self.root / "git-helper")
        g.git(repo, "init", "-b", "main")
        g.git(repo, "config", "user.name", "Test Fixture")
        g.git(repo, "config", "user.email", "fixture@example.invalid")
        (repo / "value.py").write_text("VALUE = 0\n", encoding="utf-8")
        (repo / "test.py").write_text("from pathlib import Path\nnamespace = {}\nexec(Path('value.py').read_text(), namespace)\n" +
                                      ("assert namespace['VALUE'] >= 1\n" if expected == 1 else "assert namespace['VALUE'] == 2\n"), encoding="utf-8")
        (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        g.git(repo, "add", ".")
        g.git(repo, "commit", "-m", "fixture base")
        return repo


class PolicyTests(unittest.TestCase):
    def config(self, **extra):
        return dict(DEFAULTS, allow_estimated_budget=True, **extra)

    def decide(self, plan=None, config=None, **kw):
        return assess(plan or PLAN, config or self.config(), {"status": "UNKNOWN"}, **kw)

    def test_ordinary_task_never_calls_for_multimodel_slogan(self):
        plan = dict(PLAN, expected_claude_gain="more models might be better")
        self.assertFalse(self.decide(plan).call)

    def test_gpt_only_blocks_high_risk(self):
        result = self.decide(dict(PLAN, security=True, expected_claude_gain="auth bypass review"), self.config(policy="gpt_only"))
        self.assertFalse(result.call)
        self.assertTrue(result.independent_required)

    def test_high_risk_with_specific_gain_calls(self):
        self.assertTrue(self.decide(dict(PLAN, concurrency=True, expected_claude_gain="race between rematch and move")).call)

    def test_no_specific_gain_blocks(self):
        self.assertFalse(self.decide(dict(PLAN, security=True)).call)

    def test_no_paid_authorization_blocks(self):
        self.assertFalse(self.decide(dict(PLAN, security=True, expected_claude_gain="auth"), dict(DEFAULTS)).call)

    def test_failed_twice_can_escalate(self):
        self.assertTrue(self.decide(dict(PLAN, expected_claude_gain="independent failing-case diagnosis"), failures=2).call)

    def test_same_review_does_not_repeat(self):
        self.assertFalse(self.decide(dict(PLAN, security=True, expected_claude_gain="auth"), reviewed=True).call)

    def test_call_count_blocks(self):
        self.assertFalse(self.decide(dict(PLAN, security=True, expected_claude_gain="auth"), calls=1).call)

    def test_global_budget_blocks(self):
        self.assertFalse(self.decide(dict(PLAN, security=True, expected_claude_gain="auth"), global_spent=5).call)

    def test_deep_review_still_has_budget_limit(self):
        result = self.decide(dict(PLAN, complexity="high", expected_claude_gain="independent architecture"),
                             self.config(policy="deep_review"), spent=1)
        self.assertFalse(result.call)

    def test_no_price_is_unknown(self):
        self.assertIsNone(estimate_cost("abc", DEFAULTS))

    def test_user_price_estimate_is_bounded_input_proxy(self):
        self.assertGreater(estimate_cost("abc", dict(DEFAULTS, claude_prices={"input": 3, "output": 15})), 0)


class StoreTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / "state")
        self.id = self.store.create({"request": "test", "repo": "fixture", "config": DEFAULTS})

    def test_reopens_persistent_state(self):
        self.store.update(self.id, data={"commit": "abc"}, status="PAUSED", phase="test")
        other = Store(self.root / "state")
        self.assertEqual(other.task(self.id)["data"]["commit"], "abc")
        self.assertEqual(other.task(self.id)["phase"], "test")

    def test_atomic_call_count_for_concurrent_tasks(self):
        def reserve(_):
            try:
                return self.store.reserve(self.id, "review", DEFAULTS)[0]
            except TeamError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(reserve, range(6)))
        self.assertEqual(sum(x is not None for x in results), 1)

    def test_unknown_cost_retains_reservation(self):
        id, cap = self.store.reserve(self.id, "review", DEFAULTS)
        self.store.finish_call(id, status="TIMEOUT")
        self.assertEqual(self.store.calls(self.id)[0]["charged"], cap)

    def test_client_cost_is_not_actual_bill(self):
        id, _ = self.store.reserve(self.id, "review", DEFAULTS)
        self.store.finish_call(id, status="COMPLETED", cost=0.02)
        self.assertEqual(self.store.calls()[0]["charged"], .02)
        self.assertIsNone(self.store.calls()[0]["payload"]["actual_bill_usd"])

    def test_global_reservations_block_other_task(self):
        config = dict(DEFAULTS, global_claude_budget=.06)
        with self.assertRaises(TeamError):
            self.store.reserve(self.id, "review", config)

    def test_runner_lock_prevents_two_writers(self):
        with self.store.claim(self.id):
            with self.assertRaises(TeamError) as ctx:
                with self.store.claim(self.id):
                    pass
            self.assertEqual(ctx.exception.kind, "ALREADY_RUNNING")

    def test_pause_and_cancel_controls(self):
        self.store.control(self.id, "pause")
        with self.assertRaises(TeamError) as ctx:
            self.store.check_control(self.id)
        self.assertEqual(ctx.exception.kind, "PAUSED")
        self.store.control(self.id, "cancel")
        self.assertEqual(self.store.task(self.id)["status"], "CANCELLED")

    def test_credential_fields_redacted_in_events(self):
        self.store.event(self.id, "test", "output", {"api_key": "secret fixture", "message": "sk-fixture-redaction-1234567890"})
        text = json.dumps(self.store.events(self.id))
        self.assertNotIn("secret fixture", text)
        self.assertNotIn("fixture-redaction-1234567890", text)

    def test_concurrent_atomic_report_writes(self):
        from agent_team.safety import write_json
        target = self.root / "shared-report.json"
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda i: write_json(target, {"index": i, "text": "x" * 20000}), range(24)))
        self.assertIn(json.loads(target.read_text(encoding="utf-8"))["index"], range(24))
        self.assertEqual(list(self.root.glob("*.tmp")), [])


class SafetyTests(Fixture):
    def test_rejects_traversal_drive_git_and_credentials(self):
        for name in ["../x", "D:/x", "/x", ".git/config", ".env", ".claude/settings.json", "x/private.pem"]:
            with self.subTest(name=name), self.assertRaises(TeamError):
                relative_file(name)

    def test_unicode_path_preserved(self):
        self.assertEqual(relative_file("文档/说明.md"), "文档/说明.md")

    def test_source_dirty_and_head_preserved(self):
        repo = self.repo()
        (repo / "value.py").write_text("user changes", encoding="utf-8")
        (repo / "用户未提交.txt").write_text("keep", encoding="utf-8")
        g = GitWorktrees(self.root / "worktrees")
        snapshot = g.inspect(repo)
        cwd = g.ensure(repo, "unique-task", "codex", snapshot["base"])
        self.assertNotEqual(cwd, repo)
        self.assertEqual((cwd / "value.py").read_text(), "VALUE = 0\n")
        (cwd / "value.py").write_text("VALUE = 2\n", encoding="utf-8")
        g.commit(cwd, snapshot["base"], "unique-task", "develop")
        self.assertEqual(g.git(repo, "rev-parse", "HEAD"), snapshot["source_head"])
        self.assertEqual((repo / "value.py").read_text(), "user changes")
        self.assertEqual((repo / "用户未提交.txt").read_text(), "keep")

    def test_unicode_changed_file_committed(self):
        repo = self.repo()
        g = GitWorktrees(self.root / "worktrees")
        base = g.git(repo, "rev-parse", "HEAD")
        cwd = g.ensure(repo, "task", "codex", base)
        (cwd / "中文 文件.txt").write_text("hello", encoding="utf-8")
        head, files = g.commit(cwd, base, "task", "develop")
        self.assertNotEqual(head, base)
        self.assertIn("中文 文件.txt", files)

    def test_delete_pauses_without_commit(self):
        repo = self.repo()
        g = GitWorktrees(self.root / "worktrees")
        base = g.git(repo, "rev-parse", "HEAD")
        cwd = g.ensure(repo, "task", "codex", base)
        (cwd / "value.py").unlink()
        with self.assertRaises(TeamError) as ctx:
            g.commit(cwd, base, "task", "develop")
        self.assertEqual(ctx.exception.kind, "NEEDS_APPROVAL")
        self.assertEqual(g.git(cwd, "rev-parse", "HEAD"), base)

    def test_env_removes_paid_openai_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "testsecret", "CODEX_API_KEY": "testsecret", "ANTHROPIC_AUTH_TOKEN": "testsecret"}):
            env = clean_env()
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("CODEX_API_KEY", env)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
            self.assertEqual(env["AGENT_TEAM_CHILD"], "1")

    def test_quota_not_network_or_generic_429(self):
        cases = {"insufficient_quota 429": "QUOTA_EXHAUSTED", "connection timeout": "TEMPORARY_FAILURE",
                 "429 too many requests": "RATE_LIMITED", "credit balance is too low": "QUOTA_EXHAUSTED",
                 "authentication_error 401": "AUTH_ERROR"}
        for message, expected in cases.items():
            self.assertEqual(classify_error(message, agent="codex"), expected)

    def test_recursive_cli_blocked(self):
        with patch.dict(os.environ, {"AGENT_TEAM_CHILD": "1"}), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--state-dir", str(self.root), "status"]), 2)


class ProcessTests(Fixture):
    def test_real_process_exit_and_output(self):
        result = run_process([sys.executable, "-c", "import sys; print('actual output'); sys.exit(7)"], self.root)
        self.assertEqual(result.code, 7)
        self.assertIn("actual output", result.stdout)

    def test_timeout_terminates(self):
        with self.assertRaises(TeamError) as ctx:
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], self.root, timeout=.15)
        self.assertEqual(ctx.exception.kind, "TIMEOUT")

    def test_pause_interrupts_process(self):
        def paused():
            raise TeamError("PAUSED", "pause test")
        with self.assertRaises(TeamError) as ctx:
            run_process([sys.executable, "-c", "import time; time.sleep(30)"], self.root, control=paused)
        self.assertEqual(ctx.exception.kind, "PAUSED")

    def test_noninteractive_stdin(self):
        result = run_process([sys.executable, "-c", "import sys; print(sys.stdin.read())"], self.root, stdin="中文 task\n")
        self.assertIn("中文 task", result.stdout)

    def test_output_limit(self):
        with self.assertRaises(TeamError) as ctx:
            run_process([sys.executable, "-c", "print('x'*100000)"], self.root, output_limit=1000)
        self.assertEqual(ctx.exception.kind, "OUTPUT_LIMIT")


class AdapterTests(Fixture):
    def test_codex_chatgpt_only_flags(self):
        adapter = CodexAdapter(binary="codex.exe")
        argv = adapter.command(self.root, "develop", self.root / "s.json", self.root / "a.json", DEFAULTS)
        text = " ".join(argv)
        self.assertIn('forced_login_method="chatgpt"', text)
        self.assertIn("workspace-write", text)
        self.assertIn("--ignore-user-config", text)
        self.assertNotIn("dangerously", text)

    def test_api_key_login_refused(self):
        adapter = CodexAdapter(binary="codex.exe", runner=lambda *a, **kw: Result(0, "Logged in using an API key", "", .1))
        with self.assertRaises(TeamError) as ctx:
            adapter.login(self.root)
        self.assertEqual(ctx.exception.kind, "AUTH_ERROR")

    def test_claude_toolless_permissions_and_budget(self):
        config = dict(DEFAULTS, allow_estimated_budget=True)
        argv = ClaudeAdapter(binary="claude.exe").command("review", config, .4)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--safe-mode", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("bypassPermissions", argv)
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.400000")

    def test_opus_not_automatic(self):
        with self.assertRaises(TeamError):
            ClaudeAdapter(binary="claude.exe").command("review", dict(DEFAULTS, allow_estimated_budget=True, claude_model="opus"), .4)

    def test_claude_envelope_parsed_and_cost_labelled(self):
        envelope = {"subtype": "success", "is_error": False, "structured_output": {"summary": "review", "findings": []},
                    "total_cost_usd": .03, "usage": {"input_tokens": 4}, "session_id": "mock"}
        def runner(argv, cwd, **kw):
            if "--help" in argv:
                return Result(0, "--max-budget-usd --json-schema --no-session-persistence --safe-mode --tools --strict-mcp-config --permission-prompts", "", .1)
            return Result(0, json.dumps(envelope), "", .1)
        result = ClaudeAdapter(binary="claude.exe", runner=runner).call(self.root, "review", "fixture", self.root / "out",
            dict(DEFAULTS, allow_estimated_budget=True), cap=.4)
        self.assertEqual(result.cost, .03)
        self.assertEqual(result.data["findings"], [])

    def test_cost_nan_is_unknown(self):
        self.assertIsNone(ClaudeAdapter.cost({"total_cost_usd": float("nan")}))

    def test_strict_output_rejects_missing_extra_and_bool_int(self):
        for data in [{"summary": "x"}, {"summary": "x", "findings": [], "extra": "x"}]:
            with self.assertRaises(TeamError):
                validate(data, SCHEMAS["review"])


class FakeCodex:
    def __init__(self, *, high=False, bug=False, failure=None, false_positive=False, unresolved=False, bad_evidence=False):
        self.high, self.bug, self.failure = high, bug, failure
        self.false_positive, self.unresolved, self.bad_evidence = false_positive, unresolved, bad_evidence
        self.calls = []

    def call(self, cwd, role, prompt, artifact_dir, config, **kwargs):
        self.calls.append((role, str(cwd), prompt))
        if self.failure and role == "develop":
            error, self.failure = self.failure, None
            raise TeamError(error, "simulated failure")
        if role == "plan":
            result = dict(PLAN, critical_logic=self.high, expected_claude_gain="independent value invariant" if self.high else "",
                          claude_developer_gain="isolated algorithm module" if config.get("claude_developer") else "")
        elif role in {"develop", "fix"}:
            value = 1 if role == "develop" and self.bug else 2
            target = cwd / "value.py"
            previous = target.read_text()
            target.write_text(f"VALUE = {value}\n", encoding="utf-8")
            result = {"summary": "set value", "changed_files": ["value.py"] if previous != target.read_text() else [],
                      "test_notes": "orchestrator runs tests", "remaining": []}
        elif role == "verify":
            verdict = "unresolved" if self.unresolved else "false_positive" if self.false_positive else "confirmed"
            result = {"summary": "verified source", "assessments": [{"id": "F1", "verdict": verdict,
                "evidence": "VALUE must equal 2", "reproduction": "read VALUE",
                "evidence_file": "value.py", "evidence_excerpt": "NOT IN FILE" if self.bad_evidence else "VALUE = 1"}]}
            if '"findings": []' in prompt:
                result = {"summary": "received clean Claude report and verified the change", "assessments": []}
        elif role == "final":
            result = {"summary": "actual artifact present", "acceptance_met": True, "evidence": ["value.py exists"], "remaining": []}
        else:
            raise AssertionError(role)
        return Answer(result, {"input_tokens": 1, "output_tokens": 1}, .01)


class FakeClaude:
    def __init__(self, findings=False, failure=None):
        self.findings, self.failure = findings, failure
        self.calls = []

    def call(self, cwd, role, prompt, artifact_dir, config, **kwargs):
        self.calls.append((role, str(cwd), prompt))
        if self.failure:
            raise TeamError(self.failure, "simulated Claude outage")
        if role == "claude_develop":
            result = {"summary": "algorithm", "files": [{"path": "algorithm.py", "content": "def algorithm():\n    return 2\n"}],
                      "test_notes": "Codex integration tests required", "remaining": []}
        else:
            findings = [{"id": "F1", "severity": "high", "file": "value.py", "line": 1,
                         "claim": "incorrect invariant", "evidence": "VALUE = 1", "reproduction": "read VALUE"}] if self.findings else []
            result = {"summary": "independent mock review", "findings": findings}
        return Answer(result, {"input_tokens": 1}, .01, .02)


class WorkflowTests(Fixture):
    def setup_workflow(self, *, high=False, bug=False, failure=None, claude_failure=None, findings=False,
                       expected=2, config=None, **codex_options):
        repo = self.repo(expected)
        self.store = Store(self.root / "state")
        self.codex = FakeCodex(high=high, bug=bug, failure=failure, **codex_options)
        self.claude = FakeClaude(findings, claude_failure)
        self.flow = Workflow(self.store, codex=self.codex, claude=self.claude, trusted_test_fixture=True)
        options = {**DEFAULTS, "allow_estimated_budget": True, "test_commands": [[sys.executable, "test.py"]], **(config or {})}
        self.id = self.flow.create(repo, "set value to two", options)
        self.repo_path = repo
        self.base = self.flow.git.git(repo, "rev-parse", "HEAD")

    def run_flow(self):
        with redirect_stdout(io.StringIO()):
            return self.flow.run(self.id)

    def test_gpt_only_real_git_tests_and_artifact(self):
        self.setup_workflow(config={"policy": "gpt_only"})
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(len(result["data"]["tests"]), 2)
        self.assertTrue(all(t["exit_code"] == 0 for t in result["data"]["tests"]))
        self.assertEqual(self.flow.git.git(self.repo_path, "rev-parse", "HEAD"), self.base)
        self.assertNotEqual(result["data"]["codex_worktree"], result["data"]["integration_worktree"])

    def test_ordinary_selective_skips_claude(self):
        self.setup_workflow()
        self.assertEqual(self.run_flow()["status"], "READY_FOR_MERGE")
        self.assertEqual(self.claude.calls, [])

    def test_high_risk_triggers_review_with_fixed_diff(self):
        self.setup_workflow(high=True)
        self.assertEqual(self.run_flow()["status"], "READY_FOR_MERGE")
        self.assertEqual(len(self.claude.calls), 1)
        self.assertIn("diff --git", self.claude.calls[0][2])
        self.assertNotEqual(self.claude.calls[0][1], self.codex.calls[1][1])

    def test_clean_claude_review_is_handed_to_codex(self):
        self.setup_workflow(high=True)
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        verifiers = [call for call in self.codex.calls if call[0] == "verify"]
        self.assertEqual(len(verifiers), 1)
        self.assertIn('"summary": "independent mock review"', verifiers[0][2])
        self.assertIn('"findings": []', verifiers[0][2])
        self.assertEqual(result["data"]["verifications"][0]["result"]["assessments"], [])
        final_input = next(call[2] for call in self.codex.calls if call[0] == "final")
        self.assertIn('"actual_cli_receipts"', final_input)
        self.assertIn('"summary": "independent mock review"', final_input)
        self.assertIn('"codex_verifications"', final_input)

    def test_review_handoff_validation_and_fix(self):
        self.setup_workflow(high=True, bug=True, findings=True, expected=1,
                            config={"claude_max_calls": 2, "claude_max_reviews": 2})
        # Second review should be clean once the repaired source reaches VALUE=2.
        original = self.claude.call
        def review(*args, **kw):
            self.claude.findings = len(self.claude.calls) == 0
            return original(*args, **kw)
        self.claude.call = review
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        roles = [c[0] for c in self.codex.calls]
        self.assertIn("verify", roles)
        self.assertIn("fix", roles)
        self.assertIn("Untrusted Claude report", next(c[2] for c in self.codex.calls if c[0] == "verify"))
        self.assertEqual((Path(result["data"]["integration_worktree"]) / "value.py").read_text(), "VALUE = 2\n")

    def test_false_positive_never_fixes(self):
        self.setup_workflow(high=True, findings=True, false_positive=True)
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertNotIn("fix", [c[0] for c in self.codex.calls])

    def test_unresolved_stops(self):
        self.setup_workflow(high=True, findings=True, unresolved=True)
        result = self.run_flow()
        self.assertEqual(result["status"], "NEEDS_ATTENTION")
        self.assertEqual(result["data"]["last_error"]["kind"], "UNRESOLVED_REVIEW")

    def test_fabricated_source_excerpt_never_fixes(self):
        self.setup_workflow(high=True, findings=True, bad_evidence=True)
        result = self.run_flow()
        self.assertEqual(result["data"]["last_error"]["kind"], "INVALID_EVIDENCE")
        self.assertNotIn("fix", [c[0] for c in self.codex.calls])

    def test_budget_zero_degrades_and_marks_pending(self):
        self.setup_workflow(high=True, config={"claude_budget": 0})
        result = self.run_flow()
        self.assertEqual(result["status"], "AWAITING_REVIEW")
        self.assertEqual(self.claude.calls, [])
        self.assertTrue(result["data"]["independent_pending"])

    def test_no_budget_authorization_degrades(self):
        self.setup_workflow(high=True, config={"allow_estimated_budget": False})
        self.assertEqual(self.run_flow()["status"], "AWAITING_REVIEW")
        self.assertEqual(self.claude.calls, [])

    def test_claude_quota_does_not_take_over(self):
        self.setup_workflow(high=True, claude_failure="QUOTA_EXHAUSTED")
        self.assertEqual(self.run_flow()["status"], "AWAITING_REVIEW")
        self.assertEqual(len(self.claude.calls), 1)
        self.assertIn("final", [c[0] for c in self.codex.calls])

    def test_codex_quota_saves_and_no_claude_takeover(self):
        self.setup_workflow(high=True, failure="QUOTA_EXHAUSTED")
        result = self.run_flow()
        self.assertEqual(result["status"], "PAUSED_QUOTA")
        self.assertEqual(result["phase"], "develop")
        self.assertEqual(self.claude.calls, [])
        self.assertTrue(result["data"]["snapshots"])

    def test_manual_recovery_reuses_plan_and_finishes(self):
        self.setup_workflow(failure="QUOTA_EXHAUSTED")
        self.run_flow()
        self.store.set_health("codex", "UNKNOWN", source="simulated explicit recovery")
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertEqual(sum(c[0] == "plan" for c in self.codex.calls), 1)

    def test_network_transient_retries_once_then_finishes(self):
        self.setup_workflow(failure="TEMPORARY_FAILURE")
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertEqual(sum(c[0] == "develop" for c in self.codex.calls), 2)

    def test_test_failure_gets_minimal_repair(self):
        self.setup_workflow(bug=True)
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertEqual(result["data"]["repair_rounds"], 1)
        self.assertEqual(result["data"]["tests"][0]["exit_code"], 1)
        self.assertEqual(result["data"]["tests"][-1]["exit_code"], 0)

    def test_timeout_status_retains_workspace(self):
        self.setup_workflow(failure="TIMEOUT")
        result = self.run_flow()
        self.assertEqual(result["status"], "PAUSED_TIMEOUT")
        self.assertTrue(Path(result["data"]["codex_worktree"]).exists())

    def test_claude_developer_has_own_worktree_and_handoff_commit(self):
        self.setup_workflow(high=True, config={"claude_max_calls": 2, "claude_developer": {
            "request": "implement independent algorithm", "files": ["algorithm.py"], "acceptance": "returns 2"}})
        result = self.run_flow()
        self.assertEqual(result["status"], "READY_FOR_MERGE")
        self.assertNotEqual(result["data"]["claude_worktree"], result["data"]["codex_worktree"])
        self.assertTrue((Path(result["data"]["integration_worktree"]) / "algorithm.py").exists())
        self.assertTrue(any(c["agent"] == "claude" for c in result["data"]["commits"]))

    def test_report_captures_tokens_cost_and_unchanged_main(self):
        self.setup_workflow(high=True)
        result = self.run_flow()
        path = self.store.root / "tasks" / self.id / "report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(report["actual_claude_bill_usd"])
        self.assertEqual(report["claude_calls"][0]["payload"]["usage"]["input_tokens"], 1)
        self.assertEqual(self.flow.git.git(self.repo_path, "branch", "--show-current"), "main")
        self.assertEqual((self.repo_path / "value.py").read_text(), "VALUE = 0\n")


if __name__ == "__main__":
    unittest.main()
