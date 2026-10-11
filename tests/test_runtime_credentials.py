import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
from unittest.mock import patch

from agent_team.adapters import Answer, ClaudeAdapter, executable
from agent_team.claude_tools import sandbox_command
from agent_team.native_smoke import prepare, verify_evidence, execute
from agent_team.cost_monitor import CostMonitor
from agent_team.process import Result, run_process
from agent_team.runtime_credentials import claude_runtime, provider_credentials
from agent_team.safety import TeamError, scrub
from agent_team.store import Store
from agent_team.streaming import EventSink
from agent_team.workflow import DEFAULTS, Workflow


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='credential-bridge-test-')
        self.root = Path(self.tmp.name).resolve()
        self.settings = self.root / 'settings.json'
        self.secret = 'camel-runtime-example-opaque-12345'
        self.settings.write_text(json.dumps({'env': {'ANTHROPIC_BASE_URL': 'https://api.camel-hub.cn',
                'ANTHROPIC_AUTH_TOKEN': self.secret, 'ANTHROPIC_DEFAULT_SONNET_MODEL': 'configured-sonnet',
                'OPENAI_API_KEY': 'must-not-transfer', 'CLAUDE_CODE_USE_BEDROCK': '1'}}), encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def bridge(self, artifact=None):
        return claude_runtime(artifact or self.root / 'artifact')

    def patch_provider(self):
        return patch('agent_team.runtime_credentials.provider_credentials',
                     return_value=provider_credentials({}, self.settings))

    def test_only_provider_fields_and_explicit_env_precedence(self):
        provider = provider_credentials({'ANTHROPIC_AUTH_TOKEN': 'explicit-example-token'}, self.settings)
        self.assertEqual(provider['ANTHROPIC_AUTH_TOKEN'], 'explicit-example-token')
        self.assertNotIn('OPENAI_API_KEY', provider)
        self.assertNotIn('CLAUDE_CODE_USE_BEDROCK', provider)

    def test_missing_credential_and_wrong_origin_fail_closed(self):
        for env in [{'ANTHROPIC_BASE_URL': 'https://api.camel-hub.cn'},
                    {'ANTHROPIC_BASE_URL': 'https://example.com', 'ANTHROPIC_AUTH_TOKEN': self.secret}]:
            with self.assertRaises(TeamError):
                provider_credentials(env, self.root / 'missing.json')

    def test_config_dir_override_and_malformed_settings(self):
        self.assertIn('ANTHROPIC_AUTH_TOKEN', provider_credentials({'CLAUDE_CONFIG_DIR': str(self.root)}))
        self.settings.write_text('{broken', encoding='utf-8')
        with self.assertRaises(TeamError):
            provider_credentials({}, self.settings)

    def test_runtime_is_scoped_and_parent_environment_unchanged(self):
        with self.patch_provider(), patch.dict(os.environ, {'OPENAI_API_KEY': 'never-in-native',
                'ARBITRARY_CONTEXT': 'never-in-native', 'CLAUDE_CODE_USE_VERTEX': '1'}):
            before = dict(os.environ)
            with self.bridge() as env:
                runtime = Path(env['CLAUDE_CONFIG_DIR'])
                self.assertTrue(runtime.is_dir())
                self.assertNotIn('OPENAI_API_KEY', env)
                self.assertNotIn('ARBITRARY_CONTEXT', env)
                self.assertNotIn('CLAUDE_CODE_USE_VERTEX', env)
                self.assertEqual(scrub('echo ' + self.secret), 'echo [REDACTED]')
                self.assertFalse(any(runtime.iterdir()))
            self.assertFalse(runtime.exists())
            self.assertEqual(env, {})
            self.assertEqual(dict(os.environ), before)

    def test_runtime_cleanup_on_failure(self):
        with self.patch_provider(), self.assertRaises(TeamError):
            with self.bridge() as env:
                runtime = Path(env['CLAUDE_CONFIG_DIR'])
                raise TeamError('INTERRUPTED', 'stop')
        self.assertFalse(runtime.exists())
        self.assertEqual(env, {})

    def test_buffered_split_secret_stays_redacted_after_bridge_cleanup(self):
        store = Store(self.root / 'state')
        task_id = store.create({'repo': str(self.root), 'request': 'fixture', 'config': {}})
        sink = EventSink(store, task_id, 'claude', 'invocation', 1, self.root/'visible.jsonl')
        with self.patch_provider(), self.bridge():
            for fragment in [self.secret[:12], self.secret[12:]]:
                sink.line('stdout', json.dumps({'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': fragment}}}))
        sink.close()
        self.assertNotIn(self.secret, (self.root/'visible.jsonl').read_text())
        self.assertIn('[REDACTED]', (self.root/'visible.jsonl').read_text())

    def test_auth_failure_never_starts_paid_process(self):
        seen = []
        def runner(argv, cwd, **kw):
            seen.append(argv)
            if '--help' in argv:
                return Result(0, '--max-budget-usd --json-schema --no-session-persistence --safe-mode --tools '
                    '--strict-mcp-config --permission-prompts --restricted --allowedTools --disallowedTools dontAsk', '', .01)
            return Result(1, '{"loggedIn":false,"authMethod":"none"}', '', .01)
        config = dict(DEFAULTS, native_claude_tools=True, allow_estimated_budget=True, specialist_files=['context.txt'])
        with self.patch_provider(), patch('agent_team.adapters.executable', return_value='codex.exe'):
            with self.assertRaises(TeamError) as raised:
                ClaudeAdapter(binary='claude.exe', runner=runner).call(self.root, 'claude_native_smoke', 'Read once', self.root/'artifact', config, cap=.04)
        self.assertEqual(raised.exception.kind, 'AUTH_ERROR')
        self.assertFalse(raised.exception.process_started)
        self.assertFalse(any('-p' in argv for argv in seen))
        self.assertNotIn(self.secret, json.dumps(seen))
        self.assertFalse(list((self.root/'artifact'/'runtime').iterdir()))

    def test_authenticated_native_uses_bridge_and_redacts_result(self):
        seen = []
        output = {'status': 'ready', 'role': 'claude-native', 'tools': ['read'], 'workspace': 'verified', 'context_id': self.secret}
        def runner(argv, cwd, **kw):
            seen.append(argv)
            if '--help' in argv:
                return Result(0, '--max-budget-usd --json-schema --no-session-persistence --safe-mode --tools '
                    '--strict-mcp-config --permission-prompts --restricted --allowedTools --disallowedTools dontAsk', '', .01)
            self.assertEqual(kw['env']['ANTHROPIC_AUTH_TOKEN'], self.secret)
            self.assertNotIn('OPENAI_API_KEY', kw['env'])
            if 'auth' in argv:
                return Result(0, json.dumps({'loggedIn': True, 'authMethod': 'oauth_token', 'configDirectory': kw['env']['CLAUDE_CONFIG_DIR']}), '', .01)
            return Result(0, json.dumps({'type': 'result', 'subtype': 'success', 'structured_output': output, 'usage': {}, 'total_cost_usd': .01}), '', .01)
        config = dict(DEFAULTS, native_claude_tools=True, allow_estimated_budget=True, specialist_files=['context.txt'])
        with self.patch_provider(), patch('agent_team.adapters.executable', return_value='codex.exe'):
            answer = ClaudeAdapter(binary='claude.exe', runner=runner).call(self.root, 'claude_native_smoke', 'Read', self.root/'artifact', config, cap=.04)
        self.assertEqual(answer.data['context_id'], '[REDACTED]')
        self.assertNotIn(self.secret, json.dumps(seen))
        self.assertNotIn(self.secret, (self.root/'artifact'/'answer.json').read_text())
        self.assertFalse(list((self.root/'artifact'/'runtime').iterdir()))

    @unittest.skipUnless(os.name == 'nt' and shutil.which('codex'), 'requires Windows and installed Codex sandbox; no inference')
    def test_actual_sandbox_keeps_whitelisted_keys_and_denies_other_credentials(self):
        cwd, artifact = self.root/'workspace', self.root/'artifact'
        cwd.mkdir()
        (artifact/'tool-inbox').mkdir(parents=True)
        with self.patch_provider(), self.bridge(artifact) as env:
            # Accidental launcher additions are also filtered by include_only.
            launcher_env = dict(env, OPENAI_API_KEY='should-never-arrive', OTHER_SECRET='should-never-arrive')
            code = 'import json,os;print(json.dumps({k:bool(os.environ.get(k)) for k in ["ANTHROPIC_AUTH_TOKEN","CLAUDE_CONFIG_DIR","OPENAI_API_KEY","OTHER_SECRET"]}))'
            cmd = sandbox_command(executable('codex'), cwd, artifact, [sys.executable,'-c',code],
                    provider_origin=env['ANTHROPIC_BASE_URL'], writable=False, network=False, runtime_env_keys=env)
            result = run_process(cmd, cwd, env=launcher_env, timeout=30)
            self.assertEqual(result.code, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'ANTHROPIC_AUTH_TOKEN':True,'CLAUDE_CONFIG_DIR':True,'OPENAI_API_KEY':False,'OTHER_SECRET':False})
            self.assertNotIn(self.secret, json.dumps(cmd))

    def test_smoke_preparation_is_isolated_and_has_no_paid_authorization(self):
        store = Store(self.root/'state')
        task_id = prepare(store)
        task = store.task(task_id)
        self.assertEqual(Path(task['repo']).name, 'claude-native-smoke-test')
        self.assertFalse(task['config']['allow_estimated_budget'])
        self.assertEqual(store.calls(task_id), [])
        self.assertEqual(task['config']['max_retries'], 0)
        self.assertEqual(task['config']['claude_max_calls'], 1)

    def test_smoke_rejects_reply_without_actual_read_evidence(self):
        store = Store(self.root/'state')
        task_id = prepare(store)
        task = store.task(task_id)
        with self.assertRaises(TeamError) as raised:
            verify_evidence(store, task_id, {'context_id':task['data']['context_id'], 'tools':['read']}, Path(task['repo']))
        self.assertEqual(raised.exception.kind, 'SMOKE_NOT_VERIFIED')

    def test_smoke_accepts_verified_read_and_refuses_repeat_dispatch(self):
        store = Store(self.root/'state'); task_id = prepare(store); task = store.task(task_id)
        store.event(task_id, 'claude', 'tool_authorized', {'tool':'Read','tool_id':'read1'})
        store.event(task_id, 'claude', 'visible_event', {'content':[{'type':'tool_result','tool_use_id':'read1','content':task['data']['context_id']}]})
        result = verify_evidence(store, task_id, {'context_id':task['data']['context_id'],'tools':['read']}, Path(task['repo']))
        self.assertTrue(result['fixture_unchanged'])
        store.invocation('previous', task_id, 'request', 'claude', 'claude_native_smoke', 1)
        with self.assertRaises(TeamError) as raised:
            execute(store, task_id, approved_budget=.10)
        self.assertEqual(raised.exception.kind, 'PAID_CALL_UNCERTAIN')

    def test_native_budget_denied_before_auth_or_inference(self):
        def runner(argv, cwd, **kw):
            self.assertIn('--help', argv)
            return Result(0, '--max-budget-usd --json-schema --no-session-persistence --safe-mode --tools '
                '--strict-mcp-config --permission-prompts --restricted --allowedTools --disallowedTools dontAsk', '', .01)
        config = dict(DEFAULTS, native_claude_tools=True)
        with patch('agent_team.runtime_credentials.provider_credentials', side_effect=AssertionError('must not read auth')):
            with self.assertRaises(TeamError) as raised:
                ClaudeAdapter(binary='claude.exe', runner=runner).call(self.root, 'claude_native_smoke', 'Read', self.root/'artifact', config, cap=.04)
        self.assertEqual(raised.exception.kind, 'NEEDS_BUDGET_APPROVAL')
        self.assertFalse(raised.exception.process_started)

    def test_small_smoke_cap_keeps_platform_ceiling_and_legacy_floor(self):
        store = Store(self.root/'state'); tid = prepare(store)
        config = dict(store.task(tid)['config'], allow_estimated_budget=True)
        with self.assertRaises(TeamError):
            store.reserve(tid, 'review', config, estimate=.025, multiplier=2.75)
        call_id, cap = store.reserve(tid, 'claude_native_smoke', config, estimate=.025, multiplier=2.75)
        self.assertLess(cap, .05)
        self.assertLessEqual(store.calls(tid)[0]['charged'], .10)
        self.assertAlmostEqual(cap, .10 * .8 / 2.75)

    def smoke_workflow(self, store, *, billing_failure=False, model_failure=False):
        class MockBilling(CostMonitor):
            def preflight(self, config):
                if billing_failure:
                    raise TeamError('BILLING_UNAVAILABLE', 'Mock fresh balance unavailable')
                return {'available_provider_funds_usd': 10, 'records': []}, 1.
            def estimate_specialist(self, *args, **kw):
                return {'status':'ESTIMATED', 'client_estimate_usd':.01, 'multiplier':1., 'platform_range_usd':[0,.02]}
            def after_call(self, task_id, **kw):
                return {}
        class MockNative:
            attempts = 0
            def call(self, cwd, role, prompt, artifact, config, **kw):
                self.attempts += 1
                if model_failure:
                    error = TeamError('AUTH_ERROR', 'Mock provider rejected credential')
                    error.cost = 0.
                    raise error
                contents = (cwd/'context.txt').read_text(encoding='utf-8')
                context_id = contents.split('context_id=')[1].strip()
                spec = {'task_id':config['task_id'],'revision':config['task_revision'],'invocation':artifact.name,
                        'max_tools':2,'subagents':0}
                store.guard_tool(spec, {'tool_name':'Read','tool_use_id':'mock-read1'})
                kw['on_line']('stdout', json.dumps({'type':'user','message':{'content':[
                    {'type':'tool_result','tool_use_id':'mock-read1','content':contents}]}}))
                return Answer({'status':'ready','role':'claude-native','tools':['read'],'workspace':'verified',
                               'context_id':context_id}, {'input_tokens':1,'output_tokens':1}, .01, .01)
        native = MockNative()
        workflow = Workflow(store, claude=native, cost_monitor=MockBilling(store))
        return workflow, native

    def test_mock_smoke_full_flow_keeps_ledger_and_real_fixture_evidence(self):
        store = Store(self.root/'state'); task_id = prepare(store)
        workflow, native = self.smoke_workflow(store)
        report = execute(store, task_id, approved_budget=.10, workflow=workflow)
        self.assertEqual(report['status'], 'SMOKE_VERIFIED')
        self.assertEqual(native.attempts, 1)
        self.assertEqual(len(store.calls(task_id)), 1)
        self.assertEqual(store.calls(task_id)[0]['status'], 'COMPLETED')
        self.assertTrue(report['acceptance']['fixture_unchanged'])
        self.assertEqual(report['acceptance']['completed_read_ids'], ['mock-read1'])

    def test_mock_smoke_billing_unknown_blocks_before_any_model(self):
        store = Store(self.root/'state'); task_id = prepare(store)
        workflow, native = self.smoke_workflow(store, billing_failure=True)
        with self.assertRaises(TeamError) as raised:
            execute(store, task_id, approved_budget=.10, workflow=workflow)
        self.assertEqual(raised.exception.kind, 'BILLING_UNAVAILABLE')
        self.assertEqual(native.attempts, 0)
        self.assertEqual(store.calls(task_id), [])
        self.assertTrue((store.root/'tasks'/task_id/'native-smoke-report.json').exists())

    def test_mock_smoke_model_failure_stops_and_never_repeats(self):
        store = Store(self.root/'state'); task_id = prepare(store)
        workflow, native = self.smoke_workflow(store, model_failure=True)
        with self.assertRaises(TeamError):
            execute(store, task_id, approved_budget=.10, workflow=workflow)
        self.assertEqual(native.attempts, 1)
        self.assertEqual(store.task(task_id)['status'], 'WAITING_APPROVAL')
        with self.assertRaises(TeamError) as raised:
            execute(store, task_id, approved_budget=.10, workflow=workflow)
        self.assertEqual(raised.exception.kind, 'PAID_CALL_UNCERTAIN')
        self.assertEqual(native.attempts, 1)


if __name__ == '__main__':
    unittest.main()
