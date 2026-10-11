from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import time

from test_team import Fixture, FakeCodex, PLAN
from agent_team.adapters import Answer, ClaudeAdapter
from agent_team.claude_tools import prepare_policy
from agent_team.controls import Controls
from agent_team.cost_monitor import CostMonitor, normalise_record
from agent_team.delivery import complete_sections, repair_json
from agent_team.router import route
from agent_team.safety import TeamError
from agent_team.schemas import SCHEMAS, validate
from agent_team.store import Store
from agent_team.tool_guard import check, hook
from agent_team.user_config import authorize, load, save, shanghai_month
from agent_team.workflow import DEFAULTS, Workflow


BRIEF = {'product': 'fixture', 'users': 'test user', 'pages': ['one'], 'interactions': ['button'],
         'visual_issues': ['contrast'], 'preserve': ['behavior'], 'constraints': ['no new dependencies'],
         'source_files': ['value.py'], 'visual_evidence': 'No actual screenshot input available'}


class CodexV3(FakeCodex):
    def __init__(self, kind='algorithm'):
        super().__init__()
        self.kind = kind
    def call(self, cwd, role, prompt, artifact, config, **kw):
        if role == 'plan_v3':
            self.calls.append((role, str(cwd), prompt))
            files = ['style.css'] if self.kind == 'ui_ux' else ['value.py']
            return Answer(dict(PLAN, expected_files=files, task_type=self.kind, claude_developer_gain='independent bounded specialist implementation',
                               design_brief=dict(BRIEF, source_files=files), reproduction={'evidence': 'approved test reproduces expected failure',
                               'test_commands': ['test.py'], 'module_boundary': files}, subagent_gain=''), {'input_tokens': 1}, .01)
        if role == 'specialist_verify':
            self.calls.append((role, str(cwd), prompt))
            return Answer({'summary': 'independently inspected real source and document', 'accepted': True,
                           'evidence': ['actual file exists and matches bounded task'], 'required_changes': []}, {'input_tokens': 1}, .01)
        if self.kind == 'ui_ux' and role in {'develop', 'fix'}:
            self.calls.append((role, str(cwd), prompt))
            return Answer({'summary': 'preserved real specialist CSS; orchestrator runs tests', 'changed_files': [], 'test_notes': 'test.py', 'remaining': []}, {}, .01)
        return super().call(cwd, role, prompt, artifact, config, **kw)


class ClaudeV3:
    def __init__(self, *, failure=None, partial_code=False, pause=False, amend=False):
        self.calls, self.failure, self.partial_code = [], failure, partial_code
        self.pause, self.amend, self.store, self.task_id = pause, amend, None, None
    def call(self, cwd, role, prompt, artifact, config, **kw):
        self.calls.append((role, str(cwd), prompt, kw.get('cap')))
        if role == 'claude_native_develop':
            target = cwd / config['specialist_files'][0]
            target.write_text('body { color: blue; }\n' if target.suffix == '.css' else 'VALUE = 2\n', encoding='utf-8')
            if self.pause:
                self.pause = False
                self.store.submit(self.task_id, 'pause')
            if self.amend:
                self.amend = False
                self.store.submit(self.task_id, 'amend', payload={'change': 'Preserve completed code and reassess the updated task'})
            if self.partial_code:
                error = TeamError('OUTPUT_TRUNCATED', 'Mock: code written but final text truncated')
                error.cost, error.completed_process = .1, True
                raise error
            output = {'summary': 'Mock native tool wrote actual isolated file', 'changed_files': [target.name], 'test_notes': 'orchestrator tests', 'remaining': []}
        elif role in {'claude_design', 'claude_architecture', 'claude_analysis'}:
            if self.failure:
                error = TeamError(self.failure, 'Mock specialist interruption')
                error.cost, error.completed_process = .03, True
                raise error
            output = {'summary': 'Mock expert document', 'sections': [{'title': 'Direction', 'content': 'Use blue with preserved functionality'}], 'remaining': []}
            if role == 'claude_design':
                output['implementation_files'] = config['specialist_files']
            elif role == 'claude_architecture':
                output.update(alternatives=['simple modules', 'large abstraction'], risks=['complexity'], recommendation='simple modules')
            else:
                output.update(source_evidence=['value.py VALUE = 0'], reproduction='test.py exits nonzero', recommendation='VALUE = 2')
        elif role == 'review':
            output = {'summary': 'Mock bounded independent review', 'findings': []}
        else:
            raise AssertionError(role)
        return Answer(output, {'input_tokens': 3, 'output_tokens': 10}, .01, .08)


