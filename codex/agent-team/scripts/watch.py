"""One-command observer using installed metadata and the current Git project."""
import argparse
import json
import os
from pathlib import Path
import sys


def select_task(store, cwd):
    from agent_team.entry import git
    from agent_team.safety import TeamError
    repo = Path(git(cwd, 'rev-parse', '--show-toplevel')).resolve()
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    matching = []
    for row in store.tasks():
        task = store.task(row['id'])
        try:
            target = Path(task['repo']).resolve()
            same = target == repo
            if not same and target.is_dir():
                same = Path(git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve() == common
        except (TeamError, OSError, KeyError):
            same = False
        if same:
            store.reconcile_runtime(task['id'])
            matching.append(store.task(task['id']))
    active = [t for t in matching if t['status'] in {
        'RUNNING', 'QUEUED', 'WAITING_QUOTA', 'PAUSE_REQUESTED', 'PAUSING', 'STOP_REQUESTED'}]
    if len(active) > 1:
        raise TeamError('AMBIGUOUS_TASK', 'Multiple active tasks in this project; pass a Task ID: ' + ', '.join(t['id'] for t in active))
    if active:
        return active[0]['id']
    if matching:
        return matching[0]['id']  # Store.tasks is ordered by creation time, newest first.
    raise TeamError('NOT_FOUND', 'No Agent Team task for this project; start a task through Codex first')


def main(argv=None, *, installation=None):
    parser = argparse.ArgumentParser(description='监控当前项目的唯一活动任务；没有活动任务则回放最近任务，不发起模型调用')
    parser.add_argument('task_id', nargs='?')
    parser.add_argument('--cwd', type=Path, default=Path.cwd())
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--after', type=int, default=0)
    args = parser.parse_args(argv)
    try:
        installed = installation or json.loads((Path(__file__).resolve().parents[1] / 'installation.json').read_text(encoding='utf-8'))
        tool_root = Path(installed['tool_root']).resolve()
        if not (tool_root / 'agent_team' / 'cli.py').is_file():
            raise ValueError('Installed agent-team path is unavailable')
        sys.path.insert(0, str(tool_root))
        from agent_team.cli import main as cli_main
        from agent_team.safety import TeamError, scrub
        from agent_team.store import Store
        if os.environ.get('AGENT_TEAM_CHILD'):
            raise TeamError('RECURSION_BLOCKED', 'Agent/test children cannot start the parent task observer')
        store = Store(Path(installed['state_dir']))
        task_id = args.task_id or select_task(store, args.cwd)
        task = store.task(task_id)
        if not args.json:
            print(f"Task ID: {task_id}; status: {task['status']}; repo: {task['repo']}", flush=True)
        flags = ['--once'] if args.once else []
        flags += ['--json'] if args.json else []
        flags += ['--after', str(args.after)] if args.after else ['--replay']
        return cli_main(['--state-dir', str(store.root), 'watch', task_id, '--full', *flags])
    except Exception as error:
        # No credentials are read here; scrub errors when the tool package was loaded successfully.
        cleaner = locals().get('scrub', str)
        print(cleaner(f"{getattr(error, 'kind', 'INPUT_ERROR')}: {error}"), file=sys.stderr)
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
