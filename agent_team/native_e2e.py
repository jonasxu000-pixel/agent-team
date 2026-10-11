"""Isolated Native Developer acceptance using existing workflow calls and ownership."""
import hashlib
import json
from pathlib import Path
import uuid

from .safety import TeamError, write_json
from .user_config import authorize, load
from .workflow import DEFAULTS, Workflow


def prepare(store, budget=8.):
    workflow = Workflow(store)
    repo = store.root / 'e2e' / uuid.uuid4().hex[:8] / 'claude-native-developer-e2e'
    repo.mkdir(parents=True)
    context_id = uuid.uuid4().hex
    (repo/'app.py').write_text(f'CONTEXT_ID = "{context_id}"\n\ndef answer():\n    return "before"\n', encoding='utf-8')
    (repo/'verify.py').write_text(
        'from pathlib import Path\nns = {}\nexec(Path("app.py").read_text(encoding="utf-8"), ns)\n'
        f'assert ns["CONTEXT_ID"] == "{context_id}"\nassert ns["answer"]() == "after"\n'
        f'assert Path("created.txt").read_text(encoding="utf-8").strip() == "verified:{context_id}"\n'
        f'print("native-e2e-ok:{context_id}")\n', encoding='utf-8')
    for args in [('init','-b','main'),('config','core.longpaths','true'),('config','user.name','Agent Team E2E'),
                 ('config','user.email','e2e@example.invalid'),('add','app.py','verify.py'),('commit','-m','isolated native developer fixture')]:
        workflow.git.git(repo,*args)
    import sys
    config = authorize(dict(DEFAULTS, policy='selective',allow_estimated_budget=True,
        role_workflow_version=3, global_claude_budget=None, claude_max_calls=4, claude_max_reviews=0,
        claude_call_cap=None, max_retries=0, max_repairs=3, claude_subagent_limit=0,
        claude_max_tool_calls=16, claude_developer_tokens=1200, claude_context_bytes=8192,
        agent_timeout=240, task_timeout=7200, test_timeout=45, test_commands=[[sys.executable,'verify.py']],
        native_e2e=True),load(store.root),explicit_budget=budget,unattended=True)
    request = ('Read app.py and obtain its random CONTEXT_ID. Use Edit to change only answer() from "before" to "after", '
        'preserving CONTEXT_ID. Use Write to create created.txt containing "verified:<CONTEXT_ID>" followed by a newline. '
        'Use Bash exactly once with the approved test dispatcher, then return the structured developer handoff. '
        'Do not change verify.py, run other commands, delegate, commit or access other projects.')
    tid=workflow.create(repo,request,config)
    task=store.task(tid)
    workflow.save(task,native_e2e=True,context_id=context_id,plan={'expected_files':['app.py','created.txt'],
        'expected_claude_gain':'Explicit isolated native developer acceptance', 'claude_developer_gain':'Read/Edit/Write/approved test acceptance'},
        immutable_test_hash=hashlib.sha256((repo/'verify.py').read_bytes()).hexdigest(),repair_round=-1)
    store.update(tid,phase='native_e2e')
    store.event(tid,'user','e2e_budget_authorized',{'platform_budget_usd':budget,'max_repairs':3,'max_claude_calls':4,'unattended':True})
    return tid


def evidence(store, task, cwd):
    events=store.events(task['id']);latest=next(i for i in reversed(store.invocations(task['id'])) if i['agent']=='claude')
    visible=[e['payload'] for e in events if e['kind']=='visible_event' and e['payload'].get('invocation')==latest['id']]
    authorized={e['payload']['tool_id']:e['payload']['tool'] for e in events
                if e['kind']=='tool_authorized' and e['payload'].get('invocation')==latest['id']}
    requests={p['id']:p for v in visible for p in v.get('content',[]) if p.get('type')=='tool_use'}
    results={p['tool_use_id']:p for v in visible for p in v.get('content',[]) if p.get('type')=='tool_result'}
    completed={name:[] for name in ('Read','Edit','Write','Bash')}
    for tool_id,name in authorized.items():
        if name=='mcp__agent_team__Bash':name='Bash'
        if name not in completed or tool_id not in results or tool_id not in requests:
            continue
        result=json.dumps(results[tool_id].get('content'),ensure_ascii=False)
        if name=='Read':
            path=Path(requests[tool_id].get('input',{}).get('file_path',''))
            if path.resolve()!= (cwd/'app.py').resolve() or task['data']['context_id'] not in result:
                continue
        if any(marker in result.lower() for marker in ('permission denied','hook error','is_error": true')):
            continue
        completed[name].append(tool_id)
    if not all(completed.values()):
        raise TeamError('E2E_NOT_VERIFIED','Missing real successful tool evidence: '+','.join(k for k,v in completed.items() if not v))
    if hashlib.sha256((cwd/'verify.py').read_bytes()).hexdigest()!=task['data']['immutable_test_hash']:
        raise TeamError('E2E_NOT_VERIFIED','Immutable test fixture changed')
    if not any(t['stage']=='specialist' and t['exit_code']==0 and t.get('sandbox') for t in task['data'].get('tests',[])):
        raise TeamError('E2E_NOT_VERIFIED','No real runner-approved sandbox test success')
    return {'completed_tools':completed,'immutable_test_unchanged':True,'invocation':latest['id']}


