"""Inventário somente leitura de visões, arquivos e trechos já persistidos."""
import re
from pathlib import Path

import indexer
import index_views
import project_identity
import document_text


def views(root):
    key = project_identity.project_id(root)
    current = index_views.describe(root)
    paths = list(Path(indexer.INDEX_DIR).glob(key + '.v-*.sqlite3'))
    paths += [Path(indexer.INDEX_DIR)/(item+'.sqlite3') for item in
              dict.fromkeys([key, *project_identity.legacy_ids(root)])]
    rows = []
    for path in dict.fromkeys(paths):
        if not path.is_file() or not re.fullmatch(r'[a-f0-9]{16}(?:\.v-[a-f0-9]{16})?\.sqlite3', path.name):
            continue
        conn = indexer._readonly(str(path))
        try:
            meta = indexer._metadata(conn)
            view_id = meta.get('view_id') or ('legacy' if current['git'] else 'workspace')
            rows.append({'view_id': view_id, 'label': meta.get('view_label') or 'Legacy, no identified branch',
                         'branch': meta.get('git_branch'), 'commit': meta.get('git_commit'),
                         'current': view_id == current['view_id'], 'path': str(path),
                         'files': conn.execute('SELECT count(*) FROM manifest').fetchone()[0],
                         'chunks': conn.execute('SELECT count(*) FROM chunks').fetchone()[0],
                         'model': meta.get('model_id'), 'dimensions': meta.get('dimensions'),
                         'updated_at': meta.get('updated_at'), 'bytes': path.stat().st_size})
        finally:
            conn.close()
    return sorted(rows, key=lambda row: (not row['current'], row['label']))


def inspect(root, view_id=None, file_path=None, storage_id=None):
    if file_path is not None:
        if not isinstance(file_path, str):
            raise ValueError("file_path must be a string.")
        file_path = file_path.replace(chr(92), "/").removeprefix("./")
    available = views(root)
    current = index_views.describe(root)
    explicit_view = view_id is not None or storage_id is not None
    view_id = view_id or current['view_id']
    selected = next((row for row in available if (Path(row['path']).name==storage_id if storage_id else row['view_id']==view_id)), None)
    if selected is None and available and not explicit_view:
        selected = available[0]
    if not selected:
        return {'views': available, 'selected': None, 'files': [], 'chunks': [],
                'message': 'This view has not been indexed yet. Run a search or use Index now.'}
    conn = indexer._readonly(selected['path'])
    try:
        columns = {row[1] for row in conn.execute('PRAGMA table_info(manifest)')}
        # Índices anteriores ao manifest incremental não têm size/mtime_ns.
        # A inspeção é somente leitura; não migrar nem medir o arquivo de outra branch.
        size_column = 'm.size' if 'size' in columns else 'NULL'
        files = [{'path': path, 'chunks': count, 'hash': digest, 'size': size} for path, count, digest, size in
                 conn.execute(f'SELECT m.path,count(c.id),m.hash,{size_column} FROM manifest m LEFT JOIN chunks c ON c.path=m.path GROUP BY m.path ORDER BY m.path LIMIT 1001')]
        chunks = []
        if file_path is not None:
            if not conn.execute('SELECT 1 FROM manifest WHERE path=?', (file_path,)).fetchone():
                raise ValueError(f'File is outside the indexed view: {file_path}')
            files = []
            chunks = [{'start_line': start, 'end_line': end, 'text': text, 'dimensions': int(size or 0)//4,
                       'location_kind':document_text.location_kind(file_path)}
                      for start, end, text, size in conn.execute(
                          'SELECT start_line,end_line,text,length(embedding) FROM chunks WHERE path=? ORDER BY start_line LIMIT 100', (file_path,))]
        return {'views': available, 'selected': selected, 'files': files[:1000],
                'truncated': len(files)>1000, 'chunks': chunks, 'max_chunks_displayed': 100,
                'current_view': index_views.public(current),
                'message': '' if selected['current'] else 'The current view is not shown. You are inspecting the stored index of ' + selected['label'] + '.',
                'graph_type': 'project_branch_directory_file', 'read_only': True}
    finally:
        conn.close()
