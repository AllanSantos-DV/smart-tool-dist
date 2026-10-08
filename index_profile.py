import collections
import fnmatch
import json
import os
import re
import time

import model_client

PROFILE_VERSION = 2
KINDS = ("code", "test", "doc")
RETRY_AFTER_S = 3600
MAX_DIRS = 90
PROSE = {"md", "txt", "docx", "rst", "adoc"}
# Data and markup inside a documentation folder are reports, not code (GitHub Linguist treats docs/ as documentation).
DOC_DATA = {"html", "htm", "json", "csv", "xml", "yaml", "yml", "svg"}
_NAME_DOC = re.compile(r"(^|/)(readme|changelog|license|licence|contributing|install|history|authors)(\.[^/]*)?$", re.I)
_DIR_DOC = re.compile(r"(^|/)(docs?|adrs?|documentation)/", re.I)
_TEST = re.compile(r"(^|/)(tests?|__tests__|spec|specs|e2e|fixtures?|testdata|test-utils?)(/|$)|(^|/)test_[^/]+\.py$|"
                   r"(^|/)conftest\.py$|_test\.(py|go)$|\.(test|spec)(-d)?\.[cm]?[jt]sx?$|(^|/)[^/]*[a-z0-9](?-i:Tests?)\.java$|"
                   r"(^|/)src/test/|(^|/)test[-_][^/]*\.[cm]?[jt]sx?$|[-_]test\.[cm]?[jt]sx?$", re.I)
_TEST_PROSE = re.compile(r"(^|/)(tests?|__tests__|e2e|fixtures?|testdata|test-utils?)(/|$)|(^|/)src/test/", re.I)
_TEXT_LINE = re.compile(r"[A-Za-zÀ-ú]{3,}\s+[A-Za-zÀ-ú]{3,}")

_TOOL = {"type": "function", "function": {
    "name": "submit_profile", "description": "Submit the search-index profile.",
    "parameters": {"type": "object", "required": ["groups", "languages"], "properties": {
        "groups": {"type": "array", "items": {"type": "object", "required": ["path", "kind"], "properties": {
            "path": {"type": "string", "description": "directory relative to root, '.' for root, or a glob like '**/NOTES.md'"},
            "kind": {"type": "string", "enum": list(KINDS)}}}},
        "languages": {"type": "object", "required": ["identifiers", "comments", "doc"], "properties": {
            "identifiers": {"type": "array", "items": {"type": "string"},
                            "description": "natural languages of identifier names (functions, classes, variables), most prevalent first (BCP-47)"},
            "comments": {"type": "array", "items": {"type": "string"},
                         "description": "natural languages of code comments and docstrings, most prevalent first (BCP-47)"},
            "doc": {"type": "array", "items": {"type": "string"},
                    "description": "natural languages of documentation, most prevalent first (BCP-47)"}}}}}}}

_PROMPT = """You are profiling a repository for a code-search index at {root}.
Classify where things live, by directory, not file by file. Three kinds:
- code: anything the product runs or loads: source, templates, HTML pages, config, scripts, and prompt/instruction files that the product itself executes (e.g. agent/skill/command definitions of a plugin).
- test: automated tests and their fixtures, mocks and helpers (unit, integration, e2e), even in folders with unusual names.
- doc: material written for people: README, CHANGELOG, docs/, ADRs, plans, guides, notes.
Use the most specific path needed: a directory, or a glob for files that break the rule of their directory. Paths not listed inherit from their closest listed parent.
Also report the natural languages used, most prevalent first, separately for identifier names, for code comments and docstrings, and for docs. Identifiers are often English even when comments are not. Use the text samples as evidence.
Directories (dir, file count, extensions, sample names, text sample):
{rows}
(total directories with indexed files: {total}; only the {shown} largest are shown)"""


def _ext(path):
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _sample(root, rel):
    try:
        with open(os.path.join(root, rel), "rb") as stream:
            raw = stream.read(4000).decode("utf-8", "ignore")
    except OSError:
        return ""
    return " | ".join(line.strip() for line in raw.splitlines() if _TEXT_LINE.search(line))[:260]


