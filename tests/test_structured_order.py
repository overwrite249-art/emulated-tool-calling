"""Keep live-model history consistent with the advertised output format."""
import json
import unittest
from emutools import CanonMessage, CanonRequest, Config, ToolCall, ToolDef, build_structured_payload


class StructuredOrderTests(unittest.TestCase):
    def payload(self):
        request=CanonRequest(model='test',tools=[ToolDef('Read',schema={'type':'object'})],messages=[
            CanonMessage(role='user',text='Read the file.'),
            CanonMessage(role='assistant',tool_calls=[ToolCall('Read',{'file_path':'demo.py'},id='history-only')]),
            CanonMessage(role='user',tool_results=[('history-only','Read','actual observation',False)]),
        ])
        return build_structured_payload(request,Config(),[],True,4)

    def test_history_keeps_name_before_arguments(self):
        payload=self.payload()
        message=next(m for m in payload['messages'] if m['role']=='assistant')
        call=json.loads(message['content'])['tool_calls'][0]
        self.assertEqual(list(call)[:2],['name','arguments'])
        self.assertEqual(call['id'],'history-only')
        self.assertEqual(call['arguments'],{'file_path':'demo.py'})

    def test_output_contract_follows_tool_catalog(self):
        system='\n'.join(m['content'] for m in self.payload()['messages'] if m['role']=='system')
        self.assertGreater(system.rfind('Return exactly one JSON object'),system.rfind('Available tool contract:'))


if __name__=='__main__':unittest.main()
