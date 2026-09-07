"""Private diagnostics must capture sync replies without storing reasoning."""
import http.server
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
from benchmarks.fullstack.budget import BudgetBridge


class MeterCaptureTests(unittest.TestCase):
    def test_nonstream_content_and_native_fields_are_observable(self):
        content='{"text":"Привіт </tool_call>","tool_calls":[]}'
        native=[{'id':'fixture','type':'function','function':{'name':'Read','arguments':'{}'}}]
        response={'choices':[{'message':{'role':'assistant','content':content,'reasoning_content':'PRIVATE_TRACE_MARKER','tool_calls':native},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':20}}
        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                body=json.dumps(response).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        provider=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider)
        thread=threading.Thread(target=provider.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root=Path(directory)
                bridge=BudgetBridge('test-only',root/'events.jsonl',upstream='http://127.0.0.1:%d'%provider.server_address[1])
                try:
                    url=bridge.start();body=json.dumps({'model':'deepseek-v4-pro','messages':[],'max_tokens':100,'stream':False}).encode()
                    request=urllib.request.Request(url,data=body,headers={'Authorization':'Bearer '+bridge.token,'Content-Type':'application/json'})
                    with urllib.request.urlopen(request,timeout=2) as reply:self.assertEqual(json.load(reply),response)
                    deadline=time.monotonic()+2
                    while not (root/'response-01.json').exists() and time.monotonic()<deadline:time.sleep(.01)
                    raw=(root/'response-01.json').read_text();diagnostic=json.loads(raw)
                    self.assertEqual(diagnostic['content'],content)
                    self.assertEqual(diagnostic['native_fragments'],[native])
                    self.assertEqual(diagnostic['finish_reasons'],['stop'])
                    self.assertNotIn('PRIVATE_TRACE_MARKER',raw)
                finally:bridge.stop()
        finally:provider.shutdown();provider.server_close();thread.join(timeout=2)

    def test_quota_rejection_has_an_auditable_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bridge=BudgetBridge('test-only',root/'events.jsonl',limit=0,upstream='http://127.0.0.1:9/unused')
            try:
                url=bridge.start();body=json.dumps({'model':'deepseek-v4-pro','messages':[],'max_tokens':1}).encode()
                request=urllib.request.Request(url,data=body,headers={'Authorization':'Bearer '+bridge.token})
                with self.assertRaises(urllib.error.HTTPError) as failure:urllib.request.urlopen(request,timeout=2)
                self.assertEqual(failure.exception.code,402)
                records=[json.loads(line) for line in (root/'guard-rejections.jsonl').read_text().splitlines()]
                self.assertEqual(records[-1]['reason'],'spend_reservation')
                self.assertGreater(records[-1]['reservation_usd'],records[-1]['limit_usd'])
                self.assertEqual(bridge.summary()['requests'],0)
            finally:bridge.stop()


if __name__=='__main__':unittest.main()
