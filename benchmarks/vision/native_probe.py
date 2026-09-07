#!/usr/bin/env python3
"""Paid, native-client-only image check: two requests and $0.01 peak bound.

Uses the same unchanged images and answers as the direct/proxy probes. Captures
are private. A successful read/transport is not a successful OCR check. The
meter buffers upstream responses, so this does not measure SSE latency.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import subprocess
import sys
import threading
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from emutools import Config,Server,Handler
from benchmarks.vision import proxy_probe as bridge
from benchmarks.vision.client_trace import inspect_cli_trace,score_observations
from benchmarks.vision.probe import make_image,decode_answer,MODEL


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli',type=Path,required=True);parser.add_argument('--out-dir',type=Path,required=True)
    args=parser.parse_args();out=args.out_dir.resolve();cli=args.cli.resolve()
    if out.exists():raise SystemExit('Choose a new output directory; never repeat a paid run in place.')
    if not cli.is_file():raise SystemExit('Claude Code binary missing')
    key=os.environ.get('EMU_UPSTREAM_API_KEY') or os.environ.get('DEEPSEEK_API_KEY')
    if not key:raise SystemExit('Set the upstream credential privately')
    out.mkdir(parents=True,mode=0o700);images=out/'images';images.mkdir();home=out/'home';home.mkdir();hashes={}
    for ident,expected in bridge.EXPECTED.items():
        blob=make_image(expected['code'],expected['blue_circles'],expected['triangle_color'])
        (images/('sample-'+ident+'.png')).write_bytes(blob);hashes[ident]=hashlib.sha256(blob).hexdigest()
    # This standalone benchmark process owns exactly one meter. No production
    # proxy settings or files are changed by these tighter benchmark limits.
    bridge.LIMIT=.01;bridge.MAX_REQUESTS=2
    meter=bridge.Meter(key,out)
    cfg=Config(image_inputs=True,json_output=False,model_big=MODEL,model_small=MODEL,
               upstream_base='http://127.0.0.1:'+str(meter.server.server_address[1]),upstream_key='',
               thinking='disabled',parallel=True,max_calls_per_turn=2,loop_retry=False,connect_retries=1,timeout=60)
    proxy=Server(('127.0.0.1',0),Handler,cfg=cfg);thread=threading.Thread(target=proxy.serve_forever,daemon=True);thread.start()
    mcp=out/'mcp.json';mcp.write_text('{"mcpServers":{}}')
    env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(home),'LANG':'C.UTF-8',
         'ANTHROPIC_BASE_URL':'http://127.0.0.1:'+str(proxy.server_address[1]),'ANTHROPIC_API_KEY':'dummy','ANTHROPIC_AUTH_TOKEN':'dummy',
         'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'800','MAX_THINKING_TOKENS':'0'}
    prompt=('Use Read to view BOTH '+str(images/'sample-a.png')+' and '+str(images/'sample-b.png')+'. These reads are independent: emit both Read calls in one assistant response. '
            'Then return only a JSON object with keys a and b, each containing code (header text), blue_circles (integer count), and triangle_color (lowercase). Inspect the pixels; do not guess.')
    command=[str(cli),'--bare','--restricted','--print','--model','claude-sonnet-4-5','--output-format','stream-json','--verbose','--no-session-persistence',
             '--permission-mode','dontAsk','--strict-mcp-config','--mcp-config',str(mcp),'--tools','Read','--allowedTools','Read','--max-turns','6','--max-budget-usd','1.00',
             '--system-prompt','Use the real Read tool to inspect the supplied images. Image tool results are supported by this proxy. Work only in the current directory. Keep output concise.',prompt]
    result={'model':MODEL,'fixture_sha256':hashes,'http_fixture_cases_run':False,'capture_buffers_upstream_responses':True};started=time.monotonic();client=None
    try:
        result['client_version']=subprocess.check_output([str(cli),'--version'],text=True,timeout=10).strip()
        with (out/'client.jsonl').open('w') as stdout,(out/'client.stderr').open('w') as stderr:
            client=subprocess.Popen(command,cwd=images,env=env,stdout=stdout,stderr=stderr);(out/'client.pid').write_text(str(client.pid))
            try:result['client_exit_code']=client.wait(timeout=150)
            except subprocess.TimeoutExpired:client.kill();result['client_exit_code']=client.wait(timeout=10);result['timed_out']=True
        trace_text=(out/'client.jsonl').read_text();trace=inspect_cli_trace(trace_text)
        try:answer=decode_answer(trace['final'])
        except (ValueError,TypeError,AttributeError):answer=None
        calls=[{'name':'record_scene','arguments':dict(value,fixture=ident)} for ident,value in answer.items() if isinstance(value,dict)] if isinstance(answer,dict) else []
        observations=score_observations(calls,bridge.EXPECTED);complete=set(observations)=={'a','b'} and all(x['schema_valid'] for x in observations.values())
        audit=bridge.audit_native_history(trace_text,meter.records,out,hashes);forwarded={h for r in meter.records for h in r['image_sha256']}
        read_ok=result['client_exit_code']==0 and {'sample-a.png','sample-b.png'}<=set(trace['read_paths']) and set(hashes.values())<=forwarded and complete
        result.update(actual_read_paths=trace['read_paths'],max_calls_in_one_response=trace['max_calls_in_one_response'],observations=observations,history_audit=audit,
                      read_workflow_pass=read_ok,batching_pass=trace['max_calls_in_one_response']>=2,
                      shape_answers_pass=complete and all(x['shapes_and_colors_correct'] for x in observations.values()),
                      exact_answers_pass=complete and all(x['exact'] for x in observations.values()))
        result['transport_and_workflow_pass']=read_ok and result['batching_pass'] and audit['both_reads_correlated']
        result['passed']=result['transport_and_workflow_pass'] and result['exact_answers_pass']
    except Exception as exc:
        result.update(passed=False,error={'type':type(exc).__name__,'message':str(exc)[:500]})
    finally:
        if client and client.poll() is None:client.kill();client.wait(timeout=10)
        proxy.shutdown();proxy.server_close();thread.join(3)
        deadline=time.monotonic()+30
        while meter.inflight and time.monotonic()<deadline:time.sleep(.1)
        result['budget']=meter.summary();meter.close();result['elapsed_seconds']=round(time.monotonic()-started,2)
        result['scope']='Actual Claude image reads through emutools. No synthetic tool history, no direct-provider bypass, and no native upstream tool declarations. Unknown usage remains fully reserved.'
        (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
    return 0 if result.get('passed') else 1


if __name__=='__main__':raise SystemExit(main())
