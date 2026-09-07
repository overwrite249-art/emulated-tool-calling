"""Out-of-order observations must keep their original call association."""
import json
import unittest
from emutools import Config,openai_to_canon,anthropic_to_canon,build_upstream_payload
from emutools.structured import build_structured_payload
from benchmarks.vision.client_trace import inspect_cli_trace,score_observations

IMAGE={'type':'image','source':{'type':'base64','media_type':'image/png','data':'YQ=='}}


class HistoryCorrelationTests(unittest.TestCase):
    def request(self,images=False):
        result=lambda identifier,value:{'type':'tool_result','tool_use_id':identifier,'content':[IMAGE] if images else value}
        return {'model':'model','messages':[
            {'role':'assistant','content':[{'type':'tool_use','id':'read-a','name':'Read','input':{'file_path':'a.png'}},{'type':'tool_use','id':'read-b','name':'Read','input':{'file_path':'b.png'}}]},
            {'role':'user','content':[result('read-b','B RESULT'),result('read-a','A RESULT')]}]}

    def test_text_results_preserve_ids_in_reverse_completion_order(self):
        cfg=Config();request=anthropic_to_canon(self.request(),cfg);payload=build_upstream_payload(request,cfg,[],True)
        assistant,user=[m['content'] for m in payload['messages'] if m['role']!='system']
        for identifier in ['read-a','read-b']:
            label='history_id='+json.dumps(identifier)
            self.assertIn(label,assistant);self.assertIn(label,user)
        self.assertLess(user.index('history_id="read-b"'),user.index('B RESULT'))
        self.assertLess(user.index('B RESULT'),user.index('history_id="read-a"'))

    def test_image_results_preserve_ids_next_to_their_images(self):
        cfg=Config(image_inputs=True);request=anthropic_to_canon(self.request(True),cfg);payload=build_upstream_payload(request,cfg,[],True)
        parts=payload['messages'][-1]['content'];labels=[];current=''
        for part in parts:
            if part['type']=='text':current+=part['text']
            elif part['type']=='image_url':labels.append(current);current=''
        self.assertIn('history_id="read-b"',labels[0]);self.assertIn('history_id="read-a"',labels[1])
        self.assertNotIn('history_id="read-a"',labels[0])

    def test_json_history_remains_structured_without_text_labels(self):
        cfg=Config(image_inputs=True);request=anthropic_to_canon(self.request(True),cfg);payload=build_structured_payload(request,cfg,[],True,2)
        self.assertNotIn('history_id=',json.dumps(payload))
        results=[json.loads(p['text'])['tool_results'][0]['id'] for p in payload['messages'][-1]['content'] if p['type']=='text' and 'tool_results' in p['text']]
        self.assertEqual(results,['read-b','read-a'])


class ClientTraceTests(unittest.TestCase):
    def event(self,message_id,call_id,path):
        return {'type':'assistant','message':{'id':message_id,'content':[{'type':'tool_use','id':call_id,'name':'Read','input':{'file_path':path}}]}}

    def test_fragments_with_one_message_id_are_one_batch(self):
        events=[self.event('message-1','call-a','a.png'),self.event('message-1','call-b','b.png')]
        result=inspect_cli_trace('\n'.join(map(json.dumps,events)))
        self.assertEqual(result['max_calls_in_one_response'],2);self.assertEqual(result['read_paths'],['a.png','b.png'])
        self.assertNotIn('message-1',json.dumps(result))

    def test_repeated_fragments_do_not_duplicate_calls(self):
        event=self.event('message-1','call-a','a.png');result=inspect_cli_trace('\n'.join(map(json.dumps,[event,event])))
        self.assertEqual(result['emitted_tool_calls'],1);self.assertEqual(result['read_paths'],['a.png'])

    def test_different_messages_are_not_mistaken_for_batching(self):
        events=[self.event('message-1','call-a','a.png'),self.event('message-2','call-b','b.png')]
        result=inspect_cli_trace('\n'.join(map(json.dumps,events)))
        self.assertEqual(result['max_calls_in_one_response'],1);self.assertEqual(result['emitted_tool_calls'],2)

    def test_missing_fields_and_booleans_do_not_pass_visual_scoring(self):
        expected={'a':{'code':'AB12','blue_circles':1,'triangle_color':'red'}}
        for args in [{'fixture':'a'},{'fixture':'a','code':'AB12','blue_circles':True,'triangle_color':'red'}]:
            score=score_observations([{'name':'record_scene','arguments':args}],expected)['a']
            self.assertFalse(score['schema_valid']);self.assertFalse(score['exact']);self.assertFalse(score['shapes_and_colors_correct'])


if __name__=='__main__':unittest.main()
