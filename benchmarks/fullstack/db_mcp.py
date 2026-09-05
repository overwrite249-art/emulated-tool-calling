"""Concurrent stdio MCP exposing read-only inspection of one synthetic SQLite DB."""
import concurrent.futures
import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

DB = Path(sys.argv[1]).resolve()
EVENTS = Path(sys.argv[2]).resolve()
LOCK = threading.Lock()
TOOLS = [
    {'name':'db_schema','description':'Inspect SQLite table and index DDL. Independent of db_profile.',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'db_profile','description':'Count rows in every table of the existing SQLite database. Independent of db_schema.',
     'inputSchema':{'type':'object','properties':{},'additionalProperties':False}},
    {'name':'db_query','description':'Run a bounded read-only SELECT against the existing SQLite database.',
     'inputSchema':{'type':'object','properties':{'sql':{'type':'string','maxLength':4000}},'required':['sql'],'additionalProperties':False}},
]
for tool in TOOLS:
    tool['annotations']={'readOnlyHint':True,'destructiveHint':False}


def emit(value):
    with LOCK:
        print(json.dumps(value,ensure_ascii=False),flush=True)


def event(value):
    with LOCK:
        with EVENTS.open('a',encoding='utf-8') as out:
            out.write(json.dumps(value,ensure_ascii=False)+'\n')


def query(name,args):
    with sqlite3.connect(DB.as_uri()+'?mode=ro',uri=True,timeout=3) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        if name=='db_schema':
            # Explicit instrumentation latency makes overlapping calls measurable.
            time.sleep(.6)
            return [dict(r) for r in db.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
        if name=='db_profile':
            time.sleep(.6)
            tables=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            return {'tables':[{'name':n,'rows':db.execute('SELECT COUNT(*) FROM "'+n.replace('"','""')+'"').fetchone()[0]} for n in tables]}
        if name!='db_query':
            raise ValueError('Unknown tool')
        sql=args.get('sql','')
        if not isinstance(sql,str) or len(sql)>4000:
            raise ValueError('Invalid SQL')
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        def authorize(action,a,b,dbname,source):
            # SQLite reports a None database for optimized COUNT(*) reads.
            if action not in allowed or (action==sqlite3.SQLITE_READ and dbname not in ('main',None)):
                return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_FUNCTION and str(b).lower() in ('load_extension','readfile','writefile'):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        db.set_authorizer(authorize)
        deadline=time.monotonic()+2
        db.set_progress_handler(lambda: int(time.monotonic()>deadline),1000)
        cursor=db.execute(sql)
        rows=cursor.fetchmany(101)
        return {'rows':[dict(r) for r in rows[:100]],'truncated':len(rows)>100}


def handle(msg):
    mid=msg.get('id');method=msg.get('method');params=msg.get('params',{})
    if mid is None:
        return
    if method=='initialize':
        result={'protocolVersion':params.get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},
                'serverInfo':{'name':'warehouse','version':'1.0'}}
    elif method=='tools/list':
        result={'tools':TOOLS}
    elif method=='ping':
        result={}
    elif method=='tools/call':
        name=params.get('name');args=params.get('arguments',{});started=time.monotonic()
        event({'event':'start','tool':name,'arguments':args,'time':started})
        try:
            value=query(name,args)
            result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}],'isError':False}
        except Exception as exc:
            result={'content':[{'type':'text','text':str(exc)}],'isError':True}
        event({'event':'end','tool':name,'time':time.monotonic(),'started':started,'is_error':result['isError']})
    else:
        emit({'jsonrpc':'2.0','id':mid,'error':{'code':-32601,'message':'Method not found'}})
        return
    emit({'jsonrpc':'2.0','id':mid,'result':result})


if __name__=='__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for line in sys.stdin:
            try:pool.submit(handle,json.loads(line))
            except ValueError:pass
