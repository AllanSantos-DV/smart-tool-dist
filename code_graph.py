"""Grafos estáticos do conteúdo indexado: cobertura, imports e chamadas resolvidas."""
import ast
import collections
import copy
import gzip
import contextvars
import hashlib
import json
import os
import posixpath
import subprocess
import threading
import time
from pathlib import Path

import indexer
import index_inventory
import index_scope
import project_identity
import web_search_adapters
import web_document_graph
import document_text

VERSION = 2
SOURCE_EXTENSIONS = ('.py','.java','.js','.jsx','.ts','.tsx','.mjs','.cjs','.mts','.cts','.json','.html','.htm','.css','.scss','.sass','.less','.md','.markdown')
MAX_OVERLAY_BYTES = 5 * 1024 * 1024
MAX_FILES = 3000
MAX_TEXT = 24 * 1024 * 1024
MAX_SYMBOLS = 8000
MAX_EDGES = 20000
_CACHE = collections.OrderedDict()
_LOCK = threading.Lock()
_ANALYSIS_SLOTS = threading.BoundedSemaphore(2)
_WARM_SLOT = threading.BoundedSemaphore(1)
_WARMING = set()
_WARM_STATE = {}
WARM_RETRY_S = 600


def reconstruct(rows):
    """Remove sobreposição de chunks e recusa buracos/conflitos; mantém linhas reais."""
    lines = {}
    previous = None
    for start, end, text in rows:
        parts = text.splitlines(keepends=True)
        if len(parts) != end-start+1:
            return None, 'Chunks do not reconstruct the full lines of the file.'
        for offset, part in enumerate(parts):
            line = start+offset
            if line in lines and start == end == line and previous and previous[0] == previous[1] == line and not previous[2].endswith(('\n','\r')):
                lines[line] += part
            elif line in lines and lines[line] != part:
                return None, 'Overlapping chunks contain conflicting versions.'
            else:
                lines[line] = part
        previous = (start,end,text)
    if not lines:
        return '', None
    if min(lines) != 1 or len(lines) != max(lines):
        return None, 'The snapshot has gaps; relations were not inferred for this file.'
    return ''.join(lines[i] for i in range(1,max(lines)+1)), None


def _snapshot(path):
    conn = indexer._readonly(path)
    try:
        conn.execute('BEGIN')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(manifest)')}
        size = 'm.size' if 'size' in columns else 'NULL'
        mtime = 'm.mtime_ns' if 'mtime_ns' in columns else 'NULL'
        records = conn.execute(f'SELECT m.path,m.hash,{size},{mtime},count(c.id),max(c.end_line) FROM manifest m LEFT JOIN chunks c ON c.path=m.path GROUP BY m.path ORDER BY m.path LIMIT ?', (MAX_FILES+1,)).fetchall()
        files, sources, diagnostics, used = [], {}, [], 0
        for original, digest, byte_size, stamp, chunks, line_count in records[:MAX_FILES]:
            file_path = original.replace('\\','/')
            record = {'id':file_path,'path':file_path,'hash':digest,'size':byte_size,'mtime_ns':stamp,
                      'chunks':chunks,'lines':line_count or 0,'group':posixpath.dirname(file_path) or '(root)',
                      'location_kind':document_text.location_kind(file_path),'format':posixpath.splitext(file_path)[1].lower().lstrip('.') or 'text'}
            files.append(record)
            if not file_path.lower().endswith(SOURCE_EXTENSIONS):
                continue
            rows = conn.execute('SELECT start_line,end_line,text FROM chunks WHERE path=? ORDER BY start_line,id', (original,)).fetchall()
            text, error = reconstruct(rows)
            if error:
                diagnostics.append({'path':file_path,'reason':error})
            elif used+len(text) > MAX_TEXT:
                diagnostics.append({'path':file_path,'reason':'Content limit for this analysis reached.'})
            else:
                used += len(text)
                sources[file_path] = text
        return files, sources, diagnostics, len(records)>MAX_FILES
    finally:
        conn.close()


