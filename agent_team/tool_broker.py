"""Bounded filesystem RPC into the existing runner, not a second coordinator/daemon."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from .safety import TeamError, scrub, write_json


def request(spec, operation, data, *, timeout=18):
    inbox = Path(spec['inbox']).resolve()
    request_id = uuid.uuid4().hex
    message = {'id': request_id, 'invocation': spec['invocation'], 'operation': operation, 'data': data,
               'deadline': time.time() + timeout}
    write_json(inbox / (request_id + '.request.json'), message)
    result_path = inbox / (request_id + '.result.json')
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if result_path.is_file():
            result = json.loads(result_path.read_text(encoding='utf-8'))
            if not result.get('ok'):
                raise TeamError(result.get('kind', 'TOOL_PERMISSION_ERROR'), result.get('reason', 'Tool request rejected'))
            return result['data']
        time.sleep(.05)
    raise TeamError('TOOL_BRIDGE_TIMEOUT', 'Trusted runner did not acknowledge the bounded tool request')


def service(spec, store, execute_test):
    from .tool_guard import check
    inbox = Path(spec['inbox']).resolve()
    for path in sorted(inbox.glob('*.request.json'))[:32]:
        result_path = path.with_name(path.name.replace('.request.json', '.result.json'))
        if result_path.exists():
            continue
        if path.is_symlink() or path.stat().st_size > 64000:
            continue
        try:
            message = json.loads(path.read_text(encoding='utf-8'))
            if message['invocation'] != spec['invocation'] or message['id'] != path.name.split('.')[0] or message['deadline'] < time.time():
                raise TeamError('STALE_COMMAND', 'Expired or unrelated native tool request')
            operation, data = message['operation'], message['data']
            store.assert_dispatch(spec['task_id'], spec['revision'])
            store.check_control(spec['task_id'], in_flight=True)
            if operation == 'hook':
                check(spec, data)
                store.guard_tool(spec, data)
                answer = {'ok': True, 'data': {'permission': 'allow'}}
            elif operation == 'test':
                index = data['index']
                if not spec['commands'] or type(index) is not int or not 0 <= index < len(spec['tests']):
                    raise TeamError('SAFETY_ERROR', 'Unknown approved test index')
                answer = {'ok': True, 'data': execute_test(index)}
            else:
                raise TeamError('SAFETY_ERROR', 'Unsupported tool bridge operation')
        except Exception as error:
            answer = {'ok': False, 'kind': getattr(error, 'kind', 'TOOL_PERMISSION_ERROR'), 'reason': str(scrub(str(error)))[:600]}
        write_json(result_path, answer)
