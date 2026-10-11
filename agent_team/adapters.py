from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import ExitStack
import json
import math
import os
from pathlib import Path
import shutil

from .process import run_process
from .safety import TeamError, classify_error, clean_env, scrub
from .schemas import SCHEMAS, validate


@dataclass
class Answer:
    data: dict
    usage: dict
    duration: float
    cost: float | None = None
    session_id: str | None = None


def executable(name):
    found = shutil.which(name + (".exe" if os.name == "nt" else ""))
    if not found:
        raise TeamError("UNAVAILABLE", name + " executable not found")
    return found


def json_document(text):
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise TeamError("INVALID_OUTPUT", "Invalid JSON output") from e
    if not isinstance(value, dict):
        raise TeamError("INVALID_OUTPUT", "Expected JSON object")
    return value


class CodexAdapter:
    def __init__(self, runner=run_process, binary=None):
        self.runner = runner
        self.binary = binary or executable("codex")

    def quota(self, cwd, *, control=None):
        self.login(cwd)
        from .quota import read_quota
        return read_quota(self.binary, cwd, control=control)

    def login(self, cwd):
        result = self.runner([self.binary, "login", "status"], cwd, timeout=30, env=clean_env())
        # Keep only auth method, never auth.json, tokens, account IDs or full login output.
        if result.code == 0 and "logged in using chatgpt" in (result.stdout + result.stderr).lower():
            return "ChatGPT"
        raise TeamError("AUTH_ERROR", "ChatGPT subscription login is required; no API-key fallback")

    def capabilities(self, cwd):
        result = self.runner([self.binary, "exec", "--help"], cwd, timeout=30, env=clean_env())
        required = ("--json", "--output-schema", "--ephemeral", "--ignore-user-config", "--ignore-rules")
        if result.code or any(flag not in result.stdout for flag in required):
            raise TeamError("UNSUPPORTED_CLI", "Codex lacks required safe automation flags")

    def command(self, cwd, role, schema_path, answer_path, config):
        argv = [self.binary, "-a", "never", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral",
                "--json", "--color", "never", "-C", str(cwd), "--sandbox",
                "workspace-write" if role in {"develop", "fix"} and not config.get("review_only") else "read-only",
                "--output-schema", str(schema_path), "-o", str(answer_path)]
        overrides = ['model_provider="openai"', 'forced_login_method="chatgpt"', 'approval_policy="never"',
                     'windows.sandbox="unelevated"', 'sandbox_workspace_write.network_access=false',
                     'features.prefer_mxc=true',
                     'features.multi_agent=false', 'features.memories=false', 'features.hooks=false',
                     'features.web_search=false', 'shell_environment_policy.inherit="core"']
        for override in overrides:
            argv.extend(["-c", override])
        if config.get("codex_model"):
            argv.extend(["--model", config["codex_model"]])
        argv.append("-")
        return argv

    def call(self, cwd, role, prompt, artifact_dir, config, *, control=None, on_start=None, cap=None, on_line=None):
        self.login(cwd)
        self.capabilities(cwd)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        schema_path = artifact_dir / "schema.json"
        answer_path = artifact_dir / "answer.json"
        schema_path.write_text(json.dumps(SCHEMAS[role]), encoding="utf-8")
        result = self.runner(self.command(cwd, role, schema_path, answer_path, config), cwd, stdin=prompt,
                             timeout=config["agent_timeout"], control=control, env=clean_env(), on_start=on_start,
                             **({'on_line': on_line} if on_line else {}))
        events = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        completed = [event for event in events if event.get("type") == "turn.completed"]
        last_completed = max((i for i, e in enumerate(events) if e.get('type') == 'turn.completed'), default=-1)
        failures = [event for i, event in enumerate(events) if event.get('type') == 'turn.failed' or
                    (event.get('type') == 'error' and not (i < last_completed and str(event.get('message', '')).startswith('Reconnecting...')))]
        if result.code or not completed or failures:
            error = " ".join(json.dumps(e, ensure_ascii=False) for e in failures) + result.stderr
            kind = classify_error(error, agent="codex")
            # Preserve only a short sanitized error, not command traces or private reasoning.
            raise TeamError(kind, str(scrub(error))[:1500] or "Codex process / turn did not complete")
        if not answer_path.is_file() or answer_path.stat().st_size > 500000:
            raise TeamError("INVALID_OUTPUT", "Missing / oversized Codex answer artifact")
        data = validate(json_document(answer_path.read_text(encoding="utf-8")), SCHEMAS[role])
        # Raw output can contain repo secrets; replace the artifact with the sanitized parsed form.
        answer_path.write_text(json.dumps(scrub(data), ensure_ascii=False, indent=2), encoding="utf-8")
        session = next((event.get("thread_id") for event in events if event.get("type") == "thread.started"), None)
        return Answer(data, completed[-1].get("usage", {}), result.duration, session_id=session)

    def test_command(self, cwd, argv):
        if os.name != "nt":
            raise TeamError("UNSUPPORTED_PLATFORM", "MVP native test sandbox currently supports Windows; use mocks elsewhere")
        return [self.binary, "sandbox", "-P", "agent_team_test", "-C", str(cwd),
                "-c", 'permissions.agent_team_test.extends=":workspace"',
                "-c", 'permissions.agent_team_test.network.enabled=false',
                "-c", 'windows.sandbox="unelevated"', "-c", 'features.prefer_mxc=true',
                "-c", 'approval_policy="never"', *argv]


