#!/usr/bin/env python3
"""Live vision through emutools, with optional actual Claude Code image reads.

Synthetic images only. No native upstream tools. At most eight provider
requests and $0.02 peak-tariff bound. The capture bridge buffers responses for
metering: this is not a first-token latency benchmark. Raw client data is private.
"""
from pathlib import Path
import argparse
import base64
import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from emutools import Config,Handler,Server,iter_sse
from benchmarks.vision.probe import MODEL,make_image,decode_answer

LIMIT=.02
MAX_REQUESTS=8
MAX_OUTPUT=800
INPUT_RATE=.44/1_000_000
OUTPUT_RATE=1.32/1_000_000
EXPECTED={'a':{'code':'D7K4','blue_circles':3,'triangle_color':'green'},
          'b':{'code':'R2M9','blue_circles':1,'triangle_color':'red'}}
SCHEMA={'type':'object','properties':{'fixture':{'type':'string','enum':['a','b']},
        'code':{'type':'string'},'blue_circles':{'type':'integer','minimum':0},
        'triangle_color':{'type':'string'}},'required':['fixture','code','blue_circles','triangle_color'],
        'additionalProperties':False}


def payload_stats(payload):
    text_bytes=0;hashes=[]
    for message in payload.get('messages',[]):
        content=message.get('content','')
        if isinstance(content,str):text_bytes+=len(content.encode());continue
        if not isinstance(content,list):raise ValueError('unexpected upstream content shape')
        for part in content:
            if part.get('type')=='text':text_bytes+=len(part.get('text','').encode())
            elif part.get('type')=='image_url':
                url=part['image_url']['url']
                if not url.startswith('data:image/'):raise ValueError('benchmark accepts inline images only')
                blob=base64.b64decode(url.split(',',1)[1],validate=True)
                hashes.append(hashlib.sha256(blob).hexdigest())
            else:raise ValueError('unexpected upstream content part')
    return text_bytes,hashes


class Meter:
    def __init__(self,key,out):
        self.key=key;self.out=out;self.lock=threading.Lock();self.spent=0.;self.inflight=0.;self.records=[];self.rejections=[]
        owner=self
        class Bridge(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def reply(self,status,raw,kind='application/json'):
                try:
                    self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError):pass
            def reject(self,reason,status=402):
                with owner.lock:owner.rejections.append(reason)
                self.reply(status,json.dumps({'error':{'message':'Local vision guard: '+reason}}).encode())
            def do_POST(self):
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=200000:return self.reject('request_size',413)
                raw=self.rfile.read(length)
                try:
                    payload=json.loads(raw);size,hashes=payload_stats(payload);maximum=payload.get('max_tokens')
                    if payload.get('model')!=MODEL or 'tools' in payload or 'functions' in payload:raise ValueError('wrong model or native upstream tools')
                    if isinstance(maximum,bool) or not isinstance(maximum,int) or not 1<=maximum<=MAX_OUTPUT:raise ValueError('output allowance')
                    if len(hashes)>4:raise ValueError('image count')
                except (ValueError,TypeError,KeyError):return self.reject('invalid_payload',400)
                reservation=(size+8192+384*len(hashes))*INPUT_RATE+maximum*OUTPUT_RATE
                with owner.lock:
                    if len(owner.records)>=MAX_REQUESTS:reason='request_cap'
                    elif owner.spent+owner.inflight+reservation>LIMIT:reason='spend_reservation'
                    else:
                        reason='';owner.inflight+=reservation
                        record={'number':len(owner.records)+1,'image_sha256':hashes,'stream':bool(payload.get('stream')),'reservation_usd':reservation}
                        owner.records.append(record)
                if reason:return self.reject(reason)
                n=record['number'];(out/('request-%02d.json'%n)).write_bytes(raw)
                status=502;response_raw=b'';kind='application/json';usage={};cost=reservation
                try:
                    request=urllib.request.Request('https://api.deepseek.com/chat/completions',data=raw,headers={'Content-Type':'application/json','Authorization':'Bearer '+owner.key})
                    with urllib.request.urlopen(request,timeout=45) as response:
                        status=response.status;kind=response.headers.get('Content-Type','application/json');response_raw=response.read(1000000)
                    if payload.get('stream'):
                        for event in iter_sse(io.BytesIO(response_raw)):
                            if isinstance(event.get('usage'),dict):usage=event['usage']
                    else:usage=json.loads(response_raw).get('usage',{})
                    counts=[usage.get('prompt_tokens'),usage.get('completion_tokens')]
                    if all(isinstance(x,int) and not isinstance(x,bool) and x>=0 for x in counts):cost=counts[0]*INPUT_RATE+counts[1]*OUTPUT_RATE
                except urllib.error.HTTPError as exc:
                    status=exc.code;response_raw=exc.read(20000)
                except Exception as exc:
                    response_raw=json.dumps({'error':{'message':'capture transport failure','type':type(exc).__name__}}).encode()
                finally:
                    (out/('response-%02d.bin'%n)).write_bytes(response_raw)
                    with owner.lock:
                        owner.inflight-=reservation;owner.spent+=cost
                        record.update(status=status,usage=usage,peak_tariff_bound_usd=cost)
                    self.reply(status,response_raw,kind)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Bridge);self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def summary(self):
        with self.lock:return {'requests':len(self.records),'peak_tariff_bound_usd':self.spent+self.inflight,'unsettled_reservations_usd':self.inflight,'limit_usd':LIMIT,'records':list(self.records),'guard_rejections':list(self.rejections)}

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(3)


