"""Real TCP/proxy framing; deterministic upstream responses, no paid model or tool execution."""
import json
import threading
import unittest
import urllib.request
from unittest.mock import patch
import emutools.engine as engine
from emutools import Config,ToolCall,render_tool_call_text
from emutools.server import Server,Handler

SCHEMA={'type':'object','properties':{'file_path':{'type':'string'}},'required':['file_path'],'additionalProperties':False}
BAD='<tool_call name="Read">{"arguments":{"file_path":"one.txt"},"file_path":"two.txt"}</tool_call>'
GOOD=render_tool_call_text(ToolCall('Read',{'file_path':'chosen.txt'}))


def chunks(raw):
    for character in raw:yield {'text':character}
    yield {'usage':{'prompt_tokens':3,'completion_tokens':2}}
    yield {'finish':'stop'}


def completion(raw):
    return {'choices':[{'message':{'content':raw},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':3,'completion_tokens':2}}


def decoded_calls(raw,protocol,stream):
    if not stream:
        value=json.loads(raw)
        if protocol=='anthropic':return [(b['name'],b['input']) for b in value['content'] if b['type']=='tool_use']
        return [(b['function']['name'],json.loads(b['function']['arguments'])) for b in value['choices'][0]['message'].get('tool_calls',[])]
    blocks={}
    for line in raw.decode().splitlines():
        if not line.startswith('data: '):continue
        payload=line[6:]
        if payload=='[DONE]':continue
        value=json.loads(payload)
        if value.get('type')=='error' or 'error' in value:raise AssertionError('Unexpected error event')
        if protocol=='anthropic':
            if value.get('type')=='content_block_start' and value['content_block']['type']=='tool_use':
                b=value['content_block'];blocks[value['index']]={'name':b['name'],'parts':[],'input':b.get('input',{})}
            if value.get('type')=='content_block_delta' and value.get('delta',{}).get('type')=='input_json_delta':
                blocks[value['index']]['parts'].append(value['delta']['partial_json'])
        else:
            for choice in value.get('choices',[]):
                for b in choice.get('delta',{}).get('tool_calls',[]):
                    entry=blocks.setdefault(b['index'],{'name':'','parts':[],'input':{}});f=b.get('function',{})
                    entry['name']+=f.get('name','');entry['parts'].append(f.get('arguments',''))
    return [(b['name'],json.loads(''.join(b['parts'])) if ''.join(b['parts']) else b['input']) for _,b in sorted(blocks.items())]


class TextGuardHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=Server(('127.0.0.1',0),Handler,cfg=Config(parallel=True,max_calls_per_turn=4,loop_retry=True,upstream_base='http://127.0.0.1:1'))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.base='http://127.0.0.1:%d'%cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(timeout=5)

    def check_matrix(self,responses,expected,request_count):
        for protocol in ['anthropic','openai']:
            for stream in [False,True]:
                with self.subTest(protocol=protocol,stream=stream):
                    body={'model':'test-model','messages':[{'role':'user','content':'Read the requested files'}],'max_tokens':200,'stream':stream}
                    if protocol=='anthropic':
                        path='/v1/messages';body['tools']=[{'name':'Read','description':'Read a synthetic path','input_schema':SCHEMA}]
                    else:
                        path='/v1/chat/completions';body['parallel_tool_calls']=True
                        body['tools']=[{'type':'function','function':{'name':'Read','description':'Read a synthetic path','parameters':SCHEMA}}]
                    pending=iter(responses)
                    with patch.object(engine,'upstream_complete',side_effect=lambda *_:completion(next(pending))) as sync_mock,patch.object(engine,'upstream_stream',side_effect=lambda *_:chunks(next(pending))) as stream_mock:
                        request=urllib.request.Request(self.base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                        with urllib.request.urlopen(request,timeout=10) as response:
                            self.assertEqual(response.status,200);raw=response.read()
                    self.assertEqual(decoded_calls(raw,protocol,stream),expected)
                    self.assertEqual(sync_mock.call_count+stream_mock.call_count,request_count)

    def test_ambiguous_call_recovers_before_any_call_is_delivered(self):
        self.check_matrix([BAD,GOOD],[('Read',{'file_path':'chosen.txt'})],2)

    def test_valid_peer_is_not_replayed_or_retargeted(self):
        self.check_matrix([GOOD+'\n'+BAD],[('Read',{'file_path':'chosen.txt'})],1)

    def test_independent_batch_preserves_literal_arguments(self):
        second={'file_path':'Привіт 🐈 </tool_call>.txt'}
        self.check_matrix([GOOD+'\n'+render_tool_call_text(ToolCall('Read',second))],
                          [('Read',{'file_path':'chosen.txt'}),('Read',second)],1)


if __name__=='__main__':unittest.main()
