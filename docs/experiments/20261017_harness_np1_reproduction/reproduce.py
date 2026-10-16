import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from scripts import compare_researcher_harness as exp
root=ROOT/'outputs/harness-np1-reproduction-20261017'
root.mkdir(exist_ok=False)
shutil.copytree(ROOT/'outputs/harness-c-rerun-20261017/fixture',root/'fixture')
source=next((ROOT/'outputs/harness-c-rerun-20261017/runs/choice_only/NP-1').glob('MF2033k6lC/runtime/*/llm_calls/0001*.json'))
original=exp.read(source)
payload=original['request_payload']
exp.write(root/'replayed-request.json',payload)
menu=json.loads(payload['messages'][-1]['content'].split('valid):\n',1)[1].split('\nReturn',1)[0])
replays=[]
for i in range(3):
    started=time.monotonic()
    request=urllib.request.Request(original['request_options']['endpoint'],data=json.dumps(payload).encode(),
        headers={'Content-Type':'application/json','Authorization':'Bearer local'},method='POST')
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request,timeout=120) as response: data=json.load(response)
        content=data['choices'][0]['message'].get('content')
        try: decision=json.loads(content or '{}')
        except ValueError: decision={}
        option=decision.get('option_id')
        replays.append({'attempt':i+1,'seconds':round(time.monotonic()-started,3),'response':data,
            'valid_option_id':option in menu if isinstance(option,str) else False,
            'label_used_as_id':option in menu.values() if isinstance(option,str) else False})
    except Exception as exc:
        replays.append({'attempt':i+1,'error':str(exc)})
    exp.write(root/'request-replays.json',{'source':str(source.relative_to(ROOT)),
        'payload_sha256':exp.canonical(payload),'allowed_choices':menu,'attempts':replays})
    print('replay',i+1,{k:v for k,v in replays[-1].items() if k!='response'},flush=True)

async def main():
    exp._load_dev_env()
    exp.verify_fixture(root)
    raw=next(r for r in exp.read(root/'fixture/requests.json') if r['novelty_point']['point_id']=='NP-1')
    await exp.run_case(root,'choice_only',raw)
    exp.summarize(root)
asyncio.run(main())
