import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
from unittest.mock import patch

from agent_team.adapters import executable
from agent_team.claude_tools import prepare_policy,sandbox_command
from agent_team.cost_monitor import correlate
from agent_team.delivery import failure_kind
from agent_team.native_runtime import native_runtime
from agent_team.process import run_process
from agent_team.safety import TeamError
from agent_team.store import Store
from agent_team.tool_broker import service
from agent_team.tool_guard import check


class NativeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='at-v32-test-')
        self.root=Path(self.temp.name).resolve()
    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_is_short_and_removed_on_error(self):
        with self.assertRaises(TeamError):
            with native_runtime(self.root/'artifact') as root:
                self.assertLess(len(str(root)),120)
                self.assertTrue((root/'inbox').exists())
                raise TeamError('PAUSED','control')
        self.assertFalse(root.exists())

    def test_structured_output_is_schema_checked_and_has_no_shell(self):
        cwd=self.root/'work';cwd.mkdir()
        p=prepare_policy(cwd,self.root/'artifact',{'test_commands':[]},'claude_native_smoke')
        valid={'status':'ready','role':'claude-native','tools':['read'],'workspace':'verified','context_id':'x'}
        self.assertTrue(check(p['spec'],{'tool_name':'StructuredOutput','tool_input':valid}))
        with self.assertRaises(TeamError):check(p['spec'],{'tool_name':'StructuredOutput','tool_input':{'command':'curl evil'}})

    def test_broker_rejects_stale_test_without_executing(self):
        store=Store(self.root/'state');tid=store.create({'repo':str(self.root),'request':'fixture','config':{}})
        inbox=self.root/'inbox';inbox.mkdir()
        import time
        message={'id':'one','invocation':'i','deadline':time.time()+20,'operation':'test','data':{'index':0}}
        (inbox/'one.request.json').write_text(json.dumps(message))
        spec={'inbox':str(inbox),'task_id':tid,'revision':99,'invocation':'i','commands':['approved'],'tests':[['python','test.py']]}
        called=[];service(spec,store,lambda i:called.append(i))
        self.assertEqual(called,[])
        self.assertFalse(json.loads((inbox/'one.result.json').read_text())['ok'])

    def test_parallel_tickets_are_atomic_unique_and_accounted_once(self):
        import threading,time
        from agent_team.tool_broker import request
        store=Store(self.root/'state');cwd=self.root/'work';cwd.mkdir();(cwd/'app.py').write_text('fixture')
        tid=store.create({'repo':str(cwd),'request':'concurrent free probe','config':{}})
        with native_runtime(self.root/'artifact') as runtime:
            policy=prepare_policy(cwd,self.root/'artifact',{'test_commands':[]},'claude_design',
                task_id=tid,state_dir=store.root,runtime_dir=runtime)
            outputs=[];errors=[]
            def client(index):
                try:outputs.append(request(policy['spec'],'hook',{'tool_name':'Read',
                    'tool_input':{'file_path':str(cwd/'app.py')},'tool_use_id':'parallel-'+str(index)},timeout=5))
                except Exception as e:errors.append(e)
            threads=[threading.Thread(target=client,args=(i,)) for i in range(4)]
            for thread in threads:thread.start()
            while any(thread.is_alive() for thread in threads):
                service(policy['spec'],store,lambda i:None);time.sleep(.01)
            for thread in threads:thread.join()
            self.assertEqual(errors,[])
            self.assertEqual(len(outputs),4)
            receipts=[e for e in store.events(tid) if e['kind']=='tool_authorized']
            self.assertEqual(len(receipts),4)
            service(policy['spec'],store,lambda i:None)
            self.assertEqual(len([e for e in store.events(tid) if e['kind']=='tool_authorized']),4)

    def test_error_labels_use_receipt_not_broad_budget_words(self):
        env={'subtype':'error_max_structured_output_retries','permission_denials':[{'tool_name':'Read'}]}
        self.assertEqual(failure_kind(env,''),'TOOL_PERMISSION_ERROR')
        self.assertEqual(failure_kind({'subtype':'error_max_structured_output_retries'},''),'STRUCTURED_OUTPUT_FAILED')

    def test_mcp_interface_rejects_arbitrary_shell_and_unknown_tools(self):
        from agent_team.test_mcp import handle
        spec={'commands':['exact approved command']}
        self.assertEqual(handle(spec,{'method':'tools/list'})['tools'][0]['name'],'Bash')
        for args in [{'command':'curl https://example.com'}, {'command':'exact approved command','extra':1}]:
            with self.assertRaises(TeamError):
                handle(spec,{'method':'tools/call','params':{'name':'Bash','arguments':args}})

    def test_verified_acceptance_and_excess_repair_do_not_dispatch(self):
        from agent_team.native_e2e import execute
        store=Store(self.root/'state')
        tid=store.create({'repo':str(self.root/'claude-native-developer-e2e'),'request':'fixture','config':{}})
        store.update(tid,data={'native_e2e':True,'repair_round':0},status='NATIVE_E2E_VERIFIED')
        with self.assertRaises(TeamError) as error:execute(store,tid,repair_round=1,repair_proof={})
        self.assertEqual(error.exception.kind,'STATE_ERROR')
        store.update(tid,status='WAITING_APPROVAL')
        with self.assertRaises(TeamError) as error:execute(store,tid,repair_round=4,repair_proof={})
        self.assertEqual(error.exception.kind,'REPAIR_LIMIT')
        self.assertEqual(store.calls(tid),[])

    @unittest.skipUnless(os.name=='nt' and shutil.which('codex'), 'requires local Codex sandbox; model replies remain Mock')
    def test_validation_recovery_reuses_paid_delivery_no_second_claude(self):
        from agent_team.native_e2e import prepare,execute
        from agent_team.workflow import Workflow
        from agent_team.cost_monitor import CostMonitor
        from agent_team.adapters import Answer, CodexAdapter
        store=Store(self.root/'state');tid=prepare(store)
        class MockBilling(CostMonitor):
            def preflight(self,config):return {'records':[],'available_provider_funds_usd':10},1.
            def estimate_specialist(self,*a,**kw):
                return {'status':'ESTIMATED','client_estimate_usd':.01,'multiplier':1.,'platform_range_usd':[0,.02]}
            def after_call(self,*a,**kw):return {}
        class MockCodex:
            binary=executable('codex')
            attempts=0
            def test_command(self,cwd,argv):
                return CodexAdapter(binary=self.binary).test_command(cwd,argv)
            def call(self,*a,**kw):
                self.attempts+=1
                if self.attempts==1:raise TeamError('QUOTA_EXHAUSTED','Mock subscription unavailable after Claude delivery')
                return Answer({'summary':'Mock recovered validation','accepted':True,'evidence':['fixture diff matches'],'required_changes':[]},{},.01)
        class MockClaude:
            attempts=0
            def call(self,cwd,role,prompt,artifact,config,**kw):
                self.attempts+=1
                text=(cwd/'app.py').read_text(encoding='utf-8');nonce=text.split('"')[1]
                (cwd/'app.py').write_text(text.replace('"before"','"after"'),encoding='utf-8')
                (cwd/'created.txt').write_text('verified:'+nonce+'\n')
                spec={'task_id':tid,'revision':1,'invocation':artifact.name,'max_tools':16,'subagents':0}
                for name,path in [('Read','app.py'),('Edit','app.py'),('Write','created.txt'),('mcp__agent_team__Bash','')]:
                    store.guard_tool(spec,{'tool_name':name,'tool_use_id':name})
                    kw['on_line']('stdout',json.dumps({'type':'assistant','message':{'content':[
                        {'type':'tool_use','id':name,'name':name,'input':{'file_path':str(cwd/path)}}]}}))
                    kw['on_line']('stdout',json.dumps({'type':'user','message':{'content':[
                        {'type':'tool_result','tool_use_id':name,'content':text if name=='Read' else 'success'}]}}))
                workflow.run_specialist_test(workflow.test_task,cwd,config,0)
                return Answer({'summary':'Mock native fixture changes','changed_files':['app.py','created.txt'],'test_notes':'runner test','remaining':[]},{},.01,.01)
        codex,claude=MockCodex(),MockClaude()
        workflow=Workflow(store,codex=codex,claude=claude,cost_monitor=MockBilling(store))
        original_call=workflow.call
        def scoped_call(task,*args,**kwargs):
            workflow.test_task=task
            return original_call(task,*args,**kwargs)
        workflow.call=scoped_call
        with patch('agent_team.native_e2e.Workflow',return_value=workflow):
            with self.assertRaises(TeamError):execute(store,tid)
            self.assertEqual(claude.attempts,1)
            self.assertIsNotNone(store.task(tid)['data'].get('claude_commit'))
            store.set_health('codex','AVAILABLE',source='Mock explicit subscription recovery')
            report=execute(store,tid)
        self.assertEqual(report['status'],'NATIVE_E2E_VERIFIED')
        self.assertEqual(claude.attempts,1)
        self.assertEqual(len(store.calls(tid)),1)

    def test_aggregate_bills_require_exact_complete_usage_and_unique_ids(self):
        from datetime import datetime,timezone
        now=datetime.now(timezone.utc);created=now.timestamp()
        rows=[{'type':2,'model':'m','created_at':created,'request_id':str(i),'input_tokens':1,
               'output_tokens':2,'cache_read_tokens':3,'cache_write_tokens':4,'billed_cost':.01,
               'wallet_usd':0.,'subscription_usd':.01,'gift_usd':0.} for i in range(5)]
        c={'id':'c','payload':{'model':'m','usage':{'input_tokens':5,'output_tokens':10,
            'cache_read_input_tokens':15,'cache_creation_input_tokens':20}}}
        events=[{'kind':'call_started','payload':{'call_id':'c'},'at':now.isoformat()}]
        snap={'records':rows,'configured_model':'m','queried_at':now.isoformat()}
        self.assertAlmostEqual(correlate(c,events,snap)['amount'],.05)
        rows[0]['request_id']='1'
        self.assertIsNone(correlate(c,events,snap)['amount'])

    @unittest.skipUnless(os.name=='nt' and shutil.which('codex'), 'requires Windows and installed Codex sandbox; no inference')
    def test_actual_read_edit_write_bash_dispatcher_no_model(self):
        # Same sandbox/hook/broker/test path, with a trusted test actor replacing
        # inference only. It is evidence for plumbing, not real Claude acceptance.
        store=Store(self.root/'state');cwd=self.root/'workspace';cwd.mkdir()
        (cwd/'app.py').write_text('VALUE = 0\n')
        (cwd/'verify.py').write_text('from pathlib import Path\nns={}\nexec(Path("app.py").read_text(),ns)\nassert ns["VALUE"]==2\nassert Path("created.txt").read_text()=="verified"\nprint("test-ok")\n')
        tid=store.create({'repo':str(cwd),'request':'no-model native chain','config':{}})
        store.workspace(tid,'native',cwd)
        art=store.root/'tasks'/tid/'calls'/('f'*32)
        git_root=Path('C:/Program Files/Git')
        with native_runtime(art) as runtime:
            policy=prepare_policy(cwd,art,{'specialist_files':['app.py','created.txt'],
                'test_commands':[[sys.executable,'verify.py']],'native_test_transport':'mcp'},'claude_native_develop',task_id=tid,state_dir=store.root,runtime_dir=runtime)
            def execute_test(index):
                cmd=sandbox_command(executable('codex'),cwd,art,policy['spec']['tests'][index],
                    provider_origin='https://api.camel-hub.cn',writable=True,network=False)
                r=run_process(cmd,cwd,timeout=20)
                return {'exit_code':r.code,'stdout':r.stdout,'stderr':r.stderr}
            actor='''import json,subprocess,sys
from pathlib import Path
from agent_team.tool_guard import hook
spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
out=[]
for name,args in [('Read',{'file_path':str(Path.cwd()/'app.py')}),('Edit',{'file_path':str(Path.cwd()/'app.py')}),('Write',{'file_path':str(Path.cwd()/'created.txt')}),('mcp__agent_team__Bash',{'command':spec['commands'][0]})]:
 decision=hook(spec,{'tool_name':name,'tool_input':args,'tool_use_id':name})
 if decision['hookSpecificOutput']['permissionDecision']!='allow':raise RuntimeError(decision)
 if name=='Read':out.append(Path('app.py').read_text())
 elif name=='Edit':Path('app.py').write_text('VALUE = 2\\n')
 elif name=='Write':Path('created.txt').write_text('verified')
 else:
  messages=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/list'},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'Bash','arguments':{'command':spec['commands'][0]}}}]
  r=subprocess.run([sys.executable,sys.argv[2],sys.argv[1]],input=''.join(json.dumps(m)+'\\n' for m in messages),capture_output=True,text=True)
  reply=json.loads(r.stdout.splitlines()[-1])['result']
  out.append(json.loads(reply['content'][0]['text']))
print(json.dumps(out))
'''
            # Python -c doesn't add the trusted package root automatically.
            actor='import sys;sys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+');\n'+actor
            cmd=sandbox_command(executable('codex'),cwd,art,[sys.executable,'-c',actor,str(policy['spec_path']),str(Path(__file__).resolve().parents[1]/'agent_team/test_mcp.py')],
                provider_origin='https://api.camel-hub.cn',writable=True,network=False,runtime_dir=runtime)
            r=run_process(cmd,cwd,timeout=40,control=lambda:service(policy['spec'],store,execute_test))
            self.assertEqual(r.code,0,r.stderr)
            out=json.loads(r.stdout)
            self.assertEqual(out[-1]['exit_code'],0,out[-1])
            self.assertIn('test-ok',out[-1]['stdout'])
            self.assertEqual((cwd/'app.py').read_text(),'VALUE = 2\n')


if __name__=='__main__':unittest.main()
