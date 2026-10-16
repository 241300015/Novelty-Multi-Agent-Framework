import copy,json,urllib.request,urllib.error
from pathlib import Path
root=Path(__file__).resolve().parents[3]
out=root/'outputs/harness-np1-schema-20261017'
out.mkdir(exist_ok=False)
payload=json.loads((root/'docs/experiments/20261017_harness_np1_reproduction/replayed-request.json').read_text())
menu=json.loads(payload['messages'][-1]['content'].split('valid):\n',1)[1].split('\nReturn',1)[0])
payload['response_format']={'type':'json_schema','json_schema':{'name':'harness_decision','strict':True,'schema':{
 'type':'object','properties':{'option_id':{'type':'string','enum':list(menu)}},'required':['option_id'],'additionalProperties':False}}}
(out/'probe-request.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n')
results=[]
for i in range(3):
 req=urllib.request.Request('http://127.0.0.1:8000/v1/chat/completions',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer local'},method='POST')
 try:
  with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=120) as r: response=json.load(r)
  content=response['choices'][0]['message']['content'];d=json.loads(content)
  valid=set(d)=={'option_id'} and d['option_id'] in menu
  result={'attempt':i+1,'valid':valid,'response':response}
 except urllib.error.HTTPError as e: result={'attempt':i+1,'valid':False,'http_status':e.code,'error':e.read().decode()}
 except Exception as e: result={'attempt':i+1,'valid':False,'error':str(e)}
 results.append(result)
 (out/'probe-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
 print(i+1,result.get('valid'),result.get('error'),flush=True)
 if not result['valid']:break