def execute(store, tid, *, repair_round=0, repair_proof=None):
    task=store.task(tid)
    if not task['data'].get('native_e2e') or Path(task['repo']).name!='claude-native-developer-e2e':
        raise TeamError('SAFETY_ERROR','Only the isolated prepared acceptance task may execute')
    if task['status']=='NATIVE_E2E_VERIFIED':
        raise TeamError('STATE_ERROR','Acceptance already verified; no additional model calls')
    reuse_delivery = bool(task['data'].get('claude_commit') and task['data'].get('real_tools'))
    validation_resume = reuse_delivery and repair_round==task['data']['repair_round']
    if not 0<=repair_round<=3 or (repair_round != task['data']['repair_round']+1 and not validation_resume):
        raise TeamError('REPAIR_LIMIT','Initial attempt plus at most three explicitly recorded repair rounds')
    if repair_round and not validation_resume:
        if not repair_proof or not repair_proof.get('analysis') or not repair_proof.get('changed_files') or repair_proof.get('free_tests_exit')!=0:
            raise TeamError('REPAIR_EVIDENCE_REQUIRED','Analyze failure, change code and pass no-cost tests before another paid attempt')
        if any(i['status'] in {'DISPATCHED','UNKNOWN_RESULT'} for i in store.invocations(tid) if i['agent']=='claude'):
            raise TeamError('PAID_CALL_UNCERTAIN','Unknown paid result blocks a repeat')
    workflow=Workflow(store)
    final_error=None
    with store.claim(tid):
        task=store.task(tid)
        workflow.save(task,repair_round=repair_round,repair_proof=repair_proof)
        cwd=workflow.git.ensure(task['repo'],tid,'claude-developer-r'+str(repair_round),task['base'])
        before=workflow.git.git(cwd,'rev-parse','HEAD')
        result=None
        try:
            # Only one CLI invocation per round; internal turns cover tool work.
            if validation_resume:
                saved=next(d for d in store.deliveries(tid) if d['stage']=='native-e2e' and d['status']=='COMPLETED')['payload']
                output,head,files=saved['output'],saved['commit'],saved['files']
                workflow.git.assert_head(cwd,head)
                proof=task['data']['real_tools']
                workflow.emit(task,'orchestrator','reuse_native_delivery',{'commit':head,'paid_call':False})
            else:
                output=workflow.call(task,'claude','claude_native_develop', task['request']+
                    '\nAcceptance round: '+str(repair_round)+'. For the test use mcp__agent_team__Bash with the exact enum command in its schema. '
                    'This is the approved runner test bridge, not a general shell.',cwd,
                    stage={'id':'native-e2e-'+str(repair_round),'files':['app.py','created.txt'],'platform_budget':min(.75,task['config']['claude_budget'])})
                proof=evidence(store,task,cwd)
                head,files=workflow.git.commit(cwd,before,tid,'native-e2e',allowed_files=['app.py','created.txt'])
            diff=workflow.git.diff(cwd,task['base'],head,max_chars=8000)
            workflow.save(task,claude_commit=head,changed_files=files,real_tools=proof)
            store.delivery(tid,task['revision'],'native-e2e','COMPLETED',{'output':output,'commit':head,'files':files})
            workflow.emit(task,'orchestrator','native_developer_handoff',{'output':output,'commit':head,'diff':diff,'tools':proof})
            verify_cwd=workflow.git.ensure(task['repo'],tid,'codex-verify',head,detached=True)
            verification=workflow.call(task,'codex','specialist_verify',
                'Independently inspect app.py, created.txt and immutable verify.py. Do not edit. '
                'Check exact requested behavior and actual diff/test evidence; do not trust model completion claims.\n'+
                json.dumps({'task':task['request'],'claude':output,'diff':diff,'tools':proof,'tests':task['data'].get('tests',[])},ensure_ascii=False),verify_cwd)
            workflow.emit(task,'codex','native_developer_verified',verification)
            if not verification['accepted']:
                raise TeamError('E2E_NOT_VERIFIED','Codex did not accept the real developer delivery')
            integration=workflow.git.ensure(task['repo'],tid,'integration',task['base'])
            workflow.git.integrate(integration,head)
            tested=workflow.run_specialist_test(task,integration,task['config'],0)
            if tested['exit_code'] or not tested['sandbox']:
                raise TeamError('E2E_NOT_VERIFIED','Final independent sandbox test failed')
            if workflow.git.changed(cwd) or workflow.git.changed(integration):
                raise TeamError('E2E_NOT_VERIFIED','Unexpected post-test file changes')
            if workflow.git.git(Path(task['repo']),'rev-parse','HEAD')!=task['base']:
                raise TeamError('E2E_NOT_VERIFIED','Source fixture main branch changed')
            result={'tools':proof,'claude_commit':head,'files':files,'diff':diff,'codex_verification':verification,
                    'final_test':tested,'integration_worktree':str(integration),'source_main_unchanged':True}
            workflow.save(task,native_e2e_acceptance=result)
            store.update(tid,status='NATIVE_E2E_VERIFIED')
        except TeamError as error:
            final_error=error
            store.update(tid,status='WAITING_APPROVAL')
            workflow.emit(task,'orchestrator','native_e2e_failed',{'kind':error.kind,'reason':str(error),'round':repair_round})
    if store.calls(tid):
        workflow.cost_monitor.after_call(tid)
    report={'task_id':tid,'status':store.task(tid)['status'],'round':repair_round,'acceptance':result,
            'error':{'kind':final_error.kind,'reason':str(final_error)} if final_error else None,
            'calls':store.calls(tid),'costs':workflow.cost_monitor.summary(tid),'worktree':str(cwd)}
    write_json(store.root/'tasks'/tid/('native-e2e-round-'+str(repair_round)+'.json'),report)
    if final_error:raise final_error
    return report
