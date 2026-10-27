"""smart_scout: answers a question about a project's code with the literal lines that answer it, read from disk.

The heavy model of the session passes the question; the daemon finds the candidates (the index when it is ready, the
keyword scan of the disk when it is not), reads the current lines of each candidate, keeps the window with most query
terms and returns `path:lines` plus those lines verbatim. With a `scout_model` configured, a light model of the Smart
Tool's own gateway also writes a short answer and picks which lines to show; what it writes never replaces the literal
lines, which always come from the file on disk. Without an index the model gets two fixed calls (terms and folders to
scan, then the answer); with an index, one.

Market pattern copied (docs/research/2026-10-24-scout-modelo-leve-no-smart-tool.md): read-only, step limit, return
`path:lines` with the excerpt (Windsurf Fast Context, Relace, Claude Code Explore); the light model is fed only the
excerpts, never an agent prompt, so no agent context is reopened per call.
"""
import os

import document_text
import index_scope
import indexer
from dossier import focus_terms  # noqa: F401  (one definition of a search term, shared with smart_search mode=dossier)

MAX_QUESTION_CHARS = 2000
MAX_TERMS_CHARS = 400
DEFAULT_MAX_FILES = 5
MAX_FILES = 10
WINDOWS_PER_FILE = 2
LEXICAL_WINDOW_LINES = 24
MODEL_WINDOW_LINES = 60
CITED_MAX_LINES = 40
UNCITED_WINDOWS = 2
LITERAL_BUDGET_CHARS = 9000
LINE_MAX_CHARS = 240
ANSWER_MAX_CHARS = 1200
PLAN_MAX_TERMS = 12
PLAN_MAX_FOLDERS = 6
OUTLINE_MAX_DIRS = 300
OUTLINE_MAX_ROOT_FILES = 200
_FIELDS = {"project_root", "question", "terms", "max_files"}

PLAN_SYSTEM = (
    "You plan a keyword scan of a code project that has no search index yet. Given a question and the project's "
    "folders and root files, return the identifier-style terms most likely written in the code that answers it "
    "(function, class, variable, config names; split camelCase is fine; both the code's language and the question's) "
    "and the folders worth scanning (only names from the list; the root files are always scanned). Answer with the JSON object."
)
PLAN_TOOL = {"type": "function", "function": {
    "name": "submit_scan_plan",
    "description": "Terms to look for in file contents and the folders to scan.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["terms", "folders"], "properties": {
        "terms": {"type": "array", "items": {"type": "string"}, "maxItems": PLAN_MAX_TERMS},
        "folders": {"type": "array", "items": {"type": "string"}, "maxItems": PLAN_MAX_FOLDERS}}}}}
ANSWER_SYSTEM = (
    "You answer a question about a code project using ONLY the numbered excerpts provided (each line starts with its "
    "line number). Write at most 5 short lines, citing excerpts as [E1], [E2]. Then cite the excerpts that answer the "
    "question with the narrowest line range inside each one (at most 40 lines). If no excerpt answers it, set found to "
    "false and say what is missing; never guess beyond the excerpts. Answer in the language of the question. "
    "Answer with the JSON object."
)
ANSWER_TOOL = {"type": "function", "function": {
    "name": "submit_scout_answer",
    "description": "Short answer and the excerpt line ranges that support it.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["found", "answer", "citations"],
                   "properties": {
                       "found": {"type": "boolean"},
                       "answer": {"type": "string"},
                       "citations": {"type": "array", "maxItems": MAX_FILES * WINDOWS_PER_FILE, "items": {
                           "type": "object", "additionalProperties": False, "required": ["id", "start_line", "end_line"],
                           "properties": {"id": {"type": "string"}, "start_line": {"type": "integer"},
                                          "end_line": {"type": "integer"}}}}}}}}


def arguments(raw):
    """Validated `{"project_root", "question", "terms", "max_files"}` from the tool call; raises ValueError naming the
    field. `terms` (identifier words the caller already knows) is optional and joins the question in the search."""
    if not isinstance(raw, dict):
        raise ValueError("smart_scout requires its arguments as a JSON object")
    unknown = set(raw) - _FIELDS
    if unknown:
        raise ValueError(f"Unknown fields in smart_scout: {', '.join(sorted(unknown))}. Accepted: {', '.join(sorted(_FIELDS))}.")
    root = raw.get("project_root")
    if not isinstance(root, str) or not os.path.isdir(root):
        raise ValueError("smart_scout requires 'project_root' pointing to an existing folder")
    question = raw.get("question")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise ValueError(f"smart_scout requires 'question' (non-empty text of at most {MAX_QUESTION_CHARS} characters)")
    terms = raw.get("terms")
    if terms is not None and (not isinstance(terms, str) or len(terms) > MAX_TERMS_CHARS):
        raise ValueError(f"terms must be text of at most {MAX_TERMS_CHARS} characters")
    max_files = raw.get("max_files", DEFAULT_MAX_FILES)
    if type(max_files) is not int or not 1 <= max_files <= MAX_FILES:
        raise ValueError(f"max_files must be an integer from 1 to {MAX_FILES}")
    return {"project_root": root, "question": question.strip(), "terms": (terms or "").strip(), "max_files": max_files}