def post(port,path,body):
    conn=http.client.HTTPConnection('127.0.0.1',port,timeout=65)
    try:
        conn.request('POST',path,json.dumps(body),{'Content-Type':'application/json'});response=conn.getresponse();return response.status,response.read()
    finally:conn.close()


def anthropic_calls(raw):
    calls={};arguments={}
    for event in iter_sse(io.BytesIO(raw)):
        index=event.get('index')
        if event.get('type')=='content_block_start' and event.get('content_block',{}).get('type')=='tool_use':
            calls[index]={'name':event['content_block']['name'],'arguments':event['content_block'].get('input',{})};arguments[index]=''
        elif event.get('type')=='content_block_delta' and event.get('delta',{}).get('type')=='input_json_delta':arguments[index]=arguments.get(index,'')+event['delta']['partial_json']
    for index,text in arguments.items():
        if text:calls[index]['arguments']=json.loads(text)
    return list(calls.values())


def score(calls):
    result={}
    for call in calls:
        args=call.get('arguments',{});fixture=args.get('fixture')
        if call.get('name')=='record_scene' and fixture in EXPECTED:
            result[fixture]={'observed':{k:args.get(k) for k in EXPECTED[fixture]},'exact':all(args.get(k)==v for k,v in EXPECTED[fixture].items()),
                             'shapes_and_colors_correct':all(args.get(k)==EXPECTED[fixture][k] for k in ['blue_circles','triangle_color'])}
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--out-dir',type=Path,required=True);parser.add_argument('--cli',type=Path)
    args=parser.parse_args();out=args.out_dir.resolve()
    if out.exists():raise SystemExit('Use a new output directory; do not duplicate a paid run.')
    if args.cli and not args.cli.is_file():raise SystemExit('Client binary missing')
    key=os.environ.get('EMU_UPSTREAM_API_KEY') or os.environ.get('DEEPSEEK_API_KEY')
    if not key:raise SystemExit('Set the upstream key privately in the environment')
    out.mkdir(parents=True,mode=0o700);work=out/'images';work.mkdir();urls={};hashes={}
    for ident,data in EXPECTED.items():
        image=make_image(data['code'],data['blue_circles'],data['triangle_color']);(work/('sample-'+ident+'.png')).write_bytes(image)
        urls[ident]='data:image/png;base64,'+base64.b64encode(image).decode();hashes[ident]=hashlib.sha256(image).hexdigest()
    meter=Meter(key,out);cfg=Config(image_inputs=True,model_big=MODEL,model_small=MODEL,upstream_base='http://127.0.0.1:'+str(meter.server.server_address[1]),upstream_key='',thinking='disabled',parallel=True,max_calls_per_turn=2,loop_retry=False,connect_retries=1,timeout=60)
    proxy=Server(('127.0.0.1',0),Handler,cfg=cfg);thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start();port=proxy.server_address[1]
    result={'model':MODEL,'fixture_sha256':hashes,'http_cases':[],'native_client':None,'capture_buffers_upstream_responses':True}
    try:
        content=[{'type':'text','text':'Inspect both images. Call record_scene twice in ONE response, once per fixture. Read each code and count blue circles and identify the triangle color. Use only what you see.'}]
        for ident in ['a','b']:content += [{'type':'text','text':'Fixture '+ident},{'type':'image_url','image_url':{'url':urls[ident]}}]
        body={'model':MODEL,'max_tokens':600,'messages':[{'role':'user','content':content}],
              'tools':[{'type':'function','function':{'name':'record_scene','parameters':SCHEMA}}],'parallel_tool_calls':True}
        before=len(meter.records);status,raw=post(port,'/v1/chat/completions',body);calls=[]
        if status==200:
            for call in json.loads(raw)['choices'][0]['message'].get('tool_calls',[]):calls.append({'name':call['function']['name'],'arguments':json.loads(call['function']['arguments'])})
        sent=meter.records[before:];result['http_cases'].append({'protocol':'openai','stream':False,'status':status,'image_bytes_preserved':bool(sent) and sent[0]['image_sha256']==[hashes['a'],hashes['b']],'calls_in_one_response':len(calls),'observations':score(calls)})
        print(json.dumps(result['http_cases'][-1]),flush=True)
        body={'model':'claude-sonnet-4-5','max_tokens':600,'stream':True,'tools':[{'name':'record_scene','input_schema':SCHEMA}],'tool_choice':{'type':'any'},'messages':[
              {'role':'assistant','content':[{'type':'tool_use','id':'fixture-read','name':'Read','input':{'file_path':'sample-b.png'}}]},
              {'role':'user','content':[{'type':'tool_result','tool_use_id':'fixture-read','content':[{'type':'image','source':{'type':'base64','media_type':'image/png','data':urls['b'].split(',',1)[1]}}]},
               {'type':'text','text':'This is fixture b. Call record_scene with its visible code, blue circle count and triangle color.'}]}]}
        before=len(meter.records);status,raw=post(port,'/v1/messages',body);calls=anthropic_calls(raw) if status==200 else [];sent=meter.records[before:]
        result['http_cases'].append({'protocol':'anthropic','stream':True,'synthetic_tool_history':True,'status':status,'image_bytes_preserved':bool(sent) and sent[0]['image_sha256']==[hashes['b']],'calls_in_one_response':len(calls),'observations':score(calls)})
        print(json.dumps(result['http_cases'][-1]),flush=True)
        if args.cli:
            home=out/'home';home.mkdir();mcp=out/'mcp.json';mcp.write_text('{"mcpServers":{}}')
            env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(home),'LANG':'C.UTF-8','ANTHROPIC_BASE_URL':'http://127.0.0.1:'+str(port),'ANTHROPIC_AUTH_TOKEN':'dummy','ANTHROPIC_API_KEY':'dummy',
                 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'800','MAX_THINKING_TOKENS':'0'}
            prompt=('Use Read to view BOTH '+str(work/'sample-a.png')+' and '+str(work/'sample-b.png')+'. These reads are independent: emit both Read calls in one assistant response. '
                    'Then return only a JSON object with keys a and b, each containing code (header text), blue_circles (integer count), and triangle_color (lowercase). Inspect the pixels; do not guess.')
            command=[str(args.cli.resolve()),'--bare','--restricted','--print','--model','claude-sonnet-4-5','--output-format','stream-json','--verbose','--no-session-persistence','--permission-mode','dontAsk',
                     '--strict-mcp-config','--mcp-config',str(mcp),'--tools','Read','--allowedTools','Read','--max-turns','6','--max-budget-usd','1.00','--system-prompt','Use the real Read tool to inspect the supplied images. Image tool results are supported by this proxy. Work only in the current directory. Keep output concise.',prompt]
            before=len(meter.records);started=time.monotonic()
            with (out/'client.jsonl').open('w') as stdout,(out/'client.stderr').open('w') as stderr:
                client=subprocess.Popen(command,cwd=work,env=env,stdout=stdout,stderr=stderr);(out/'client.pid').write_text(str(client.pid))
                try:code=client.wait(timeout=150)
                except subprocess.TimeoutExpired:client.kill();code=client.wait(timeout=10)
            reads=[];maximum=0;final=''
            for line in (out/'client.jsonl').read_text().splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('type')=='assistant':
                    blocks=event.get('message',{}).get('content',[]);tools=[x for x in blocks if x.get('type')=='tool_use'];maximum=max(maximum,len(tools))
                    reads.extend(Path(x.get('input',{}).get('file_path','')).name for x in tools if x.get('name')=='Read')
                elif event.get('type')=='result':final=event.get('result','')
            try:answer=decode_answer(final)
            except (ValueError,TypeError):answer=None
            observations=score([{'name':'record_scene','arguments':dict(value,fixture=ident)} for ident,value in answer.items() if isinstance(value,dict)]) if isinstance(answer,dict) else {}
            sent=meter.records[before:];forwarded={h for record in sent for h in record['image_sha256']}
            result['native_client']={'version':subprocess.check_output([str(args.cli),'--version'],text=True,timeout=10).strip(),'exit_code':code,'elapsed_seconds':round(time.monotonic()-started,2),
               'actual_read_paths':reads,'max_calls_in_one_response':maximum,'both_fixture_image_hashes_forwarded':set(hashes.values())<=forwarded,'observations':observations,
               'workflow_pass':code==0 and {'sample-a.png','sample-b.png'}<=set(reads) and set(hashes.values())<=forwarded and set(observations)=={'a','b'}}
            print(json.dumps(result['native_client']),flush=True)
    except Exception as exc:
        result['error']={'type':type(exc).__name__,'message':str(exc)[:500]}
    finally:
        proxy.shutdown();proxy.server_close();thread.join(3)
        deadline=time.monotonic()+50
        while meter.inflight and time.monotonic()<deadline:time.sleep(.1)
        result['budget']=meter.summary();meter.close()
        result['scope']='Real provider through emutools; the HTTP tool-result case uses synthetic history. Only native_client reports actual CLI tool execution. OCR exactness is separate from transport/workflow.'
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return 0 if not result.get('error') and all(c['image_bytes_preserved'] and c['status']==200 for c in result['http_cases']) and (not args.cli or result['native_client'] and result['native_client']['workflow_pass']) else 1


if __name__=='__main__':raise SystemExit(main())
