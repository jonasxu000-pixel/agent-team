from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import threading
import time

from test_team import Fixture, FakeCodex, FakeClaude, PLAN
from agent_team.adapters import Answer, ClaudeAdapter, CodexAdapter
from agent_team.controls import Controls
from agent_team.process import run_process, Result
from agent_team.safety import TeamError, pid_alive
from agent_team.store import Store
from agent_team.streaming import EventSink, visible, watch
from agent_team.workflow import Workflow, DEFAULTS


class V2Tests(Fixture):
    def setup_flow(self, *, high=False, **config):
        self.store = Store(self.root / '状态 中文')
        self.codex, self.claude = FakeCodex(high=high), FakeClaude()
        self.flow = Workflow(self.store, codex=self.codex, claude=self.claude, trusted_test_fixture=True)
        self.id = self.flow.create(self.repo(), 'set value', dict(DEFAULTS, allow_estimated_budget=True,
                                  test_commands=[[sys.executable, 'test.py']], **config))
        self.controls = Controls(self.store, self.flow.git)

    def run_flow(self):
        with redirect_stdout(io.StringIO()):
            return self.flow.run(self.id)

    def prepare_paused(self):
        self.setup_flow()
        original = self.codex.call
        def pause(cwd, role, *args, **kw):
            answer = original(cwd, role, *args, **kw)
            if role == 'develop':
                self.store.submit(self.id, 'pause')
            return answer
        self.codex.call = pause
        task = self.run_flow()
        self.codex.call = original
        return task

    def test_safe_pause_commits_receipt_and_stops_next_dispatch(self):
        task = self.prepare_paused()
        self.assertEqual(task['status'], 'PAUSED')
        self.assertEqual(task['phase'], 'test')
        self.assertEqual(len(task['data']['commits']), 1)
        self.assertEqual([x[0] for x in self.codex.calls], ['plan', 'develop'])
        self.assertEqual(self.store.commands(self.id)[-1]['status'], 'APPLIED')
        self.store.submit(self.id, 'resume')
        self.assertEqual(self.run_flow()['status'], 'READY_FOR_MERGE')
        self.assertEqual(sum(x[0] == 'develop' for x in self.codex.calls), 1)

    def test_request_is_not_pause_confirmation(self):
        self.setup_flow()
        with self.store.claim(self.id):
            cmd = self.store.submit(self.id, 'pause')
            self.assertEqual(cmd['status'], 'PENDING')
            self.assertEqual(self.store.task(self.id)['status'], 'PAUSE_REQUESTED')
            self.store.check_control(self.id, in_flight=True)
            self.assertEqual(self.store.task(self.id)['status'], 'PAUSING')
        self.assertEqual(self.store.task(self.id)['status'], 'PAUSED')

    def test_pause_grace_escalates_only_owned_process(self):
        self.setup_flow()
        child = None
        with self.store.claim(self.id):
            def started(pid):
                nonlocal child
                child = pid
                self.store.submit(self.id, 'pause', payload={'grace': .05})
            with self.assertRaises(TeamError) as ctx:
                run_process([sys.executable, '-c', 'import time; time.sleep(30)'], self.root,
                            on_start=started, control=lambda: self.store.check_control(self.id, in_flight=child is not None))
            self.assertEqual(ctx.exception.kind, 'INTERRUPTED')
            self.store.update(self.id, status='INTERRUPTED')
        self.assertFalse(pid_alive(child))
        self.assertEqual(self.store.task(self.id)['status'], 'INTERRUPTED')

    def test_emergency_stop(self):
        self.setup_flow()
        with self.store.claim(self.id):
            self.store.submit(self.id, 'stop')
            self.assertEqual(self.store.task(self.id)['status'], 'STOP_REQUESTED')
            with self.assertRaises(TeamError) as ctx:
                self.store.check_control(self.id, in_flight=True)
            self.assertEqual(ctx.exception.kind, 'INTERRUPTED')
        self.assertEqual(self.store.task(self.id)['status'], 'INTERRUPTED')

    def test_paid_pause_finishes_once_then_resume_verifies_without_repay(self):
        self.setup_flow(high=True)
        original = self.claude.call
        def pause(*args, **kw):
            answer = original(*args, **kw)
            self.store.submit(self.id, 'pause')
            return answer
        self.claude.call = pause
        task = self.run_flow()
        self.assertEqual(task['status'], 'PAUSED')
        self.assertEqual(task['phase'], 'verify')
        self.assertEqual(self.store.calls(self.id)[0]['status'], 'COMPLETED')
        self.store.submit(self.id, 'resume')
        self.assertEqual(self.run_flow()['status'], 'READY_FOR_MERGE')
        self.assertEqual(len(self.claude.calls), 1)

    def test_unknown_paid_result_never_replays(self):
        self.setup_flow(high=True)
        def interrupt(*args, **kw):
            self.claude.calls.append(('review',))
            raise TeamError('INTERRUPTED', 'paid result unknown')
        self.claude.call = interrupt
        self.assertEqual(self.run_flow()['status'], 'INTERRUPTED')
        call = self.store.calls(self.id)[0]
        self.assertEqual(call['status'], 'UNKNOWN_RESULT')
        self.assertEqual(call['charged'], call['reserved'])
        self.assertEqual(self.run_flow()['status'], 'WAITING_APPROVAL')
        self.assertEqual(len(self.claude.calls), 1)

    def test_amend_during_development_keeps_commit_and_replans(self):
        self.setup_flow()
        original = self.codex.call
        amended = False
        def amend(cwd, role, *args, **kw):
            nonlocal amended
            answer = original(cwd, role, *args, **kw)
            if role == 'develop' and not amended:
                amended = True
                self.store.submit(self.id, 'amend', payload={'change': 'preserve value and also verify it'})
            return answer
        self.codex.call = amend
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE')
        self.assertEqual(task['applied_revision'], 2)
        self.assertEqual(len(task['data']['commits']), 2)
        plans = [x for x in self.codex.calls if x[0] == 'plan']
        self.assertEqual(len(plans), 2)
        self.assertIn('preserve value', plans[-1][2])
        self.assertEqual(task['data']['commits'][0]['revision'], 1)

    def test_stale_review_is_archived_and_cannot_become_current(self):
        self.setup_flow(high=True)
        original = self.claude.call
        def amend(*args, **kw):
            result = original(*args, **kw)
            self.store.submit(self.id, 'amend', payload={'change': 'updated review scope'})
            return result
        self.claude.call = amend
        task = self.run_flow()
        self.assertEqual(task['revision'], 2)
        self.assertFalse(task['data'].get('current_review'))
        self.assertTrue(any(e['kind'] == 'stale_review_archived' for e in self.store.events(self.id)))
        self.assertEqual(len(self.claude.calls), 1)

    def take(self):
        task = self.prepare_paused()
        self.store.submit(self.id, 'takeover')
        self.controls.takeover(self.id)
        return Path(task['data']['codex_worktree'])

    def test_takeover_records_snapshot_and_blocks_resume_and_dispatch(self):
        cwd = self.take()
        w = self.store.workspaces(self.id)[0]
        self.assertEqual(w['owner'], 'USER')
        self.assertEqual(w['snapshot']['head'], self.flow.git.git(cwd, 'rev-parse', 'HEAD'))
        for operation in (lambda: self.store.submit(self.id, 'resume'), lambda: self.store.assert_dispatch(self.id, 1),
                          lambda: self.run_flow()):
            with self.assertRaises(TeamError):
                operation()

    def test_takeover_waits_for_writer(self):
        self.setup_flow()
        with self.store.claim(self.id):
            self.store.submit(self.id, 'takeover')
            with self.assertRaises(TeamError) as ctx:
                self.controls.takeover(self.id)
            self.assertEqual(ctx.exception.kind, 'WRITER_ACTIVE')

    def test_committed_human_handoff_replans_from_real_head(self):
        cwd = self.take()
        (cwd / 'notes.txt').write_text('human code\n', encoding='utf-8')
        self.flow.git.git(cwd, 'add', 'notes.txt')
        self.flow.git.git(cwd, 'commit', '-m', 'human change')
        snapshot = self.controls.handoff(self.id, dedup='handoff-1')
        self.assertEqual(snapshot, self.controls.handoff(self.id, dedup='handoff-1'))
        task = self.store.task(self.id)
        self.assertEqual(task['revision'], 2)
        self.assertEqual(task['data']['codex_head'], snapshot['head'])
        self.store.submit(self.id, 'resume')
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE')
        self.assertEqual((Path(task['data']['integration_worktree']) / 'notes.txt').read_text(), 'human code\n')
        self.assertIn('Human handoff', self.codex.calls[-1][2])

    def test_dirty_handoff_requires_explicit_adoption(self):
        cwd = self.take()
        (cwd / 'notes.txt').write_text('uncommitted\n', encoding='utf-8')
        with self.assertRaises(TeamError) as ctx:
            self.controls.handoff(self.id)
        self.assertEqual(ctx.exception.kind, 'DIRTY_HANDOFF')
        self.assertEqual(self.store.workspaces(self.id)[0]['owner'], 'USER')
        snapshot = self.controls.handoff(self.id, include_uncommitted=True)
        self.assertIn('notes.txt', snapshot['adopted_files'])
        self.assertEqual(self.flow.git.changed(cwd), [])

    def test_conflict_blocks_handoff_and_preserves_user_ownership(self):
        cwd = self.take()
        gitdir = Path(self.flow.git.git(cwd, 'rev-parse', '--absolute-git-dir'))
        (gitdir / 'MERGE_HEAD').write_text(self.flow.git.git(cwd, 'rev-parse', 'HEAD'))
        with self.assertRaises(TeamError) as ctx:
            self.controls.handoff(self.id)
        self.assertEqual(ctx.exception.kind, 'GIT_CONFLICT')
        self.assertEqual(self.store.workspaces(self.id)[0]['owner'], 'USER')

    def test_stale_control_and_duplicate_amend(self):
        self.setup_flow()
        first = self.store.submit(self.id, 'amend', payload={'change': 'new target'}, expected_revision=1, dedup='x')
        self.assertEqual(first['id'], self.store.submit(self.id, 'amend', payload={'change': 'new target'}, expected_revision=1, dedup='x')['id'])
        self.assertEqual(self.store.task(self.id)['revision'], 2)
        with self.assertRaises(TeamError) as ctx:
            self.store.submit(self.id, 'pause', expected_revision=1)
        self.assertEqual(ctx.exception.kind, 'STALE_COMMAND')

    def test_concurrent_commands_cas_only_one_amends(self):
        self.setup_flow()
        results = []
        def submit():
            try:
                results.append(self.store.submit(self.id, 'amend', payload={'change': 'target'}, expected_revision=1)['status'])
            except TeamError as error:
                results.append(error.kind)
        workers = [threading.Thread(target=submit) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sorted(results), ['PENDING', 'STALE_COMMAND'])

    def test_pending_pause_cannot_be_erased_by_duplicate_runner(self):
        self.setup_flow()
        self.store.submit(self.id, 'pause')
        with self.assertRaises(TeamError) as ctx:
            with self.store.claim(self.id):
                pass
        self.assertEqual(ctx.exception.kind, 'CONTROL_PENDING')

    def test_crashed_runner_or_reboot_marks_unknown_paid_receipt(self):
        self.setup_flow()
        call, cap = self.store.reserve(self.id, 'review', DEFAULTS)
        self.store.invocation('dead', self.id, 'key', 'claude', 'review', 1, call_id=call)
        with self.store.connection() as c:
            c.execute("UPDATE tasks SET owner='dead',pid=2147483646 WHERE id=?", (self.id,))
        task = self.run_flow()
        self.assertEqual(task['status'], 'READY_FOR_MERGE')
        self.assertEqual(self.store.calls(self.id)[0]['status'], 'UNKNOWN_RESULT')
        self.assertEqual(self.claude.calls, [])

    def test_partial_writes_after_crash_require_human_adoption(self):
        self.setup_flow()
        task = self.store.task(self.id)
        cwd = self.flow.git.ensure(task['repo'], self.id, 'codex', task['base'])
        (cwd / 'value.py').write_text('VALUE = 9\n')
        self.store.update(self.id, phase='develop', data={'codex_worktree': str(cwd), 'codex_head': task['base']})
        self.assertEqual(self.run_flow()['status'], 'WAITING_APPROVAL')
        self.assertEqual(self.codex.calls, [])

    def test_watch_reconnect_and_replay_does_not_call_agents(self):
        self.prepare_paused()
        events = self.store.events(self.id)
        cursor = events[-3]['seq']
        self.assertEqual(self.store.events(self.id, cursor), events[-2:])
        before = len(self.codex.calls)
        with redirect_stdout(io.StringIO()) as output:
            last = watch(self.store, self.id, after=cursor, once=True, json_output=True)
        self.assertEqual(last, events[-1]['seq'])
        self.assertEqual(len(self.codex.calls), before)
        self.assertIn('watch_cursor', output.getvalue())

    def test_live_stream_is_visible_before_process_exit(self):
        self.setup_flow()
        seen = threading.Event()
        def line(source, text):
            if text == '实时中文':
                seen.set()
        completed = threading.Event()
        release = self.root / 'release'
        def run():
            run_process([sys.executable, '-u', '-c', "import time; from pathlib import Path; print('实时中文', flush=True);\nwhile not Path('release').exists(): time.sleep(.1)"],
                        self.root, env={**os.environ, 'PYTHONUTF8': '1'}, on_line=line)
            completed.set()
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(seen.wait(10))
            self.assertFalse(completed.is_set())
        finally:
            release.write_text('continue')
            thread.join(10)
        self.assertTrue(completed.is_set())

    def test_stream_sink_scrubs_credentials_and_drops_private_reasoning(self):
        self.setup_flow()
        sink = EventSink(self.store, self.id, 'codex', 'stream', 1, self.root / 'visible.jsonl')
        sink.line('stdout', json.dumps({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private thinking'}}))
        sink.line('stdout', json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'key sk-fixture-stream-1234567890'}}))
        sink.close()
        content = (self.root / 'visible.jsonl').read_text()
        self.assertNotIn('private thinking', content)
        self.assertNotIn('sk-fixture-stream-1234567890', content)
        self.assertEqual(len([e for e in self.store.events(self.id) if e['kind'] == 'visible_event']), 1)

    def test_claude_stream_parser_keeps_only_final_result(self):
        self.setup_flow()
        envelope = {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'summary': 'clean', 'findings': []},
                    'usage': {'input_tokens': 2}, 'total_cost_usd': .01}
        def runner(argv, cwd, **kw):
            if '--help' in argv:
                return Result(0, '--max-budget-usd --json-schema --no-session-persistence --safe-mode --tools --strict-mcp-config --permission-prompts', '', 0)
            return Result(0, json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'thinking', 'thinking': 'private'}]}}) + '\n' + json.dumps(envelope), '', .1)
        adapter = ClaudeAdapter(runner=runner, binary='fixture.exe')
        answer = adapter.call(self.root, 'review', 'review', self.root / 'artifact', dict(DEFAULTS, allow_estimated_budget=True), cap=.1)
        self.assertEqual(answer.cost, .01)
        self.assertEqual(answer.data['findings'], [])
        self.assertIn('stream-json', adapter.command('review', dict(DEFAULTS, allow_estimated_budget=True), .1))
        self.assertIsNone(visible('claude', {'type': 'stream_event', 'event': {'delta': {'type': 'thinking_delta', 'thinking': 'private'}}}))

    def test_pause_blocks_paid_reservation(self):
        self.setup_flow()
        self.store.submit(self.id, 'pause')
        with self.assertRaises(TeamError):
            self.store.reserve(self.id, 'review', DEFAULTS)
        self.assertEqual(self.store.calls(self.id), [])

    def test_split_claude_credentials_are_redacted_together(self):
        self.setup_flow()
        sink = EventSink(self.store, self.id, 'claude', 'partial', 1, self.root / 'partial.jsonl')
        for chunk in ('API_KEY=sk-', 'abcdefghijklmnop123456', '\n'):
            sink.line('stdout', json.dumps({'type': 'stream_event', 'event': {'delta': {'type': 'text_delta', 'text': chunk}}}))
        sink.close()
        text = (self.root / 'partial.jsonl').read_text()
        self.assertNotIn('abcdefghijklmnop123456', text)
        self.assertIn('REDACTED', text)

    def test_spool_recovery_deduplicates_events_after_database_writer_failure(self):
        self.setup_flow()
        self.store.invocation('spool', self.id, 'key', 'codex', 'plan', 1)
        path = self.store.root / 'tasks' / self.id / 'calls' / 'spool' / 'visible.jsonl'
        original = self.store.visible_event
        self.store.visible_event = lambda *a: (_ for _ in ()).throw(OSError('disk fixture'))
        sink = EventSink(self.store, self.id, 'codex', 'spool', 1, path)
        sink.line('stdout', json.dumps({'type': 'turn.started'}))
        with self.assertRaises(TeamError):
            sink.close()
        self.store.visible_event = original
        self.store.recover_events(self.id)
        self.store.recover_events(self.id)
        self.assertEqual(sum(e['kind'] == 'visible_event' for e in self.store.events(self.id)), 1)

    def test_repeated_takeover_preserves_original_snapshot(self):
        cwd = self.take()
        first = self.store.workspaces(self.id)[0]['snapshot']
        (cwd / 'notes.txt').write_text('user has started editing')
        self.assertEqual(self.controls.takeover(self.id), first)

    def test_codex_recovered_reconnect_is_not_a_failed_final_turn(self):
        for message, code, succeeds in [('Reconnecting... 2/5 (request timed out)', 0, True),
                                        ('fatal unrelated error', 0, False), ('Reconnecting... 2/5', 1, False)]:
            def runner(argv, cwd, **kw):
                if 'login' in argv:
                    return Result(0, 'Logged in using ChatGPT', '', 0)
                if '--help' in argv:
                    return Result(0, '--json --output-schema --ephemeral --ignore-user-config --ignore-rules', '', 0)
                Path(argv[argv.index('-o') + 1]).write_text(json.dumps(PLAN), encoding='utf-8')
                return Result(code, json.dumps({'type': 'error', 'message': message}) + '\n' +
                              json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 1}}), '', .1)
            adapter = CodexAdapter(runner=runner, binary='fixture.exe')
            if succeeds:
                self.assertEqual(adapter.call(self.root, 'plan', 'task', self.root / 'adapter', DEFAULTS).data, PLAN)
            else:
                with self.assertRaises(TeamError):
                    adapter.call(self.root, 'plan', 'task', self.root / 'adapter', DEFAULTS)

    def test_pause_after_reservation_before_spawn_releases_only_proven_unissued_call(self):
        self.setup_flow(high=True)
        original = self.claude.call
        def pre_spawn(cwd, *args, **kw):
            self.store.submit(self.id, 'pause')
            return run_process([sys.executable, '-c', "from pathlib import Path; Path('unexpected').write_text('bad')"],
                               cwd, control=kw['control'])
        self.claude.call = pre_spawn
        task = self.run_flow()
        self.assertEqual(task['status'], 'PAUSED')
        call = self.store.calls(self.id)[0]
        self.assertEqual(call['status'], 'NOT_DISPATCHED')
        self.assertEqual(call['charged'], 0)
        self.assertEqual(self.store.daily_budget()['pending_reconciliation'], 0)
        self.store.submit(self.id, 'resume')
        self.claude.call = original
        self.assertEqual(self.run_flow()['status'], 'READY_FOR_MERGE')
        self.assertEqual(len(self.claude.calls), 1)

    def test_pause_between_tests_reuses_successful_test_receipt(self):
        self.setup_flow()
        task = self.store.task(self.id)
        task['config']['test_commands'].append([sys.executable, '-c', "print('second test')"])
        self.store.update(self.id, config=task['config'])
        calls, paused = [], False
        def runner(argv, cwd, **kw):
            nonlocal paused
            calls.append((argv, str(cwd)))
            result = run_process(argv, cwd, **kw)
            if not paused and argv[-1] == 'test.py':
                paused = True
                self.store.submit(self.id, 'pause')
            return result
        self.flow.test_runner = runner
        first = self.run_flow()
        self.assertEqual(first['status'], 'PAUSED')
        self.assertEqual(len(first['data']['tests']), 1)
        self.store.submit(self.id, 'resume')
        self.assertEqual(self.run_flow()['status'], 'READY_FOR_MERGE')
        development = first['data']['codex_worktree']
        self.assertEqual(sum(argv[-1] == 'test.py' and cwd == development for argv, cwd in calls), 1)
