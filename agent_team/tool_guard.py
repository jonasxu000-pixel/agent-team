"""Deterministic PreToolUse decisions and a non-shell approved-test dispatcher."""
from __future__ import annotations

import json
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_team.safety import TeamError, in_root, relative_file, scrub, clean_env
from agent_team.store import Store


def check(spec, event):
    tool, args = event.get('tool_name'), event.get('tool_input', {})
    if tool=='mcp__agent_team__Bash' and spec.get('test_transport')=='mcp':
        tool='Bash'
    cwd = Path(spec['worktree']).resolve()
    if tool in {'Read', 'Edit', 'Write', 'Glob', 'Grep'}:
        raw = args.get('file_path', args.get('path', '.'))
        path = Path(raw)
        path = path if path.is_absolute() else cwd / path
        resolved = path.resolve()
        if not resolved.is_relative_to(cwd):
            allowed_artifact = tool in {'Read', 'Glob', 'Grep'} and any(resolved.is_relative_to(Path(p).resolve()) for p in spec.get('read_artifacts', []))
            if not allowed_artifact:
                raise TeamError('SAFETY_ERROR', 'Tool path escapes its assigned worktree')
            return True
        if path.is_symlink() or any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in path.parents if p != cwd and p.is_relative_to(cwd)):
            raise TeamError('SAFETY_ERROR', 'Links/junctions are not allowed in a tool path')
        name = resolved.relative_to(cwd).as_posix()
        if name != '.':
            in_root(cwd, name)
        if tool in {'Edit', 'Write'} and name not in spec['files']:
            raise TeamError('SAFETY_ERROR', 'File is outside the exact development assignment')
        if tool == 'Read' and resolved.is_file() and resolved.stat().st_size > spec.get('max_context_bytes', 48000):
            if args.get('limit') is None:
                raise TeamError('CONTEXT_TOO_LARGE', 'Read a bounded range instead of the whole oversized file')
    elif tool == 'Bash':
        if args.get('command') not in spec['commands'] or args.get('run_in_background'):
            raise TeamError('SAFETY_ERROR', 'Only exact orchestrator-approved test dispatcher commands may run')
    elif tool == 'Agent':
        if not spec['subagents'] or args.get('subagent_type') != 'expert_read' or args.get('run_in_background'):
            raise TeamError('SAFETY_ERROR', 'Only bounded synchronous read-only specialist delegation is allowed')
    elif tool == 'StructuredOutput':
        from agent_team.schemas import validate
        if not spec.get('output_schema'):
            raise TeamError('SAFETY_ERROR', 'No assigned structured output schema')
        validate(args, spec['output_schema'])
    else:
        raise TeamError('SAFETY_ERROR', 'Tool not assigned to this role')
    return True


def hook(spec, event):
    try:
        check(spec, event)
        if not spec.get('state_dir') or not spec.get('task_id'):
            raise TeamError('SAFETY_ERROR', 'Missing persistent task guard context')
        if spec.get('inbox'):
            from agent_team.tool_broker import request
            request(spec, 'hook', event)
        else:
            store = Store(Path(spec['state_dir']))
            store.guard_tool(spec, event)
        decision, reason = 'allow', 'Tool is within the bounded role, revision and ownership'
    except Exception as error:
        decision, reason = 'deny', str(scrub(str(error)))[:500]
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': decision,
                                  'permissionDecisionReason': reason}}


def main():
    operation, path = sys.argv[1:3]
    spec = json.loads(Path(path).read_text(encoding='utf-8'))
    if operation == 'hook':
        event = json.loads(sys.stdin.read())
        print(json.dumps(hook(spec, event), ensure_ascii=False))
        return 0
    if operation == 'test':
        index = int(sys.argv[3])
        if not 0 <= index < len(spec['tests']):
            return 2
        from agent_team.tool_broker import request
        result = request(spec, 'test', {'index': index}, timeout=180)
        print(scrub(result.get('stdout', '')))
        print(scrub(result.get('stderr', '')), file=sys.stderr)
        return result['exit_code']
    return 2


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TeamError, IndexError) as error:
        print(str(scrub(str(error))), file=sys.stderr)
        raise SystemExit(2)
