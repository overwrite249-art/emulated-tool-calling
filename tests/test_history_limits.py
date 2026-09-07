"""Older observations may be clipped explicitly; current batches and source inputs are not."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch
from emutools import CanonMessage,CanonRequest,Config,ToolCall,ToolDef
from emutools.wire import build_upstream_messages
from emutools.structured import build_structured_payload

OLD='old-start '+('x'*600)+' OLD-CENTER '+('y'*600)+' old-end'
NEW_A='a-start '+('a'*600)+' A-CENTER '+('b'*600)+' a-end'
NEW_B='b-start '+('c'*600)+' B-CENTER '+('d'*600)+' b-end'
IMAGE='data:image/png;base64,YQ=='


def request(images=False):
    messages=[CanonMessage('assistant',tool_calls=[ToolCall('Read',{'file_path':'old.txt','literal':OLD},id='old')]),
        CanonMessage('user',text='User text stays intact.',tool_results=[('old','Read',OLD,True)]),
        CanonMessage('assistant',tool_calls=[ToolCall('Read',{'file_path':'a.txt'},id='a'),ToolCall('Read',{'file_path':'b.txt'},id='b')]),
        CanonMessage('user',tool_results=[('b','Read',NEW_B,False)]),
        CanonMessage('user',tool_results=[('a','Read',NEW_A,False)])]
    if images:
        for message in messages:
            for tid,name,content,error in message.tool_results:
                message.content_parts=[{'type':'tool_result','id':tid,'name':name,'is_error':error,'content':[
                    {'type':'text','text':content},{'type':'image_url','image_url':{'url':IMAGE}}]}]
    return CanonRequest('test',messages,tools=[ToolDef('Read')])


def structured_results(payload):
    results=[]
    for message in payload['messages']:
        if message['role']!='user' or not isinstance(message['content'],str):continue
        try:body=json.loads(message['content'])
        except ValueError:continue
        results.extend(body.get('tool_results',[]))
    return results


class HistoryLimitTests(unittest.TestCase):
    def test_default_and_environment_configuration(self):
        with patch.dict(os.environ,{},clear=True):self.assertEqual(Config().history_result_chars,0)
        with patch.dict(os.environ,{'EMU_HISTORY_RESULT_CHARS':'512'},clear=True):self.assertEqual(Config().history_result_chars,512)
        for invalid in [-1,True,1.5,'80']:
            with self.assertRaises(ValueError):Config(history_result_chars=invalid)

    def test_disabled_mode_is_byte_identical(self):
        req=request()
        left=build_upstream_messages(req,Config(merge_roles=False),[])
        right=build_upstream_messages(req,Config(merge_roles=False,history_result_chars=0),[])
        self.assertEqual(left,right)
        self.assertIn(OLD,right[2]['content'])

    def test_only_older_results_are_clipped_in_text_mode(self):
        req=request();before=deepcopy(req)
        messages=build_upstream_messages(req,Config(history_result_chars=80,merge_roles=False),[])
        old=messages[2]['content'];self.assertNotIn('OLD-CENTER',old);self.assertIn('characters omitted',old)
        self.assertIn('history_id="old"',old);self.assertIn('status="error"',old);self.assertIn('User text stays intact.',old)
        self.assertIn(NEW_B,messages[4]['content']);self.assertIn('history_id="b"',messages[4]['content'])
        self.assertIn(NEW_A,messages[5]['content']);self.assertIn('history_id="a"',messages[5]['content'])
        self.assertIn('OLD-CENTER',messages[1]['content']) # call arguments are never clipped
        self.assertEqual(req,before)

    def test_structured_history_keeps_ids_arguments_and_latest_results(self):
        req=request();before=deepcopy(req)
        payload=build_structured_payload(req,Config(history_result_chars=80,merge_roles=False),[],True,2)
        results=structured_results(payload);self.assertEqual([r['id'] for r in results],['old','b','a'])
        self.assertNotIn('OLD-CENTER',results[0]['content']);self.assertTrue(results[0]['is_error'])
        self.assertEqual([r['content'] for r in results[1:]],[NEW_B,NEW_A])
        assistant=next(m for m in payload['messages'] if m['role']=='assistant')
        self.assertEqual(json.loads(assistant['content'])['tool_calls'][0]['arguments']['literal'],OLD)
        self.assertEqual(req,before)

    def test_image_bytes_and_reverse_order_correlation_are_preserved(self):
        req=request(images=True);before=deepcopy(req)
        for structured in [False,True]:
            cfg=Config(image_inputs=True,history_result_chars=80,merge_roles=False)
            messages=(build_structured_payload(req,cfg,[],True,2)['messages'] if structured else build_upstream_messages(req,cfg,[]))
            user=[m['content'] for m in messages if m['role']=='user']
            self.assertEqual([part['image_url']['url'] for parts in user for part in parts if part['type']=='image_url'],[IMAGE]*3)
            texts=[''.join(part.get('text','') for part in parts) for parts in user]
            self.assertNotIn('OLD-CENTER',texts[0]);self.assertIn('old',texts[0])
            self.assertIn('B-CENTER',texts[1]);self.assertIn('A-CENTER',texts[2])
        self.assertEqual(req,before)

    def test_history_limit_cannot_increase_the_existing_result_limit(self):
        payload=build_structured_payload(request(),Config(max_result_chars=80,history_result_chars=512,merge_roles=False),[],True,2)
        for item in structured_results(payload):self.assertIn('characters omitted',item['content']);self.assertLess(len(item['content']),100)

    def test_explicit_history_limit_works_with_unlimited_current_results(self):
        payload=build_structured_payload(request(),Config(max_result_chars=0,history_result_chars=80,merge_roles=False),[],True,2)
        results=structured_results(payload);self.assertNotIn('OLD-CENTER',results[0]['content']);self.assertEqual(results[-1]['content'],NEW_A)

    def test_no_call_boundary_does_not_invent_older_groups(self):
        req=CanonRequest('test',[CanonMessage('user',tool_results=[('a','Read',OLD,False)]),CanonMessage('user',tool_results=[('b','Read',NEW_B,False)])])
        payload=build_structured_payload(req,Config(history_result_chars=80,merge_roles=False),[],False,0)
        self.assertEqual([r['content'] for r in structured_results(payload)],[OLD,NEW_B])


if __name__=='__main__':unittest.main()
