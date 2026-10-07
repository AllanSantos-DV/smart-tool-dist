"""Compara snapshots guardados sem checkout, rede ou novos embeddings."""
import contextlib
import difflib
from pathlib import Path

import code_graph
import index_inventory
import indexer
import project_storage


def _files(path):
    with contextlib.closing(indexer._readonly(path)) as db:
        return dict(db.execute('SELECT path,hash FROM manifest'))


def _text(path,file_path):
    with contextlib.closing(indexer._readonly(path)) as db:
        rows=db.execute('SELECT start_line,end_line,text FROM chunks WHERE path=? ORDER BY start_line,id',(file_path,)).fetchall()
    return code_graph.reconstruct(rows)


def compare(root,left_id,right_id,file_path=None,relations=True):
    views=index_inventory.views(root);by_id={Path(v['path']).name:v for v in views}
    left=by_id.get(left_id);right=by_id.get(right_id)
    if not left or not right or left_id==right_id:raise ValueError('Choose two different views of the same project.')
    stamps={v['path']:project_storage._stamp(v['path']) for v in (left,right)}
    a,b=_files(left['path']),_files(right['path'])
    added=set(b)-set(a);removed=set(a)-set(b)
    old_hash={};new_hash={}
    for name in removed:old_hash.setdefault(a[name],[]).append(name)
    for name in added:new_hash.setdefault(b[name],[]).append(name)
    renamed=[]
    for digest,old in old_hash.items():
        new=new_hash.get(digest,[])
        if len(old)==len(new)==1:
            renamed.append({'from':old[0],'to':new[0]});removed.remove(old[0]);added.remove(new[0])
    changed=sorted(name for name in set(a)&set(b) if a[name]!=b[name])
    result={'left':left,'right':right,'files':{'added':sorted(added),'removed':sorted(removed),'changed':changed,'renamed':renamed},
            'unchanged':sum(a[name]==b[name] for name in set(a)&set(b)),
            'notes':['Comparison of stored indexes. A missing item may come from a different scope, not only from a deletion in Git.'],
            'read_only':True}
    if file_path is not None:
        if file_path not in a and file_path not in b:raise ValueError('File not found in these views.')
        old,error_a=_text(left['path'],file_path) if file_path in a else ('',None)
        new,error_b=_text(right['path'],file_path) if file_path in b else ('',None)
        if error_a or error_b:result['diff_error']=error_a or error_b
        else:
            lines=list(difflib.unified_diff(old.splitlines(),new.splitlines(),fromfile=left['label']+'/'+file_path,tofile=right['label']+'/'+file_path,lineterm='',n=3))
            output='\n'.join(lines[:1000]);result['diff']=output[:128000];result['diff_truncated']=len(lines)>1000 or len(output)>128000
    if relations:
        ga=code_graph.build(root,storage_id=left_id);gb=code_graph.build(root,storage_id=right_id)
        def relations_of(graph):
            nodes={s['id']:s for s in graph['symbols']}
            def key(id):
                n=nodes[id]
                return n['path']+'::'+n['name']+(':'+str(n['start_line']) if n['name']=='callback' else '')
            imports={(e['source'],e['target'],e.get('kind','import')) for e in graph['dependencies'] if e.get('target')}
            calls={(key(e['source']),key(e['target']),e['kind']) for e in graph['calls']}
            return imports,calls
        ai,ac=relations_of(ga);bi,bc=relations_of(gb)
        result['relations']={name:{'added':[list(x) for x in sorted(y-x)[:500]],'removed':[list(z) for z in sorted(x-y)[:500]],
                                  'added_count':len(y-x),'removed_count':len(x-y)} for name,x,y in [('imports',ai,bi),('calls',ac,bc)]}
        result['notes'].append('Named calls are compared by file/name; anonymous callbacks also use their location. Only resolved static relations are compared.')
        result['analysis_partial']=ga['truncated'] or gb['truncated'] or bool(ga['diagnostics'] or gb['diagnostics'])
    if any(project_storage._stamp(path)!=stamp for path,stamp in stamps.items()):
        raise RuntimeError('A view was updated during the comparison. Compare again after the job finishes.')
    return result
