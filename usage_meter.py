"""Consumo por projeto/visão. Só guarda contagens, modelos e horários, nunca conteúdo."""
import contextlib
import contextvars
import datetime
import json
import math
import time

import project_identity
import project_store

_CONTEXT=contextvars.ContextVar('smart_usage',default=None)
_PHASES={'scope':'scope','embedding_query':'query_embedding','model_probe':'embedding_probe',
         'indexing':'index_embedding','reranking':'rerank'}


@contextlib.contextmanager
def job(root,view,job_id):
    value={'project_id':project_identity.project_id(root),'view_id':view['view_id'],
           'view_label':view['label'],'job_id':job_id,'purpose':'other'}
    token=_CONTEXT.set(value)
    try:yield
    finally:_CONTEXT.reset(token)


def phase(name):
    current=_CONTEXT.get()
    if current and name in _PHASES:_CONTEXT.set({**current,'purpose':_PHASES[name]})


def _tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS usage_daily(day TEXT,project_id TEXT,view_id TEXT,view_label TEXT,purpose TEXT,model TEXT,attempts INTEGER,successes INTEGER,errors INTEGER,input_items INTEGER,input_characters INTEGER,prompt_tokens INTEGER,output_tokens INTEGER,token_reports INTEGER,reported_usd REAL,price_reports INTEGER,elapsed_s REAL,PRIMARY KEY(day,project_id,view_id,purpose,model))')
    db.execute('CREATE TABLE IF NOT EXISTS usage_index_jobs(job_id TEXT PRIMARY KEY,project_id TEXT,view_id TEXT,view_label TEXT,at REAL,kind TEXT,embedded_chunks INTEGER,reused_chunks INTEGER,unchanged_files INTEGER,embedded_characters INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS usage_imports(project_id TEXT PRIMARY KEY)')


def _number(value):
    return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None


def record(*args,**kwargs):
    try:_record(*args,**kwargs)
    except Exception:pass  # Telemetria opcional não altera o resultado da chamada.


def _record(path,body,response=None,headers=None,error=None,elapsed=0):
    current=_CONTEXT.get()
    if not current or path not in ('/v1/embeddings','/v1/rerank','/v1/chat/completions'):
        return
    body=body if isinstance(body,dict) else {}
    response=response if isinstance(response,dict) else {}
    usage=response.get('usage') or (response.get('meta') or {}).get('tokens') or {}
    if not isinstance(usage,dict):usage={}
    prompt=_number(usage.get('prompt_tokens',usage.get('input_tokens')))
    output=_number(usage.get('completion_tokens',usage.get('output_tokens')))
    if prompt is None and path=='/v1/embeddings':prompt=_number(usage.get('total_tokens'))
    texts=body.get('input') if path.endswith('embeddings') else body.get('documents') if path.endswith('rerank') else []
    if isinstance(texts,str):texts=[texts]
    texts=texts if isinstance(texts,list) else []
    characters=sum(len(t) for t in texts if isinstance(t,str))
    if path.endswith('rerank'):characters+=len(body.get('query') or '')
    if path.endswith('completions'):
        characters=sum(len(m.get('content','')) for m in body.get('messages',[]) if isinstance(m,dict) and isinstance(m.get('content'),str))
    cost=None
    try:cost=_number(float((headers or {}).get('x-litellm-response-cost')))
    except (TypeError,ValueError):pass
    day=datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    values=(day,current['project_id'],current['view_id'],current['view_label'],current['purpose'],str(body.get('model','')),
            1,int(error is None),int(error is not None),len(texts),characters,int(prompt or 0),int(output or 0),
            int(prompt is not None or output is not None),cost or 0,int(cost is not None),round(elapsed,4))
    # Telemetria opcional não pode derrubar a chamada do modelo.
    try:
        with project_store._LOCK,project_store._db() as db:
            _tables(db)
            fields=['attempts','successes','errors','input_items','input_characters','prompt_tokens','output_tokens','token_reports','reported_usd','price_reports','elapsed_s']
            update=','.join(f'{name}={name}+excluded.{name}' for name in fields)
            db.execute('INSERT INTO usage_daily VALUES('+','.join('?' for _ in values)+') ON CONFLICT(day,project_id,view_id,purpose,model) DO UPDATE SET '+update,values)
            cutoff=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=366)).date().isoformat()
            db.execute('DELETE FROM usage_daily WHERE day<?',(cutoff,))
    except Exception:
        pass


