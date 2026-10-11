import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_team.native_e2e import prepare, execute
from agent_team.store import Store
from agent_team.safety import TeamError,scrub


def main():
    if os.environ.get('AGENT_TEAM_CHILD'):
        raise TeamError('RECURSION_BLOCKED','A child cannot start acceptance orchestration')
    parser=argparse.ArgumentParser()
    parser.add_argument('--approve-task-budget',type=float)
    parser.add_argument('--task-id')
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--repair-round',type=int,default=0)
    parser.add_argument('--repair-proof',type=Path)
    args=parser.parse_args()
    meta=json.loads((Path.home()/'.agents/skills/agent-team/installation.json').read_text(encoding='utf-8-sig'))
    store=Store(Path(meta['state_dir']))
    if args.execute:
        if not args.task_id:raise TeamError('TASK_ID_REQUIRED','Use the isolated prepared Task ID')
        proof=json.loads(args.repair_proof.read_text(encoding='utf-8')) if args.repair_proof else None
        return execute(store,args.task_id,repair_round=args.repair_round,repair_proof=proof)
    if args.approve_task_budget is None:
        raise TeamError('NEEDS_BUDGET_APPROVAL','Explicit infrastructure acceptance budget required')
    tid=prepare(store,args.approve_task_budget)
    return {'task_id':tid,'status':'PREPARED','model_calls':0,'budget_usd':args.approve_task_budget,'max_repairs':3}


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    try:print(json.dumps(scrub(main()),ensure_ascii=False,indent=2))
    except TeamError as e:
        print(json.dumps({'kind':e.kind,'reason':str(scrub(str(e)))}),file=sys.stderr)
        raise SystemExit(2)