def _python_graph(sources):
    trees, symbols, dependencies, calls, unresolved, diagnostics = {}, [], [], [], [], []
    decls, aliases, scopes, classes, owners, shadow_scopes, node_symbols = {}, {}, {}, {}, {}, {}, {}
    def bind_import(path,scope,name,binding):
        table=aliases[path].setdefault(scope,{})
        table[name]=binding if name not in table or table[name]==binding else None
    def resolve_file(spec):
        spec=posixpath.normpath(spec)
        return next((p for p in (spec+'.py',spec+'/__init__.py','src/'+spec+'.py','src/'+spec+'/__init__.py') if p in sources), None)
    for path, text in sources.items():
        if not path.endswith('.py'):
            continue
        try:
            tree=ast.parse(text,filename=path)
        except (SyntaxError,ValueError,RecursionError) as exc:
            diagnostics.append({'path':path,'reason':'Incomplete or invalid Python syntax in the snapshot.','line':getattr(exc,'lineno',None)})
            continue
        trees[path]=tree;aliases[path]={};decls[path]={};classes[path]={}
        def collect(node,qual=(),owner=None):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                qual=qual+(node.name,)
                sym={'id':path+'::'+'.'.join(qual)+'@'+str(node.lineno),'path':path,'name':'.'.join(qual),
                     'start_line':node.lineno,'end_line':node.end_lineno,'kind':'class' if isinstance(node,ast.ClassDef) else 'function', 'language':'python'}
                decls[path][qual]=sym if qual not in decls[path] else None
                scopes[id(node)]=qual;node_symbols[id(node)]=sym
                if isinstance(node,ast.ClassDef):classes[path][qual]=node
                if len(symbols)<MAX_SYMBOLS:symbols.append(sym)
                owner=sym
            owners[id(node)]=qual
            for child in ast.iter_child_nodes(node):collect(child,qual,owner)
        collect(tree)
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                for item in node.names:
                    spec=item.name.replace('.','/');target=resolve_file(spec)
                    bind_import(path,owners[id(node)],item.asname or item.name,(target,None))
                    dependencies.append({'source':path,'target':target,'specifier':item.name,'line':node.lineno,'kind':'import','resolution':'resolved' if target else 'external'})
            elif isinstance(node,ast.ImportFrom):
                prefix=posixpath.dirname(path)
                if node.level:
                    for _ in range(node.level-1):prefix=posixpath.dirname(prefix)
                    spec=posixpath.join(prefix,(node.module or '').replace('.','/'))
                else:spec=(node.module or '').replace('.','/')
                base=resolve_file(spec)
                for item in node.names:
                    target=resolve_file(spec+'/'+item.name) if item.name!='*' else None
                    target=target or base
                    bind_import(path,owners[id(node)],item.asname or item.name,(target,None if target and target!=base else item.name))
                    dependencies.append({'source':path,'target':target,'specifier':('.'*node.level)+(node.module or '')+'.'+item.name,'line':node.lineno,'kind':'import','resolution':'resolved' if target else 'unresolved' if node.level else 'external'})
    def chain(expr):
        if isinstance(expr,ast.Name):return expr.id
        if isinstance(expr,ast.Attribute):
            base=chain(expr.value)
            return base+'.'+expr.attr if base else None
        return None
    def resolve(path,qual,name,shadow,instances):
        if not name:return None
        parts=name.split('.')
        if parts[0] in instances:
            typ=instances[parts[0]]
            return decls.get(typ['path'],{}).get(tuple(typ['name'].split('.'))+tuple(parts[1:]))
        if parts[0] in ('self','cls') and len(parts)>1:
            for i in range(len(qual),0,-1):
                if qual[:i] in classes[path]:return decls[path].get(qual[:i]+tuple(parts[1:]))
        if parts[0] in shadow:return None
        for i in range(len(qual),-1,-1):
            if i<len(qual) and qual[:i] in classes[path]:
                continue  # O namespace da classe não é escopo lexical de seus métodos.
            if parts[0] in shadow_scopes.get((path,qual[:i]),set()):
                return None
            if qual[:i]+tuple(parts) in decls[path]:
                return decls[path][qual[:i]+tuple(parts)]
            for length in range(len(parts),0,-1):
                table=aliases[path].get(qual[:i],{})
                binding=table.get('.'.join(parts[:length]))
                if '.'.join(parts[:length]) in table:
                    if binding is None:return None
                    target_path,imported=binding
                    rest=(([imported] if imported and imported!='*' else [])+parts[length:])
                    return decls.get(target_path,{}).get(tuple(rest))
        return None
    for path,tree in trees.items():
        module={'id':path+'::<module>','path':path,'name':'<module>','start_line':1,'end_line':1,'kind':'module','language':'python'}
        used_module=False
        def visit_scope(node,qual=()):
            nonlocal used_module
            owner=node_symbols.get(id(node),module)
            shadow=set();instances={}
            args=getattr(node,'args',None)
            if args:
                params=[*args.posonlyargs,*args.args,*args.kwonlyargs]+([args.vararg] if args.vararg else [])+([args.kwarg] if args.kwarg else [])
                shadow.update(arg.arg for arg in params)
                for arg in params:
                    target=resolve(path,qual,chain(arg.annotation),set(),{}) if arg.annotation else None
                    if target and target['kind']=='class':instances[arg.arg]=target
            # Levanta ligações locais sem atravessar escopos aninhados.
            def local_nodes(n):
                for child in ast.iter_child_nodes(n):
                    if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef,ast.Lambda)):
                        continue
                    yield child
                    yield from local_nodes(child)
            local=list(local_nodes(node))
            assigned={}
            for n in local:
                if isinstance(n,(ast.Assign,ast.AnnAssign)):
                    targets=n.targets if isinstance(n,ast.Assign) else [n.target]
                    for target in targets:
                        if isinstance(target,ast.Name):
                            shadow.add(target.id);assigned.setdefault(target.id,[]).append(n.value)
            for name,values in assigned.items():
                if len(values)==1 and isinstance(values[0],ast.Call):
                    typ=resolve(path,qual,chain(values[0].func),shadow-set([name]),instances)
                    if typ and typ['kind']=='class':instances[name]=typ
            shadow_scopes[(path,qual)]=shadow
            for n in local:
                if not isinstance(n,ast.Call):continue
                expr=chain(n.func)
                target=resolve(path,qual,expr,shadow,instances)
                if target:
                    if owner is module:used_module=True
                    if len(calls)<MAX_EDGES:calls.append({'source':owner['id'],'target':target['id'],'line':n.lineno,'kind':'construct' if target['kind']=='class' else 'call','resolution':'static'})
                elif len(unresolved)<1000:
                    unresolved.append({'path':path,'line':n.lineno,'expression':expr or '<dynamic expression>','reason':'External, dynamic or unresolved target in this snapshot.'})
            for child in ast.iter_child_nodes(node):
                if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                    visit_scope(child,scopes[id(child)])
                else:
                    # Definições em blocos if/try também podem declarar escopos.
                    def nested(n):
                        for c in ast.iter_child_nodes(n):
                            if isinstance(c,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):visit_scope(c,scopes[id(c)])
                            else:nested(c)
                    nested(child)
        visit_scope(tree)
        if used_module:symbols.append(module)
    return {'symbols':symbols,'dependencies':dependencies[:MAX_EDGES],'calls':calls,'unresolved':unresolved,
            'diagnostics':diagnostics,'parsed':list(trees),'truncated':len(symbols)>=MAX_SYMBOLS or len(dependencies)>MAX_EDGES}