def disk_lines(root, path, start, end):
    """`[(line_number, text)]` of lines `start..end` of the file as it is on disk now, or None when the file cannot be
    read by the index rules (secret, dotfile, too large, binary, gone) or holds no line text (docx)."""
    if document_text.location_kind(path) != "source_line":
        return None
    data, encoding = indexer.read_indexable(root, path)
    if data is None:
        return None
    try:
        lines = data.decode(encoding or "utf-8", "replace").splitlines()
    except LookupError:
        return None
    start = max(1, start)
    end = min(len(lines), end)
    return [(number, lines[number - 1]) for number in range(start, end + 1)] if start <= end else None


def best_window(lines, terms, size):
    """The run of at most `size` consecutive `(number, text)` lines holding most occurrences of `terms` (first run on a
    tie), trimmed of blank edges."""
    if len(lines) <= size:
        window = lines
    else:
        hits = [sum(text.casefold().count(term) for term in terms) for _number, text in lines]
        best, best_at, current = -1, 0, sum(hits[:size])
        for at in range(len(lines) - size + 1):
            if at:
                current += hits[at + size - 1] - hits[at - 1]
            if current > best:
                best, best_at = current, at
        window = lines[best_at:best_at + size]
    while window and not window[0][1].strip():
        window = window[1:]
    while window and not window[-1][1].strip():
        window = window[:-1]
    return window


def order_candidates(blocks, max_files):
    """Search rows of the code, doc and test blocks in reading order: code first, with the best doc and test rows kept
    inside the first `max_files` files when the code rows would crowd them out."""
    code, doc, test = (list(blocks.get(name) or []) for name in ("code", "doc", "test"))
    reserved = [rows[0] for rows in (doc, test) if rows]
    head = code[:max(1, max_files - len(reserved))]
    rest = code[len(head):] + doc[1:] + test[1:]
    return head + reserved + rest


def evidence(root, rows, terms, max_files, window_lines):
    """Evidence items `{id, path, start, end, lines, symbols}` for the first `max_files` files of `rows` (search rows
    `{path, lines: "a-b", snippet, symbols}`), at most WINDOWS_PER_FILE non-overlapping windows per file, each the
    `window_lines` lines of the chunk with most `terms`, read from disk now. A file that cannot be read from disk keeps
    the indexed snippet with its chunk range."""
    items, files = [], []
    for row in rows:
        path = row["path"]
        try:
            start, end = (int(part) for part in str(row["lines"]).split("-", 1))
        except ValueError:
            continue
        if path not in files:
            if len(files) >= max_files:
                continue
            files.append(path)
        mine = [item for item in items if item["path"] == path]
        if len(mine) >= WINDOWS_PER_FILE or any(item["start"] <= end and start <= item["end"] for item in mine):
            continue
        lines = disk_lines(root, path, start, end)
        if lines:
            window = best_window(lines, terms, window_lines)
            if not window:
                continue
            start, end = window[0][0], window[-1][0]
        else:
            window = [(None, text) for text in str(row.get("snippet") or "").splitlines()[:window_lines]]
        items.append({"id": f"E{len(items) + 1}", "path": path, "start": start, "end": end, "lines": window,
                      "symbols": row.get("symbols") or ""})
    return items


def numbered(lines):
    """Lines as `  123| text`, long lines cut at LINE_MAX_CHARS."""
    out = []
    for number, text in lines:
        text = text if len(text) <= LINE_MAX_CHARS else text[:LINE_MAX_CHARS] + " …"
        out.append(f"{number:>6}| {text}" if number is not None else f"      | {text}")
    return "\n".join(out)


def outline(root):
    """`(folders, root_files)` shown to the plan call: folders up to two levels deep and loose files at the root, with
    the index's mechanical exclusions (dependency caches, hidden folders, generated output) already applied."""
    skipped = set(index_scope.mechanical_excludes(root))
    folders = index_scope._structure(root, max_dirs=OUTLINE_MAX_DIRS,
                                     keep=lambda rel: rel.count("/") < 2 and rel.split("/")[0] not in skipped)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    files = [name for name in names if not name.startswith(".") and os.path.isfile(os.path.join(root, name))]
    return folders, files[:OUTLINE_MAX_ROOT_FILES]