class ClaudeAdapter:
    def __init__(self, runner=run_process, binary=None):
        self.runner = runner
        self.binary = binary or executable("claude")

    def capabilities(self, cwd, *, native_tools=False):
        result = self.runner([self.binary, "--help"], cwd, timeout=30, env=clean_env(claude=True))
        for flag in ("--max-budget-usd", "--json-schema", "--no-session-persistence", "--safe-mode", "--tools",
                     "--strict-mcp-config", "--permission-prompts"):
            if result.code or flag not in result.stdout:
                raise TeamError("UNSUPPORTED_CLI", "Claude lacks safe automation flag: " + flag)
        if native_tools and any(flag not in result.stdout for flag in ('--restricted', '--allowedTools', '--disallowedTools', 'dontAsk')):
            raise TeamError('UNSUPPORTED_CLI', 'Claude native roles require restricted mode and non-prompting tool permissions')

    def command(self, role, config, cap):
        if not config["allow_estimated_budget"]:
            raise TeamError("NEEDS_BUDGET_APPROVAL", "No paid Claude authorization")
        model = config["claude_model"]
        if "opus" in model.lower() and not config.get("allow_expensive_model"):
            raise TeamError("NEEDS_BUDGET_APPROVAL", "Expensive model requires explicit authorization")
        policy = config.get('claude_tools_policy')
        if policy:
            argv = [self.binary, '-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
                    '--json-schema', json.dumps(SCHEMAS[role]), '--model', model, '--max-budget-usd', f'{cap:.6f}',
                    '--max-turns', str(config.get('claude_max_turns', 24)), '--restricted', '--tools', ','.join(policy['tools']),
                    '--permission-mode', 'dontAsk', '--permission-prompts', 'none', '--allowedTools', *policy['allowed'],
                    '--disallowedTools', 'PowerShell', 'WebFetch', 'WebSearch', 'Skill', 'Task',
                    '--strict-mcp-config', '--mcp-config', json.dumps(policy.get('mcp_config', {'mcpServers':{}})), '--no-chrome', '--disable-slash-commands',
                    '--settings', json.dumps(policy['settings']), '--no-session-persistence']
            if policy['agents']:
                argv += ['--agents', json.dumps(policy['agents']), '--forward-subagent-text']
            for directory in config.get('read_artifacts', []):
                argv += ['--add-dir', str(directory)]
            return argv
        return [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--json-schema", json.dumps(SCHEMAS[role]),
                "--model", model, "--max-budget-usd", f"{cap:.6f}", "--max-turns", "2",
                "--tools", "", "--safe-mode", "--disable-slash-commands", "--no-chrome",
                "--permission-mode", "plan", "--permission-prompts", "none",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "user", "--settings", '{"disableAllHooks":true}',
                "--no-session-persistence"]

    def call(self, cwd, role, prompt, artifact_dir, config, *, control=None, on_start=None, cap=None, on_line=None):
        native = bool(config.get('native_claude_tools'))
        try:
            self.capabilities(cwd, native_tools=native)
        except TeamError as error:
            error.process_started = False
            raise
        env = clean_env(claude=True)
        # One bounded schema-formatting attempt within this CLI, never an external
        # replay. Tool workflows need a final output turn after their tool calls.
        env.update(CLAUDE_CODE_MAX_RETRIES="0", MAX_STRUCTURED_OUTPUT_RETRIES="1")
        env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = str(config.get('claude_output_tokens', 3000))
        env.setdefault("CLAUDE_CODE_EFFORT_LEVEL", config.get("claude_effort", "low"))
        env.pop("CLAUDE_CODE_RETRY_WATCHDOG", None)
        env.pop("FALLBACK_FOR_ALL_PRIMARY_MODELS", None)
        config = dict(config)
        if native:
            from .claude_tools import prepare_policy, sandbox_command
            from .runtime_credentials import claude_runtime
            from .native_runtime import native_runtime
            from .safety import write_json
            lifetime = ExitStack()
            try:
                runtime_dir = lifetime.enter_context(native_runtime(artifact_dir))
                if role=='claude_native_develop':
                    config['native_test_transport']='mcp'
                policy = prepare_policy(cwd, artifact_dir, config, role, task_id=config.get('task_id'),
                                        revision=config.get('task_revision', 1), state_dir=config.get('state_dir'), runtime_dir=runtime_dir)
                config['claude_tools_policy'] = policy
                if config.get('tool_broker_spec') is not None:
                    config['tool_broker_spec'].update(policy['spec'])
                native_command = self.command(role, config, cap)
                runtime_env = lifetime.enter_context(claude_runtime(artifact_dir, runtime_root=runtime_dir / 'auth'))
            except TeamError as error:
                lifetime.close()
                error.process_started = False
                raise
            try:
                bash_root = None
                for key in ('CLAUDE_CODE_MAX_RETRIES', 'MAX_STRUCTURED_OUTPUT_RETRIES',
                            'CLAUDE_CODE_MAX_OUTPUT_TOKENS', 'CLAUDE_CODE_EFFORT_LEVEL'):
                    runtime_env[key] = env[key]
                def isolated(command, *, network):
                    return sandbox_command(executable('codex'), cwd, artifact_dir, command,
                        provider_origin=runtime_env['ANTHROPIC_BASE_URL'], writable=role == 'claude_native_develop',
                        state_dir=config.get('state_dir'), extra_read=config.get('read_artifacts', []),
                        network=network, runtime_env_keys=runtime_env, runtime_dir=runtime_dir, bash_root=bash_root)
                # Local, offline authentication resolution. Never invoke /login or
                # dispatch inference if the bridge did not arrive in the real sandbox.
                try:
                    auth = self.runner(isolated([self.binary, '--restricted', '--permission-mode', 'dontAsk',
                        '--settings', '{"disableAllHooks":true}', 'auth', 'status', '--json'], network=False),
                        cwd, timeout=30, control=control, env=runtime_env)
                    try:
                        status = json.loads(auth.stdout)
                    except ValueError:
                        status = {}
                    if not isinstance(status, dict):
                        status = {}
                    auth_receipt = {'exit_code': auth.code, 'logged_in': status.get('loggedIn') is True,
                                    'auth_method': status.get('authMethod'), 'model_requests': 0,
                                    'temporary_config': status.get('configDirectory') == runtime_env['CLAUDE_CONFIG_DIR']}
                    write_json(artifact_dir / 'runtime-auth.json', auth_receipt)
                    if auth.code or not auth_receipt['logged_in'] or not auth_receipt['temporary_config']:
                        raise TeamError('AUTH_ERROR', 'Claude sandbox runtime authentication is unavailable; no inference dispatched')
                except TeamError as error:
                    error.process_started = False
                    raise
                result = self.runner(isolated(native_command, network=True), cwd, stdin=prompt,
                    timeout=config['agent_timeout'], control=control, env=runtime_env, on_start=on_start,
                    **({'on_line': lambda source, line: on_line(source, str(scrub(line)))} if on_line else {}))
                result = replace(result, stdout=str(scrub(result.stdout)), stderr=str(scrub(result.stderr)))
            finally:
                lifetime.close()
        else:
            result = self.runner(self.command(role, config, cap), cwd, stdin=prompt,
                                 timeout=config["agent_timeout"], control=control, env=env, on_start=on_start,
                                 **({'on_line': on_line} if on_line else {}))
        envelope = None
        try:
            documents = []
            for line in result.stdout.splitlines():
                try:
                    documents.append(json_document(line))
                except TeamError:
                    pass
            envelope = next((x for x in reversed(documents) if x.get('type') == 'result'), None)
            if envelope is None:
                envelope = json_document(result.stdout)  # Legacy receipt/mock compatibility.
        except TeamError:
            if result.code == 0:
                raise
        from .delivery import repair_json, failure_kind
        receipt = {k: (envelope or {}).get(k) for k in ('usage', 'modelUsage', 'total_cost_usd', 'session_id', 'subtype', 'stop_reason', 'num_turns')}
        receipt.update(process_exit_code=result.code, duration=result.duration)
        from .safety import write_json
        artifact_dir.mkdir(parents=True, exist_ok=True)
        write_json(artifact_dir / 'receipt.json', receipt)
        if result.code or not envelope or envelope.get("is_error") or envelope.get("subtype") != "success":
            detail = json.dumps(envelope or {}, ensure_ascii=False) + result.stderr
            error = TeamError(failure_kind(envelope or {}, result.stderr, code=result.code) or classify_error(detail, agent="claude"), str(scrub(detail))[:1500])
            error.cost = self.cost(envelope or {})
            error.receipt = receipt
            error.completed_process = True
            raise error
        cost = self.cost(envelope)
        try:
            raw = envelope.get('structured_output', envelope.get('result'))
            data = validate(repair_json(raw), SCHEMAS[role])
        except TeamError as error:
            error.cost = cost
            error.receipt = receipt
            error.completed_process = True
            raise
        artifact_dir.mkdir(parents=True, exist_ok=True)
        write_json(artifact_dir / "answer.json", data)
        return Answer(data, envelope.get("usage", {}), result.duration, cost, envelope.get("session_id"))

    @staticmethod
    def cost(envelope):
        cost = envelope.get("total_cost_usd")
        if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
            return float(cost)
        return None
