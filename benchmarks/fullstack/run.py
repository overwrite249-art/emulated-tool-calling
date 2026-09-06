#!/usr/bin/env python3
"""Paid, bounded Claude Code -> emutools -> V4 Pro full-stack challenge.

Creates an empty app workspace, seeded DB, read-only MCP tools and an independent
verifier. The agent—not this runner—writes all application code. Linux/POSIX only.
An optional --resume-app copies earlier agent-authored source, with provenance.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from budget import BudgetBridge
from seed import seed
from source import copy_source
from verify import verify


HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]


def stop(proc):
    if proc and proc.poll() is None:
        os.killpg(proc.pid,signal.SIGTERM)
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid,signal.SIGKILL);proc.wait(timeout=5)


def cleanup_app(app):
    count=0
    for entry in Path('/proc').glob('[0-9]*'):
        try:
            if (entry/'cwd').resolve()==app and int(entry.name)!=os.getpid():
                os.kill(int(entry.name),signal.SIGTERM);count+=1
        except (OSError,RuntimeError):pass
    return count


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def analyze(work):
    groups={};results=[];tool_names=[];tool_results=0
    transcript=work/'client.jsonl'
    if transcript.exists():
        for line in transcript.read_text(errors='replace').splitlines():
            try:r=json.loads(line)
            except ValueError:continue
            if r.get('type')=='assistant':
                m=r.get('message',{});calls=groups.setdefault(m.get('id','unknown'),{})
                for b in m.get('content',[]):
                    if b.get('type')=='tool_use':calls[b.get('id')]=b.get('name');tool_names.append(b.get('name'))
            if r.get('type')=='user':tool_results+=sum(b.get('type')=='tool_result' for b in r.get('message',{}).get('content',[]))
            if r.get('type')=='result':results.append(r)
    turns=[{'turn':i+1,'tools':list(c.values()),'call_count':len(c)} for i,c in enumerate(groups.values()) if c]
    eventfile=work/'mcp-events.jsonl'
    events=[json.loads(x) for x in eventfile.read_text().splitlines()] if eventfile.exists() else []
    ends=[e for e in events if e['event']=='end']
    overlap=any(a['tool']!=b['tool'] and a['started']<b['time'] and b['started']<a['time'] for i,a in enumerate(ends) for b in ends[i+1:])
    summary={'tool_turns':turns,'tool_calls':sum(t['call_count'] for t in turns),'tool_results':tool_results,
             'max_calls_in_one_response':max([t['call_count'] for t in turns] or [0]),
             'mcp_calls':len(ends),'mcp_errors':sum(e['is_error'] for e in ends),'mcp_overlap_observed':overlap,
             'client_claimed_done':bool(results and not results[-1].get('is_error') and 'FULLSTACK_DONE' in results[-1].get('result',''))}
    if results:summary['client_usage']=results[-1].get('usage',{})
    return summary


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--cli',required=True);ap.add_argument('--out-dir',required=True)
    ap.add_argument('--timeout',type=int,default=600);ap.add_argument('--budget-usd',type=float,default=.40)
    ap.add_argument('--resume-app',help='Prior agent-authored app; copies source only, never its database or built assets')
    ap.add_argument('--thinking',choices=('enabled','disabled'),help='Explicit upstream thinking mode; omitted keeps the provider default')
    ap.add_argument('--max-output-tokens',type=int,default=6000,help='Per-response output allowance, 1..6000')
    ap.add_argument('--focus',default='',help='Additional reviewer feedback for a focused continuation')
    ap.add_argument('--json-output',action='store_true',help='Use opt-in provider JSON response mode')
    a=ap.parse_args();key=os.environ.get('EMU_UPSTREAM_API_KEY') or os.environ.get('DEEPSEEK_API_KEY')
    if not 1<=a.max_output_tokens<=6000:ap.error('--max-output-tokens must be between 1 and 6000')
    if not key:ap.error('set EMU_UPSTREAM_API_KEY; this test makes paid requests')
    work=Path(a.out_dir).resolve();work.mkdir(parents=True,exist_ok=False);app=work/'app';app.mkdir();home=work/'home';home.mkdir()
    seed(app/'data/inventory.sqlite');resumed=copy_source(a.resume_app,app) if a.resume_app else []
    spec=(HERE/'SPEC.md').read_text()
    spec+='\n\nYour current working directory is '+str(app)+'. All file tools must use paths inside that directory.\n'
    if resumed:
        spec+='\n\n## Continuation for this run\nUnfinished source from an earlier real model run is already present. Inspect it and finish it rather than starting over unnecessarily. The empty-workspace description does not apply to this continuation. All other requirements still apply. Use the current working directory, not paths from an earlier run.\n'
    if a.focus:spec+='\n\n## Reviewer feedback\n'+a.focus+'\n'
    (app/'REQUIREMENTS.md').write_text(spec)
    protected={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in list(HERE.iterdir())+list((REPO/'emutools').glob('*.py')) if p.is_file()}
    env={k:v for k,v in os.environ.items() if k in ('PATH','LANG','LC_ALL','LD_LIBRARY_PATH','TMPDIR','SSL_CERT_FILE','SSL_CERT_DIR')}
    env.update(HOME=str(home),PWD=str(app),XDG_CONFIG_HOME=str(home/'.config'),XDG_DATA_HOME=str(home/'.local/share'),XDG_CACHE_HOME=str(home/'.cache'),DISABLE_TELEMETRY='1',DO_NOT_TRACK='1')
    meter=BudgetBridge(key,work/'upstream.jsonl',limit=a.budget_usd)
    upstream=meter.start();proxy_port=port();app_port=port();base='http://127.0.0.1:%d'%proxy_port
    proxy_env=dict(env,EMU_HOST='127.0.0.1',EMU_PORT=str(proxy_port),EMU_UPSTREAM_BASE_URL=upstream,
                   EMU_UPSTREAM_API_KEY=meter.token,EMU_MODEL_BIG='deepseek-v4-pro',EMU_MODEL_SMALL='deepseek-v4-pro',
                   EMU_PARALLEL='true',EMU_MAX_CALLS_PER_TURN='4',EMU_MAX_TOOL_ROUNDS='25',EMU_USE_STOP='false',
                   EMU_MAX_RETRIES='1',EMU_TIMEOUT='180',EMU_LOG_BODIES='false')
    proxy_env['EMU_JSON_OUTPUT']='true' if a.json_output else 'false'
    if a.thinking:proxy_env['EMU_THINKING']=a.thinking
    client_env=dict(env,ANTHROPIC_BASE_URL=base,ANTHROPIC_API_KEY='dummy',ANTHROPIC_AUTH_TOKEN='dummy',
                    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CLAUDE_CODE_MAX_OUTPUT_TOKENS=str(a.max_output_tokens),MAX_THINKING_TOKENS='0')
    mcp={'mcpServers':{'warehouse':{'command':sys.executable,'args':[str(HERE/'db_mcp.py'),str(app/'data/inventory.sqlite'),str(work/'mcp-events.jsonl')]}}}
    (work/'mcp.json').write_text(json.dumps(mcp))
    command=[a.cli,'--bare','--restricted','--print','--model','claude-sonnet-4-5','--output-format','stream-json','--verbose',
             '--no-session-persistence','--permission-mode','dontAsk','--strict-mcp-config','--mcp-config',str(work/'mcp.json'),
             '--tools','Read,Write,Edit,Bash','--allowedTools','Read,Write,Edit,Bash,mcp__warehouse__db_schema,mcp__warehouse__db_profile,mcp__warehouse__db_query',
             '--max-turns','30','--max-budget-usd','2.00','--system-prompt',
             'You are performing a real coding integration test. Implement the app, use the available tools, and verify actual outcomes. Work only in the provided app workspace. Never edit evaluator files or emutools. Keep output concise. Use independent tool calls in batches when safe. Do not install packages or access external services. Do not claim success without passing commands.',
             spec+'\nUse port '+str(app_port)+' for your server. To run the independent verifier: python3 '+str(HERE/'verify.py')+' --app '+str(app)+' --out-dir '+str(app/'acceptance-1')+'. Use a NEW output directory (acceptance-2 etc.) on each repeat. Fix failures before finishing.']
    proxy=client=None;start=time.monotonic();result={'model':'deepseek-v4-pro','parallel_enabled':True,'max_calls_per_turn':4,'resumed_source_files':resumed,'thinking_mode':a.thinking or 'provider_default','output_token_limit':a.max_output_tokens,'reviewer_feedback_supplied':bool(a.focus),'json_output':a.json_output}
    try:
        with (work/'proxy.log').open('w') as log:
            proxy=subprocess.Popen([sys.executable,'-m','emutools'],cwd=REPO,env=proxy_env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            for _ in range(100):
                try:
                    with urllib.request.urlopen(base+'/health',timeout=1) as response:json.load(response)
                    break
                except Exception:
                    if proxy.poll() is not None:raise RuntimeError('Proxy startup failed')
                    time.sleep(.1)
            else:raise RuntimeError('Proxy startup timed out')
            result['client_version']=subprocess.run([a.cli,'--version'],env=client_env,capture_output=True,text=True,timeout=15).stdout.strip()
            with (work/'client.jsonl').open('w') as out,(work/'client.stderr').open('w') as err:
                client=subprocess.Popen(command,cwd=app,env=client_env,stdout=out,stderr=err,start_new_session=True)
                try:result['client_exit_code']=client.wait(timeout=a.timeout)
                except subprocess.TimeoutExpired:
                    result['timed_out']=True;stop(client);result['client_exit_code']=client.returncode
        result['protected_files_unchanged']=all(Path(p).exists() and hashlib.sha256(Path(p).read_bytes()).hexdigest()==sha for p,sha in protected.items())
        result.update(analyze(work))
        result['stopped_agent_app_processes']=cleanup_app(app)
        if result['protected_files_unchanged']:
            independent=verify(app,work/'independent',variant=73)
            result['independent_checks']={k:v for k,v in independent.items() if k!='checks'}
        else:result['independent_checks']={'all_passed':False,'error':'Evaluator modified'}
        result['source_files']=[{'path':str(p.relative_to(app)),'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in app.rglob('*') if p.is_file() and p.suffix in ('.py','.ts','.html','.css','.md') and not any(x.startswith('acceptance-') or x=='__pycache__' for x in p.relative_to(app).parts)]
        result['passed']=bool(result.get('client_exit_code')==0 and result['protected_files_unchanged'] and result['client_claimed_done'] and result['max_calls_in_one_response']>=2 and result['mcp_calls']>=3 and result['independent_checks']['all_passed'])
    except Exception as exc:
        result['error']=str(exc);result['passed']=False
    finally:
        stop(client);stop(proxy);cleanup_app(app);meter.stop()
        result['budget']=meter.summary();result['elapsed_seconds']=round(time.monotonic()-start,2)
        (work/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result.get('passed') else 1


if __name__=='__main__':sys.exit(main())
