import asyncio,json,time
from pathlib import Path
from backend.env import ChatMessage,ModelCallOptions,ToolDefinition
from novelty_agent_framework.config import load_application_config,build_model_registry
async def main():
 c=load_application_config(profile_path='config/profiles/cloud-workflow.json')
 client=build_model_registry(c).client_for('deepseek-official-flash')
 tool=ToolDefinition(name='echo_probe',description='Echo a synthetic test value.',parameters={'type':'object','properties':{'value':{'type':'string'}},'required':['value']})
 results=[]
 for name,options,prompt in [
  ('tools_and_auto',ModelCallOptions(max_tokens=256,timeout_seconds=60,tools=[tool],tool_choice='auto',extra_body={'enable_thinking':False}),'Call echo_probe with value probe. Do not answer with text.'),
  ('choice_none',ModelCallOptions(max_tokens=256,timeout_seconds=60,tools=[tool],tool_choice='none',extra_body={'enable_thinking':False}),'Reply with OK.'),
  ('json_object',ModelCallOptions(max_tokens=256,timeout_seconds=60,response_format={'type':'json_object'},extra_body={'enable_thinking':False}),'Return JSON only: {"ok":true}.')]:
  start=time.monotonic()
  r=await client.acomplete([ChatMessage(role='user',content=prompt)],options=options)
  passed=bool(r.tool_calls) if name=='tools_and_auto' else (not r.tool_calls and bool(r.content)) if name=='choice_none' else json.loads(r.content).get('ok') is True
  row={'name':name,'passed':passed,'duration_seconds':round(time.monotonic()-start,2),'tool_names':[x.name for x in r.tool_calls],'usage':r.usage}
  results.append(row);print(json.dumps(row),flush=True)
  assert passed,name
 out={'provider':'deepseek_official','base_url':client.profile.base_url,'model':client.profile.model,'credential_reference':'DEEPSEEK_API_KEY','synthetic_payload_only':True,'results':results}
 Path('docs/experiments/20261010_web_retirement/official-capabilities.json').write_text(json.dumps(out,indent=2)+'\n')
asyncio.run(main())
