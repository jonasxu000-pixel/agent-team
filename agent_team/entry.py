"""User-level Codex entry: discover a Git project, inspect tests, reuse the existing CLI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from .safety import TeamError, finite_positive, scrub, write_json


def git(cwd, *args, check=True):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if check and result.returncode:
        raise TeamError("NOT_GIT_PROJECT", "Current directory is not a usable Git project")
    return result.stdout.strip()


def profile_root():
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AgentTeam" / "projects"


def test_commands(value):
    if not isinstance(value, list) or not value:
        raise TeamError("TEST_CONFIG_ERROR", "Test config must contain nonempty argv arrays")
    for command in value:
        if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
            raise TeamError("TEST_CONFIG_ERROR", "Each test must be an argv string array")
        if Path(command[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise TeamError("TEST_CONFIG_ERROR", "Use native node/python executables instead of shell wrappers")
    return value


def read_tests(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if isinstance(value, dict):
        if set(value) - {"version", "test_commands", 'claude_budget'}:
            raise TeamError("TEST_CONFIG_ERROR", "Project test config cannot set permissions, providers or fee authorization")
        value = value.get("test_commands")
    return test_commands(value)


def instruction_paths(repo, cwd):
    directories = [repo]
    if cwd.is_relative_to(repo):
        relative = cwd.relative_to(repo)
        for index in range(1, len(relative.parts) + 1):
            directories.append(repo.joinpath(*relative.parts[:index]))
    selected = []
    for directory in directories:
        for name in ("AGENTS.override.md", "AGENTS.md"):
            p = directory / name
            if p.is_file() and p.stat().st_size:
                selected.append(str(p))
                break
    return selected


def candidates(repo):
    result = []
    package = repo / "package.json"
    if package.is_file():
        data = json.loads(package.read_text(encoding="utf-8-sig"))
        script = data.get("scripts", {}).get("test")
        if script and "no test specified" not in script.lower():
            node = shutil.which("node.exe" if os.name == "nt" else "node")
            npm = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js" if node else None
            if npm and npm.is_file():
                result.append({"source": "package.json scripts.test", "argv": ["node", str(npm), "test"],
                               "script": script, "review_before_registering": True})
    tests = repo / "tests"
    if tests.is_dir() and next(tests.glob("test_*.py"), None):
        result.append({"source": "tests/test_*.py", "argv": ["python", "-m", "unittest", "discover", "-s", "tests"],
                       "review_before_registering": True})
    return result


def inspect_project(cwd, *, profiles=None, explicit_tests=None):
    cwd = Path(cwd).resolve()
    repo = Path(git(cwd, "rev-parse", "--show-toplevel")).resolve()
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    identity = hashlib.sha256(os.path.normcase(str(common)).encode()).hexdigest()[:24]
    local_profile = Path(profiles or profile_root()) / identity / "tests.json"
    branch = git(repo, "symbolic-ref", "--short", "HEAD", check=False)
    managed_path = (repo.parent.parent.name == "worktrees" and len(repo.parent.name) == 16
                    and all(c in "0123456789abcdef" for c in repo.parent.name)
                    and (repo.parent.parent.parent / "state.sqlite3").is_file())
    nested = bool(os.environ.get("AGENT_TEAM_CHILD")) or branch.startswith("codex/team/") or managed_path
    test_file = Path(explicit_tests).resolve() if explicit_tests else repo / ".agent-team.json"
    source = "explicit" if explicit_tests else "project .agent-team.json"
    if not test_file.is_file() and not explicit_tests:
        test_file, source = local_profile, "local profile for this Git repository"
    commands = read_tests(test_file) if test_file.is_file() else []
    project_budget = None
    if test_file.is_file():
        obj = json.loads(test_file.read_text(encoding='utf-8-sig'))
        if isinstance(obj, dict) and 'claude_budget' in obj:
            project_budget = finite_positive(obj['claude_budget'], 'project budget', zero=True)
    unavailable = []
    for command in commands:
        if len(command) > 1 and not command[1].startswith("-") and Path(command[1]).suffix.lower() in {".py", ".js", ".mjs", ".cjs"}:
            script = Path(command[1])
            script = script if script.is_absolute() else repo / script
            missing = not script.is_file()
            not_committed = False
            if script.is_relative_to(repo):
                rel = script.relative_to(repo).as_posix()
                not_committed = not bool(git(repo, "ls-tree", "--name-only", "HEAD", "--", rel, check=False))
            if missing or not_committed:
                unavailable.append({"path": str(script), "missing": missing, "absent_from_base_commit": not_committed})
    status = git(repo, "status", "--porcelain=v1", "-uall")
    return {"repo": str(repo), "cwd": str(cwd), "base": git(repo, "rev-parse", "HEAD"),
            "project_id": identity, "common_git_dir": str(common), "branch": branch,
            "instruction_files": instruction_paths(repo, cwd), "source_status": status,
            "uncommitted_excluded": bool(status), "nested_orchestrator_blocked": nested,
            "test_config_source": source if commands else "UNKNOWN", "test_config_path": str(test_file) if commands else None,
            "test_commands": commands, "tests_ready": bool(commands) and not unavailable, "unavailable_test_files": unavailable,
            "test_candidates": candidates(repo) if not commands else [],
            "profile_path": str(local_profile), 'project_claude_budget': project_budget}


def prepare_run(context, request, mode, *, approved_budget=None, no_tests=False, background=False, queue=False, state_dir=None,
                use_default_budget=False, task_budget=None, watch=False, unattended=False, task_type='auto', claude_role='auto'):
    if context["nested_orchestrator_blocked"]:
        raise TeamError("RECURSION_BLOCKED", "An agent-team child/worktree must execute its assigned role directly")
    if not context["tests_ready"] and not no_tests:
        raise TeamError("TEST_CONFIG_REQUIRED", "Review this project's tests and register them; another project's tests are never reused")
    if watch and queue:
        raise TeamError('CONFIG_ERROR', 'Queued tasks do not execute; use resume --watch later')
    if approved_budget is not None:
        finite_positive(approved_budget, "approved Claude budget")
        if mode == "gpt_only":
            raise TeamError("CONFIG_ERROR", "GPT-only never accepts paid Claude authorization")
    from .user_config import load
    defaults = load(Path(state_dir) if state_dir else profile_root().parent)
    default_limit = defaults['unattended_task_budget'] if unattended else defaults['default_task_budget']
    use_default_budget = use_default_budget or unattended
    budget = approved_budget if approved_budget is not None else task_budget if task_budget is not None else default_limit
    if context.get('project_claude_budget') is not None:
        budget = min(budget, context['project_claude_budget'])
    policy = "gpt_only" if mode == "gpt_only" else "deep_review" if mode == "review" else "selective"
    prefix = ""
    if mode == "review":
        prefix = ("本任务为只读独立 AI 审查：所有角色都不得修改文件；只记录确认问题和待人工实施事项，不自动修复。"
                  "规划 independent_review=true，并说明用户要求的独立核查收益。审查是否完成与问题是否已修复分开判断。\n")
    args = ["run", "--repo", context["repo"], "--base", context["base"], "--request", prefix + request, "--policy", policy]
    args += ['--task-type', task_type, '--claude-role', claude_role]
    if unattended:
        args.append('--unattended')
    if mode == "review":
        args.extend(["--risk", "high", "--review-only"])
    if no_tests:
        args.append("--no-tests")
    elif context["test_config_path"]:
        for command in context["test_commands"]:
            args.extend(["--test-command", json.dumps(command, ensure_ascii=False)])
    if approved_budget is not None:
        args.extend(["--allow-estimated-budget", "--claude-budget", str(budget)])
    elif use_default_budget and mode != 'gpt_only':
        args.extend(['--use-default-budget', '--claude-budget', str(budget)])
    authorized = approved_budget is not None or (use_default_budget and mode != 'gpt_only' and budget <= default_limit and defaults['default_budget_enabled'])
    if background:
        args.append("--background")
    if watch:
        args.append('--watch')
    if queue:
        args.append("--queue")
    if state_dir:
        args = ["--state-dir", str(Path(state_dir).resolve()), *args]
    return {"mode": mode, "policy": policy, "repo": context["repo"], "base": context["base"],
            "paid_claude_authorized": authorized, "claude_budget": 0 if mode == 'gpt_only' else budget if authorized or use_default_budget else None,
            "read_only": mode == "review", "test_config_source": context["test_config_source"], "cli_args": args}


def main(argv=None):
    p = argparse.ArgumentParser(prog="agent-team-entry", description="Discover the current Git project and reuse agent-team")
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.add_argument("--profiles-dir", type=Path)
    p.add_argument("--state-dir", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("context", help="Read-only project and test discovery")
    configure = sub.add_parser("configure", help="Save approved tests in this repository's local profile; does not edit the repo")
    configure.add_argument("--tests-file", type=Path, required=True)
    for name in ("plan", "run"):
        action = sub.add_parser(name, help="Preview existing CLI arguments" if name == "plan" else "Run existing orchestrator")
        action.add_argument("--mode", choices=["gpt_only", "selective", "review"], default="selective")
        request = action.add_mutually_exclusive_group(required=True)
        request.add_argument("--request")
        request.add_argument("--request-file", type=Path)
        action.add_argument("--tests-file", type=Path)
        action.add_argument("--no-tests", action="store_true")
        action.add_argument("--approve-claude-budget", type=float, help="Use only when the user explicitly approved this task's paid budget")
        action.add_argument('--use-default-budget', action='store_true', help='Explicit natural-language multi-Agent intent uses saved default authorization')
        action.add_argument('--task-budget', type=float, help='Smaller task override; larger value requires --approve-claude-budget')
        action.add_argument("--background", action="store_true")
        action.add_argument('--watch', action='store_true', help='Automatically attach live output when starting the task')
        action.add_argument('--unattended', action='store_true', help='Explicit unattended authorization; never inferred from task complexity')
        action.add_argument('--task-type', choices=['auto', 'general', 'ui_ux', 'architecture', 'algorithm', 'debug', 'review'], default='auto')
        action.add_argument('--claude-role', choices=['auto', 'frontend', 'architect', 'developer', 'analyst', 'reviewer'], default='auto')
        action.add_argument("--queue", action="store_true")
    args = p.parse_args(argv)
    try:
        context = inspect_project(args.cwd, profiles=args.profiles_dir, explicit_tests=getattr(args, "tests_file", None) if args.command in {"plan", "run"} else None)
        if args.command == "configure":
            commands = read_tests(args.tests_file)
            write_json(Path(context["profile_path"]), {"version": 1, "test_commands": commands})
            context = inspect_project(args.cwd, profiles=args.profiles_dir)
        if args.command in {"context", "configure"}:
            print(json.dumps(scrub(context), ensure_ascii=False, indent=2))
            return 0
        request = args.request or args.request_file.read_text(encoding="utf-8-sig")
        manifest = prepare_run(context, request, args.mode, approved_budget=args.approve_claude_budget,
                               no_tests=args.no_tests, background=args.background, queue=args.queue, state_dir=args.state_dir,
                               use_default_budget=args.use_default_budget, task_budget=args.task_budget, watch=args.watch,
                               unattended=args.unattended, task_type=args.task_type, claude_role=args.claude_role)
        if args.command == "plan":
            print(json.dumps(scrub(manifest), ensure_ascii=False, indent=2))
            return 0
        from .cli import main as existing_cli
        return existing_cli(manifest["cli_args"])
    except (TeamError, OSError, ValueError) as error:
        print(str(scrub(f"{getattr(error, 'kind', 'INPUT_ERROR')}: {error}")), file=sys.stderr)
        return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