def record_job(job_id,payload):
    try:_record_job(job_id,payload)
    except Exception:pass


def _record_job(job_id,payload):
    if payload.get('status')=='pending' or not isinstance(payload.get('stats'),dict):return
    stats=payload['stats'];view=payload.get('view') or stats.get('view') or {'view_id':'legacy','label':'History without view'}
    with project_store._LOCK,project_store._db() as db:
        _tables(db)
        db.execute('INSERT OR IGNORE INTO usage_index_jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
            (job_id,payload.get('project_id'),view.get('view_id','legacy'),view.get('label',''),payload.get('finished_at',time.time()),payload.get('kind',''),
             stats.get('embedded_chunks',0),stats.get('reused_chunks',0),stats.get('unchanged',0),stats.get('embedded_characters',0)))
        db.execute('DELETE FROM usage_index_jobs WHERE at<?',(time.time()-366*86400,))


def summary(project_id,days=30,view_id=None):
    if type(days) is not int or not 1<=days<=366:raise ValueError('Choose a period from 1 to 366 days.')
    with project_store._LOCK,project_store._db() as db:
        _tables(db)
        imported=db.execute('SELECT 1 FROM usage_imports WHERE project_id=?',(project_id,)).fetchone()
    if not imported:
        for data in project_store.jobs(project_id,limit=project_store.MAX_JOBS):record_job(data['job_id'],data)
        with project_store._LOCK,project_store._db() as db:
            db.execute('INSERT OR IGNORE INTO usage_imports VALUES(?)',(project_id,))
    cutoff=(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=days-1)).date().isoformat()
    args=[project_id,cutoff];where='project_id=? AND day>=?'
    if view_id:where+=' AND view_id=?';args.append(view_id)
    with project_store._LOCK,project_store._db() as db:
        _tables(db);db.row_factory=__import__('sqlite3').Row
        rows=[dict(r) for r in db.execute('SELECT * FROM usage_daily WHERE '+where+' ORDER BY day DESC,view_label,purpose',args)]
        job_args=[project_id,datetime.datetime.fromisoformat(cutoff).replace(tzinfo=datetime.timezone.utc).timestamp()];job_where='project_id=? AND at>=?'
        if view_id:job_where+=' AND view_id=?';job_args.append(view_id)
        jobs=[dict(r) for r in db.execute('SELECT view_id,view_label,count(*) AS jobs,sum(embedded_chunks) AS embedded_chunks,sum(reused_chunks) AS reused_chunks,sum(unchanged_files) AS unchanged_files,sum(embedded_characters) AS embedded_characters FROM usage_index_jobs WHERE '+job_where+' GROUP BY view_id,view_label',job_args)]
    totals={key:sum(row[key] for row in rows) for key in ('attempts','successes','errors','input_items','input_characters','prompt_tokens','output_tokens','token_reports','reported_usd','price_reports','elapsed_s')}
    if not totals['price_reports']:totals['reported_usd']=None
    if not totals['token_reports']:totals['prompt_tokens']=totals['output_tokens']=None
    return {'days':days,'daily':rows,'indexing':jobs,'totals':totals,'currency':'USD',
            'notes':['Calls and tokens are measured starting with this version. Earlier history only contains statistics that were already recorded.',
                     'Monetary values appear only when reported by the model gateway; they are not an estimate of the full bill.',
                     'Characters are not tokens. Cache reuse and unchanged files are counted separately.']}
