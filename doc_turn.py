"""End-of-turn documentation step, run by the Claude Code Stop hook (hooks/turn_end.py, a `command` hook) through the
daemon route /hooks/stop, outside the search executor.

Flow: a continuation (stop_hook_active) or a folder without a generated docs/scope exits at once; otherwise the files
the turn touched (noted by the PreToolUse of Edit, MultiEdit and Write) and the documented files whose size or mtime
changed (subagents and shell writes) are re-stamped against the page markers in the registry (doc_registry), at most
MAX_FUNCTIONS pending functions and MAX_NODES chain nodes are claimed, the feature links are recorded (the graph's,
plus embedder candidates for public functions no entry point reaches, never confirmed by the embedder), the pages are
regenerated without a model (only pages whose text changes are written) and the registry is settled. Walking the
chain back, the written functional and simple paragraphs of the features those functions belong to whose flow stamp
changed are rewritten by the docs_model, within the same node budget. When a public function's body changed and its
docstring did not, the hook blocks once with the short list the session agent must review: the Smart Tool never
writes the user's code. What could not be done (no Docs model, a rejected paragraph, the embeddings home down) reaches
the user as a systemMessage, never silently.
"""
import os
import threading
from collections import defaultdict

import doc_candidates
import doc_check
import doc_prose
import doc_registry
import doc_scope

MAX_FUNCTIONS = 20
MAX_NODES = 60
PROSE_BUDGET_S = 40
ORIGIN = "[hook-origin=smart-tool]"
_TOUCHED = defaultdict(set)
_SEEN_SESSIONS = set()
_LOCK = threading.Lock()


def project_root(path):
    """Nearest folder from path upward whose docs/scope index was written by the generator; None when there is none."""
    current = os.path.abspath(path)
    while True:
        index = os.path.join(current, doc_scope.DOCS_DIR, "README.md")
        if os.path.isfile(index):
            with open(index, encoding="utf-8") as stream:
                if stream.readline().rstrip("\n") == doc_scope.GENERATED:
                    return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def note_tool(payload):
    """PreToolUse: note the file an Edit, MultiEdit or Write touches for the session's next Stop, and on the session's
    first call resume the documentation work a stopped run left behind (doc_registry.recover)."""
    session, cwd = payload.get("session_id") or "", payload.get("cwd") or ""
    tool, given = payload.get("tool_name"), (payload.get("tool_input") or {}).get("file_path")
    with _LOCK:
        first = bool(session) and session not in _SEEN_SESSIONS
        _SEEN_SESSIONS.add(session)
    if tool in ("Edit", "MultiEdit", "Write") and given:
        path = given if os.path.isabs(given) else os.path.join(cwd, given)
        root = project_root(os.path.dirname(path))
        if root:
            with _LOCK:
                _TOUCHED[session].add((root, os.path.relpath(path, root).replace(os.sep, "/")))
    root = project_root(cwd) if first and cwd else None
    if root:
        resume(root)


def resume(root):
    """Return the expired in_progress rows of a project to pending. Returns: int rows recovered."""
    conn = doc_registry.connect()
    try:
        return doc_registry.recover(conn, root)
    finally:
        conn.close()


def resume_all():
    """Daemon start: resume every tracked project that still has its generated documents. Returns: int rows recovered."""
    conn = doc_registry.connect()
    try:
        return sum(doc_registry.recover(conn, root) for root in doc_registry.projects(conn)
                   if os.path.isdir(os.path.join(root, doc_scope.DOCS_DIR)))
    finally:
        conn.close()


def on_stop(payload, view_id=None):
    """Stop hook answer: {} when there is nothing to do; {decision: block, reason} once per turn with the functions whose
    docstring the session agent must check against their new body; a systemMessage with what the step could not do."""
    if payload.get("stop_hook_active"):
        return {}
    cwd = payload.get("cwd") or ""
    root = project_root(cwd) if cwd else None
    if not root:
        return {}
    with _LOCK:
        touched = {rel for owner, rel in _TOUCHED.pop(payload.get("session_id") or "", set()) if owner == root}
    result = run(root, touched, view_id)
    answer = {}
    if result["review"]:
        lines = [f"- `{item['fn_id']}`: the body changed and the docstring did not"
                 + (f"; features: {', '.join(item['features'])}" if item["features"] else "") for item in result["review"]]
        answer = {"decision": "block",
                  "reason": "Smart Tool documentation: check each docstring below against the function's new body and fix "
                            "it in the code if it no longer describes it (the technical pages were already updated):\n"
                            + "\n".join(lines)}
    if result["notes"]:
        answer["systemMessage"] = f"{ORIGIN} documentation step: " + " ".join(result["notes"])
    return answer


def _functions_now(root, paths, by_path, public):
    """Stamps of the functions of paths on disk: fn_id -> {path, public, has_docstring, signature, body, docstring}."""
    return {fn_id: {"path": path, "public": fn_id in public, "has_docstring": function["documented"],
                    **doc_scope.function_stamps(source, function)}
            for path, source, function, fn_id in doc_scope.disk_functions(
                root, [p for p in paths if doc_check.supported(p)], by_path)}


