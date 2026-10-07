"""Prévia e limpeza seletiva de visões locais; raiz ativa e fixadas são protegidas."""
import contextlib
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path

import code_graph
import embedding_cache
import index_inventory
import index_scope
import indexer
import index_views
import project_identity
import project_store

_PLANS={}


def _assets(root):
    files={}
    for key in dict.fromkeys([project_identity.project_id(root),*project_identity.legacy_ids(root)]):
        pattern=re.compile(re.escape(key)+r'(?:\.v-[a-f0-9]{16})?(?:\.previous|\.building-[a-f0-9]+|\.vectors)?\.sqlite3$')
        for path in Path(indexer.INDEX_DIR).glob(key+'*.sqlite3'):
            if pattern.fullmatch(path.name):files[path.name]=str(path)
    return files


def _stamp(path):
    stat=os.stat(path);return [stat.st_size,stat.st_mtime_ns,stat.st_ino]


def inventory(project):
    root=project['root'];views=index_inventory.views(root);assets=_assets(root)
    active=indexer.existing_db_path(root)
    for view in views:view['current']=bool(active and os.path.normcase(view['path'])==os.path.normcase(active))
    pins=project.get('pinned_views',[])
    fallback=None if any(v['current'] for v in views) else (max(views,key=lambda v:v.get('updated_at') or 0)['path'] if views else None)
    rows=[]
    for view in views:
        row={**view,'storage_id':Path(view['path']).name,'pinned':view['view_id'] in pins}
        row['protected_reason']='Active view' if view['current'] else 'Pinned view' if row['pinned'] else 'Last available view; the active branch has no index' if view['path']==fallback else ''
        row['protected']=bool(row['protected_reason']);rows.append(row)
    total=sum(os.path.getsize(path) for path in assets.values())
    cache_path=Path(indexer.INDEX_DIR)/(project_identity.project_id(root)+'.vectors.sqlite3')
    return {'views':rows,'total_bytes':total,'cache_bytes':cache_path.stat().st_size if cache_path.exists() else 0,
            'auxiliary_bytes':max(0,total-sum(v['bytes'] for v in rows)-(cache_path.stat().st_size if cache_path.exists() else 0)),
            'cleanup':'manual','source_files_untouched':True}


def pin(project,storage_id,pinned):
    if type(pinned) is not bool:raise ValueError('pinned must be true or false.')
    row=next((v for v in inventory(project)['views'] if v['storage_id']==storage_id),None)
    if row is None:raise ValueError('View not found in this project.')
    pins=set(project.get('pinned_views',[]))
    if pinned:pins.add(row['view_id'])
    else:pins.discard(row['view_id'])
    project_store.update(project['id'],pinned_views=sorted(pins))
    return inventory(project_store.get(project['id']))


def _cache_unused(root,assets,removed):
    cache=Path(indexer.INDEX_DIR)/(project_identity.project_id(root)+'.vectors.sqlite3')
    if not cache.exists():return [],0,''
    keep=set()
    for name,path in assets.items():
        if path in removed or name.endswith('.vectors.sqlite3'):continue
        with contextlib.closing(indexer._readonly(path)) as db:
            meta=indexer._metadata(db)
            if not all(meta.get(k) for k in ('model_id','dimensions','chunker_version','chunk_lines')):
                return [],0,'Cache kept: a generation without a known model contract exists.'
            contract={'model':meta['model_id'],'dimensions':meta['dimensions'],'chunker':meta['chunker_version'],'chunk_lines':meta['chunk_lines'],'preprocessing':1}
            prefix=json.dumps(contract,sort_keys=True,ensure_ascii=False)+'\0'
            for text, in db.execute('SELECT text FROM chunks'):
                keep.add(hashlib.sha256((prefix+text).encode('utf-8')).hexdigest())
                if len(keep)>1000000:return [],0,'Cache kept: reference check limit reached.'
    with contextlib.closing(indexer._readonly(str(cache))) as db:
        unused=[(key,size) for key,size in db.execute('SELECT key,length(vector) FROM vectors') if key not in keep]
    return [key for key,_ in unused],sum(size for _,size in unused),''


