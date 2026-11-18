"""Reúso de vetores por texto exato e contrato; não guarda texto nem faz rede."""
import array
import concurrent.futures
import contextvars
import hashlib
import json
import math
import os
import sqlite3
import time

import project_identity


def _open(root, directory):
    os.makedirs(directory, exist_ok=True)
    db = sqlite3.connect(os.path.join(directory, project_identity.project_id(root) + '.vectors.sqlite3'), timeout=10)
    db.execute('PRAGMA secure_delete=ON')
    db.execute('CREATE TABLE IF NOT EXISTS vectors(key TEXT PRIMARY KEY, vector BLOB NOT NULL, used_at REAL NOT NULL, '
               'model TEXT)')
    if 'model' not in {row[1] for row in db.execute('PRAGMA table_info(vectors)')}:
        db.execute('ALTER TABLE vectors ADD COLUMN model TEXT')
    return db


def drop_other_models(root, directory, model):
    """Deletes the cached vectors of every model but `model` and gives their space back to the disk. Rows written
    before the cache recorded the model are deleted too, unless a build of `model` reused them since."""
    db = _open(root, directory)
    try:
        dropped = db.execute('DELETE FROM vectors WHERE model IS NOT ?', (model,)).rowcount
        db.commit()
        if dropped:
            db.execute('VACUUM')
        return dropped
    finally:
        db.close()


class VectorCache:
    def __init__(self, root, directory, contract):
        self.db = _open(root, directory)
        self.model = contract.get('model')
        self.contract = json.dumps(contract, sort_keys=True, ensure_ascii=False)
        self.dimensions = contract.get('dimensions')
        self.hits = self.misses = self.characters = 0

    def key(self, text):
        return hashlib.sha256((self.contract + '\0' + text).encode('utf-8')).hexdigest()

    def put(self, text, vector):
        if not self.dimensions or len(vector) != self.dimensions:
            return
        packed = array.array('f', vector)
        if not all(math.isfinite(value) for value in packed):
            raise ValueError('Embedding does not fit in float32; cache not written.')
        self.db.execute('INSERT OR REPLACE INTO vectors VALUES(?,?,?,?)',
                        (self.key(text), packed.tobytes(), time.time(), self.model))

    def embed(self, texts, embed_batch, validate, use_cache=True, split=None, workers=1):
        """Cache lookups and writes stay on the calling thread (sqlite); only embed_batch runs in workers."""
        output = [None] * len(texts)
        missing = {}
        for i, text in enumerate(texts):
            row = self.db.execute('SELECT vector, model FROM vectors WHERE key=?', (self.key(text),)).fetchone() if use_cache else None
            if row and self.dimensions:
                vector = array.array('f'); vector.frombytes(row[0])
                validate(vector, self.dimensions)
                if row[1] is None:
                    self.db.execute('UPDATE vectors SET model=? WHERE key=?', (self.model, self.key(text)))
                output[i] = list(vector)
                self.hits += 1
            else:
                missing.setdefault(text, []).append(i)
        if missing:
            unique = list(missing)
            batches = split(unique) if split else [unique]
            dimensions = [self.dimensions]

            def store(part, vectors):
                # Each batch is cached as soon as it returns: a later failure never discards paid embeddings.
                if len(vectors) != len(part):
                    raise ValueError('Vector count differs from the chunks sent.')
                dimensions[0] = dimensions[0] or (len(vectors[0]) if vectors else None)
                for vector in vectors:
                    validate(vector, dimensions[0])
                for text, vector in zip(part, vectors):
                    self.put(text, vector)
                    positions = missing[text]
                    for i in positions:
                        output[i] = vector
                    self.hits += len(positions) - 1
                self.misses += len(part)
                self.characters += sum(map(len, part))
                self.db.commit()

            if workers > 1 and len(batches) > 1:
                pool = concurrent.futures.ThreadPoolExecutor(min(workers, len(batches)))
                futures = {pool.submit(contextvars.copy_context().run, embed_batch, part): part for part in batches}
                error = None
                try:
                    for future in concurrent.futures.as_completed(futures):
                        if future.cancelled():
                            continue
                        try:
                            store(futures[future], list(future.result()))
                        except Exception as exc:
                            if error is None:
                                error = exc
                                for other in futures:
                                    other.cancel()
                finally:
                    pool.shutdown(wait=True, cancel_futures=True)
                if error is not None:
                    raise error
            else:
                for part in batches:
                    store(part, list(embed_batch(part)))
        return output

    def close(self):
        self.db.commit()
        self.db.close()