def _evidence(root, included):
    by_dir = collections.defaultdict(list)
    for rel in included:
        by_dir[rel.rsplit("/", 1)[0] if "/" in rel else "."].append(rel)
    rows = []
    for rel_dir, files in sorted(by_dir.items(), key=lambda item: -len(item[1]))[:MAX_DIRS]:
        names = [path.rsplit("/", 1)[-1] for path in files]
        text = next((path for path in files if _ext(path) in PROSE | {"py", "js", "ts", "java", "html", "mjs", "cjs"}), None)
        rows.append({"dir": rel_dir, "files": len(files),
                     "ext": dict(collections.Counter(_ext(path) or "(none)" for path in files).most_common(6)),
                     "sample": names[:5], "text_sample": _sample(root, text) if text else ""})
    return rows, len(by_dir)


def _valid(args):
    groups, languages = args.get("groups"), args.get("languages")
    return (isinstance(groups, list) and all(isinstance(g, dict) and isinstance(g.get("path"), str)
                                             and g.get("kind") in KINDS for g in groups)
            and isinstance(languages, dict) and all(isinstance(languages.get(key), list) and
                                                    all(isinstance(item, str) for item in languages[key])
                                                    for key in ("identifiers", "comments", "doc")))


def decide(root, included, model, deadline=None):
    rows, total = _evidence(root, included)
    remaining = deadline - time.monotonic() if deadline is not None else 120
    if remaining <= 0:
        raise TimeoutError("Profile analysis deadline exceeded.")
    response = model_client.fetch("/v1/chat/completions", model_client.get_token(), method="POST",
                                  timeout=min(120, remaining), body={
        "model": model, "temperature": 0, "tools": [_TOOL], "tool_choice": "required",
        "messages": [{"role": "user", "content": _PROMPT.format(
            root=root, rows=json.dumps(rows, ensure_ascii=False), total=total, shown=len(rows))}]})
    calls = response["choices"][0]["message"].get("tool_calls") or []
    try:
        args = next((json.loads(call["function"]["arguments"] or "{}") for call in calls
                     if call["function"]["name"] == "submit_profile"), None)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"The model returned a profile with invalid JSON: {exc}") from exc
    if not isinstance(args, dict) or not _valid(args):
        raise RuntimeError("The model did not define a valid profile of groups and languages.")
    return {"version": PROFILE_VERSION, "groups": args["groups"], "languages": args["languages"],
            "model": model, "decided_at": time.time()}


def _explicit_kind(path, groups):
    """Kind of the most specific profile group that names this path; None when only the root group covers it."""
    covering = [group for group in groups if _covers(path, group)]
    return _model_kind(path, covering) if covering else None


def _covers(path, group):
    pattern = group["path"].replace(chr(92), "/").removeprefix("./").strip("/") or "."
    if pattern == ".":
        return False
    if any(char in pattern for char in "*?["):
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("**/", ""))
    return path.startswith(pattern + "/")


def _model_kind(path, groups):
    best = None
    for group in groups:
        pattern = group["path"].replace(chr(92), "/").removeprefix("./").strip("/") or "."
        if any(char in pattern for char in "*?["):
            if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("**/", "")):
                return group["kind"]
        elif pattern == "." or path.startswith(pattern + "/"):
            depth = 0 if pattern == "." else len(pattern)
            if best is None or depth > best[0]:
                best = (depth, group["kind"])
    return best[1] if best else "doc"


def current(profile):
    return profile if (profile or {}).get("version") == PROFILE_VERSION else None


def kind(path, profile):
    groups = (current(profile) or {}).get("groups") or []
    ext = _ext(path)
    if ext in PROSE:
        if _NAME_DOC.search(path) or _DIR_DOC.search(path):
            return "doc"
        if _TEST_PROSE.search(path):
            return "test"
        return _model_kind(path, groups or [{"path": ".", "kind": "doc"}])
    if _TEST.search(path):
        return "test"
    if ext in DOC_DATA and _DIR_DOC.search(path) and _explicit_kind(path, groups) != "code":
        return "doc"
    if not ext and _NAME_DOC.search(path):
        return "doc"
    return "test" if groups and _model_kind(path, groups) == "test" else "code"


def public(profile):
    profile = current(profile)
    if not profile:
        return {"source": "rules", "identifier_languages": [], "comment_languages": [], "doc_languages": []}
    languages = profile["languages"]
    return {"source": "model", "identifier_languages": languages["identifiers"],
            "comment_languages": languages["comments"], "doc_languages": languages["doc"]}
