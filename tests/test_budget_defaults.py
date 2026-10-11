from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
import sys
import time

from test_team import Fixture, FakeCodex, FakeClaude, PLAN
from agent_team.cli import budget_config, parser
from agent_team.cost_monitor import CostMonitor
from agent_team.entry import prepare_run
from agent_team.policy import assess
from agent_team.safety import TeamError
from agent_team.store import Store
from agent_team.user_config import DEFAULT_USER_CONFIG, authorize, load, save, shanghai_day
from agent_team.workflow import DEFAULTS, Workflow


class BudgetDefaultsTests(Fixture):
    def setUp(self):
        super().setUp()
        self.store = Store(self.root / 'state')
        # Preserve the V2 user-config contract explicitly; V3 defaults are covered separately.
        self.defaults = save(self.store.root, {'version': 1, 'default_task_budget': 2, 'daily_budget': 5,
                                               'claude_call_cap': .2, 'claude_max_calls': 1})

    def config(self, **kw):
        return authorize(dict(DEFAULTS, global_claude_budget=None, **kw), self.defaults, use_defaults=True)

    def task(self, config=None):
        return self.store.create({'request': 'test', 'repo': 'fixture', 'config': config or self.config()})

    def test_defaults_persist_and_are_configurable(self):
        self.assertEqual(load(self.store.root)['default_task_budget'], 2)
        save(self.store.root, {'default_task_budget': 1.5})
        self.assertEqual(load(self.store.root)['default_task_budget'], 1.5)
        self.assertTrue(list((self.store.root / 'config-backups').rglob('user-config.json')))

    def test_explicit_multi_agent_uses_default_two(self):
        cfg = self.config()
        self.assertEqual(cfg['claude_budget'], 2)
        self.assertTrue(cfg['allow_estimated_budget'])
        self.assertEqual(cfg['daily_claude_budget'], 5)

    def test_ordinary_and_gpt_only_do_not_authorize(self):
        cfg = authorize(dict(DEFAULTS), self.defaults)
        self.assertFalse(cfg['allow_estimated_budget'])
        cfg = authorize(dict(DEFAULTS, policy='gpt_only', allow_estimated_budget=True), self.defaults, use_defaults=True)
        self.assertFalse(cfg['allow_estimated_budget'])
        self.assertEqual(cfg['claude_max_calls'], 0)

    def test_selective_ordinary_tests_suffice_despite_budget(self):
        result = assess(dict(PLAN, expected_claude_gain='optional independent check'), self.config(), {'status': 'AVAILABLE'})
        self.assertFalse(result.call)
        self.assertIn('ordinary task', result.reason)
        self.assertTrue(assess(dict(PLAN, concurrency=True, expected_claude_gain='race check'), self.config(), {'status': 'AVAILABLE'}).call)

    def test_smaller_half_dollar_overrides(self):
        cfg = authorize(dict(DEFAULTS), self.defaults, use_defaults=True, explicit_budget=.5)
        self.assertTrue(cfg['allow_estimated_budget'])
        self.assertEqual(cfg['claude_budget'], .5)

    def test_more_than_default_needs_explicit_approval(self):
        cfg = authorize(dict(DEFAULTS), self.defaults, use_defaults=True, explicit_budget=3)
        self.assertFalse(cfg['allow_estimated_budget'])
        cfg = authorize(dict(DEFAULTS, allow_estimated_budget=True), self.defaults, explicit_budget=3)
        self.assertTrue(cfg['allow_estimated_budget'])
        self.assertEqual(cfg['daily_claude_budget'], 5)

    def test_project_budget_can_only_restrict(self):
        self.assertEqual(authorize(dict(DEFAULTS), self.defaults, use_defaults=True, project_budget=10)['claude_budget'], 2)
        self.assertEqual(authorize(dict(DEFAULTS), self.defaults, use_defaults=True, project_budget=.3)['claude_budget'], .3)

    def test_concurrent_reservations_across_tasks_share_daily_limit(self):
        cfg = self.config(claude_call_cap=2)
        tasks = [self.task(cfg) for _ in range(12)]
        def reserve(task):
            try:
                return self.store.reserve(task, 'review', cfg)[0]
            except TeamError as error:
                self.assertEqual(error.kind, 'BUDGET_EXHAUSTED')
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(reserve, tasks))
        today = self.store.daily_budget()
        self.assertLessEqual(today['accounted_usd'], 5)
        self.assertGreater(today['accounted_usd'], 4.8)
        with self.assertRaises(TeamError):
            self.store.reserve(self.task(cfg), 'review', cfg)

    def test_delayed_bill_keeps_reservation_then_reconciles_once(self):
        cfg = self.config()
        task = self.task(cfg)
        call, cap = self.store.reserve(task, 'review', cfg, context={'retain_until_reconciled': True})
        self.store.finish_call(call, status='COMPLETED', cost=.01)
        self.assertEqual(self.store.daily_budget()['accounted_usd'], cap)
        self.store.attach_bill(call, {'amount': None, 'status': 'UNKNOWN'})
        self.assertEqual(self.store.daily_budget()['pending_reconciliation'], 1)
        for _ in range(2):
            self.store.attach_bill(call, {'amount': .092, 'status': 'UNIQUE_USAGE_TIME_CORRELATION'})
        self.assertAlmostEqual(self.store.daily_budget()['accounted_usd'], .092)
        self.assertEqual(self.store.daily_budget()['pending_reconciliation'], 0)

    def test_actual_overrun_blocks_further_calls(self):
        cfg = self.config()
        task = self.task(cfg)
        call, _ = self.store.reserve(task, 'review', cfg)
        self.store.attach_bill(call, {'amount': 6, 'status': 'REQUEST_ID_MATCH'})
        with self.assertRaises(TeamError):
            self.store.reserve(self.task(cfg), 'review', cfg)

    def test_shanghai_day_midnight_and_separate_package_window(self):
        self.assertEqual(shanghai_day('2026-10-09T15:59:59+00:00'), '2026-10-09')
        self.assertEqual(shanghai_day('2026-10-09T16:00:00+00:00'), '2026-10-10')

    def test_cli_and_entry_default_authorization_manifest(self):
        args = parser().parse_args(['run', '--repo', str(self.root), '--request', 'task', '--no-tests', '--use-default-budget'])
        cfg = budget_config(args, user_defaults=self.defaults)
        self.assertEqual(cfg['claude_budget'], 2)
        self.assertTrue(cfg['allow_estimated_budget'])
        context = {'nested_orchestrator_blocked': False, 'tests_ready': False, 'repo': str(self.root), 'base': 'HEAD', 'test_config_path': None, 'test_config_source': 'UNKNOWN'}
        first = prepare_run(context, '使用多 Agent', 'selective', no_tests=True, use_default_budget=True, state_dir=self.store.root)
        self.assertTrue(first['paid_claude_authorized'])
        self.assertIn('--use-default-budget', first['cli_args'])
        context['repo'] = 'another Git project'
        second = prepare_run(context, 'Claude独立审查', 'review', no_tests=True, use_default_budget=True, state_dir=self.store.root)
        self.assertEqual(first['claude_budget'], second['claude_budget'])
        self.assertEqual(second['claude_budget'], 2)

    def pricing_snapshot(self):
        return {'configured_model': 'sonnet-fixture', 'quota_per_usd': 500000, 'provider_scope': 'fixture',
                'available_provider_funds_usd': 10, 'records': [{'model': 'sonnet-fixture', 'created_at': time.time(),
                'pricing': {'model_ratio': 1.5, 'completion_ratio': 5, 'group_ratio': 2.46}}]}

    def test_fresh_price_estimate_and_stale_price_warning(self):
        monitor = CostMonitor(self.store)
        config = self.config()
        snapshot = self.pricing_snapshot()
        estimated = monitor.estimate_review('short diff', config, snapshot)
        self.assertEqual(estimated['status'], 'ESTIMATED')
        self.assertEqual(estimated['multiplier'], 2.46)
        self.assertLess(estimated['platform_range_usd'][1], 2)
        snapshot['records'][0]['created_at'] -= 48 * 3600
        self.assertEqual(monitor.estimate_review('diff', config, snapshot)['status'], 'UNKNOWN')

    def flow(self, *, high=True, snapshot=None, budget=2):
        repo = self.repo()
        codex, claude = FakeCodex(high=high), FakeClaude()
        monitor = CostMonitor(self.store)
        monitor.preflight = lambda cfg: (snapshot or self.pricing_snapshot(), 2.46)
        def reconcile(task_id):
            for call in self.store.calls(task_id):
                self.store.attach_bill(call['id'], {'amount': .09, 'status': 'MOCK_BILL'})
        monitor.after_call = reconcile
        cfg = self.config(test_commands=[[sys.executable, 'test.py']])
        cfg['claude_budget'] = budget
        flow = Workflow(self.store, codex=codex, claude=claude, cost_monitor=monitor, trusted_test_fixture=True)
        task_id = flow.create(repo, 'set value and independent check', cfg)
        with redirect_stdout(io.StringIO()):
            task = flow.run(task_id)
        return task, codex, claude

    def test_default_authorized_selective_really_dispatches_mock_claude_once(self):
        task, codex, claude = self.flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE')
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(self.store.daily_budget()['actual_billed_usd'], .09)
        self.assertEqual(task['config']['claude_budget'], 2)

    def test_price_unknown_blocks_paid_but_gpt_completes_its_part(self):
        task, codex, claude = self.flow(snapshot={'records': [], 'available_provider_funds_usd': 10})
        self.assertEqual(task['status'], 'AWAITING_REVIEW')
        self.assertEqual(claude.calls, [])
        self.assertTrue(task['data']['cost_warning'])
        self.assertTrue(any(x[0] == 'develop' for x in codex.calls))

    def test_zero_budget_does_not_dispatch(self):
        task, codex, claude = self.flow(budget=0)
        self.assertEqual(claude.calls, [])
        self.assertEqual(task['status'], 'AWAITING_REVIEW')

    def test_ordinary_task_budget_is_not_a_consumption_target(self):
        task, codex, claude = self.flow(high=False)
        self.assertEqual(task['status'], 'READY_FOR_MERGE')
        self.assertEqual(claude.calls, [])

    def test_explicit_independent_review_even_low_complexity_is_required(self):
        result = assess(PLAN, self.config(policy='deep_review'), {'status': 'AVAILABLE'})
        self.assertTrue(result.call)
        self.assertTrue(result.independent_required)

    def test_finite_key_quota_blocks_despite_sufficient_wallet(self):
        from test_cost_monitor import FakeAPI, FakeBrowser
        class LimitedKey(FakeAPI):
            def get(self, component):
                if component == 'token':
                    return {'data': {'unlimited_quota': False, 'total_available': 10000}}
                return super().get(component)
        monitor = CostMonitor(self.store, api=LimitedKey(), browser=FakeBrowser())
        with self.assertRaises(TeamError) as ctx:
            monitor.preflight(self.config())
        self.assertEqual(ctx.exception.kind, 'QUOTA_EXHAUSTED')

    def test_next_preflight_reconciles_delayed_previous_task_bill(self):
        from test_cost_monitor import FakeAPI, FakeBrowser, ROW
        cfg = self.config()
        task = self.task(cfg)
        call, cap = self.store.reserve(task, 'review', cfg, context={'retain_until_reconciled': True,
                                       'provider_scope': 'fixture-scope', 'model': 'claude-sonnet-5-5'})
        self.store.event(task, 'claude', 'call_started', {'call_id': call})
        self.store.finish_call(call, status='COMPLETED', cost=.02, duration=3,
                               usage={'input_tokens': 4, 'output_tokens': 100, 'cache_creation_input_tokens': 1200, 'cache_read_input_tokens': 0})
        monitor = CostMonitor(self.store, api=FakeAPI(rows=[dict(ROW, created_at=time.time())]), browser=FakeBrowser())
        monitor.preflight(cfg)
        self.assertAlmostEqual(self.store.calls(task)[0]['charged'], .04)
        self.assertEqual(self.store.daily_budget()['pending_reconciliation'], 0)