def run(root, touched=(), view_id=None):
    """One end-of-turn pass over a project (see the module docstring).

    Parameters: root (str) project with a generated docs/scope; touched (iterable of project-relative paths).
    Returns: dict with claimed (fn_id list), settled, still_pending, review (fn_id, features) lists, candidates (new
    candidate rows), prose (doc_prose.rewrite_stale result or None) and notes (what could not be done, for the user).
    """
    conn = doc_registry.connect()
    try:
        doc_registry.recover(conn, root)
        technical = {m["id"]: m for m in doc_scope._scan(root)[0] if m["file"].startswith("technical/")}
        changed = doc_registry.changed_files(conn, root, {f.rpartition("::")[0] for f in technical} | set(touched))
        model = doc_scope._Model(root, view_id)
        by_path = defaultdict(list)
        for symbol in model.graph.get("symbols") or []:
            by_path[symbol["path"]].append(symbol)
        public = set(model.public())
        now = _functions_now(root, [p for p, state in changed.items() if state], by_path, public)
        gone = [f for f in technical if f.rpartition("::")[0] in changed and f not in now]
        doc_registry.observe(conn, root, now, technical, changed, gone)
        features, notes = doc_scope.features(model), []
        candidates, moved = _candidates(model, features, doc_registry.links(conn, root), notes)
        doc_registry.record_links(conn, root, doc_scope.feature_links(model) + candidates)
        claimed = doc_registry.claim(conn, root, MAX_FUNCTIONS, cost=lambda f: 1 + len(features.get(f, ())),
                                     budget=MAX_NODES)
        if not claimed and not moved:
            return {"claimed": [], "settled": [], "still_pending": [], "review": [], "candidates": [], "prose": None,
                    "notes": notes}
        before = {row["fn_id"]: row for row in doc_registry.rows(conn, root) if row["fn_id"] in claimed}
        try:
            doc_scope.generate(root, view_id, model=model)
        except Exception as exc:
            doc_registry.settle(conn, root, {f: (None, f"{type(exc).__name__}: {exc}") for f in claimed})
            raise
        after = {m["id"]: m for m in doc_scope._scan(root)[0] if m["file"].startswith("technical/")}
        current = doc_scope._current_code(root, {f.rpartition("::")[0] for f in claimed}, by_path)
        results = {f: (current[f], None) if f in current and f in after and not doc_scope.changed_parts(after[f], current[f])
                   else (None, "the regenerated technical entry still differs from the code") for f in claimed}
        doc_registry.settle(conn, root, results)
        prose = _rewrite_prose(root, model, claimed, features, notes)
        entries = model.entry_points()
        review = [{"fn_id": f, "features": [doc_scope.entry_title(model, e, entries) for e, _h, _o in features.get(f, ())]}
                  for f, row in before.items() if row["public"] and row["doc_body_hash"]
                  and row["doc_body_hash"] != row["body_hash"] and row["doc_docstring_hash"] == row["docstring_hash"]]
        return {"claimed": claimed, "settled": [f for f, (s, _e) in results.items() if s],
                "still_pending": [f for f, (s, _e) in results.items() if not s], "review": review,
                "candidates": [c for c in candidates if c.get("computed_at") is None], "prose": prose, "notes": notes}
    finally:
        conn.close()


def _candidates(model, features, known, notes):
    """Embedder candidate rows to keep: those still about an orphan function and an existing feature, plus new ones for
    the orphans that have none. Returns (rows, whether the set shown on the pages changed)."""
    orphans, entries = set(doc_candidates.orphans(model, features)), set(model.entry_points())
    old = [row for row in known if row["origin"] == "embedder"]
    kept = [row for row in old if row["fn_id"] in orphans and row["feature_id"] in entries]
    try:
        new = doc_candidates.propose(model, features, {row["fn_id"] for row in kept})
    except Exception as exc:
        notes.append(f"no feature candidate was proposed ({type(exc).__name__}: {exc}).")
        new = []
    return kept + new, bool(new) or len(kept) != len(old)


def _rewrite_prose(root, model, claimed, features, notes):
    """Rewrite the stale paragraphs of the features the claimed functions belong to, as many features as the node
    budget left by the claimed functions allows. Returns doc_prose.rewrite_stale's result, or None when nothing was
    stale or no Docs model is configured (said in notes)."""
    spent = sum(1 + len(features.get(f, ())) for f in claimed)
    affected = list(dict.fromkeys(entry for f in claimed for entry, _h, _o in features.get(f, ())))
    affected = affected[:max(0, MAX_NODES - spent)]
    pages = doc_scope._read_pages(root, ("functional.md", "simple.md"))
    if not affected or not doc_scope.stale_prose(pages, {a: doc_scope.flow_stamp(model, a) for a in affected}):
        return None
    try:
        result = doc_prose.rewrite_stale(root, model, affected, PROSE_BUDGET_S)
    except ValueError as exc:
        notes.append(str(exc))
        return None
    if result["failed"]:
        notes.append(f"{len(result['failed'])} stale paragraph(s) kept their old text: "
                     + "; ".join(f"{item['block']} in {item['file']}: {item['problem']}" for item in result["failed"][:3])
                     + ".")
    return result
