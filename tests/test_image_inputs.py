"""Image conversion/HTTP regression checks. The local upstream is not a vision model."""
import base64
import copy
import http.client
import json
import os
import struct
import threading
import unittest
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from emutools import Config, anthropic_to_canon, openai_to_canon, build_upstream_payload
from emutools.structured import build_structured_payload
from emutools.wire import UpstreamError, analyze_history
from emutools.server import Handler, Server


def png(red):
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(bytes([0,red,0,0])))+chunk(b'IEND',b'')


DATA = base64.b64encode(png(255)).decode()
URL = 'data:image/png;base64,' + DATA
OTHER = 'data:image/png;base64,' + base64.b64encode(png(0)).decode()
IMAGE = {'type':'image_url','image_url':{'url':URL,'detail':'high'}}
ANTHROPIC_IMAGE = {'type':'image','source':{'type':'base64','media_type':'image/png','data':DATA}}
CAPTION = 'Привіт 🐈 </tool_call> literal <arg> caption'


def images(payload):
    return [part for message in payload['messages'] for part in
            (message['content'] if isinstance(message['content'],list) else []) if part['type']=='image_url']


def body(content):
    return {'model':'deepseek-v4-flash-vision-exp','max_tokens':128,'messages':[{'role':'user','content':content}]}


