"""Test-only upstream meter with conservative V4 Pro peak rates.

Paid runs reserve worst-case output plus an input byte-count upper estimate.
Verified cache hits use the peak cache-hit rate; unknown/inconsistent cache
metrics get no discount. Missing usage is charged at the reservation. Private diagnostics omit headers
and reasoning traces; never publish raw request/response files automatically.
"""
import http.server
import json
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def usage_upper_bound(usage,reservation):
    """Peak prices: input miss $1.32/M, verified hit $0.044/M, output $3.96/M."""
    if not isinstance(usage,dict):return reservation
    prompt=usage.get('prompt_tokens');output=usage.get('completion_tokens')
    if any(type(n) is not int or n<0 for n in (prompt,output)):return reservation
    hit=usage.get('prompt_cache_hit_tokens',0)
    if type(hit) is not int or not 0<=hit<=prompt:hit=0
    miss=usage.get('prompt_cache_miss_tokens')
    if miss is not None and (type(miss) is not int or miss<0 or miss+hit!=prompt):hit=0
    return ((prompt-hit)*1.32+hit*.044+output*3.96)/1000000


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
                        rejection={'reason':'request_cap' if outer.calls>=outer.max_calls else 'spend_reservation',
                                   'accepted_requests':outer.calls,'spent_usd':outer.spent,'in_flight_usd':outer.reserved,
                                   'reservation_usd':estimate,'limit_usd':outer.limit,'time':time.time()}
                        with (outer.log.parent/'guard-rejections.jsonl').open('a') as out:out.write(json.dumps(rejection)+'\n')
                        self.error(402,'Conservative challenge spend/request limit reached');return
                    outer.calls+=1;index=outer.calls;outer.reserved+=estimate
                (outer.log.parent/('request-%02d.json'%index)).write_bytes(raw)
                started=time.monotonic();usage=None;status=0;stream=bool(body.get('stream'));buffer=b'';connected=True
                content=[];content_chars=0;native=[];delta_keys=set();finish_reasons=[]
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
                                            for choice in event.get('choices',[]):
                                                delta=choice.get('delta',{});delta_keys.update(delta)
                                                if choice.get('finish_reason'):finish_reasons.append(choice['finish_reason'])
                                                if isinstance(delta.get('content'),str) and content_chars<64000:
                                                    content.append(delta['content']);content_chars+=len(delta['content'])
                                                if delta.get('tool_calls') and len(native)<64:native.append(delta['tool_calls'])
                                        except (ValueError,UnicodeError):pass
                        if not stream:
                            try:
                                packet=json.loads(buffer)
                                if isinstance(packet,dict):
                                    usage=packet.get('usage')
                                    for choice in packet.get('choices',[]):
                                        if not isinstance(choice,dict):continue
                                        message=choice.get('message')
                                        if not isinstance(message,dict):continue
                                        delta_keys.update(message)
                                        if choice.get('finish_reason'):finish_reasons.append(choice['finish_reason'])
                                        if isinstance(message.get('content'),str):
                                            piece=message['content'][:max(0,64000-content_chars)]
                                            content.append(piece);content_chars+=len(piece)
                                        if message.get('tool_calls') and len(native)<64:native.append(message['tool_calls'])
                            except (ValueError,UnicodeError):pass
                except Exception:
                    if not status:
                        try:self.error(502,'Test upstream connection failed')
                        except OSError:pass
                finally:
                    charge=usage_upper_bound(usage,estimate)
                    diagnostic={'stream':stream,'content':''.join(content),'native_fragments':native,'delta_keys':sorted(delta_keys),'finish_reasons':finish_reasons}
                    (outer.log.parent/('response-%02d.json'%index)).write_text(json.dumps(diagnostic,ensure_ascii=False))
                    event={'request':index,'status':status,'stream':stream,'input_bytes':len(raw),'max_output_tokens':maximum,
                           'native_tools_sent':False,'thinking':body.get('thinking'),'usage':usage,
                           'response_chars':content_chars,'delta_keys':sorted(delta_keys),'finish_reasons':finish_reasons,
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