class V3Tests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / 'state')
        self.defaults = save(self.store.root, {})

    def config(self, **kw):
        cfg = {**DEFAULTS, 'global_claude_budget': None, 'claude_call_cap': None, 'claude_max_calls': 4,
               'role_workflow_version': 3, 'task_type': 'auto', 'claude_role': 'auto', 'claude_context_bytes': 48000,
               'claude_subagent_limit': 1, **kw}
        return authorize(cfg, self.defaults, use_defaults=True)

    def setup_flow(self, kind='algorithm', claude=None, **config):
        repo = self.repo()
        if kind == 'ui_ux':
            (repo / 'style.css').write_text('body { color: black; }\n')
            (repo / 'test.py').write_text("from pathlib import Path\nassert 'blue' in Path('style.css').read_text()\n")
            from agent_team.gitwork import GitWorktrees
            git = GitWorktrees(self.root / 'git')
            git.git(repo, 'add', 'style.css', 'test.py')
            git.git(repo, 'commit', '-m', 'UI fixture')
        self.codex, self.claude = CodexV3(kind), claude or ClaudeV3()
        monitor = CostMonitor(self.store)
        snapshot = {'configured_model': 'sonnet-fixture', 'quota_per_usd': 500000, 'available_provider_funds_usd': 100,
                    'records': [{'model': 'sonnet-fixture', 'created_at': time.time(), 'pricing': {'model_ratio': 1,
                    'completion_ratio': 5, 'group_ratio': 2.75}}]}
        monitor.preflight = lambda cfg: (snapshot, 2.75)
        def after(task_id):
            for call in self.store.calls(task_id):
                self.store.attach_bill(call['id'], {'amount': call['estimated'] if call['estimated'] is not None else .1,
                                                  'status': 'MOCK_BILL', 'funding': {'wallet_usd': 0, 'subscription_usd': .08, 'gift_usd': 0}})
        monitor.after_call = after
        self.flow = Workflow(self.store, codex=self.codex, claude=self.claude, cost_monitor=monitor, trusted_test_fixture=True)
        cfg = self.config(test_commands=[[sys.executable, 'test.py']], **config)
        self.id = self.flow.create(repo, 'bounded V3 fixture task', cfg)
        self.claude.store, self.claude.task_id = self.store, self.id
        self.source_repo, self.source_head = repo, self.flow.git.git(repo, 'rev-parse', 'HEAD')

    def run_flow(self):
        with redirect_stdout(io.StringIO()):
            return self.flow.run(self.id)

    def test_v3_defaults_and_explicit_unattended_tier(self):
        self.assertEqual((self.defaults['default_task_budget'], self.defaults['unattended_task_budget'],
                          self.defaults['daily_budget'], self.defaults['monthly_wallet_budget']), (3, 8, 15, 100))
        self.assertEqual(self.config()['claude_budget'], 3)
        self.assertEqual(authorize(dict(DEFAULTS), self.defaults, use_defaults=True, unattended=True)['claude_budget'], 8)
        self.assertEqual(authorize(dict(DEFAULTS), self.defaults, use_defaults=True, unattended=True, explicit_budget=.5)['claude_budget'], .5)
        self.assertFalse(authorize(dict(DEFAULTS), self.defaults, use_defaults=True, explicit_budget=8)['allow_estimated_budget'])

    def test_gpt_only_blocks_all_expert_stages(self):
        self.setup_flow(policy='gpt_only')
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE')
        self.assertEqual(self.claude.calls, [])
        self.assertEqual(self.store.calls(self.id), [])

    def test_actual_native_code_handoff_then_codex_integration_and_tests(self):
        self.setup_flow()
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE', task['data'].get('last_error'))
        commits = [c for c in task['data']['commits'] if c['agent'] == 'claude']
        self.assertEqual(commits[0]['files'], ['value.py'])
        self.assertEqual((Path(task['data']['integration_worktree']) / 'value.py').read_text(), 'VALUE = 2\n')
        self.assertTrue(all(t['exit_code'] == 0 for t in task['data']['tests']))
        self.assertTrue(any(c[0] == 'specialist_verify' for c in self.codex.calls))
        self.assertEqual(self.flow.git.git(self.source_repo, 'rev-parse', 'HEAD'), self.source_head)
        self.assertFalse(self.flow.git.changed(self.source_repo))

    def test_ui_routes_design_and_actual_code_with_visual_gap_explicit(self):
        self.setup_flow(kind='ui_ux')
        task = self.run_flow()
        self.assertEqual(task['status'], 'MANUAL_VISUAL_VALIDATION_REQUIRED', task['data'].get('last_error'))
        preflight = task['data']['specialist_preflight']
        self.assertIsNotNone(preflight['spend_forecast_platform_range_usd'])
        self.assertEqual(preflight['reservation_upper_total_usd'], preflight['estimated_platform_total_usd'])
        report = (self.store.root / 'tasks' / self.id / 'report.md').read_text(encoding='utf-8')
        self.assertIn('预计费用情景区间', report)
        self.assertIn('预算门禁使用此值，不是预计实付', report)
        self.assertEqual([c[0] for c in self.claude.calls], ['claude_design', 'claude_native_develop'])
        self.assertTrue(any(d['payload'].get('sections_saved') == 1 for d in self.store.deliveries(self.id)))
        self.assertEqual(task['data']['visual_validation']['status'], 'UNVERIFIED')

    def test_architecture_is_analysis_not_reviewer(self):
        self.setup_flow(kind='architecture')
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE', task['data'].get('last_error'))
        self.assertEqual([c[0] for c in self.claude.calls], ['claude_architecture'])

    def test_single_client_cap_is_dynamic_above_old_twenty_cents(self):
        cfg = self.config()
        task_id = self.store.create({'repo': 'fixture', 'request': 'cap test', 'config': cfg})
        _, cap = self.store.reserve(task_id, 'native', cfg, multiplier=2.75, context={'stage_platform_budget': 2.5})
        self.assertGreater(cap, .2)
        self.assertLess(cap, cfg['claude_budget'])
        self.assertAlmostEqual(cap, 2.5 * .8 / 2.75)

    def test_daily_and_monthly_atomic_reservations(self):
        cfg = self.config()
        tasks = [self.store.create({'repo': 'fixture', 'request': 'parallel', 'config': cfg}) for _ in range(12)]
        def reserve(task):
            try:
                self.store.reserve(task, 'native', cfg)
            except TeamError as error:
                self.assertEqual(error.kind, 'BUDGET_EXHAUSTED')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(reserve, tasks))
        self.assertLessEqual(self.store.daily_budget()['accounted_usd'], 15)
        self.assertGreater(self.store.daily_budget()['accounted_usd'], 14)
        self.assertLessEqual(self.store.monthly_wallet()['accounted_wallet_usd'], 100)

    def test_subscription_and_gift_do_not_count_as_actual_wallet(self):
        cfg = self.config()
        for source in ('subscription', 'gift'):
            task_id = self.store.create({'repo': 'fixture', 'request': source, 'config': cfg})
            call, _ = self.store.reserve(task_id, 'native', cfg)
            record = normalise_record({'type': 2, 'charged_quota': 50000, 'charged_pool': source, 'other': {}}, 500000)
            self.store.attach_bill(call, {'amount': .1, 'funding': {k: record[k] for k in ('wallet_usd', 'subscription_usd', 'gift_usd')}})
        month = self.store.monthly_wallet()
        self.assertEqual(month['accounted_wallet_usd'], 0)
        self.assertAlmostEqual(month['actual_subscription_usd'], .1)
        self.assertAlmostEqual(month['actual_gift_usd'], .1)

    def test_unknown_funding_reserves_wallet_then_reconciles(self):
        cfg = self.config()
        task_id = self.store.create({'repo': 'fixture', 'request': 'funding', 'config': cfg})
        call, cap = self.store.reserve(task_id, 'native', cfg)
        self.assertAlmostEqual(self.store.monthly_wallet()['pending_wallet_risk_usd'], cap)
        self.store.attach_bill(call, {'amount': .2, 'funding': {'wallet_usd': None}})
        self.assertAlmostEqual(self.store.monthly_wallet()['pending_wallet_risk_usd'], .2)
        self.store.attach_bill(call, {'amount': .2, 'funding': {'wallet_usd': 0, 'subscription_usd': .2}})
        self.assertEqual(self.store.monthly_wallet()['accounted_wallet_usd'], 0)

    def test_monthly_wallet_exhaustion_blocks_legacy_and_v3_tasks(self):
        cfg = self.config()
        save(self.store.root, {'daily_budget': 1000})
        cfg['daily_claude_budget'] = 1000
        task_id = self.store.create({'repo': 'fixture', 'request': 'funding', 'config': cfg})
        call, _ = self.store.reserve(task_id, 'native', cfg)
        self.store.attach_bill(call, {'amount': 100, 'funding': {'wallet_usd': 100}})
        self.assertGreater(self.store.daily_budget()['remaining_usd'], 0)
        for config in (cfg, dict(DEFAULTS, global_claude_budget=None)):
            with self.assertRaises(TeamError):
                self.store.reserve(self.store.create({'repo': 'x', 'request': 'next', 'config': config}), 'native', config)

    def test_month_boundary_is_shanghai_calendar(self):
        self.assertEqual(shanghai_month('2026-10-31T15:59:59+00:00'), '2026-10')
        self.assertEqual(shanghai_month('2026-10-31T16:00:00+00:00'), '2026-11')

    def test_partial_native_code_survives_text_truncation_and_is_verified(self):
        self.setup_flow(claude=ClaudeV3(partial_code=True))
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE', task['data'].get('last_error'))
        self.assertEqual(len(self.claude.calls), 1)
        self.assertTrue(any(d['status'] == 'CODE_RECOVERED' for d in self.store.deliveries(self.id)))
        self.assertTrue(any(c['agent'] == 'claude' for c in task['data']['commits']))

    def test_document_failure_leaves_partial_task_and_does_not_repay(self):
        self.setup_flow(kind='architecture', claude=ClaudeV3(failure='OUTPUT_TRUNCATED'))
        task = self.run_flow()
        self.assertEqual(task['status'], 'PARTIAL_COMPLETE_WAITING_APPROVAL', task['data'].get('last_error'))
        self.assertTrue(task['data']['tests'])
        self.assertTrue(any(c['agent'] == 'codex' for c in task['data']['commits']))
        self.assertEqual(len(self.claude.calls), 1)
        self.store.submit(self.id, 'resume')
        self.run_flow()
        self.assertEqual(len(self.claude.calls), 1)

    def test_pause_after_native_commit_resume_only_verifies_existing_delivery(self):
        self.setup_flow(claude=ClaudeV3(pause=True))
        first = self.run_flow()
        self.assertEqual(first['status'], 'PAUSED')
        self.assertTrue(any(c['agent'] == 'claude' for c in first['data']['commits']))
        self.store.submit(self.id, 'resume')
        self.assertEqual(self.run_flow()['status'], 'READY_FOR_MERGE')
        self.assertEqual(len(self.claude.calls), 1)

    def test_local_json_repair_and_complete_sections_never_invent_content(self):
        self.assertEqual(repair_json('```json\n{"summary":"ok",}\n```'), {'summary': 'ok'})
        partial = '{"sections":[{"title":"one","content":"complete"},{"title":"two","content":"cut'
        self.assertEqual(complete_sections(partial), [{'title': 'one', 'content': 'complete'}])
        with self.assertRaises(TeamError):
            repair_json(partial)

    def test_ui_large_context_and_old_output_limit_regression(self):
        self.setup_flow(kind='ui_ux')
        task = self.store.task(self.id)
        task['data']['plan'] = dict(PLAN, expected_files=['style.css'])
        with self.assertRaises(TeamError) as error:
            self.flow.call(task, 'claude', 'claude_design', '界' * 37000, self.source_repo)
        self.assertEqual(error.exception.kind, 'CONTEXT_TOO_LARGE')
        self.assertEqual(self.store.calls(self.id), [])

    def policy(self, subagents=1):
        cwd = self.root / 'scoped'
        cwd.mkdir(exist_ok=True)
        (cwd / 'value.py').write_text('VALUE = 0\n')
        task_id = self.store.create({'repo': str(cwd), 'request': 'tool test', 'config': DEFAULTS})
        policy = prepare_policy(cwd, self.root / 'artifact', {'specialist_files': ['value.py'], 'test_commands': [[sys.executable, 'test.py']],
                                    'claude_subagent_limit': subagents}, 'claude_native_develop', task_id=task_id, state_dir=self.store.root)
        return cwd, task_id, policy

    def test_native_tool_surface_has_real_edits_no_bypass_and_scoped_tests(self):
        cwd, task_id, policy = self.policy()
        config = dict(DEFAULTS, allow_estimated_budget=True, claude_tools_policy=policy)
        argv = ClaudeAdapter(binary='claude.exe').command('claude_native_develop', config, .6)
        self.assertIn('--restricted', argv)
        self.assertIn('dontAsk', argv)
        self.assertIn('Edit,Write,Bash', ','.join(policy['tools']))
        self.assertNotIn('bypassPermissions', argv)
        self.assertEqual(policy['agents']['expert_read']['maxTurns'], 4)
        self.assertNotIn('Agent', policy['agents']['expert_read']['tools'])
        self.assertEqual(policy['spec']['max_depth'], 1)

    def test_tools_reject_escape_other_file_and_shell_chaining(self):
        cwd, task_id, policy = self.policy()
        for tool, args in [('Write', {'file_path': str(self.root / 'outside.py')}),
                           ('Edit', {'file_path': str(cwd / 'unassigned.py')}),
                           ('Bash', {'command': policy['test_commands'][0] + ' && git push'}),
                           ('Agent', {'subagent_type': 'general-purpose'})]:
            with self.assertRaises(TeamError):
                check(policy['spec'], {'tool_name': tool, 'tool_input': args})

    def test_tool_counter_revision_and_user_ownership(self):
        cwd, task_id, policy = self.policy()
        spec = policy['spec']
        event = {'tool_name': 'Agent', 'tool_use_id': 'one', 'tool_input': {'subagent_type': 'expert_read'}}
        self.store.guard_tool(spec, event)
        self.store.guard_tool(spec, event)
        with self.assertRaises(TeamError):
            self.store.guard_tool(spec, dict(event, tool_use_id='two'))
        self.store.submit(task_id, 'amend', payload={'change': 'new scope'})
        with self.assertRaises(TeamError):
            self.store.guard_tool(spec, {'tool_name': 'Read', 'tool_use_id': 'read', 'tool_input': {'file_path': str(cwd / 'value.py')}})

    def test_claude_workspace_takeover_and_handoff_preserves_human_code(self):
        self.setup_flow(claude=ClaudeV3(pause=True))
        task = self.run_flow()
        controls = Controls(self.store, self.flow.git)
        self.store.submit(self.id, 'takeover', payload={'workspace': 'claude'})
        controls.takeover(self.id)
        cwd = Path(task['data']['claude_worktree'])
        (cwd / 'notes.txt').write_text('human notes')
        snapshot = controls.handoff(self.id, workspace_role='claude', include_uncommitted=True)
        self.assertIn('notes.txt', snapshot['adopted_files'])
        self.assertTrue((Path(self.store.task(self.id)['data']['codex_worktree']) / 'notes.txt').is_file())
        self.assertEqual(self.store.task(self.id)['revision'], 2)

    def test_ordinary_general_task_does_not_spend_available_budget(self):
        self.setup_flow(kind='general')
        self.assertEqual(self.run_flow()['status'], 'READY_FOR_MERGE')
        self.assertEqual(self.claude.calls, [])

    def test_explicit_independent_reviewer_uses_review_role(self):
        self.setup_flow(kind='general', claude_role='reviewer')
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE', task['data'].get('last_error'))
        self.assertEqual([c[0] for c in self.claude.calls], ['review'])

    def test_debug_has_actual_reproduction_before_paid_native_repair(self):
        self.setup_flow(kind='debug')
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE', task['data'].get('last_error'))
        self.assertTrue(task['data']['reproduction_observed']['reproduced'])
        self.assertNotEqual(task['data']['reproduction_observed']['tests'][0]['exit_code'], 0)

    def test_insufficient_budget_warns_before_any_paid_stage_and_gpt_continues(self):
        self.setup_flow(kind='architecture')
        task = self.store.task(self.id)
        task['config']['claude_budget'] = .01
        self.store.update(self.id, config=task['config'])
        task = self.run_flow()
        self.assertEqual(task['status'], 'PARTIAL_COMPLETE_WAITING_APPROVAL')
        self.assertEqual(self.claude.calls, [])
        self.assertTrue(task['data']['commits'])
        self.assertTrue(all(t['exit_code'] == 0 for t in task['data']['tests']))

    def test_amend_during_native_development_keeps_old_commit_and_replans(self):
        self.setup_flow(claude=ClaudeV3(amend=True))
        task = self.run_flow()
        self.assertEqual(task['revision'], 2)
        self.assertEqual(task['applied_revision'], 2)
        self.assertTrue(any(c['agent'] == 'claude' and c['revision'] == 1 for c in task['data']['commits']))
        self.assertTrue(any(e['kind'] == 'stale_specialist_delivery' for e in self.store.events(self.id)))
        self.assertGreaterEqual(sum(c[0] == 'plan_v3' for c in self.codex.calls), 2)

    def test_native_pending_commit_recovery_does_not_repay(self):
        self.setup_flow()
        original = self.flow.git.commit
        def crash(cwd, head, task_id, phase, **kw):
            result = original(cwd, head, task_id, phase, **kw)
            if phase.startswith('specialist-'):
                raise TeamError('PAUSED', 'Mock crash after durable native commit')
            return result
        self.flow.git.commit = crash
        self.assertEqual(self.run_flow()['status'], 'PAUSED')
        self.flow.git.commit = original
        self.assertEqual(self.run_flow()['status'], 'READY_FOR_MERGE')
        self.assertEqual(len(self.claude.calls), 1)

    def test_unattended_entry_needs_explicit_flag_and_gpt_only_still_forbids(self):
        from agent_team.entry import prepare_run
        context = {'nested_orchestrator_blocked': False, 'tests_ready': False, 'repo': str(self.root), 'base': 'HEAD',
                   'test_config_path': None, 'test_config_source': 'UNKNOWN'}
        normal = prepare_run(context, 'long complex task while away', 'selective', no_tests=True, use_default_budget=True, state_dir=self.store.root)
        unattended = prepare_run(context, 'task', 'selective', no_tests=True, unattended=True, state_dir=self.store.root)
        self.assertEqual(normal['claude_budget'], 3)
        self.assertEqual(unattended['claude_budget'], 8)
        self.assertIn('--unattended', unattended['cli_args'])
        gpt = prepare_run(context, 'no Claude', 'gpt_only', no_tests=True, unattended=True, state_dir=self.store.root)
        self.assertFalse(gpt['paid_claude_authorized'])
