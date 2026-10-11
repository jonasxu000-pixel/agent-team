"""Claude tool permissions plus a native Windows OS boundary; never bypass mode."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from .safety import TeamError, in_root, relative_file, write_json


def quoted(value):
    return '"' + str(value).replace('\\', '/').replace('"', '\\"') + '"'


def test_commands(spec_path, count):
    script = Path(__file__).with_name('tool_guard.py').resolve()
    return [f'{quoted(sys.executable)} {quoted(script)} test {quoted(spec_path)} {i}' for i in range(count)]


def prepare_policy(cwd, artifact, config, role, *, task_id=None, revision=1, state_dir=None, runtime_dir=None):
    cwd, artifact = Path(cwd).resolve(), Path(artifact).resolve()
    (artifact / 'runtime').mkdir(parents=True, exist_ok=True)
    writable = role == 'claude_native_develop'
    files = [relative_file(f) for f in config.get('specialist_files', [])]
    if writable and not files:
        raise TeamError('ASSIGNMENT_REQUIRED', 'Native Claude developer requires an exact file assignment')
    for name in files:
        in_root(cwd, name)
    subagents = config.get('claude_subagent_limit', 0)
    if not 0 <= subagents <= 2:
        raise TeamError('CONFIG_ERROR', 'Subagent limit must be 0..2; children are read-only and cannot spawn agents')
    artifact.mkdir(parents=True, exist_ok=True)
    runtime = Path(runtime_dir).resolve() if runtime_dir is not None else artifact
    spec_path = runtime / 'policy.json' if runtime_dir is not None else artifact / 'tool-policy.json'
    commands = test_commands(spec_path, len(config['test_commands'])) if writable else []
    test_transport = config.get('native_test_transport', 'bash')
    spec = {'worktree': str(cwd), 'files': files if writable else [], 'tests': config['test_commands'],
            'commands': commands, 'subagents': subagents, 'max_depth': 1 if subagents else 0,
            'max_tools': config.get('claude_max_tool_calls', 96), 'state_dir': str(state_dir) if state_dir else None,
            'task_id': task_id, 'revision': revision, 'invocation': artifact.name,
            'read_artifacts': config.get('read_artifacts', []), 'max_context_bytes': config.get('claude_context_bytes', 48000),
            'inbox': str(runtime / 'inbox' if runtime_dir is not None else artifact / 'tool-inbox'),
            'role': role, 'test_transport': test_transport}
    from .schemas import SCHEMAS
    if role in SCHEMAS:
        spec['output_schema'] = SCHEMAS[role]
    Path(spec['inbox']).mkdir(parents=True, exist_ok=True)
    write_json(spec_path, spec)
    if runtime_dir is not None:
        write_json(artifact / 'tool-policy.json', spec)
    hook_script = Path(__file__).with_name('tool_guard.py').resolve()
    allowed = ['Read', 'Glob', 'Grep', 'StructuredOutput']
    if writable:
        for name in files:
            absolute = (cwd / name).as_posix()
            # Claude absolute permission paths use //; file permissions remain bounded even if hooks fail.
            allowed.extend([f'Edit(//{absolute})', f'Write(//{absolute})'])
        allowed.extend(['mcp__agent_team__Bash'] if test_transport=='mcp' else ['Bash(' + command + ')' for command in commands])
    tools = ['Read', 'Glob', 'Grep'] + (['Edit', 'Write'] + ([] if test_transport=='mcp' else ['Bash']) if writable else [])
    mcp={'mcpServers':{}}
    if writable and test_transport=='mcp' and commands:
        mcp={'mcpServers':{'agent_team':{'type':'stdio','command':sys.executable,
             'args':[str(Path(__file__).with_name('test_mcp.py').resolve()),str(spec_path)]}}}
    if role == 'claude_native_smoke':
        tools = ['Read']
        allowed = ['Read(//' + (cwd / 'context.txt').as_posix() + ')', 'StructuredOutput']
    agents = {}
    if subagents:
        tools.append('Agent')
        allowed.append('Agent(expert_read)')
        agents['expert_read'] = {'description': 'Bounded read-only source investigation',
                                'prompt': 'Investigate the assigned files only. Do not edit, execute commands or delegate.',
                                'tools': ['Read', 'Glob', 'Grep'], 'permissionMode': 'dontAsk', 'model': 'inherit', 'maxTurns': 4}
    settings = {'disableAllHooks': False,
                'permissions': {'allow': allowed, 'deny': ['PowerShell', 'WebFetch', 'WebSearch', 'Skill', 'Task',
                                                         'Bash(git *)', 'Bash(curl *)', 'Bash(cmd *)', 'Bash(powershell *)']},
                'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [{'type': 'command', 'command': sys.executable,
                          'args': [str(hook_script), 'hook', str(spec_path)], 'timeout': 20}]}]}}
    return {'spec': spec, 'spec_path': spec_path, 'tools': tools, 'allowed': allowed,
            'settings': settings, 'agents': agents, 'test_commands': commands, 'mcp_config':mcp}


def sandbox_command(codex_binary, cwd, artifact, command, *, provider_origin, writable, extra_read=(), state_dir=None, network=True,
                    runtime_env_keys=None, runtime_dir=None, bash_root=None):
    if os.name != 'nt':
        raise TeamError('UNSUPPORTED_PLATFORM', 'Native Claude isolation currently uses the verified Codex Windows sandbox')
    host = urlsplit(provider_origin).hostname
    if host != 'api.camel-hub.cn':
        raise TeamError('BILLING_ORIGIN_UNSUPPORTED', 'Native developer requires the existing verified CaMeL origin')
    cwd, artifact = Path(cwd).resolve(), Path(artifact).resolve()
    roots = {':minimal': 'read', str(cwd): 'write' if writable else 'read', str(artifact): 'read',
             str(artifact / 'runtime'): 'write', str(Path(__file__).resolve().parent): 'read',
             str(Path(command[0]).resolve().parent): 'read', str(Path(sys.executable).resolve().parent): 'read'}
    package_root = Path(__file__).resolve().parents[1]
    roots[str(package_root)] = 'read'  # Python needs to enumerate the package's parent during import.
    for child in package_root.iterdir():
        if child.name != 'agent_team':
            roots[str(child.resolve())] = 'deny'
    roots[str(Path(codex_binary).resolve().parent)] = 'read'
    roots[str(artifact / 'tool-inbox')] = 'write'
    if runtime_dir is not None:
        runtime = Path(runtime_dir).resolve()
        roots[str(runtime)] = {'.': 'read', 'auth': 'write', 'inbox': 'write'}
    if bash_root is not None:
        roots[str(Path(bash_root).resolve())] = 'read'
    for path in extra_read:
        roots[str(Path(path).resolve())] = 'read'
    roots[str(cwd)] = {'.': 'write' if writable else 'read', '.git': 'deny'}
    for pattern in ('.env', '*.env', '**/*.env', '**/.env', '**/*.pem', '**/*.key', '.claude', '.codex'):
        roots[str(cwd)][pattern] = 'deny'
    for path in (Path.home() / '.codex', Path.home() / '.claude', Path.home() / '.cc-switch'):
        roots[str(path.resolve())] = 'deny'
    # Native sandbox rules require TOML, not JSON inline-table syntax.
    def toml(value):
        if isinstance(value, dict):
            return '{' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in value.items()) + '}'
        return json.dumps(value)
    table = '{glob_scan_max_depth=8,' + ','.join(json.dumps(k) + '=' + toml(v) for k, v in roots.items()) + '}'
    argv = [codex_binary, 'sandbox', '-P', 'agent_team_claude', '-C', str(cwd),
            '-c', 'permissions.agent_team_claude.filesystem=' + table,
            '-c', 'permissions.agent_team_claude.network.enabled=' + ('true' if network else 'false'),
            '-c', 'windows.sandbox="unelevated"', '-c', 'features.prefer_mxc=true', '-c', 'approval_policy="never"']
    if network:
        argv += ['-c', 'features.network_proxy=true',
                 '-c', 'permissions.agent_team_claude.network.domains={"' + host + '"="allow"}']
    if runtime_env_keys is not None:
        # Codex's default core filter drops provider auth AND CLAUDE_CONFIG_DIR.
        # The input environment was constructed by the credential bridge; include_only
        # further pins its keys. No values or credentials are placed in these overrides.
        argv += ['-c', 'shell_environment_policy.inherit="all"',
                 '-c', 'shell_environment_policy.ignore_default_excludes=true',
                 '-c', 'shell_environment_policy.include_only=' + json.dumps(sorted(runtime_env_keys))]
    return [*argv, *command]