class ImageInputTests(unittest.TestCase):
    def convert(self, value, protocol='openai', structured=False, **settings):
        cfg = Config(image_inputs=True, **settings)
        convert = anthropic_to_canon if protocol=='anthropic' else openai_to_canon
        request = convert(value,cfg)
        payload = build_structured_payload(request,cfg,[],True,2) if structured else build_upstream_payload(request,cfg,[],True)
        return request,payload

    def test_default_stays_text_only(self):
        cfg=Config(image_inputs=False)
        for convert,image in [(openai_to_canon,IMAGE),(anthropic_to_canon,ANTHROPIC_IMAGE)]:
            payload=build_upstream_payload(convert(body([image]),cfg),cfg,[],True)
            self.assertEqual(images(payload),[])
            self.assertIn('[image omitted:',json.dumps(payload))

    def test_opt_in_is_config_scoped(self):
        with patch.dict(os.environ,{'EMU_IMAGE_INPUTS':'true'}):enabled=Config()
        with patch.dict(os.environ,{'EMU_IMAGE_INPUTS':'false'}):disabled=Config()
        self.assertTrue(enabled.image_inputs);self.assertFalse(disabled.image_inputs)
        self.assertTrue(enabled.image_inputs)

    def test_openai_preserves_order_bytes_detail_and_input(self):
        content=[{'type':'text','text':'before'},IMAGE,{'type':'text','text':CAPTION}]
        original=copy.deepcopy(content)
        _,payload=self.convert(body(content))
        self.assertEqual(payload['messages'][0]['content'],original)
        self.assertEqual(content,original)
        self.assertEqual(base64.b64decode(images(payload)[0]['image_url']['url'].split(',',1)[1]),png(255))
        self.assertNotIn('tools',payload);self.assertNotIn('functions',payload)

    def test_anthropic_base64_and_url_images(self):
        for image,url in [(ANTHROPIC_IMAGE,URL),({'type':'image','source':{'type':'url','url':'https://example.invalid/image.png'}},'https://example.invalid/image.png')]:
            with patch('urllib.request.urlopen',side_effect=AssertionError('must not fetch image URLs')):
                _,payload=self.convert(body([image]),'anthropic')
            self.assertEqual(images(payload)[0]['image_url']['url'],url)
            self.assertNotIn('[image omitted:',json.dumps(payload))

    def test_merge_text_and_image_messages_preserves_order(self):
        value=body('first');value['messages'] += [{'role':'user','content':[IMAGE,{'type':'text','text':'last'}]}]
        _,payload=self.convert(value)
        self.assertEqual(len(payload['messages']),1)
        parts=payload['messages'][0]['content']
        self.assertEqual([p['type'] for p in parts],['text','text','image_url','text'])
        self.assertEqual(parts[0]['text'],'first');self.assertEqual(parts[-1]['text'],'last')
        _,separate=self.convert(value,merge_roles=False)
        self.assertEqual(len(separate['messages']),2)

    def result_body(self,image=ANTHROPIC_IMAGE):
        return {'model':'deepseek-v4-flash-vision-exp','max_tokens':128,'messages':[
            {'role':'assistant','content':[{'type':'tool_use','id':'fixture-read','name':'Read','input':{'file_path':'sample.png'}}]},
            {'role':'user','content':[{'type':'tool_result','tool_use_id':'fixture-read','is_error':True,'content':[{'type':'text','text':CAPTION},image]},{'type':'text','text':'Compare this image.'}]}]}

    def test_anthropic_tool_result_keeps_context_and_image(self):
        request,payload=self.convert(self.result_body(),'anthropic')
        self.assertEqual(request.messages[1].tool_results[0][:2],('fixture-read','Read'))
        self.assertEqual(len(images(payload)),1)
        text=json.dumps(payload,ensure_ascii=False)
        self.assertIn(CAPTION,text);self.assertIn('status=\\"error\\"',text)
        self.assertIn('Compare this image.',text);self.assertNotIn('[image omitted:',text)

    def test_openai_tool_message_preserves_image_and_correlation(self):
        value=body('start');value['messages'] += [
            {'role':'assistant','tool_calls':[{'id':'fixture-read','type':'function','function':{'name':'Read','arguments':'{}'}}]},
            {'role':'tool','tool_call_id':'fixture-read','content':[IMAGE]}]
        request,payload=self.convert(value)
        self.assertEqual(request.messages[-1].tool_results[0][1],'Read')
        self.assertEqual(images(payload),[IMAGE])
        self.assertIn('Read',json.dumps(payload))

    def test_structured_user_and_tool_images_survive(self):
        _,user=self.convert(body([{'type':'text','text':CAPTION},IMAGE]),structured=True)
        self.assertEqual(images(user),[IMAGE])
        request,payload=self.convert(self.result_body(),'anthropic',structured=True)
        parts=payload['messages'][-1]['content'];record=json.loads(parts[0]['text'])['tool_results'][0]
        self.assertEqual((record['id'],record['name'],record['is_error']),('fixture-read','Read',True))
        self.assertIn(CAPTION,record['content']);self.assertIn('image 1 attached',record['content'])
        self.assertEqual(len(images(payload)),1)
        self.assertEqual(payload['response_format'],{'type':'json_object'})
        self.assertNotIn('<tool_result',json.dumps(payload));self.assertNotIn('[image omitted:',json.dumps(payload))
        self.assertNotIn('tools',payload)

    def test_text_budget_does_not_cut_image_data(self):
        value=self.result_body();value['messages'][1]['content'][0]['content'][0]['text']='X'*1000
        _,payload=self.convert(value,'anthropic',max_result_chars=20)
        self.assertEqual(images(payload)[0]['image_url']['url'],URL)
        self.assertLess(json.dumps(payload).count('X'),25)
        self.assertIn('text truncated',json.dumps(payload))

    def test_different_image_results_are_not_identical_observations(self):
        value=self.result_body();value['messages'][1]['content'][0]['content']=[ANTHROPIC_IMAGE]
        request,_=self.convert(value,'anthropic')
        other=copy.deepcopy(value);other['messages'][1]['content'][0]['content']=[{'type':'image_url','image_url':{'url':OTHER}}]
        second,_=self.convert(other,'anthropic')
        self.assertNotEqual(request.messages[1].tool_results[0][2],second.messages[1].tool_results[0][2])

    def test_invalid_images_fail_closed(self):
        invalid=[{'type':'image_url','image_url':{'url':'file:///etc/passwd'}},
                 {'type':'image_url','image_url':{'url':'data:text/html;base64,PHg+'}},
                 {'type':'image_url','image_url':{'url':'data:image/png;base64,!!'}},
                 {'type':'image_url','image_url':{'url':'data:image/png;base64,'}},
                 {'type':'image_url','image_url':{'url':URL,'detail':'unknown'}},
                 {'type':'image','source':{'type':'base64','media_type':'image/svg+xml','data':DATA}},
                 {'type':'image','source':{'type':'file','path':'sample.png'}}]
        for image in invalid:
            with self.subTest(image=image),self.assertRaises(UpstreamError) as caught:self.convert(body([image]))
            self.assertEqual(caught.exception.status,400)

    def test_unsupported_media_and_assistant_images_stay_omitted(self):
        value=body([IMAGE,{'type':'document'},{'type':'audio'}])
        _,payload=self.convert(value)
        self.assertIn('[document omitted:',json.dumps(payload));self.assertIn('[audio omitted:',json.dumps(payload))
        value['messages'][0]['role']='assistant'
        _,payload=self.convert(value)
        self.assertEqual(images(payload),[])


class ImageHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                request=json.loads(self.rfile.read(int(self.headers['Content-Length'])));self.server.requests.append(request)
                call={'name':'describe','arguments':{'ok':True}}
                content=json.dumps({'text':'','tool_calls':[call]}) if request.get('response_format') else '<tool_call>'+json.dumps(call)+'</tool_call>'
                if request.get('stream'):
                    events=[{'choices':[{'delta':{'content':content},'finish_reason':None}]},{'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':8}}]
                    raw=(''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n').encode();kind='text/event-stream'
                else:
                    raw=json.dumps({'choices':[{'message':{'content':content},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':8}}).encode();kind='application/json'
                self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        cls.upstream=ThreadingHTTPServer(('127.0.0.1',0),Upstream);cls.upstream.requests=[]
        cls.upthread=threading.Thread(target=cls.upstream.serve_forever,daemon=True);cls.upthread.start()
        cls.cfg=Config(image_inputs=True,upstream_base='http://127.0.0.1:'+str(cls.upstream.server_address[1]),upstream_key='',connect_retries=1,timeout=3,loop_retry=False)
        cls.proxy=Server(('127.0.0.1',0),Handler,cfg=cls.cfg);cls.thread=threading.Thread(target=cls.proxy.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        for server,thread in [(cls.proxy,cls.thread),(cls.upstream,cls.upthread)]:server.shutdown();server.server_close();thread.join(3)

    def post(self,value,path):
        conn=http.client.HTTPConnection('127.0.0.1',self.proxy.server_address[1],timeout=5)
        try:
            conn.request('POST',path,json.dumps(value),{'Content-Type':'application/json'});response=conn.getresponse();return response.status,response.read()
        finally:conn.close()

    def test_http_protocols_and_streams_preserve_images_and_emulated_calls(self):
        for protocol in ('openai','anthropic'):
            for stream in (False,True):
                for structured in (False,True):
                    with self.subTest(protocol=protocol,stream=stream,structured=structured):
                        self.cfg.json_output=structured
                        value=body([ANTHROPIC_IMAGE if protocol=='anthropic' else IMAGE]);value['stream']=stream
                        schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok']}
                        if protocol=='anthropic':value['tools']=[{'name':'describe','input_schema':schema}];path='/v1/messages'
                        else:value['tools']=[{'type':'function','function':{'name':'describe','parameters':schema}}];path='/v1/chat/completions'
                        status,raw=self.post(value,path)
                        self.assertEqual(status,200,raw);self.assertIn(b'describe',raw)
                        sent=self.upstream.requests[-1]
                        self.assertEqual(images(sent)[0]['image_url']['url'],URL)
                        self.assertNotIn('tools',sent);self.assertNotIn('functions',sent)

    def test_http_invalid_image_is_400_without_upstream_request(self):
        before=len(self.upstream.requests)
        status,_=self.post(body([{'type':'image_url','image_url':{'url':'file:///private'}}]),'/v1/chat/completions')
        self.assertEqual(status,400);self.assertEqual(len(self.upstream.requests),before)


if __name__=='__main__':unittest.main()