NODE_HEAP_MB = (1024, 4096)


def _run_analyzer(script, payload, failure):
    """Runs a Node analyzer, retrying once with a larger heap when it runs out of memory (634 TypeScript files, 2.9 MB,
    needed more than the former fixed 512 MB)."""
    for heap in NODE_HEAP_MB:
        process=subprocess.run([web_search_adapters.NODE_EXECUTABLE,f'--max-old-space-size={heap}',str(Path(__file__).with_name(script))],
            input=payload,capture_output=True,text=True,encoding='utf-8',timeout=120,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode==0:
            return json.loads(process.stdout)
        if 'heap out of memory' not in (process.stderr or '') or heap==NODE_HEAP_MB[-1]:
            break
    detail=' (out of memory)' if 'heap out of memory' in (process.stderr or '') else ''
    raise RuntimeError(failure+detail)


def _javascript_graph(sources, all_paths=None):
    extensions=('.js','.jsx','.ts','.tsx','.mjs','.cjs','.mts','.cts')
    relevant={path:text for path,text in sources.items() if path.endswith(extensions+('.json','.html','.htm'))}
    if not any(path.endswith(extensions) for path in relevant):
        return {}
    try:
        return _run_analyzer('code_graph_js.cjs',
                             json.dumps({'files':[{'path':path,'text':text} for path,text in relevant.items()],
                                         'paths':list(all_paths or sources)},ensure_ascii=False),
                             'The TypeScript analyzer did not finish. Check the local code-analysis runtime.')
    except (OSError,subprocess.TimeoutExpired,ValueError,RuntimeError) as exc:
        return {'diagnostics':[{'path':'JavaScript/TypeScript','reason':str(exc)[:250]}],'parsed':[],'retryable':True}


def _java_graph(sources):
    files=[{'path':path,'text':text} for path,text in sources.items() if path.lower().endswith('.java')]
    if not files:return {}
    try:
        return _run_analyzer('code_graph_java.mjs',json.dumps({'files':files},ensure_ascii=False),
                             'The Java analyzer did not finish. Check the local runtime.')
    except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
        return {'diagnostics':[{'path':'Java','reason':str(exc)[:250]}],'parsed':[],'retryable':True}


def _analyze(path):
    return _analyze_sources(*_snapshot(path))


def _analyze_sources(files,sources,diagnostics,truncated):
    merged={'symbols':[],'dependencies':[],'calls':[],'unresolved':[],'diagnostics':diagnostics,'parsed':[]}
    retryable=False
    for output in (_python_graph(sources),_javascript_graph(sources,[f['path'] for f in files]),_java_graph(sources),web_document_graph.analyze(sources,[f['path'] for f in files])):
        for key in merged:merged[key].extend(output.get(key,[]))
        truncated |= output.get('truncated',False)
        retryable |= output.get('retryable',False)
    by_id={sym['id']:sym for sym in merged['symbols'][:MAX_SYMBOLS]}
    # Relações repetidas preservam contagem e locais de chamada sem poluir o desenho.
    def collapse(rows,fields):
        result={}
        for row in rows:
            key=tuple(row.get(field) for field in fields)
            if key not in result:result[key]={**row,'count':0,'lines':[]}
            result[key]['count']+=1
            if len(result[key]['lines'])<20:result[key]['lines'].append(row.get('line'))
        return list(result.values())
    deps=collapse(merged['dependencies'],('source','target','specifier','kind'))
    calls=collapse([r for r in merged['calls'] if r['source'] in by_id and r['target'] in by_id],('source','target','kind'))
    supported=set(merged['parsed'])
    for file in files:file['analysis']='parsed' if file['path'] in supported else 'not_analyzed'
    return {'files':files,'symbols':merged['symbols'][:MAX_SYMBOLS],
            'dependencies':deps[:MAX_EDGES],'calls':calls[:MAX_EDGES],
            'unresolved':merged['unresolved'][:1000],'diagnostics':merged['diagnostics'],
            'truncated':truncated or len(deps)>MAX_EDGES or len(calls)>MAX_EDGES,'retryable':retryable,
            'counts':{'files':len(files),'parsed_files':len(supported),'symbols':len(merged['symbols']),
                      'resolved_imports':sum(bool(r['target']) for r in deps),
                      'external_imports':sum(r['resolution']=='external' for r in deps),
                      'unresolved_calls':len(merged['unresolved']),'calls':len(calls)}}


def _focus(data,file_path):
    if not isinstance(file_path,str):
        raise ValueError('file_path must be a string.')
    target=file_path.replace('\\','/').removeprefix('./')
    files=[f for f in data['files'] if f['path']==target]
    if not files:
        raise ValueError(f'File is outside the indexed view: {file_path}')
    by_id={s['id']:s for s in data['symbols']}
    own={s['id'] for s in data['symbols'] if s.get('path')==target}
    calls=[c for c in data['calls'] if c['source'] in own or c['target'] in own]
    linked=own|{c['source'] for c in calls}|{c['target'] for c in calls}
    deps=[d for d in data['dependencies'] if target in (d['source'],d['target'])]
    paths={f['path'] for f in data['files']}
    data.update(files=files,symbols=[by_id[i] for i in linked if i in by_id],dependencies=deps,calls=calls,
                unresolved=[u for u in data['unresolved'] if u.get('path')==target],
                diagnostics=[d for d in data['diagnostics'] if d.get('path')==target or d.get('path') not in paths],
                focus={'file_path':target,'symbols':len(own),'imported_by':sum(d['target']==target for d in deps),
                       'imports':sum(d['source']==target for d in deps),
                       'incoming_calls':sum(c['target'] in own and c['source'] not in own for c in calls),
                       'outgoing_calls':sum(c['source'] in own and c['target'] not in own for c in calls)})
    return data


def _by_file(data):
    by_file={}
    for symbol in data['symbols']:
        by_file.setdefault(symbol['path'],[]).append(symbol)
    parsed={f['path'] for f in data['files'] if f.get('analysis')=='parsed'}
    note='Graph truncated by analysis limits; some files have no functions.' if data.get('truncated') else None
    return {'symbols':by_file,'parsed':parsed,'note':note}


def _disk_path(view_path):
    path=indexer.graph_cache_path(view_path)
    return os.path.dirname(path),path


def _disk_load(key):
    """Analysis saved for exactly this index state and analyzer version, or None. A damaged file is a miss: the
    caller analyzes again and overwrites it."""
    _folder,path=_disk_path(key[0])
    try:
        with gzip.open(path,'rt',encoding='utf-8') as stream:
            saved=json.load(stream)
    except (OSError,EOFError,ValueError):
        return None
    return saved.get('graph') if saved.get('key')==[key[1],key[2],key[3]] else None


def _disk_save(key,data):
    """Writes the analysis next to the index (atomic replace) and removes caches of views that no longer exist.
    Returns the error text when it could not be written, so the caller can report it."""
    folder,path=_disk_path(key[0])
    try:
        os.makedirs(folder,exist_ok=True)
        temporary=f'{path}.{os.getpid()}.{threading.get_ident()}.tmp'
        with gzip.open(temporary,'wt',encoding='utf-8',compresslevel=5) as stream:
            json.dump({'key':[key[1],key[2],key[3]],'graph':data},stream,ensure_ascii=False)
        os.replace(temporary,path)
        for name in os.listdir(folder):
            if name.endswith('.json.gz') and not os.path.isfile(os.path.join(os.path.dirname(folder),name[:-8]+'.sqlite3')):
                os.remove(os.path.join(folder,name))
    except OSError as exc:
        return f'Graph cache not saved ({type(exc).__name__}: {exc}); the next restart analyzes again.'[:300]
    return None


def cached_symbols(root):
    path=indexer.existing_db_path(root)
    if not path:
        return None,None
    stat=os.stat(path);key=(path,stat.st_mtime_ns,stat.st_size,VERSION)
    with _LOCK:
        data=_CACHE.get(key)
        if data is not None:
            _CACHE.move_to_end(key)
    if data is None:
        data=_disk_load(key)
        if data is not None:
            with _LOCK:
                _CACHE[key]=data
                while len(_CACHE)>3:_CACHE.popitem(last=False)
    with _LOCK:
        state=_WARM_STATE.get(root)
        if data is None and state and state['key']==key and (state['result'] or time.time()-state['at']<WARM_RETRY_S):
            return state['result'],state['note']
        start=data is None and root not in _WARMING
        if start:_WARMING.add(root)
    if data is not None:
        return _by_file(data),None
    if start:
        context=contextvars.copy_context()
        threading.Thread(target=context.run,args=(_warm,root,key),daemon=True).start()
    return None,None


def warm(root):
    """Starts the analysis of the project's current index in the background unless memory or disk already has it, so
    the next edit hook or search finds the graph ready (called when an indexing job ends)."""
    cached_symbols(root)


def _warm(root,key):
    result,note=None,None
    try:
        data=build(root,background=True)
        if data.get('retryable'):
            result=_by_file(data)
            reasons='; '.join(d.get('reason','') for d in data.get('diagnostics',[])[:2])
            note=f'Partial analysis: {reasons}'[:300]
    except Exception as exc:
        note=f'The last analysis failed: {type(exc).__name__}: {exc}'[:300]
    finally:
        with _LOCK:
            if note:_WARM_STATE[root]={'key':key,'at':time.time(),'result':result,'note':note}
            else:_WARM_STATE.pop(root,None)
            _WARMING.discard(root)


def build(root,view_id=None,storage_id=None,file_path=None,background=False):
    inventory=index_inventory.inspect(root,view_id,storage_id=storage_id)
    if not inventory['selected']:
        return {**inventory,'symbols':[],'dependencies':[],'calls':[],'diagnostics':[],'counts':{}}
    path=inventory['selected']['path'];stat=os.stat(path)
    key=(path,stat.st_mtime_ns,stat.st_size,VERSION)
    with _LOCK:
        cached=_CACHE.get(key)
        if cached:_CACHE.move_to_end(key)
    was_cached=cached is not None
    save_note=None
    if cached is None:
        cached=_disk_load(key)
        if cached is not None:
            with _LOCK:
                _CACHE[key]=cached
                while len(_CACHE)>3:_CACHE.popitem(last=False)
    if cached is None:
        slots=_WARM_SLOT if background else _ANALYSIS_SLOTS
        if not slots.acquire(timeout=None if background else 3):
            raise RuntimeError('Two map analyses are in progress. Wait for them to finish and reload.')
        try:
            cached=_analyze(path)
        finally:
            slots.release()
        after=os.stat(path)
        if not cached['retryable'] and (after.st_mtime_ns,after.st_size)==(stat.st_mtime_ns,stat.st_size):
            with _LOCK:
                _CACHE[key]=cached
                while len(_CACHE)>3:_CACHE.popitem(last=False)
            save_note=_disk_save(key,cached)
    data=copy.deepcopy(cached)
    if save_note:data['diagnostics']=[*data.get('diagnostics',[]),{'path':'','reason':save_note}]
    # Só compara metadados do diretório de trabalho quando a visão é a atual.
    for file in data['files']:
        file['status']='stored'
        if inventory['selected']['current'] and file.get('mtime_ns'):
            candidate=os.path.join(root,file['path'])
            if project_identity.within_root(root,candidate):
                try:
                    current=os.stat(candidate)
                    file['status']='indexed' if (current.st_mtime_ns,current.st_size)==(file['mtime_ns'],file['size']) else 'pending'
                except OSError:file['status']='missing'
        file.pop('mtime_ns',None)
    data.update(views=inventory['views'],selected=inventory['selected'],current_view=inventory['current_view'],
                message=inventory['message'],read_only=True,source='indexed_snapshot',generated_at=time.time(),
                limits={'files':MAX_FILES,'symbols':MAX_SYMBOLS,'edges':MAX_EDGES,'display_nodes_default':120},
                languages=['Java','Angular','Python','JavaScript','TypeScript','JSX','TSX','HTML','CSS','Markdown'],cache_hit=was_cached)
    return _focus(data,file_path) if file_path else data


def working_tree_changes(root):
    """Project-relative paths changed in the git working tree (staged, unstaged, untracked) -> 'deleted' or
    'changed'; empty when the folder is not in a git repository."""
    def git(*args):
        return subprocess.run(['git',*args],cwd=root,capture_output=True,timeout=30,
                              creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    prefix=git('rev-parse','--show-prefix')
    if prefix.returncode:
        return {}
    prefix=prefix.stdout.decode('utf-8','replace').strip()
    status=git('status','--porcelain=v1','-z','--untracked-files=all','--','.')
    if status.returncode:
        raise RuntimeError('git status failed: '+status.stderr.decode('utf-8','replace').strip()[:200])
    entries=status.stdout.decode('utf-8','replace').split('\0');changes={};i=0
    while i<len(entries):
        entry=entries[i];i+=1
        if len(entry)<4:continue
        code,path=entry[:2],entry[3:]
        if 'R' in code or 'C' in code:i+=1
        if path.startswith(prefix):changes[path[len(prefix):]]='deleted' if 'D' in code else 'changed'
    return changes


def pending_changes(root,view_path):
    """Files the indexed view does not reflect yet: working-tree changes, indexed files whose size or modification time
    differs on disk, and files tracked by git inside the index scope that were never indexed (commits made after the
    last indexing). Path -> 'deleted' or 'changed'."""
    changes=working_tree_changes(root)
    conn=indexer._readonly(view_path)
    try:
        columns={row[1] for row in conn.execute('PRAGMA table_info(manifest)')}
        if not {'size','mtime_ns'} <= columns:
            raise RuntimeError('Index without file sizes and times; reindex the project.')
        rows=conn.execute('SELECT path,size,mtime_ns FROM manifest').fetchall()
    finally:
        conn.close()
    indexed=set()
    for original,size,stamp in rows:
        rel=original.replace('\\','/');indexed.add(rel)
        if rel in changes:continue
        try:
            stat=os.stat(os.path.join(root,rel))
        except OSError:
            changes[rel]='deleted';continue
        if (stat.st_size,stat.st_mtime_ns)!=(size,stamp):changes[rel]='changed'
    scope=index_scope.load_scope(root)
    listed=subprocess.run(['git','ls-files','-z'],cwd=root,capture_output=True,timeout=30,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if scope and listed.returncode==0:
        for rel in listed.stdout.decode('utf-8','replace').split('\0'):
            if rel and rel not in indexed and rel not in changes and rel.lower().endswith(SOURCE_EXTENSIONS) \
                    and index_scope.in_scope(scope,rel) and os.path.isfile(os.path.join(root,rel)):
                changes[rel]='changed'
    return changes


def build_current(root,view_id=None):
    """The graph an agent should see while editing: the indexed view plus every file it does not reflect yet (see
    pending_changes and build_overlay) when that view is the current one; a pinned or older view is returned as
    indexed."""
    inventory=index_inventory.inspect(root,view_id)
    selected=inventory.get('selected') or {}
    changes=pending_changes(root,selected['path']) if selected.get('current') else {}
    return build_overlay(root,changes,view_id) if changes else build(root,view_id)


def build_overlay(root,changes,view_id=None):
    """Graph of the indexed view with the given changed files read from the working tree instead of the snapshot
    (changes: project-relative path -> 'deleted' or any other status), so a caller right after an edit sees new files,
    new imports and current line numbers without reindexing or embeddings. A deleted file keeps its indexed copy so
    the files that imported it stay linked to it. Cached per snapshot and file contents."""
    inventory=index_inventory.inspect(root,view_id)
    if not inventory['selected']:
        return {**inventory,'symbols':[],'dependencies':[],'calls':[],'diagnostics':[],'counts':{},'files':[]}
    path=inventory['selected']['path'];stat=os.stat(path)
    files,sources,diagnostics,truncated=_snapshot(path)
    by_path={f['path']:f for f in files};overlaid=[]
    for rel,status in sorted(changes.items()):
        full=os.path.join(root,rel)
        if status=='deleted' or not project_identity.within_root(root,full):
            continue
        try:
            if os.path.getsize(full)>MAX_OVERLAY_BYTES:continue
            with open(full,encoding='utf-8') as stream:text=stream.read()
        except (OSError,UnicodeDecodeError):continue
        by_path.setdefault(rel,{'id':rel,'path':rel,'hash':None,'size':len(text),'mtime_ns':None,'chunks':0,
                                'lines':text.count('\n')+1,'group':posixpath.dirname(rel) or '(root)',
                                'location_kind':document_text.location_kind(rel),
                                'format':posixpath.splitext(rel)[1].lower().lstrip('.') or 'text'})
        if rel.lower().endswith(SOURCE_EXTENSIONS):
            sources[rel]=text;overlaid.append((rel,hashlib.sha1(text.encode('utf-8')).hexdigest()))
    key=(path,stat.st_mtime_ns,stat.st_size,VERSION,'overlay',tuple(overlaid))
    with _LOCK:
        cached=_CACHE.get(key)
    if cached is None:
        if not _ANALYSIS_SLOTS.acquire(timeout=3):
            raise RuntimeError('Two map analyses are in progress. Wait for them to finish and retry.')
        try:
            cached=_analyze_sources(list(by_path.values()),sources,diagnostics,truncated)
        finally:
            _ANALYSIS_SLOTS.release()
        if not cached['retryable']:
            with _LOCK:
                _CACHE[key]=cached
                while len(_CACHE)>3:_CACHE.popitem(last=False)
    data=copy.deepcopy(cached)
    data.update(selected=inventory['selected'],source='indexed_snapshot_with_working_tree',overlaid=[r for r,_h in overlaid])
    return data
