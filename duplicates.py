import array
import contextvars
import hashlib
import heapq
import math
import os
import re
import threading
import time

import code_graph
import embedding_cache
import index_inventory
import index_profile
import index_scope
import indexer

DEFAULT_MIN_SIMILARITY = 0.90
NEAR_JACCARD = 0.80
MIN_LINES = 5
MAX_TEXT = 3000
MAX_SEMANTIC_FUNCTIONS = 4000
SEMANTIC_DEADLINE_S = 45
EMBED_SLICE = 64
CUTOFF_NOTE = ("Default semantic cutoff 0.90: in the 2026-10-02 measurement (claude-code, independent judge) pairs above "
               "0.95 were 10/10 actionable and those from 0.80 to 0.94 only 7/20; below 0.90 most are false positives "
               "and reviewing them costs time without leading to a refactor.")
_HASH_COMMENT = {"py", "rb", "sh"}
_TOKEN = re.compile(r"[A-Za-z_]\w*|\d+|[^\s\w]")
_CONTAINER_KINDS = {"module", "template", "class", "interface"}
_GENERIC_NAMES = {"callback", "anonymous", "constructor"}
_WARMING = set()
_WARM_ERRORS = {}
LAST_FINDINGS = {}
MAX_REMEMBERED_FINDINGS = 20000
NOTE_MAX = 500
TYPE_BY_PREFIX = {"e": "exact", "n": "near", "s": "semantic"}
DISMISS_REASONS = {"false_positive", "intentional"}
_LOCK = threading.Lock()


def _strip_comments(text, ext):
    hash_comments = ext in _HASH_COMMENT
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1]); i = j + 1
        elif hash_comments and ch == "#":
            while i < n and text[i] != "\n":
                i += 1
        elif not hash_comments and text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif not hash_comments and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(ch); i += 1
    return "".join(out)


def _body(text, ext):
    if ext in _HASH_COMMENT:
        lines = text.splitlines()
        start = next((k for k, line in enumerate(lines) if re.match(r"\s*(async\s+)?def\s", line)), 0)
        end = next((k for k in range(start, len(lines)) if lines[k].rstrip().endswith(":")), start)
        return "\n".join(lines[end + 1:])
    brace = text.find("{")
    return text[brace + 1:] if brace >= 0 else text


def _shingles(text):
    tokens = ["ID" if re.match(r"[A-Za-z_]", t) else t for t in _TOKEN.findall(text)]
    return frozenset(tuple(tokens[i:i + 5]) for i in range(max(len(tokens) - 4, 1)))


def _index_state(path):
    stat = os.stat(path)
    return stat.st_mtime_ns, stat.st_size


def _functions(root, include_tests):
    inventory = index_inventory.inspect(root)
    if not inventory["selected"]:
        raise ValueError("Project not indexed in this view yet; run a search or index it first.")
    view = inventory["selected"]
    before = _index_state(view["path"])
    graph = code_graph.build(root, storage_id=os.path.basename(view["path"]))
    _files, sources, _diagnostics, _truncated = code_graph._snapshot(view["path"])
    if _index_state(view["path"]) != before:
        raise RuntimeError("The index changed during the analysis (reindex in progress); try again shortly.")
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    candidates = [s for s in graph["symbols"] if s.get("kind") not in _CONTAINER_KINDS
                  and s["end_line"] - s["start_line"] + 1 >= MIN_LINES
                  and s["name"].split("(")[0].split(".")[-1] not in _GENERIC_NAMES and "@" not in s["name"]]
    by_file = {}
    for s in candidates:
        by_file.setdefault(s["path"], []).append(s)
    functions = []
    for path, symbols in by_file.items():
        kind = index_profile.kind(path, profile)
        if kind == "doc" or (kind == "test" and not include_tests):
            continue
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        file_lines = sources.get(path, "").splitlines()
        occurrences = {}
        for s in sorted(symbols, key=lambda item: (item["start_line"], item["end_line"])):
            if any(o is not s and o["start_line"] <= s["start_line"] and s["end_line"] <= o["end_line"]
                   and (o["start_line"], o["end_line"]) != (s["start_line"], s["end_line"]) for o in symbols):
                continue
            text = "\n".join(file_lines[s["start_line"] - 1:s["end_line"]])
            if not text.strip():
                continue
            normalized = re.sub(r"\s+", " ", _strip_comments(_body(text, ext), ext)).strip()
            functions.append({"name": s["name"].split("(")[0].split(".")[-1], "path": path, "line": s["start_line"],
                              "end": s["end_line"], "lines": s["end_line"] - s["start_line"] + 1, "text": text[:MAX_TEXT],
                              "hash": hashlib.sha1(normalized.encode("utf-8")).hexdigest(), "shingles": _shingles(normalized)})
            occurrence = occurrences[functions[-1]["hash"]] = occurrences.get(functions[-1]["hash"], 0) + 1
            functions[-1]["kind"] = kind
            functions[-1]["fp"] = hashlib.sha1(f"{path}\0{functions[-1]['hash']}\0{occurrence}".encode("utf-8")).hexdigest()
    notes = []
    if inventory.get("message"):
        notes.append(inventory["message"])
    if graph.get("truncated"):
        notes.append("Graph truncated by analysis limits; some functions were left out.")
    if graph.get("retryable"):
        notes.append("Partial analysis: " + "; ".join(d.get("reason", "") for d in graph.get("diagnostics", [])[:2]))
    return functions, notes, view


