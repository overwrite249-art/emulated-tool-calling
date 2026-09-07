"""Independent black-box checks. Seeds a FRESH database outside the generated app.

Usage: python3 verify.py --app /path/to/app --out-dir /new/test/output
Only the app's documented build/run commands are invoked. No app test is trusted
as evidence for these checks. Exit status is nonzero on any failure.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from seed import seed


def verify(app, out, variant=37):
    app=Path(app).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    dbpath=seed(out/'fresh.sqlite',variant)
    checks=[];server=None
    def check(name,fn):
        try:
            fn();checks.append({'name':name,'passed':True});print('PASS',name,flush=True)
        except Exception as exc:
            checks.append({'name':name,'passed':False,'error':str(exc)});print('FAIL',name,str(exc),flush=True)
    def equal(a,b):
        if a!=b:raise AssertionError('%r != %r' % (a,b))
    def sql(query,args=()):
        with sqlite3.connect(dbpath,timeout=10) as db:
            db.row_factory=sqlite3.Row
            return [dict(r) for r in db.execute(query,args)]
    def request(path,payload=None):
        raw=None if payload is None else json.dumps(payload).encode()
        req=urllib.request.Request(base+path,data=raw,headers={'Content-Type':'application/json'})
        try:r=urllib.request.urlopen(req,timeout=12)
        except urllib.error.HTTPError as exc:r=exc
        with r:
            data=r.read();kind=r.headers.get('Content-Type','')
            return r.status,json.loads(data) if 'json' in kind else data.decode(errors='replace')
    def ok(path):
        status,body=request(path);equal(status,200);return body
    def stock_snapshot():
        return sql('SELECT * FROM stock ORDER BY product_id,warehouse_id')
    def move_count():return sql('SELECT COUNT(*) AS n FROM stock_movements')[0]['n']
    def transfer(**updates):
        value={'product_id':3,'from_warehouse_id':1,'to_warehouse_id':2,'quantity':2,'idempotency_key':'normal'}
        value.update(updates);return value
    def expected_inventory(warehouse=None,q=''):
        rows=sql('SELECT p.id AS product_id,p.sku,p.name,s.warehouse_id,w.name AS warehouse_name,s.on_hand,s.reserved,s.on_hand-s.reserved AS available,p.price_cents,p.reorder_point FROM stock s JOIN products p ON p.id=s.product_id JOIN warehouses w ON w.id=s.warehouse_id WHERE p.active=1 ORDER BY p.sku,s.warehouse_id')
        return [r for r in rows if (warehouse is None or r['warehouse_id']==warehouse) and (q.casefold() in r['sku'].casefold() or q.casefold() in r['name'].casefold())]
    def inventory_test(warehouse=None,q='',limit=20,offset=0):
        params={'q':q,'limit':limit,'offset':offset}
        if warehouse is not None:params['warehouse_id']=warehouse
        body=ok('/api/inventory?'+urllib.parse.urlencode(params));rows=expected_inventory(warehouse,q)
        equal(body['total'],len(rows));expected=rows[offset:offset+limit]
        equal([{k:r[k] for k in expected[0]} for r in body['items']] if expected else body['items'],expected)
    def summary():
        body=ok('/api/summary')
        rows=expected_inventory()
        expected={'product_count':len({r['product_id'] for r in rows}),
                  'available_units':sum(r['available'] for r in rows),
                  'inventory_value_cents':sum(r['on_hand']*r['price_cents'] for r in rows),
                  'low_stock_count':sum(r['available']<r['reorder_point'] for r in rows)}
        for key,value in expected.items():equal(body[key],value)
    try:
        if (app/'dist').exists():shutil.rmtree(app/'dist')
        build=subprocess.run([sys.executable,'build.py'],cwd=app,capture_output=True,text=True,timeout=45)
        (out/'build.log').write_text(build.stdout+build.stderr)
        check('actual_frontend_build',lambda:equal(build.returncode,0))
        check('compiled_javascript',lambda:equal((app/'dist/app.js').is_file() and (app/'dist/app.js').stat().st_size>100,True))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        base='http://127.0.0.1:%d'%port
        log=(out/'server.log').open('w')
        server=subprocess.Popen([sys.executable,'app.py','--db',str(dbpath),'--port',str(port)],cwd=app,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        for _ in range(100):
            try:
                if request('/api/health')[0]==200:break
            except (OSError,ValueError):pass
            if server.poll() is not None:raise RuntimeError('App exited; inspect server.log')
            time.sleep(.1)
        else:raise RuntimeError('App did not become healthy')
        check('health',lambda:equal(ok('/api/health')['ok'],True))
        check('html_route',lambda:equal('<html' in str(ok('/')).lower(),True))
        check('javascript_route',lambda:equal(len(str(ok('/app.js')))>100,True))
        check('warehouses',lambda:equal(ok('/api/warehouses'),sql('SELECT id,name FROM warehouses ORDER BY id')))
        check('migration_preserves_existing_data',lambda:equal(sql('SELECT value FROM fixture_marker')[0]['value'],'preserve-seed-%d'%variant))
        check('summary_uses_fresh_database',summary)
        check('inventory_pagination',lambda:inventory_test(limit=3,offset=4))
        check('warehouse_filter',lambda:inventory_test(warehouse=2,limit=100))
        check('unicode_and_literal_tags',lambda:inventory_test(q='привіт',limit=100))
        check('literal_percent_search',lambda:inventory_test(q='%',limit=100))
        check('sql_injection_is_literal',lambda:inventory_test(q="' OR 1=1 --",limit=100))
        for query in ['limit=0','limit=101','offset=-1','warehouse_id=nope']:
            check('invalid_query_'+query,lambda query=query:equal(request('/api/inventory?'+query)[0],400))
        def ordinary():
            before=stock_snapshot();n=move_count();payload=transfer();status,body=request('/api/transfers',payload)
            equal(status,200);equal(body['ok'],True)
            after=stock_snapshot();expected=[dict(r) for r in before]
            for r in expected:
                if r['product_id']==3 and r['warehouse_id']==1:r['on_hand']-=2
                if r['product_id']==3 and r['warehouse_id']==2:r['on_hand']+=2
            equal(after,expected);equal(move_count(),n+2)
            deltas=sql('SELECT delta FROM stock_movements ORDER BY id DESC LIMIT 2')
            equal(sorted(r['delta'] for r in deltas),[-2,2])
            equal(request('/api/transfers',payload),(200,body));equal(stock_snapshot(),after);equal(move_count(),n+2)
            equal(request('/api/transfers',transfer(quantity=1))[0],409);equal(stock_snapshot(),after)
        check('atomic_transfer_audit_and_idempotent_replay',ordinary)
        def rejected(payload,status):
            before=stock_snapshot();n=move_count();code,body=request('/api/transfers',payload)
            equal(code,status);equal(isinstance(body.get('error'),str),True);equal(stock_snapshot(),before);equal(move_count(),n)
        for value in [0,-1,True,1.5,'2']:
            check('reject_quantity_'+repr(value),lambda value=value:rejected(transfer(quantity=value,idempotency_key='bad'+str(value)),400))
        check('reject_same_warehouse',lambda:rejected(transfer(to_warehouse_id=1,idempotency_key='same'),400))
        check('reject_unknown_product',lambda:rejected(transfer(product_id=9999,idempotency_key='unknown'),404))
        check('reject_insufficient_available',lambda:rejected(transfer(quantity=999999,idempotency_key='overdraw'),409))
        def concurrent_overdraw():
            with sqlite3.connect(dbpath) as db:db.execute('UPDATE stock SET on_hand=5,reserved=0 WHERE product_id=2 AND warehouse_id=1')
            n=move_count()
            payloads=[transfer(product_id=2,quantity=4,idempotency_key='race-'+str(i)) for i in range(2)]
            with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(lambda p:request('/api/transfers',p),payloads))
            equal(sorted(x[0] for x in responses),[200,409])
            equal(sql('SELECT on_hand FROM stock WHERE product_id=2 AND warehouse_id=1')[0]['on_hand'],1);equal(move_count(),n+2)
        check('concurrent_transfers_cannot_overspend',concurrent_overdraw)
        def concurrent_replay():
            n=move_count();before=stock_snapshot();p=transfer(product_id=4,quantity=1,idempotency_key='same-race')
            with ThreadPoolExecutor(max_workers=2) as pool:responses=list(pool.map(lambda _:request('/api/transfers',p),range(2)))
            equal([x[0] for x in responses],[200,200]);equal(responses[0][1],responses[1][1]);equal(move_count(),n+2)
            after=stock_snapshot();equal(sum(r['on_hand'] for r in after),sum(r['on_hand'] for r in before))
        check('concurrent_idempotency_is_exactly_once',concurrent_replay)
        check('summary_after_mutations',summary)
        check('database_not_served',lambda:equal(request('/data/inventory.sqlite')[0],404))
        check('static_path_traversal_rejected',lambda:equal(request('/%2e%2e/app.py')[0] in (400,403,404),True))
    except Exception as exc:
        checks.append({'name':'runner','passed':False,'error':str(exc)})
    finally:
        if server and server.poll() is None:
            server.terminate()
            try:server.wait(timeout=5)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
        result={'checks':checks,'passed':sum(x['passed'] for x in checks),'total':len(checks),'all_passed':bool(checks) and all(x['passed'] for x in checks),'variant':variant}
        (out/'result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps({k:v for k,v in result.items() if k!='checks'}),flush=True)
    return result


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--app',required=True);ap.add_argument('--out-dir',required=True);ap.add_argument('--variant',type=int,default=37)
    a=ap.parse_args();sys.exit(0 if verify(a.app,a.out_dir,a.variant)['all_passed'] else 1)
