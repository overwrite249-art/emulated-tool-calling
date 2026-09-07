#!/usr/bin/env python3
"""Supplemental real-HTTP stock conservation and idempotency stress checks.

Uses only a new disposable fixture outside the app; never a user database.
Run build.py first, then this script with --app and a NEW --out-dir.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from seed import seed


def stress(app,out,iterations=50):
    app=Path(app).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    dbpath=seed(out/'fresh.sqlite',73);checks=[];server=None
    concurrent_requests=0;counter_lock=threading.Lock()
    def equal(actual,expected):
        if actual!=expected:raise AssertionError('%r != %r'%(actual,expected))
    def check(name,fn):
        try:fn();checks.append({'name':name,'passed':True})
        except Exception as exc:checks.append({'name':name,'passed':False,'error':str(exc)})
    def sql(statement,args=()):
        with sqlite3.connect(dbpath,timeout=10) as db:
            db.row_factory=sqlite3.Row
            return [dict(row) for row in db.execute(statement,args)]
    def state():return sql('SELECT warehouse_id,on_hand,reserved FROM stock WHERE product_id=2 ORDER BY warehouse_id')
    def movements():return sql('SELECT COUNT(*) AS n FROM stock_movements')[0]['n']
    def request(path,payload=None):
        data=None if payload is None else json.dumps(payload).encode()
        req=urllib.request.Request(base+path,data=data,headers={'Content-Type':'application/json'})
        try:response=urllib.request.urlopen(req,timeout=8)
        except urllib.error.HTTPError as exc:response=exc
        with response:
            content=response.read();kind=response.headers.get('Content-Type','')
            body=json.loads(content) if 'json' in kind else content.decode(errors='replace')
            return response.status,body,kind
    def pair(kind,index):
        sql('UPDATE stock SET on_hand=10,reserved=2 WHERE product_id=2 AND warehouse_id IN (1,2)')
        before=state();audit_before=movements();barrier=threading.Barrier(2)
        def send(member):
            nonlocal concurrent_requests
            key='%s-%d'%(kind,index)
            if kind=='overspend':key+='-%d'%member
            quantity=7 if kind=='overspend' else (3+member if kind=='conflicting_key' else 3)
            payload={'product_id':2,'from_warehouse_id':1,'to_warehouse_id':2,'quantity':quantity,'idempotency_key':key}
            barrier.wait(timeout=3)
            response=request('/api/transfers',payload)
            with counter_lock:concurrent_requests+=1
            return response
        with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(send,range(2)))
        # A successful response must reflect committed state immediately.
        after=state();expected_codes=[200,200] if kind=='same_key' else [200,409]
        equal(sorted(r[0] for r in responses),expected_codes)
        equal(sum(r['on_hand'] for r in before),sum(r['on_hand'] for r in after))
        equal([r['reserved'] for r in before],[r['reserved'] for r in after])
        equal(movements(),audit_before+2)
        for row in after:
            if row['on_hand']<row['reserved']:raise AssertionError('reserved inventory was consumed')
        if kind=='same_key':equal(responses[0][1],responses[1][1])
        winner=next(r[1] for r in responses if r[0]==200);equal(winner['ok'],True)
        moved=7 if kind=='overspend' else winner.get('quantity',3)
        equal(next(r['on_hand'] for r in after if r['warehouse_id']==1),10-moved)
        equal(next(r['on_hand'] for r in after if r['warehouse_id']==2),10+moved)
    def stylesheet():
        status,body,kind=request('/style.css');equal(status,200)
        equal('text/css' in kind,True);equal('#191919' in body,True)
    def missing_destination():
        sql('DELETE FROM stock WHERE product_id=2 AND warehouse_id=2')
        sql('UPDATE stock SET on_hand=10,reserved=2 WHERE product_id=2 AND warehouse_id=1')
        before=state();audit_before=movements()
        status,body,_=request('/api/transfers',{'product_id':2,'from_warehouse_id':1,'to_warehouse_id':2,'quantity':2,'idempotency_key':'create-destination'})
        equal(status,200);equal(body['ok'],True)
        after=state();equal(sum(r['on_hand'] for r in before),sum(r['on_hand'] for r in after))
        equal(next(r for r in after if r['warehouse_id']==2),{'warehouse_id':2,'on_hand':2,'reserved':0})
        equal(movements(),audit_before+2)
    try:
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base='http://127.0.0.1:%d'%port
        with (out/'server.log').open('w') as log:
            server=subprocess.Popen([sys.executable,'app.py','--db',str(dbpath),'--port',str(port)],cwd=app,stdout=log,stderr=subprocess.STDOUT)
            for _ in range(100):
                try:
                    if request('/api/health')[0]==200:break
                except (OSError,ValueError):pass
                if server.poll() is not None:raise RuntimeError('App exited during startup')
                time.sleep(.05)
            else:raise RuntimeError('App startup timed out')
            check('stylesheet_is_served_as_css',stylesheet)
            for kind in ('overspend','same_key','conflicting_key'):
                for index in range(iterations):check('%s_%03d'%(kind,index),lambda kind=kind,index=index:pair(kind,index))
            check('missing_destination_row_is_created',missing_destination)
    except Exception as exc:checks.append({'name':'runner','passed':False,'error':str(exc)})
    finally:
        if server and server.poll() is None:
            server.terminate()
            try:server.wait(timeout=5)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
    result={'passed':sum(c['passed'] for c in checks),'total':len(checks),'all_passed':bool(checks) and all(c['passed'] for c in checks),'iterations_per_case':iterations,'concurrent_transfer_requests':concurrent_requests,'checks':checks}
    (out/'result.json').write_text(json.dumps(result,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k!='checks'}))
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--app',required=True);parser.add_argument('--out-dir',required=True);parser.add_argument('--iterations',type=int,default=50)
    args=parser.parse_args()
    if not 1<=args.iterations<=500:parser.error('--iterations must be between 1 and 500')
    sys.exit(0 if stress(args.app,args.out_dir,args.iterations)['all_passed'] else 1)