def _ref(f):
    return {"name": f["name"], "path": f["path"], "lines": f"{f['line']}-{f['end']}"}


def _finding_id(prefix, members):
    production = [f for f in members if f["kind"] != "test"]
    basis = production if len(production) >= 2 else members
    return prefix + hashlib.sha1("|".join(sorted(f["fp"] for f in basis)).encode("utf-8")).hexdigest()[:16]


def _cache(root, view):
    if not view.get("model") or not view.get("dimensions"):
        raise ValueError("Index has no registered embedding model; reindex the project.")
    return embedding_cache.VectorCache(root, indexer.INDEX_DIR, {"purpose": "function_duplicates", "model": view["model"],
                                                                 "dimensions": view["dimensions"]})


def _cached_vectors(root, view, functions):
    cache = _cache(root, view)
    try:
        vectors, missing = [], 0
        for f in functions:
            row = cache.db.execute("SELECT vector FROM vectors WHERE key=?", (cache.key(f["text"]),)).fetchone()
            if row is None:
                missing += 1
                continue
            vector = array.array("f"); vector.frombytes(row[0])
            norm = math.sqrt(indexer._sumprod(vector, vector))
            vectors.append(array.array("f", (x / norm for x in vector)) if norm else None)
        return (vectors if not missing else None), missing
    finally:
        cache.close()


def _warm(root, view, functions, embed):
    try:
        cache = _cache(root, view)
        try:
            for start in range(0, len(functions), EMBED_SLICE):
                cache.embed([f["text"] for f in functions[start:start + EMBED_SLICE]],
                            lambda texts: embed(view["model"], texts), indexer.validate_vector)
        finally:
            cache.close()
    except Exception as exc:
        with _LOCK:
            _WARM_ERRORS[root] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        with _LOCK:
            _WARMING.discard(root)


def _near(functions):
    order = sorted(range(len(functions)), key=lambda i: len(functions[i]["shingles"]))
    found = []
    for position, a in enumerate(order):
        sa = functions[a]["shingles"]
        for b in order[position + 1:]:
            sb = functions[b]["shingles"]
            if len(sb) * NEAR_JACCARD > len(sa):
                break
            if functions[a]["hash"] == functions[b]["hash"]:
                continue
            inter = len(sa & sb)
            score = inter / (len(sa) + len(sb) - inter)
            if score >= NEAR_JACCARD:
                found.append((score, a, b))
    found.sort(reverse=True)
    return found


def _semantic(root, view, functions, embed, configured_model, min_similarity, limit, lexical, skip):
    if embed is None or not configured_model:
        return "unavailable", [], 0, ["Semantic level unavailable: no embedding_model configured."]
    if configured_model != view.get("model"):
        return "unavailable", [], 0, [f"Semantic level unavailable: the configured model ({configured_model}) differs from the "
                                      f"index model ({view.get('model')}); reindex the project to compare vectors from the same model."]
    pool = sorted(range(len(functions)), key=lambda i: -functions[i]["lines"])[:MAX_SEMANTIC_FUNCTIONS]
    notes = []
    if len(functions) > MAX_SEMANTIC_FUNCTIONS:
        notes.append(f"Semantic level limited to the {MAX_SEMANTIC_FUNCTIONS} largest of {len(functions)} functions.")
    chosen = [functions[i] for i in pool]
    with _LOCK:
        error = _WARM_ERRORS.pop(root, None)
    vectors, missing = _cached_vectors(root, view, chosen)
    if vectors is None:
        with _LOCK:
            start = root not in _WARMING
            if start:
                _WARMING.add(root)
        if start:
            context = contextvars.copy_context()
            threading.Thread(target=context.run, args=(_warm, root, view, chosen, embed), daemon=True).start()
        pending = f"{missing} functions without embedding" if missing else "preparation in progress"
        notes.append(f"Semantic level being prepared ({pending}); available on the next call."
                     + (f" The last preparation failed: {error}" if error else ""))
        return "preparing", [], 0, notes
    heap, total, deadline = [], 0, time.monotonic() + SEMANTIC_DEADLINE_S
    for a in range(len(chosen)):
        if time.monotonic() > deadline:
            notes.append(f"Semantic comparison stopped at the {SEMANTIC_DEADLINE_S} s deadline; partial result.")
            break
        va, fa = vectors[a], chosen[a]
        if va is None:
            continue
        for b in range(a + 1, len(chosen)):
            vb, fb = vectors[b], chosen[b]
            if vb is None or fa["hash"] == fb["hash"] or (pool[a], pool[b]) in lexical or (pool[b], pool[a]) in lexical:
                continue
            score = indexer._sumprod(va, vb)
            if score >= min_similarity:
                if _finding_id("s", (fa, fb)) in skip:
                    skip[_finding_id("s", (fa, fb))] = (score, fa, fb)
                    continue
                total += 1
                item = (score, pool[a], pool[b])
                if len(heap) < limit:
                    heapq.heappush(heap, item)
                elif item > heap[0]:
                    heapq.heapreplace(heap, item)
    return "ready", sorted(heap, reverse=True), total, notes


