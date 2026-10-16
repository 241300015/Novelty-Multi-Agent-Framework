import asyncio
import json

from backend.env import ChatMessage, ModelCallOptions, ModelResponse
from scripts.compare_researcher_harness import DecisionSchemaClient


def test_decision_schema_tracks_current_menu_without_changing_prompt_or_native_tools():
    class Client:
        deadline = 123
        async def acomplete(self, messages, *, options):
            menu = json.loads(messages[-1].content.split('\n', 1)[1].split('\nReturn', 1)[0])
            format = options.response_format
            assert format['type'] == 'json_schema' and format['json_schema']['strict']
            schema = format['json_schema']['schema']
            assert schema['properties']['option_id']['enum'] == list(menu)
            assert schema['required'] == ['option_id'] and not schema['additionalProperties']
            assert options.tools == () and options.tool_choice == 'none'
            assert options.temperature == 0.3 and options.max_tokens == 4096
            return ModelResponse(content=json.dumps({'option_id':next(iter(menu))}))
    wrapper = DecisionSchemaClient(Client())
    assert wrapper.deadline == 123
    options = ModelCallOptions(temperature=0.3,max_tokens=4096,tools=(),tool_choice='none')
    async def run():
        for menu in [{'v1_0':'检索','v1_1':'停止'},{'v2_0':'读取摘要'}]:
            message = ChatMessage(role='user',content='Current Harness choices (only these option_id values are valid):\n'+json.dumps(menu)+'\nReturn JSON')
            before = message.content
            await wrapper.acomplete([message],options=options)
            assert message.content == before and options.response_format is None
    asyncio.run(run())


def test_empty_menu_finalization_and_correction_keep_original_output_contract():
    options = ModelCallOptions(tools=(),tool_choice='none')
    class Client:
        async def acomplete(self,messages,*,options):
            assert options is original_options
            return ModelResponse(content='{"cards":[],"no_evidence_reason":"test"}')
    original_options=options
    wrapper=DecisionSchemaClient(Client())
    async def run():
        for content in ['Current Harness choices (only these option_id values are valid):\n{}\nReturn final JSON',
                        'Research stopped: budget exhausted. Return final JSON.',
                        '{"rejected_cards": [], "reader_results": []}']:
            result=await wrapper.acomplete([ChatMessage(role='user',content=content)],options=options)
            assert 'cards' in result.content
    asyncio.run(run())
