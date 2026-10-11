from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlparse

from . import __version__
from .adapters import ClaudeAdapter, CodexAdapter, executable
from .process import run_process
from .safety import TeamError, clean_env, finite_positive, relative_file, scrub, write_json, pid_alive
from .store import Store
from .workflow import DEFAULTS, Workflow


def parser():
    p = argparse.ArgumentParser(prog="agent-team", description="GPT 主导的本地 Git 协调器；不会合并原分支、部署或切换 API 计费")
    p.add_argument("--state-dir", type=Path, default=Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AgentTeam")
    p.add_argument("--version", action="version", version=__version__)
    subs = p.add_subparsers(dest="command", required=True)
    doctor = subs.add_parser("doctor", help="检查本机能力和认证方式，不发起模型推理")
    doctor.add_argument("--probe-sandbox", action="store_true", help="运行一次无网络、无模型的本地沙箱探针")
    doctor.add_argument("--quota", action="store_true", help="查询官方 App Server 额度窗口，不发起推理或购买额度")
    run = subs.add_parser("run", help="创建并执行一次隔离任务")
    run.add_argument("--repo", type=Path, required=True)
    request = run.add_mutually_exclusive_group(required=True)
    request.add_argument("--request")
    request.add_argument("--request-file", type=Path)
    run.add_argument("--base", default="HEAD", help="固定基准 commit；未提交文件不复制")
    run.add_argument("--policy", choices=["gpt_only", "selective", "deep_review"], default='selective')
    run.add_argument("--risk", choices=["auto", "normal", "high"], default="auto")
    run.add_argument("--test-command", action="append", default=[], metavar="JSON_ARGV", help='用户审核过的测试数组，如 ["node","scripts/verify-gomoku-ai.js"]')
    run.add_argument("--tests-file", type=Path, help="JSON 测试数组文件；避免 Windows 命令行引号问题")
    run.add_argument("--no-tests", action="store_true", help="显式接受缺少自动测试；结果需人工验收")
    run.add_argument("--codex-model")
    run.add_argument("--review-only", action="store_true", help="所有模型只读审查，确认问题也不自动修复")
    run.add_argument('--task-type', choices=['auto', 'general', 'ui_ux', 'architecture', 'algorithm', 'debug', 'review'], default='auto')
    run.add_argument('--claude-role', choices=['auto', 'frontend', 'architect', 'developer', 'analyst', 'reviewer'], default='auto')
    add_budget_flags(run, defaults=True)
    run.add_argument("--claude-developer-spec", type=Path, help="JSON: request, files, acceptance；只允许指定文件的完整内容交付")
    run.add_argument("--max-repairs", type=int, default=2)
    run.add_argument("--max-retries", type=int, default=2)
    run.add_argument("--agent-timeout", type=float, default=900)
    run.add_argument("--task-timeout", type=float, help='整体秒数；普通默认3600，明确无人值守默认7200，均可配置')
    run.add_argument("--test-timeout", type=float, default=180)
    run.add_argument("--background", action="store_true", help="后台启动，本窗口立即返回 Task ID")
    run.add_argument('--watch', action='store_true', help='启动后台任务并在当前输出中自动接入实时监控；Ctrl+C仅退出监控')
    run.add_argument("--queue", action="store_true", help="只创建任务，稍后 resume 执行")
    status = subs.add_parser("status", help="查看状态或任务列表")
    status.add_argument("task_id", nargs="?")
    status.add_argument("--json", action="store_true")
    report = subs.add_parser("report", help="生成并显示 Markdown 报告")
    report.add_argument("task_id")
    report.add_argument("--json", action="store_true")
    for name in ("pause", "cancel", "stop", "takeover", "amend", "handoff"):
        descriptions = {'pause': '请求安全暂停，等待检查点确认', 'cancel': '永久取消任务，保留工作区',
                        'stop': '紧急中断本工具的受控进程树', 'takeover': '安全暂停后把开发工作区交给用户',
                        'amend': '记录需求变更并递增revision', 'handoff': '检查并纳入人工修改，交还开发工作区'}
        sub = subs.add_parser(name, help=descriptions[name])
        sub.add_argument("task_id")
        sub.add_argument('--expected-revision', type=int, help='拒绝作用于过期需求版本的命令')
        sub.add_argument('--command-id', help='重复提交相同命令时使用的幂等 token')
        if name in {'pause', 'takeover'}:
            sub.add_argument('--grace', type=float, default=120, help='当前调用安全结束的宽限秒数；超时中断受控子进程')
            sub.add_argument('--wait', type=float, default=0, help='最多等待状态确认的秒数；超时只报告已请求')
        if name in {'takeover', 'handoff'}:
            sub.add_argument('--workspace', default='codex', help='codex、当前claude开发工作区或具体已登记开发角色')
        if name == 'amend':
            change = sub.add_mutually_exclusive_group(required=True)
            change.add_argument('--request')
            change.add_argument('--request-file', type=Path)
        if name == 'handoff':
            sub.add_argument('--include-uncommitted', action='store_true', help='明确授权安全提交并纳入人工未提交修改')
    watch = subs.add_parser('watch', help='监控公开输出、工具和协调器事件；Ctrl+C 只断开监控')
    watch.add_argument('task_id')
    watch.add_argument('--after', type=int, default=0, help='从上次 watch_cursor 之后重连')
    watch.add_argument('--replay', action='store_true', help='从历史开头回放')
    watch.add_argument('--once', action='store_true')
    watch.add_argument('--json', action='store_true')
    watch.add_argument('--full', action='store_true', help='不截断公开输出展示；不包含隐藏推理')
    resume = subs.add_parser("resume", help="从已保存的阶段继续；已完成的模型调用不重复付费")
    resume.add_argument("task_id")
    resume.add_argument('--expected-revision', type=int)
    resume.add_argument('--command-id')
    resume.add_argument("--background", action="store_true")
    resume.add_argument('--watch', action='store_true', help='恢复后台任务并自动接入实时监控')
    resume.add_argument("--confirm-codex-recovered", action="store_true", help="人工确认订阅额度/登录已恢复，允许下一次订阅推理")
    resume.add_argument("--confirm-claude-recovered", action="store_true", help="人工确认 Claude 可用；不更改 CC Switch")
    resume.add_argument("--extend-timeout", type=float, help="为整体任务追加秒数")
    resume.add_argument("--wait-quota", action="store_true", help="先有界等待官方额度恢复，然后从原阶段继续")
    resume.add_argument("--wait-timeout", type=float, default=900, help="额度等待最多秒数，默认 900")
    add_budget_flags(resume, defaults=False)
    costs = subs.add_parser("costs", help="查看分开的客户端估算、CaMeL 实际账单和余额")
    costs.add_argument("--json", action="store_true")
    costs.add_argument("--refresh", action="store_true", help="只读刷新 CaMeL 日志、钱包与订阅，不调用模型")
    costs.add_argument("--task", dest="task_id", help="按 Task ID 关联真实账单")
    costs.add_argument("--api-only", action="store_true", help="只读 API；不使用已登录浏览器，账户余额可能未知")
    budget = subs.add_parser('budget', help='用户级平台额度配置与上海自然日累计；不修改密钥')
    budget.add_argument('operation', choices=['show', 'set'], nargs='?', default='show')
    budget.add_argument('--task-default', type=float)
    budget.add_argument('--daily-default', type=float)
    budget.add_argument('--unattended-default', type=float)
    budget.add_argument('--monthly-wallet-default', type=float)
    budget.add_argument('--call-cap', type=float)
    worker = subs.add_parser("_execute", help=argparse.SUPPRESS)
    worker.add_argument("task_id")
    return p


def add_budget_flags(p, *, defaults):
    for flag, key, typ in [("--claude-budget", "claude_budget", float), ("--global-claude-budget", "global_claude_budget", float),
                           ("--claude-call-cap", "claude_call_cap", float), ("--claude-max-calls", "claude_max_calls", int),
                           ("--claude-max-reviews", "claude_max_reviews", int), ("--claude-model", "claude_model", str)]:
        p.add_argument(flag, type=typ, default=None)
    p.add_argument('--use-default-budget', action='store_true', default=False if defaults else None,
                   help='用户明确要求多 Agent 时授权用户级默认预算；GPT-only始终禁止')
    p.add_argument('--approve-daily-budget', type=float, help='明确批准本上海自然日更高累计额度；不改变服务端限制')
    p.add_argument('--unattended', action='store_true', default=False if defaults else None,
                   help='用户明确授权无人值守模式；使用独立的无人值守单任务默认预算')
    p.add_argument('--claude-subagents', type=int, default=None, help='允许经规划确认有收益的只读子代理数量，0..2，最大嵌套深度1')
    p.add_argument("--allow-estimated-budget", action="store_true", default=False if defaults else None,
                   help="明确授权付费调用，接受客户端预算可能超限且不等于 CaMeL 实际账单")
    p.add_argument("--allow-expensive-model", action="store_true", default=False if defaults else None,
                   help="明确授权 Opus 等高成本模型；仍需费用授权和预算")
    p.add_argument("--allow-unknown-billing", action="store_true", default=False if defaults else None,
                   help="单独明确接受当前余额查询未知；不绕过已知余额不足或认证错误")
    p.add_argument("--claude-effort", choices=["low", "medium", "high"], default="low" if defaults else None)
    p.add_argument("--claude-input-price", type=float, help="可选，用户核实的每百万输入 token USD 估算价")
    p.add_argument("--claude-output-price", type=float, help="可选，用户核实的每百万输出 token USD 估算价")


def budget_config(args, original=None, user_defaults=None):
    result = dict(original or DEFAULTS)
    if user_defaults and original is None:
        result.update(claude_budget=user_defaults['default_task_budget'], global_claude_budget=None,
                      claude_call_cap=user_defaults['claude_call_cap'], claude_max_calls=user_defaults['claude_max_calls'],
                      claude_max_reviews=user_defaults['claude_max_reviews'], role_workflow_version=3,
                      claude_subagent_limit=user_defaults['claude_subagent_limit'], claude_max_tool_calls=user_defaults['claude_max_tool_calls'],
                      claude_context_bytes=user_defaults['claude_context_bytes'], claude_document_tokens=user_defaults['claude_document_tokens'],
                      claude_developer_tokens=user_defaults['claude_developer_tokens'])
    for key in ("claude_budget", "global_claude_budget", "claude_call_cap", "claude_max_calls", "claude_max_reviews",
                "claude_model", "allow_estimated_budget", "allow_expensive_model", "allow_unknown_billing", "claude_effort"):
        value = getattr(args, key, None)
        if value is not None:
            result[key] = value
    for key in ("claude_budget", "global_claude_budget"):
        if result[key] is not None:
            finite_positive(result[key], key, zero=True)
    if result['claude_call_cap'] is not None:
        finite_positive(result["claude_call_cap"], "claude_call_cap")
    if getattr(args, 'claude_subagents', None) is not None:
        if not 0 <= args.claude_subagents <= 2:
            raise TeamError('CONFIG_ERROR', 'Subagent limit must be 0..2')
        result['claude_subagent_limit'] = args.claude_subagents
    for key in ("claude_max_calls", "claude_max_reviews"):
        if not 0 <= result[key] <= 10:
            raise TeamError("CONFIG_ERROR", key + " must be between 0 and 10")
    rates = (getattr(args, "claude_input_price", None), getattr(args, "claude_output_price", None))
    if any(x is not None for x in rates):
        if any(x is None for x in rates):
            raise TeamError("CONFIG_ERROR", "Both Claude price estimates must be specified")
        result["claude_prices"] = {"input": finite_positive(rates[0], "input price", zero=True),
                                  "output": finite_positive(rates[1], "output price", zero=True),
                                  "source": "user-supplied estimate; not a bill"}
    if user_defaults and (original is None or any(getattr(args, k, None) is not None for k in ('claude_budget', 'use_default_budget', 'allow_estimated_budget', 'approve_daily_budget', 'unattended'))):
        from .user_config import authorize
        result['policy'] = getattr(args, 'policy', result['policy'])
        unattended = getattr(args, 'unattended', None)
        result = authorize(result, user_defaults, use_defaults=bool(getattr(args, 'use_default_budget', False) or unattended),
                           explicit_budget=getattr(args, 'claude_budget', None), daily_approval=getattr(args, 'approve_daily_budget', None),
                           unattended=result.get('unattended', False) if unattended is None else unattended)
    return result


def background(store, task_id, *, attach_watch=False):
    folder = store.root / "tasks" / task_id
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "runner.log").open("ab") as log:
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        child_env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        # Only the trusted worker inherits the environment; child adapters strip provider keys.
        worker = subprocess.Popen([sys.executable, "-m", "agent_team", "--state-dir", str(store.root), "_execute", task_id],
                                  cwd=Path(__file__).resolve().parent.parent, stdout=log, stderr=log,
                                  stdin=subprocess.DEVNULL, env=child_env, creationflags=flags,
                                  start_new_session=os.name != "nt")
    store.event(task_id, "orchestrator", "background_spawned", {"pid": worker.pid})
    print(json.dumps({"task_id": task_id, "worker_pid": worker.pid, "status": "DISPATCHED",
                      "report": str(folder / "report.md")}, ensure_ascii=False, indent=2))
    if attach_watch:
        from .streaming import watch
        print('实时监控已接入；Ctrl+C只退出监控，后台任务继续。', flush=True)
        watch(store, task_id, replay=True, full=True)
    return 0


