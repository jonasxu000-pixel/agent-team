from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
from pathlib import Path
from unittest.mock import patch

from test_team import Fixture
from agent_team.cli import background, main
from agent_team.entry import prepare_run
from agent_team.safety import TeamError
from agent_team.store import Store
from agent_team.workflow import DEFAULTS


class WatchEntryTests(Fixture):
    def setUp(self):
        super().setUp()
        self.repo_path = self.repo()
        self.store = Store(self.root / 'state')
        path = Path(__file__).resolve().parents[1] / 'codex' / 'agent-team' / 'scripts' / 'watch.py'
        spec = importlib.util.spec_from_file_location('watch_bootstrap_fixture', path)
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)

    def task(self, repo=None, status='QUEUED'):
        task_id = self.store.create({'repo': str(repo or self.repo_path), 'request': 'observer fixture', 'config': DEFAULTS})
        self.store.update(task_id, status=status)
        return task_id

    def test_observer_selects_current_project_active_task(self):
        selected = self.task()
        self.task(repo=self.root / 'another project')
        (self.repo_path / '.agent-team.json').write_text('invalid test configuration')
        self.assertEqual(self.helper.select_task(self.store, self.repo_path), selected)

    def test_multiple_active_tasks_require_an_explicit_id(self):
        self.task()
        self.task()
        with self.assertRaises(TeamError) as error:
            self.helper.select_task(self.store, self.repo_path)
        self.assertEqual(error.exception.kind, 'AMBIGUOUS_TASK')

    def test_recent_task_replay_does_not_dispatch_agents(self):
        task_id = self.task(status='PAUSED')
        self.store.event(task_id, 'claude', 'visible_event', {'category': 'model_output', 'text': 'Mock Claude visible text'})
        metadata = {'tool_root': str(Path(__file__).resolve().parents[1]), 'state_dir': str(self.store.root)}
        with redirect_stdout(io.StringIO()) as output:
            result = self.helper.main(['--cwd', str(self.repo_path), '--once', '--json'], installation=metadata)
        self.assertEqual(result, 0)
        self.assertIn('Mock Claude visible text', output.getvalue())
        self.assertEqual(self.store.calls(task_id), [])
        self.assertEqual(self.store.task(task_id)['status'], 'PAUSED')

    def test_background_attaches_watch_and_spawns_only_one_worker(self):
        task_id = self.task()
        def launch(*args, **kw):
            self.store.update(task_id, status='PAUSED')
            self.store.event(task_id, 'claude', 'visible_event', {'text': 'Mock automatic review output'})
            return type('Worker', (), {'pid': 2147483646})()
        with patch('agent_team.cli.subprocess.Popen', side_effect=launch) as spawned, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(background(self.store, task_id, attach_watch=True), 0)
        spawned.assert_called_once()
        argv = spawned.call_args.args[0]
        self.assertIn('_execute', argv)
        self.assertNotIn('--watch', argv)
        self.assertIn('Mock automatic review output', output.getvalue())
        self.assertEqual(self.store.calls(task_id), [])

    def test_run_watch_routes_to_background_observer(self):
        with patch('agent_team.cli.background', return_value=0) as start, redirect_stdout(io.StringIO()):
            result = main(['--state-dir', str(self.store.root), 'run', '--repo', str(self.repo_path),
                           '--request', 'test', '--policy', 'gpt_only', '--no-tests', '--watch'])
        self.assertEqual(result, 0)
        self.assertTrue(start.call_args.kwargs['attach_watch'])
        self.assertFalse(self.store.task(start.call_args.args[1])['config']['allow_estimated_budget'])

    def test_queue_watch_rejected_before_creating_task(self):
        with redirect_stderr(io.StringIO()):
            result = main(['--state-dir', str(self.store.root), 'run', '--repo', str(self.repo_path),
                           '--request', 'test', '--no-tests', '--watch', '--queue'])
        self.assertEqual(result, 2)
        self.assertEqual(self.store.tasks(), [])

    def test_resume_watch_routes_to_the_same_task(self):
        task_id = self.task(status='PAUSED')
        with patch('agent_team.cli.background', return_value=0) as start, redirect_stdout(io.StringIO()):
            result = main(['--state-dir', str(self.store.root), 'resume', task_id, '--watch'])
        self.assertEqual(result, 0)
        self.assertEqual(start.call_args.args[1], task_id)
        self.assertTrue(start.call_args.kwargs['attach_watch'])

    def test_entry_manifest_attaches_monitor_without_granting_budget(self):
        context = {'nested_orchestrator_blocked': False, 'tests_ready': False, 'repo': str(self.repo_path),
                   'base': 'HEAD', 'test_config_path': None, 'test_config_source': 'UNKNOWN'}
        manifest = prepare_run(context, 'task', 'gpt_only', no_tests=True, watch=True, state_dir=self.store.root)
        self.assertIn('--watch', manifest['cli_args'])
        self.assertFalse(manifest['paid_claude_authorized'])
