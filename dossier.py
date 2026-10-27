"""Dossier mode of smart_search: a narrow keyword query over the notes a project keeps about its own intent
(docs/research, the backlog, CLAUDE.md and AGENTS.md), answered with ids and one short excerpt per hit, inside a time
budget. It reads straight from disk, uses no index and no model, and never raises on a slow or unreadable file: it
stops at the deadline and says so in `degraded`/`reasons`."""
import json
import os
import re
import time

DEFAULT_TIMEOUT_MS = 800
MAX_TIMEOUT_MS = 5000
DEFAULT_MAX_HITS = 5
MAX_HITS = 10
SNIPPET_CHARS = 160
TITLE_CHARS = 80
MAX_FILE_BYTES = 512 * 1024
BACKLOG_FILES = ("docs/BACKLOG.md", "BACKLOG.md")
INSTRUCTION_FILES = (("CLAUDE.md", "claude_md"), (".claude/CLAUDE.md", "claude_md"), ("AGENTS.md", "agents_md"))
_WORD = re.compile(r"[\w]{3,}", re.UNICODE)
_STOPWORDS = {
    "the", "and", "for", "how", "what", "where", "which", "does", "with", "from", "that", "this", "are", "when", "who",
    "why", "into", "used", "use", "uses", "its", "has", "have", "code", "project", "file", "files",
    "como", "onde", "qual", "quais", "que", "quem", "por", "para", "com", "uma", "dos", "das", "nos", "nas", "pelo",
    "pela", "ele", "ela", "isso", "esse", "essa", "este", "esta", "fica", "ficam", "usa", "usado", "usada", "sobre",
    "entre", "depois", "antes", "quando", "cada", "seu", "sua", "seus", "suas", "não", "sem", "mais", "código",
    "projeto", "arquivo", "arquivos", "decide", "funciona",
}
_UNIT_START = re.compile(r"#{1,6}\s|[-*]\s")


def focus_terms(*texts):
    """Casefolded content words of the texts (3+ characters, common question words dropped), camelCase split, in
    order: what a window is scored by."""
    found = []
    for text in texts:
        spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", text or "")
        for word in _WORD.findall(spaced):
            folded = word.casefold()
            if folded not in _STOPWORDS and folded not in found:
                found.append(folded)
    return found[:24]


def _sources(root):
    """`[(rel_path, kind)]` of the notes to read, newest research first so a timeout drops the oldest ones."""
    found = []
    base = os.path.join(root, "docs", "research")
    for folder, dirs, files in os.walk(base):
        dirs.sort()
        found += [os.path.relpath(os.path.join(folder, name), root).replace(os.sep, "/")
                  for name in files if name.lower().endswith(".md")]
    sources = [(rel, "research") for rel in sorted(found, reverse=True)]
    sources += [(rel, "backlog") for rel in BACKLOG_FILES if os.path.isfile(os.path.join(root, rel))]
    sources += [(rel, kind) for rel, kind in INSTRUCTION_FILES if os.path.isfile(os.path.join(root, rel))]
    return sources


def _units(lines):
    """`[(first_line_number, [lines])]`: a unit starts at a heading or a bullet at column 0 and runs to the next one."""
    units, current = [], None
    for number, line in enumerate(lines, 1):
        if _UNIT_START.match(line) or current is None:
            current = (number, [])
            units.append(current)
        current[1].append(line)
    return units


def _clean(text):
    return re.sub(r"\s+", " ", re.sub(r"[#*`>]+", "", text)).strip()


def _snippet(unit_lines, terms):
    """The text around the first term found in the unit body, `SNIPPET_CHARS` long at most."""
    body = _clean(" ".join(unit_lines))
    low = body.casefold()
    at = min((low.find(term) for term in terms if term in low), default=0)
    start = max(0, at - SNIPPET_CHARS // 4)
    piece = body[start:start + SNIPPET_CHARS].strip()
    return ("…" if start else "") + piece + ("…" if start + SNIPPET_CHARS < len(body) else "")


def _score(unit_lines, terms):
    """Distinct terms in the unit, a term in its first line (the title) counting double."""
    low = " ".join(unit_lines).casefold()
    title = unit_lines[0].casefold()
    return sum((term in low) + (term in title) for term in terms)


def query(root, text, timeout_ms=DEFAULT_TIMEOUT_MS, max_hits=DEFAULT_MAX_HITS):
    """The dossier answer for `text` over the notes of `root`: `{"mode", "degraded", "reasons", "elapsed_ms",
    "scanned", "hits": [{"id", "source", "title", "snippet"}]}`, best hit first. `id` is `path:line` of the unit's
    first line. `degraded` is true when the deadline cut the scan or a file could not be read; `hits` then holds what
    was found until then."""
    started = time.monotonic()
    deadline = started + timeout_ms / 1000
    terms = focus_terms(text)
    reasons, scanned, unreadable, scored = [], 0, [], []
    for rel, kind in _sources(root):
        if time.monotonic() > deadline:
            reasons.append(f"timeout after {timeout_ms} ms")
            break
        scanned += 1
        path = os.path.join(root, rel)
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                unreadable.append(f"{rel}: over {MAX_FILE_BYTES} bytes")
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError as exc:
            unreadable.append(f"{rel}: {type(exc).__name__}")
            continue
        for number, unit in _units(lines):
            score = _score(unit, terms)
            if score:
                scored.append((score, rel, kind, number, unit))
    if unreadable:
        reasons.append("not read: " + "; ".join(unreadable[:5]))
    scored.sort(key=lambda item: -item[0])
    hits = [{"id": f"{rel}:{number}", "source": kind, "title": _clean(unit[0])[:TITLE_CHARS],
             "snippet": _snippet(unit, terms)} for _score_value, rel, kind, number, unit in scored[:max_hits]]
    return {"mode": "dossier", "degraded": bool(reasons), "reasons": reasons,
            "elapsed_ms": round((time.monotonic() - started) * 1000), "scanned": scanned, "hits": hits}


def answer(arguments):
    """The text smart_search returns for `mode=dossier`: the compact JSON of `query`. Raises ValueError for a missing
    folder or text, or a `timeout_ms`/`max_hits` out of range, so the caller sees the error instead of an empty dossier."""
    root = arguments.get("project_root")
    if not isinstance(root, str) or not os.path.isdir(root):
        raise ValueError("smart_search mode=dossier requires 'project_root' pointing to an existing folder")
    text = next((arguments[key] for key in ("query_identifiers", "query_comments", "doc_query")
                 if isinstance(arguments.get(key), str) and arguments[key].strip()), "")
    if not focus_terms(text):
        raise ValueError("smart_search mode=dossier requires 'query_identifiers' (or 'doc_query') with at least one word.")
    timeout_ms = _bounded(arguments, "timeout_ms", DEFAULT_TIMEOUT_MS, 1, MAX_TIMEOUT_MS)
    max_hits = _bounded(arguments, "max_hits", DEFAULT_MAX_HITS, 1, MAX_HITS)
    return json.dumps(query(root, text, timeout_ms, max_hits), ensure_ascii=False, separators=(",", ":"))


def _bounded(arguments, name, default, low, high):
    value = arguments.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}.")
    return value