def doctor(store, probe=False, quota=False):
    facts = {"python": sys.version.split()[0], "platform": sys.platform, "claude_balance": "UNKNOWN",
             "billing": "ChatGPT-only Codex; Claude calls require explicit estimated-budget authorization"}
    for name in ("git", "codex", "claude", "node"):
        try:
            binary = executable(name)
            result = run_process([binary, "--version"], store.root, timeout=30)
            facts[name] = {"path": binary, "version": scrub(result.stdout.strip()), "exit_code": result.code}
        except TeamError as error:
            facts[name] = {"status": error.kind, "reason": str(error)}
    try:
        codex = CodexAdapter()
        facts["codex_auth"] = codex.login(store.root)
        codex.capabilities(store.root)
        facts["codex_automation"] = "supported; actual inference still requires a successful run"
    except TeamError as error:
        facts["codex_auth"] = error.kind
    try:
        ClaudeAdapter().capabilities(store.root)
        facts["claude_automation"] = "supported locally; endpoint not tested (no paid call)"
    except TeamError as error:
        facts["claude_automation"] = error.kind
    # Extract non-secret metadata only. Never read Codex auth.json or CC Switch database.
    settings = Path.home() / ".claude" / "settings.json"
    if settings.is_file():
        try:
            obj = json.loads(settings.read_text(encoding="utf-8"))
            env = obj.get("env", {})
            facts["claude_config"] = {"settings_present": True, "host": urlparse(env.get("ANTHROPIC_BASE_URL", "")).hostname,
                                      "credential_configured": bool(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")),
                                      "sonnet_model": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
                                      "config_changed": False}
        except (ValueError, OSError):
            facts["claude_config"] = {"settings_present": True, "metadata_readable": False}
    facts["codex_quota"] = store.health("codex")
    if quota and facts.get("codex_auth") == "ChatGPT":
        try:
            snapshot = CodexAdapter().quota(store.root)
            facts["codex_quota"] = snapshot
            if snapshot["status"] != "UNKNOWN":
                store.set_health("codex", snapshot["status"], **{k: v for k, v in snapshot.items() if k not in {"status", "checked_at"}})
        except TeamError as error:
            facts["codex_quota_probe"] = {"status": "UNKNOWN", "reason": str(error)}
    if probe:
        try:
            folder = store.root / "sandbox-probe"
            folder.mkdir(exist_ok=True)
            result = run_process(CodexAdapter().test_command(folder, [sys.executable, "-c", "print('sandbox-probe-ok')"]), folder, timeout=45)
            facts["sandbox_probe"] = {"exit_code": result.code, "stdout": scrub(result.stdout), "stderr": scrub(result.stderr[-2000:])}
        except TeamError as error:
            facts["sandbox_probe"] = {"status": error.kind, "reason": str(error)}
    write_json(store.root / "doctor.json", facts)
    print(json.dumps(scrub(facts), ensure_ascii=False, indent=2))
    return 0 if facts.get("codex_auth") == "ChatGPT" else 2


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    args = parser().parse_args(argv)
    # Prevent recursive orchestration even from test processes or model tool subprocesses.
    if os.environ.get("AGENT_TEAM_CHILD"):
        print("RECURSION_BLOCKED: an agent/test cannot invoke agent-team again", file=sys.stderr)
        return 2
    try:
        store = Store(args.state_dir)
        from .user_config import load as load_budget, save as save_budget
        user_defaults = load_budget(store.root)
        if args.command == "doctor":
            return doctor(store, args.probe_sandbox, args.quota)
        workflow = Workflow(store)
        if args.command == "run":
            if args.queue and args.watch:
                raise TeamError('CONFIG_ERROR', '--queue does not start execution; attach watch when resuming instead')
            request = args.request or args.request_file.read_text(encoding="utf-8-sig")
            if not request.strip() or len(request) > 50000:
                raise TeamError("CONFIG_ERROR", "Request must be 1..50000 characters")
            config = budget_config(args, user_defaults=user_defaults)
            config.update({key: getattr(args, key) for key in ("policy", "risk", "codex_model", "max_repairs", "max_retries",
                                                             "agent_timeout", "task_timeout", "test_timeout")})
            config["review_only"] = args.review_only
            config.update(task_type='review' if args.review_only else args.task_type, claude_role=args.claude_role)
            if args.task_timeout is None:
                config['task_timeout'] = user_defaults['unattended_timeout_seconds'] if config.get('unattended') else user_defaults['normal_timeout_seconds']
            for key in ("agent_timeout", "task_timeout", "test_timeout"):
                finite_positive(config[key], key)
            if not 0 <= config["max_repairs"] <= 4 or not 0 <= config["max_retries"] <= 3:
                raise TeamError("CONFIG_ERROR", "Repair/retry limits out of safe range")
            tests = []
            test_inputs = list(args.test_command)
            if args.tests_file:
                loaded = json.loads(args.tests_file.read_text(encoding="utf-8-sig"))
                if not isinstance(loaded, list):
                    raise TeamError("CONFIG_ERROR", "tests-file must contain a list of argv arrays")
                test_inputs.extend(json.dumps(command) for command in loaded)
            for text in test_inputs:
                command = json.loads(text)
                if not isinstance(command, list) or not command or not all(isinstance(s, str) and s for s in command):
                    raise TeamError("CONFIG_ERROR", "test-command must be a nonempty JSON argv string array")
                if Path(command[0]).suffix.lower() in {".cmd", ".bat", ".ps1"}:
                    raise TeamError("CONFIG_ERROR", "Use native executables, e.g. node instead of npm.cmd")
                tests.append(command)
            if not tests and not args.no_tests:
                raise TeamError("CONFIG_ERROR", "Supply approved --test-command or explicitly --no-tests")
            config["test_commands"] = tests
            if args.claude_developer_spec:
                spec = json.loads(args.claude_developer_spec.read_text(encoding="utf-8-sig"))
                if set(spec) != {"request", "files", "acceptance"} or not isinstance(spec["files"], list) or not spec["files"]:
                    raise TeamError("CONFIG_ERROR", "Developer spec requires request, exact files, acceptance")
                for file in spec["files"]:
                    relative_file(file)
                config["claude_developer"] = spec
            task_id = workflow.create(args.repo, request, config, args.base)
            print("Task ID: " + task_id, flush=True)
            if args.queue:
                store.update(task_id, data={"queued_only": True})
                workflow.report(task_id)
                return 0
            if args.background or args.watch:
                return background(store, task_id, attach_watch=args.watch)
            result = workflow.run(task_id)
        elif args.command in {"resume", "_execute"}:
            task = store.task(args.task_id)
            if args.command == "resume":
                if task["owner"]:
                    if pid_alive(task["pid"]):
                        raise TeamError("ALREADY_RUNNING", "Task already has an active runner")
                config = budget_config(args, task["config"], user_defaults=user_defaults)
                if args.extend_timeout is not None:
                    config["task_timeout"] += finite_positive(args.extend_timeout, "extend timeout")
                store.update(args.task_id, config=config)
                if task['phase'] == 'specialists':
                    data = store.task(args.task_id)['data']
                    data.update(specialist_preflight=None, specialist_pending=[])
                    store.update(args.task_id, data=data)
                if task["data"].get("queued_only"):
                    data = store.task(args.task_id)["data"]
                    data["queued_execution_resumed"] = True
                    store.update(args.task_id, data=data)
                if args.wait_quota:
                    finite_positive(args.wait_timeout, "wait timeout")
                    data = store.task(args.task_id)["data"]
                    data["resume_quota_wait"] = args.wait_timeout
                    store.update(args.task_id, data=data)
                if args.confirm_codex_recovered:
                    CodexAdapter().login(store.root)
                    store.set_health("codex", "UNKNOWN", source="user confirmed recovery; next inference verifies availability")
                if args.confirm_claude_recovered:
                    store.set_health("claude", "UNKNOWN", source="user confirmed recovery; actual bill still unknown")
                store.submit(args.task_id, 'resume', expected_revision=args.expected_revision, dedup=args.command_id)
                if args.background or args.watch:
                    return background(store, args.task_id, attach_watch=args.watch)
            task = store.task(args.task_id)
            if task["data"].get("resume_quota_wait"):
                from .quota_watch import wait_for_codex
                if not wait_for_codex(store, args.task_id, timeout=task["data"]["resume_quota_wait"]):
                    workflow.report(args.task_id)
                    print(json.dumps({"task_id": args.task_id, "status": store.task(args.task_id)["status"], "reason": "Bounded quota wait stopped; checkpoints retained"}))
                    return 2
                data = store.task(args.task_id)["data"]
                data.pop("resume_quota_wait", None)
                store.update(args.task_id, data=data)
            result = workflow.run(args.task_id)
        elif args.command in {'pause', 'stop', 'cancel', 'takeover', 'amend', 'handoff'}:
            from .controls import Controls
            controls = Controls(store, workflow.git)
            if args.command == 'handoff':
                controls.handoff(args.task_id, include_uncommitted=args.include_uncommitted,
                                 expected_revision=args.expected_revision, dedup=args.command_id, workspace_role=args.workspace)
            else:
                payload = {}
                if args.command in {'pause', 'takeover'}:
                    payload['grace'] = finite_positive(args.grace, 'pause grace')
                    if args.command == 'takeover':
                        payload['workspace'] = args.workspace
                    if not 0 <= args.wait <= 60:
                        raise TeamError('CONFIG_ERROR', '--wait must be 0..60 seconds; reconnect via status/watch after that')
                if args.command == 'amend':
                    payload = {'change': args.request or args.request_file.read_text(encoding='utf-8-sig'), 'actor': 'user'}
                command = store.submit(args.task_id, args.command, payload=payload,
                                       expected_revision=args.expected_revision, dedup=args.command_id)
                until = time.monotonic() + getattr(args, 'wait', 0)
                while True:
                    store.settle(args.task_id)
                    task = store.task(args.task_id)
                    live = pid_alive(task['pid']) or pid_alive(task['data'].get('active_pid')) or pid_alive(task['data'].get('active_test_pid'))
                    if args.command == 'takeover' and not live:
                        controls.takeover(args.task_id)
                    if not live or time.monotonic() >= until:
                        break
                    time.sleep(.2)
                print(json.dumps({'command_id': command['id'], 'command_status': next(x['status'] for x in store.commands(args.task_id) if x['id'] == command['id'])}, ensure_ascii=False))
            task = store.task(args.task_id)
            print(json.dumps({'task_id': task['id'], 'status': task['status'], 'revision': task['revision'],
                              'workspaces': store.workspaces(task['id'])}, ensure_ascii=False, indent=2))
            workflow.report(args.task_id)
            return 0
        elif args.command == 'watch':
            from .streaming import watch
            watch(store, args.task_id, after=args.after, once=args.once, replay=args.replay, json_output=args.json, full=args.full)
            return 0
        elif args.command == 'budget':
            if args.operation == 'set':
                changes = {key: finite_positive(value, key) for key, value in
                           (('default_task_budget', args.task_default), ('unattended_task_budget', args.unattended_default),
                            ('daily_budget', args.daily_default), ('monthly_wallet_budget', args.monthly_wallet_default), ('claude_call_cap', args.call_cap)) if value is not None}
                user_defaults = save_budget(store.root, changes)
            print(json.dumps({'config_path': str(store.root / 'user-config.json'), 'config': user_defaults,
                              'today': store.daily_budget(), 'monthly_wallet': store.monthly_wallet()}, ensure_ascii=False, indent=2))
            return 0
        elif args.command == "status":
            if args.task_id:
                store.reconcile_runtime(args.task_id)
            result = store.task(args.task_id) if args.task_id else store.tasks()
            print(json.dumps(scrub(result), ensure_ascii=False, indent=2))
            return 0
        elif args.command == "report":
            path = workflow.report(args.task_id)
            print(path.with_suffix(".json").read_text(encoding="utf-8") if args.json else path.read_text(encoding="utf-8"))
            return 0
        elif args.command == "costs":
            from .cost_monitor import CostMonitor
            monitor = CostMonitor(store)
            if args.task_id:
                store.task(args.task_id)
            if args.refresh:
                monitor.refresh(args.task_id, use_browser=not args.api_only)
                if args.task_id:
                    workflow.report(args.task_id)
            result = monitor.summary(args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        else:
            raise TeamError("CONFIG_ERROR", "Unknown command")
        print(json.dumps({"task_id": result["id"], "status": result["status"], "phase": result["phase"],
                          "report": str(store.root / "tasks" / result["id"] / "report.md")}, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "READY_FOR_MERGE" else 2
    except (TeamError, OSError, ValueError, TypeError) as error:
        print(str(scrub(f"{getattr(error, 'kind', 'INPUT_ERROR')}: {error}")), file=sys.stderr)
        return 2