def plan_prompt(question, folders, root_files):
    """User message of the plan call."""
    return ("Question: " + question + "\n\nFolders:\n" + "\n".join(folders or ["(none)"]) +
            "\n\nRoot files:\n" + "\n".join(root_files or ["(none)"]))


def valid_plan(raw, folders):
    """`(terms, folders)` from the plan call: terms as non-empty words, folders only those listed (none means only the
    root files, which `index_scope.resolve_included_files` always adds); raises ValueError when no usable term
    remains."""
    if not isinstance(raw, dict) or not isinstance(raw.get("terms"), list) or not isinstance(raw.get("folders"), list):
        raise ValueError("the scan plan came back without terms and folders")
    terms = [term.strip() for term in raw["terms"] if isinstance(term, str) and term.strip()][:PLAN_MAX_TERMS]
    if not terms:
        raise ValueError("the scan plan came back without terms")
    allowed = set(folders)
    picked = [folder.strip().strip("/") for folder in raw["folders"] if isinstance(folder, str)]
    return terms, [folder for folder in dict.fromkeys(picked) if folder in allowed][:PLAN_MAX_FOLDERS]


def answer_prompt(question, items):
    """User message of the answer call: the question and each excerpt with its id, path and numbered lines."""
    parts = [f"Question: {question}"]
    for item in items:
        parts.append(f"[{item['id']}] {item['path']}:{item['start']}-{item['end']}"
                     + (f" ({item['symbols']})" if item["symbols"] else "") + "\n" + numbered(item["lines"]))
    return "\n\n".join(parts)


def valid_answer(raw, items):
    """`(found, answer, citations)` from the answer call. Citations name an excerpt id and a range clipped to that
    excerpt and to CITED_MAX_LINES; unknown ids and empty ranges are dropped. Raises ValueError on a malformed reply."""
    if (not isinstance(raw, dict) or type(raw.get("found")) is not bool or not isinstance(raw.get("answer"), str)
            or not isinstance(raw.get("citations"), list)):
        raise ValueError("the answer came back outside the expected fields")
    by_id = {item["id"]: item for item in items}
    citations = []
    for cite in raw["citations"]:
        if not isinstance(cite, dict) or cite.get("id") not in by_id:
            continue
        item = by_id[cite["id"]]
        try:
            start, end = int(cite.get("start_line")), int(cite.get("end_line"))
        except (TypeError, ValueError):
            continue
        if item["lines"][0][0] is None:
            start, end = item["start"], item["end"]
        start, end = max(start, item["start"]), min(end, item["end"], max(start, item["start"]) + CITED_MAX_LINES - 1)
        if start <= end and not any(c["id"] == item["id"] and c["start"] <= end and start <= c["end"] for c in citations):
            citations.append({"id": item["id"], "start": start, "end": end})
    answer = raw["answer"].strip()[:ANSWER_MAX_CHARS]
    if not answer:
        raise ValueError("the answer came back empty")
    return raw["found"], answer, citations


def render(question, source, items, citations=None, answer=None, found=None, model_note="", warnings=(), others=()):
    """The text returned to the agent. Lexical mode (`citations` None): every evidence window, within
    LITERAL_BUDGET_CHARS, after the model's answer when there is one (a model that cited nothing never leaves the
    answer without lines). Model mode: the model's answer, then only the cited ranges (sliced from the windows read
    from disk), and the uncited excerpts as `path:lines` pointers. `others` are candidates not shown, listed as
    pointers."""
    out = [f"question: {question}", f"source: {source}", f"model: {model_note or 'none (literal lines only)'}"]
    if answer is not None:
        out.append(("answer: " if found else "answer (not found in the excerpts): ") + answer)
    shown, budget = [], LITERAL_BUDGET_CHARS
    if citations is None:
        picks = [(item, item["start"], item["end"]) for item in items]
    else:
        by_id = {item["id"]: item for item in items}
        picks = [(by_id[c["id"]], c["start"], c["end"]) for c in citations]
    out.append("evidence:" if picks else "evidence: none")
    for item, start, end in picks:
        lines = [(n, t) for n, t in item["lines"] if n is None or start <= n <= end]
        block = numbered(lines)
        head = f"- [{item['id']}] {item['path']}:{start}-{end}" + (f" ({item['symbols']})" if item["symbols"] else "")
        if len(block) > budget:
            out.append(head + " (lines not shown: output budget reached)")
            continue
        budget -= len(block)
        out += [head, block]
        shown.append(item["id"])
    pointers = [f"{item['path']}:{item['start']}-{item['end']}" for item in items if item["id"] not in shown
                and citations is not None] + list(others)
    if pointers:
        out.append("other candidates (not shown): " + ", ".join(pointers))
    for warning in warnings:
        out.append(f"warning: {warning}")
    return "\n".join(out) + "\n"