def preview(project,storage_ids,clear_cache=True):
    if not isinstance(storage_ids,list) or any(not isinstance(x,str) for x in storage_ids) or len(storage_ids)>200:
        raise ValueError('Choose up to 200 views of this project.')
    storage_ids=sorted(set(storage_ids))
    if type(clear_cache) is not bool:raise ValueError('clear_cache must be true or false.')
    current=inventory(project);lookup={v['storage_id']:v for v in current['views']}
    assets=_assets(project['root']);removed=[];selected=[]
    for storage_id in set(storage_ids):
        row=lookup.get(storage_id)
        if not row:raise ValueError('A selected view no longer belongs to the project.')
        if row['protected']:raise ValueError(row['label']+': '+row['protected_reason']+'.')
        selected.append(row)
        stem=storage_id[:-8]
        removed.extend(path for name,path in assets.items() if name==storage_id or re.fullmatch(re.escape(stem)+r'(?:\.previous|\.building-[a-f0-9]+)\.sqlite3',name))
        if '.v-' in stem:
            scope=Path(index_scope.SCOPE_DIR)/(stem+'.scope.json')
            if scope.is_file():removed.append(str(scope))
    if not selected:raise ValueError('Select at least one unprotected view.')
    for path in removed:project_identity.state_path(path,indexer.INDEX_DIR)
    keys,vector_bytes,note=_cache_unused(project['root'],assets,set(removed)) if clear_cache else ([],0,'Shared cache kept by user choice.')
    signature={path:_stamp(path) for path in set(assets.values())|set(removed)}
    token=secrets.token_urlsafe(24)
    for old,value in list(_PLANS.items()):
        if value['expires']<time.time():_PLANS.pop(old,None)
    while len(_PLANS)>=8:_PLANS.pop(next(iter(_PLANS)))
    result={'plan_id':token,'views':[{'storage_id':v['storage_id'],'label':v['label']} for v in selected],
            'index_bytes':sum(os.path.getsize(p) for p in removed),'unused_vectors':len(keys),
            'vector_bytes':vector_bytes,'note':note,'expires_in_s':900,'source_files_untouched':True}
    _PLANS[token]={'project_id':project['id'],'expires':time.time()+900,'paths':removed,'signatures':signature,
                   'keys':keys,'storage_ids':storage_ids,'clear_cache':clear_cache,'result':result}
    return result


def commit(project,plan_id):
    plan=_PLANS.pop(plan_id,None)
    if not plan or plan['project_id']!=project['id'] or plan['expires']<time.time():
        raise ValueError('Preview expired or invalid. Generate a new cleanup preview.')
    # Atualiza a identidade antes de conferir proteções: um checkout recente não
    # pode usar a visão anterior mantida pelo cache de descoberta durante 1 s.
    view=index_views.describe(project['root'],fresh=True)
    current={v['storage_id']:v for v in inventory(project)['views']}
    index_views.assert_current(view)
    for key in plan['storage_ids']:
        if key not in current or current[key]['protected']:raise ValueError('View protection changed. Generate another preview.')
    assets=_assets(project['root'])
    if set(assets.values())!=set(p for p in plan['signatures'] if p.endswith('.sqlite3')):
        raise ValueError('The set of indexes changed. Generate another preview.')
    if any(not os.path.exists(path) or _stamp(path)!=stamp for path,stamp in plan['signatures'].items()):
        raise ValueError('An index changed since the preview. Generate another preview.')
    moved=[];warnings=[]
    try:
        for source in plan['paths']:
            index_views.assert_current(view)
            safe=project_identity.state_path(source,indexer.INDEX_DIR)
            target=project_identity.state_path(source+'.cleanup-'+secrets.token_hex(6),indexer.INDEX_DIR)
            os.replace(safe,target);moved.append((safe,target))
        index_views.assert_current(view)
    except (OSError,index_views.ViewChanged) as exc:
        for source,target in reversed(moved):os.replace(target,source)
        if isinstance(exc,index_views.ViewChanged):raise
        raise RuntimeError('An index is open in another process. Close the external reader and generate a new preview.') from exc
    cache=Path(indexer.INDEX_DIR)/(project_identity.project_id(project['root'])+'.vectors.sqlite3')
    deleted_vectors=0
    if plan['keys'] and cache.exists():
        try:
            with contextlib.closing(sqlite3.connect(cache,timeout=5)) as db:
                db.execute('PRAGMA secure_delete=ON')
                with db:db.executemany('DELETE FROM vectors WHERE key=?',[(key,) for key in plan['keys']])
                deleted_vectors=len(plan['keys'])
                try:db.execute('VACUUM')
                except sqlite3.Error:warnings.append('Vectors removed; SQLite could not compact the cache right now.')
        except sqlite3.Error:warnings.append('The views were removed; the cache in use by another process was kept.')
    for _,path in moved:os.remove(project_identity.state_path(path,indexer.INDEX_DIR))
    indexer.invalidate_cache(project['root'])
    with code_graph._LOCK:
        for key in list(code_graph._CACHE):
            if key[0] in plan['paths']:code_graph._CACHE.pop(key,None)
    return {'removed_views':len(plan['storage_ids']),'removed_index_bytes':plan['result']['index_bytes'],
            'removed_vectors':deleted_vectors,'warnings':warnings,'source_files_untouched':True,
            'storage':inventory(project_store.get(project['id']))}