def find(root, embed, configured_model, min_similarity=DEFAULT_MIN_SIMILARITY, include_tests=False, limit=30,
         dismissed=None, include_dismissed=False):
    started = time.monotonic()
    dismissed = dismissed or {}
    functions, notes, view = _functions(root, include_tests)
    groups = {}
    for f in functions:
        groups.setdefault(f["hash"], []).append(f)
    exact_all = sorted((g for g in groups.values() if len(g) > 1), key=lambda g: (-g[0]["lines"], -len(g)))
    near_all = _near(functions)
    lexical = {(a, b) for _s, a, b in near_all}
    hidden = []
    exact = []
    for g in exact_all:
        fid = _finding_id("e", g)
        (hidden.append(("exact", fid, None, g)) if fid in dismissed else exact.append((fid, g)))
    near = []
    for s, a, b in near_all:
        fid = _finding_id("n", (functions[a], functions[b]))
        (hidden.append(("near", fid, s, (functions[a], functions[b]))) if fid in dismissed else near.append((fid, s, a, b)))
    skip = {fid: None for fid in dismissed if fid.startswith("s")}
    try:
        status, semantic, semantic_total, semantic_notes = _semantic(root, view, functions, embed, configured_model,
                                                                     min_similarity, limit, lexical, skip)
    except Exception as exc:
        status, semantic, semantic_total, semantic_notes = "error", [], 0, [f"Semantic level failed: {type(exc).__name__}: {exc}"[:300]]
    notes += semantic_notes
    if min_similarity < DEFAULT_MIN_SIMILARITY:
        notes.append(f"Semantic cutoff below the default ({min_similarity:.2f}): expect many false positives.")
    semantic_rows = [(_finding_id("s", (functions[a], functions[b])), s, a, b) for s, a, b in semantic]
    hidden_semantic = [fid for fid, score in skip.items() if score is not None]
    with _LOCK:
        remembered = LAST_FINDINGS.setdefault(root, {})
        remembered.update({fid: "exact" for fid, _g in exact})
        remembered.update({fid: "near" for fid, *_r in near})
        remembered.update({fid: "semantic" for fid, *_r in semantic_rows})
        remembered.update({fid: kind for kind, fid, *_r in hidden})
        remembered.update({fid: "semantic" for fid in hidden_semantic})
        while len(remembered) > MAX_REMEMBERED_FINDINGS:
            remembered.pop(next(iter(remembered)))
    result = {
        "view": view["label"], "functions_analyzed": len(functions), "include_tests": include_tests,
        "min_similarity": min_similarity, "cutoff_note": CUTOFF_NOTE, "semantic_status": status, "notes": notes,
        "counts": {"exact_groups": len(exact), "near_pairs": len(near), "semantic_pairs": semantic_total,
                   "dismissed_hidden": len(hidden) + len(hidden_semantic)},
        "exact": [{"id": fid, "lines": g[0]["lines"], "copies": [_ref(f) for f in g]} for fid, g in exact[:limit]],
        "near": [{"id": fid, "score": round(s, 3), "a": _ref(functions[a]), "b": _ref(functions[b])} for fid, s, a, b in near[:limit]],
        "semantic": [{"id": fid, "score": round(s, 3), "a": _ref(functions[a]), "b": _ref(functions[b])} for fid, s, a, b in semantic_rows],
        "elapsed_s": round(time.monotonic() - started, 1),
    }
    if include_dismissed:
        result["dismissed"] = ([{"id": fid, "type": kind, **({"score": round(s, 3)} if s is not None else {}),
                                 "members": [_ref(f) for f in members], **dismissed[fid]} for kind, fid, s, members in hidden]
                               + [{"id": fid, "type": "semantic", "score": round(skip[fid][0], 3),
                                   "members": [_ref(skip[fid][1]), _ref(skip[fid][2])], **dismissed[fid]}
                                  for fid in hidden_semantic])
    return result
