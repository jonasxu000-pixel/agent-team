"""One bounded real smoke invocation using the existing Workflow, Store and billing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from .safety import TeamError, finite_positive, write_json
from .user_config import authorize, load
from .workflow import DEFAULTS, Workflow


def prepare(store, *, ceiling=.10):
    finite_positive(ceiling, 'smoke platform budget')
    workflow = Workflow(store)
    repo = store.root / 'smoke' / uuid.uuid4().hex[:8] / 'claude-native-smoke-test'
    repo.mkdir(parents=True)
    context_id = uuid.uuid4().hex
    (repo / 'context.txt').write_text('Isolated Claude native smoke context.\ncontext_id=' + context_id + '\n', encoding='utf-8')
    for args in [('init', '-b', 'main'), ('config', 'core.longpaths', 'true'), ('config', 'user.name', 'Agent Team Smoke'),
                 ('config', 'user.email', 'smoke@example.invalid'), ('add', 'context.txt'),
                 ('commit', '-m', 'isolated read-only smoke fixture')]:
        workflow.git.git(repo, *args)
    config = authorize(dict(DEFAULTS, policy='selective', allow_estimated_budget=False,
                            role_workflow_version=3, claude_max_calls=1, max_retries=0,
                            global_claude_budget=None, claude_call_cap=None, claude_document_tokens=256,
                            claude_max_tool_calls=2, claude_subagent_limit=0, agent_timeout=90,
                            allow_unknown_billing=False, claude_context_bytes=4096), load(store.root),
                       explicit_budget=ceiling)
    request = ('Use only Read to read context.txt exactly once. It is the complete isolated task context. '
               'Do not scan, edit, execute commands or delegate. Return the structured ready receipt with '
               'tools=["read"], workspace="verified", role="claude-native", and context_id copied from the file.')
    task_id = workflow.create(repo, request, config)
    task = store.task(task_id)
    workflow.save(task, smoke_only=True, context_id=context_id, plan={'expected_files': ['context.txt'],
                  'expected_claude_gain': 'Explicit minimal native authentication and Read acceptance'},
                  fixture_sha256=hashlib.sha256((repo / 'context.txt').read_bytes()).hexdigest())
    store.update(task_id, phase='native_smoke')
    return task_id


def verify_evidence(store, task_id, output, cwd):
    task = store.task(task_id)
    if output.get('context_id') != task['data']['context_id'] or output.get('tools') != ['read']:
        raise TeamError('SMOKE_NOT_VERIFIED', 'Receipt does not match the actual isolated file context')
    events = store.events(task_id)
    read_ids = {e['payload']['tool_id'] for e in events if e['kind'] == 'tool_authorized' and e['payload'].get('tool') == 'Read'}
    completed = set()
    for event in events:
        for part in event['payload'].get('content', []):
            if (part.get('type') == 'tool_result' and part.get('tool_use_id') in read_ids
                    and task['data']['context_id'] in json.dumps(part.get('content'))):
                completed.add(part['tool_use_id'])
    if not completed:
        raise TeamError('SMOKE_NOT_VERIFIED', 'No authorized Read plus real tool-result evidence; model reply alone is insufficient')
    actual = hashlib.sha256((cwd / 'context.txt').read_bytes()).hexdigest()
    if actual != task['data']['fixture_sha256'] or Workflow(store).git.changed(cwd):
        raise TeamError('SMOKE_NOT_VERIFIED', 'Read-only smoke fixture changed')
    return {'status': 'verified', 'authorized_read_ids': sorted(read_ids), 'completed_read_ids': sorted(completed),
            'fixture_unchanged': True, 'model_receipt': output}


def execute(store, task_id, *, approved_budget, workflow=None):
    finite_positive(approved_budget, 'explicitly approved smoke budget')
    task = store.task(task_id)
    if not task['data'].get('smoke_only') or Path(task['repo']).name != 'claude-native-smoke-test':
        raise TeamError('SAFETY_ERROR', 'Smoke execution accepts only its isolated prepared fixture')
    if approved_budget > task['config']['claude_budget']:
        raise TeamError('NEEDS_BUDGET_APPROVAL', 'Prepare a reviewable task with the new ceiling before executing')
    if store.invocations(task_id) or store.calls(task_id):
        raise TeamError('PAID_CALL_UNCERTAIN', 'Smoke already attempted; inspect evidence, never automatically retry')
    workflow = workflow or Workflow(store)
    config = dict(task['config'], allow_estimated_budget=True, budget_permission='explicit', claude_budget=approved_budget)
    store.update(task_id, config=config)
    result = None
    failure = None
    with store.claim(task_id):
        task = store.task(task_id)
        cwd = workflow.git.ensure(task['repo'], task_id, 'claude-smoke', task['base'], detached=True)
        try:
            output = workflow.call(task, 'claude', 'claude_native_smoke', task['request'], cwd,
                                   stage={'id': 'native-smoke', 'files': ['context.txt'], 'platform_budget': approved_budget})
            result = verify_evidence(store, task_id, output, cwd)
            store.update(task_id, status='SMOKE_VERIFIED')
            workflow.emit(task, 'orchestrator', 'native_smoke_verified', result)
        except TeamError as error:
            failure = error
            store.update(task_id, status='WAITING_APPROVAL')
            workflow.emit(task, 'orchestrator', 'native_smoke_stopped', {'kind': error.kind, 'reason': str(error), 'automatic_retry': False})
    # A billing query retries only read-only reconciliation, never inference.
    if failure and store.calls(task_id):
        try:
            workflow.cost_monitor.after_call(task_id)
        except TeamError as error:
            workflow.emit(task, 'cost_monitor', 'billing_query_failed', {'kind': error.kind, 'reason': str(error)})
    report = {'task_id': task_id, 'status': store.task(task_id)['status'], 'worktree': str(cwd),
              'acceptance': result, 'error': {'kind': failure.kind, 'reason': str(failure)} if failure else None,
              'costs': workflow.cost_monitor.summary(task_id), 'calls': store.calls(task_id),
              'automatic_retry': False, 'budget_is_client_soft_limit': True}
    write_json(store.root / 'tasks' / task_id / 'native-smoke-report.json', report)
    if failure:
        raise failure
    return report
