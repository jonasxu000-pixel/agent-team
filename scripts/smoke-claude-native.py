"""Prepare by default. Real inference requires an explicit task ID and approval amount."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_team.native_smoke import prepare, execute
from agent_team.safety import TeamError, scrub
from agent_team.store import Store


def main():
    if os.environ.get('AGENT_TEAM_CHILD'):
        print('RECURSION_BLOCKED: an agent/test cannot invoke a smoke task', file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--ceiling', type=float, default=.10)
    parser.add_argument('--task-id')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--approve-claude-budget', type=float)
    args = parser.parse_args()
    if args.state_dir is None:
        installation = json.loads((Path.home()/'.agents'/'skills'/'agent-team'/'installation.json').read_text(encoding='utf-8-sig'))
        args.state_dir = Path(installation['state_dir'])
    store = Store(args.state_dir)
    try:
        if args.execute:
            if not args.task_id or args.approve_claude_budget is None:
                raise TeamError('NEEDS_BUDGET_APPROVAL', 'Real smoke requires the prepared Task ID and explicit fee approval')
            result = execute(store, args.task_id, approved_budget=args.approve_claude_budget)
        else:
            if args.task_id or args.approve_claude_budget is not None:
                raise TeamError('CONFIG_ERROR', 'Approval flags are only accepted with --execute')
            task_id = prepare(store, ceiling=args.ceiling)
            result = {'task_id': task_id, 'status': 'PREPARED_NOT_DISPATCHED', 'paid_calls': 0,
                      'platform_ceiling_usd': args.ceiling, 'model': 'existing configured sonnet channel'}
        print(json.dumps(scrub(result), ensure_ascii=False, indent=2))
        return 0
    except TeamError as error:
        print(json.dumps({'status': error.kind, 'reason': str(scrub(str(error))), 'automatic_retry': False}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
