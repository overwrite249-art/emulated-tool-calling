"""Transparent, test-only upstream meter with a conservative USD spend guard.

Uses DeepSeek V4 Pro PEAK cache-miss rates published 2026-09-05 ($1.32/M
input, $3.96/M output), even off-peak and on cache hits. Each request reserves
an input byte-count upper estimate plus its maximum output before forwarding.
Missing usage is charged at the full reservation, not treated as free.
"""
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


class BudgetBridge:
    def __init__(self,key,log,limit=.40,max_calls=28,upstream='https://api.deepseek.com/chat/completions'):
        self.key=key;self.log=Path(log);self.limit=limit;self.max_calls=max_calls;self.upstream=upstream
        self.token=secrets.token_hex(24);self.lock=threading.Lock();self.spent=0.;self.reserved=0.;self.calls=0;self.events=[]
        outer=self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args):pass
            def error(self,status,message):
                body=json.dumps({'error':{'message':message,'type':'challenge_guard'}}).encode()
                self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.send_header('Connection','close');self.end_headers();self.wfile.write(body);self.close_connection=True
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+outer.token:
                    self.error(401,'Unauthorized test bridge');return
                size=int(self.headers.get('Content-Length','0'))
                if size<1 or size>4000000:
                    self.error(413,'Challenge request too large');return
                raw=self.rfile.read(size)
                try:body=json.loads(raw)
                except ValueError:self.error(400,'Invalid JSON');return
                if body.get('model')!='deepseek-v4-pro' or 'tools' in body or 'functions' in body:
                    self.error(400,'Challenge requires emulated tools and deepseek-v4-pro');return
                maximum=body.get('max_tokens',4096)
                if not isinstance(maximum,int) or maximum<1 or maximum>6000:
                    self.error(400,'Challenge output limit is 6000 tokens');return
                estimate=((len(raw)+256*len(body.get('messages',[]))+4096)*1.32+maximum*3.96)/1000000
                with outer.lock:
                    if outer.calls>=outer.max_calls or outer.spent+outer.reserved+estimate>outer.limit:
                        self.error(402,'Conservative challenge spend/request limit reached');return
                    outer.calls+=1;index=outer.calls;outer.reserved+=estimate
                started=time.monotonic();usage=None;status=0;stream=bool(body.get('stream'));buffer=b'';connected=True
                try:
                    req=urllib.request.Request(outer.upstream,data=raw,headers={'Content-Type':'application/json','Authorization':'Bearer '+outer.key})
                    try:response=urllib.request.urlopen(req,timeout=180)
                    except urllib.error.HTTPError as exc:response=exc
                    with response:
                        status=response.status
                        self.send_response(status);self.send_header('Content-Type',response.headers.get('Content-Type','application/json'));self.send_header('Connection','close');self.end_headers();self.close_connection=True
                        while True:
                            chunk=response.read1(65536)
                            if not chunk:break
                            if connected:
                                try:self.wfile.write(chunk);self.wfile.flush()
                                except (OSError,ConnectionError):connected=False
                            buffer+=chunk
                            if stream:
                                while b'\n' in buffer:
                                    line,buffer=buffer.split(b'\n',1)
                                    if line.startswith(b'data:'):
                                        try:
                                            event=json.loads(line[5:].strip())
                                            if event.get('usage'):usage=event['usage']
                                        except (ValueError,UnicodeError):pass
                        if not stream:
                            try:usage=json.loads(buffer).get('usage')
                            except (ValueError,UnicodeError):pass
                except Exception:
                    if not status:
                        try:self.error(502,'Test upstream connection failed')
                        except OSError:pass
                finally:
                    if usage and isinstance(usage.get('prompt_tokens'),int) and isinstance(usage.get('completion_tokens'),int):
                        charge=(usage['prompt_tokens']*1.32+usage['completion_tokens']*3.96)/1000000
                    else:charge=estimate
                    event={'request':index,'status':status,'input_bytes':len(raw),'max_output_tokens':maximum,
                           'native_tools_sent':False,'thinking':body.get('thinking'),'usage':usage,
                           'upper_bound_usd':round(charge,8),'elapsed_seconds':round(time.monotonic()-started,3)}
                    with outer.lock:
                        outer.reserved-=estimate;outer.spent+=charge;outer.events.append(event)
                        with outer.log.open('a') as out:out.write(json.dumps(event)+'\n')
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
    def start(self):
        self.thread.start();return 'http://127.0.0.1:%d'%self.server.server_address[1]
    def stop(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=5)
    def summary(self):
        with self.lock:return {'requests':self.calls,'upper_bound_usd':round(self.spent+self.reserved,6),'limit_usd':self.limit,'usage_observed_for':sum(e['usage'] is not None for e in self.events)}
