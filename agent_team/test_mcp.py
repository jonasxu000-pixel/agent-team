"""Invocation-local stdio test adapter. It only requests existing runner tests."""
import json
from pathlib import Path
import sys

if __package__ in {None,''}:
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agent_team.safety import TeamError,scrub
from agent_team.tool_broker import request


def handle(spec,message):
    method=message.get('method');params=message.get('params') or {}
    if method=='initialize':
        return {'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'agent-team-approved-tests','version':'0.3.2'}}
    if method=='ping':return {}
    if method=='tools/list':
        return {'tools':[{'name':'Bash','description':'Execute only an exact orchestrator-approved test command through its isolated runner. No general shell.',
            'inputSchema':{'type':'object','properties':{'command':{'type':'string','enum':spec['commands']}},
                           'required':['command'],'additionalProperties':False}}]}
    if method=='tools/call':
        args=params.get('arguments') or {}
        if params.get('name')!='Bash' or set(args)!={'command'} or args.get('command') not in spec['commands']:
            raise TeamError('SAFETY_ERROR','Only exact approved test commands are accepted')
        index=spec['commands'].index(args['command'])
        result=request(spec,'test',{'index':index},timeout=180)
        return {'content':[{'type':'text','text':json.dumps(scrub(result),ensure_ascii=False)}],
                'isError':result['exit_code']!=0}
    raise TeamError('METHOD_NOT_FOUND','Unsupported local test method')


def main():
    spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    for raw in sys.stdin:
        if len(raw)>64000:break
        message=json.loads(raw)
        if 'id' not in message:continue
        try:reply={'jsonrpc':'2.0','id':message['id'],'result':handle(spec,message)}
        except Exception as e:
            reply={'jsonrpc':'2.0','id':message['id'],'error':{'code':-32602,'message':str(scrub(str(e)))[:500]}}
        print(json.dumps(reply,ensure_ascii=False),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    main()
