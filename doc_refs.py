"""Stale references in the project docs, asked of the whole project and not only before a move: for each README and `docs`
file, the paths, links and functions it writes that no longer exist. Paths and functions come from the inline code of the
Markdown text, the lines that write a missing path are found with the mention rule of `move_preview`, relative links come
from `link_check`, and the functions defined in the code from `doc_check`. Nothing is written.
"""
import builtins
import os
import posixpath
import re
from collections import defaultdict

import doc_check
import link_check
import move_preview
import project_scan
import rename_preview

DEFAULT_LIMIT = 30
NOTE = ("Read from the git-tracked README and `docs` text (the generated docs/scope is left out): paths and functions in "
        "inline code, outside fenced blocks, and relative links. A path counts only when its extension exists in the "
        "project or its first folder does; a bare file name is found anywhere in the project; a function name that appears "
        "nowhere in the code is stale, so a name invented as an example is listed too and a function that is still "
        "called somewhere is not.")

_FENCE = re.compile(r"^\s*(```|~~~)")
_SPAN = re.compile(r"`([^`\n]+)`")
_LOCATION = re.compile(r"(?::\d+(?::\d+)?|#[\w.-]*)$")
_PATH = re.compile(r"^(?:\.\./)*[\w.@+-]+(?:/[\w.@+-]+)*/?$")
_PLACEHOLDER = re.compile(r"(?:^|/)(?:path/to|your[-_\w]*|foo|bar|baz)(?:/|$)")
_EXTENSION = re.compile(r"^\.[A-Za-z][A-Za-z0-9]{0,7}$")
_FILE_SYMBOL = re.compile(r"^(?P<path>[\w./@+-]+\.[A-Za-z][A-Za-z0-9]{0,7})::(?P<name>[A-Za-z_][\w.]*)(?:\(\))?$")
_CALL = re.compile(r"^([A-Za-z_]\w*)\(\)$")
_QUALIFIED = re.compile(r"^(?P<owner>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\.(?P<member>[A-Za-z_]\w*)(?P<call>\(\))?$")
_WORD = re.compile(r"[A-Za-z_]\w*")
_FILE_KINDS = {"cfg", "conf", "css", "csv", "exe", "html", "ini", "js", "json", "lock", "md", "ps1", "sh", "sql", "toml",
               "ts", "txt", "xml", "yaml", "yml"}


class _Tree:
    """The tracked paths of the project, indexed to tell whether a path written in a doc exists."""

    def __init__(self, root, files):
        self.root = root
        self.known = set(files)
        self.tails = set()
        self.extensions = {posixpath.splitext(rel)[1].lower() for rel in files}
        for rel in files:
            parts = rel.split("/")
            for start in range(len(parts)):
                self.tails.add("/".join(parts[start:]))
            for end in range(1, len(parts)):
                folder = "/".join(parts[:end])
                self.known.add(folder)
                self.tails.update("/".join(parts[start:end]) for start in range(end))
        self.top = {rel.split("/")[0] for rel in files}

    def candidate(self, span, folder):
        """The path a piece of inline code writes, or None when it is not written as a path.

        Parameters: span (str) the inline code; folder (str) folder of the doc, relative to the project.
        """
        text = _LOCATION.sub("", span.strip())
        text = text[2:] if text.startswith("./") else text
        if not _PATH.match(text) or _PLACEHOLDER.search(text):
            return None
        body = text.rstrip("/")
        extension = posixpath.splitext(body.rpartition("/")[2])[1].lower()
        if _EXTENSION.match(extension):
            return text if extension in self.extensions else None
        if "/" not in body:
            return None
        head = posixpath.normpath(posixpath.join(folder, body)) if body.startswith("../") else body
        first = head.split("/")[0]
        return text if first in self.top or posixpath.join(folder, first) in self.known else None

    def exists(self, text, folder):
        """Whether the path written in a doc is a file or folder of the project: from the project folder, from the folder
        of the doc, or as the end of a longer tracked path. A path that climbs out of the project cannot be checked and
        counts as existing."""
        body = text.rstrip("/")
        starts = [folder] if body.startswith("../") else ["", folder]
        for base in starts:
            joined = posixpath.normpath(posixpath.join(base, body))
            if joined == ".." or joined.startswith("../"):
                return True
            if os.path.lexists(os.path.join(self.root, *joined.split("/"))):
                return True
        return body in self.tails


class _Code:
    """The functions and identifiers of the tracked code files, read once and only when a doc names a function."""

    def __init__(self, root):
        self.functions, self.words, self.owners, self.stems = {}, {}, defaultdict(set), defaultdict(set)
        items = [(rel, text) for rel, text in project_scan.tracked_texts(root, doc_check.SUPPORTED) if text is not None]
        scripts = [item for item in items if item[0].lower().endswith(doc_check.SCRIPT + doc_check.JAVA)]
        symbols = dict(zip((rel for rel, _text in scripts), doc_check.analyze_files(scripts)))
        for rel, text in items:
            found = doc_check.functions(rel, text, symbols.get(rel))
            self.functions[rel] = {item["name"].rpartition(".")[2] for item in found}
            self.words[rel] = set(_WORD.findall(text))
            self.stems[posixpath.splitext(posixpath.basename(rel))[0]].add(rel)
            for item in found:
                owner = item["name"].rpartition(".")[0].rpartition(".")[2]
                if owner:
                    self.owners[owner].add(rel)
        self.every_word = set().union(*self.words.values()) if self.words else set()

    def defines(self, name, files, called):
        """Whether one of `files` defines `name`; a name that is not called may also be a constant, a class or an
        attribute, so it counts when the file uses it as an identifier."""
        return any(name in self.functions[rel] or (not called or name[:1].isupper()) and name in self.words[rel]
                   for rel in files)


def _doc_lines(text):
    """The (number, text) of the lines of a doc outside fenced blocks."""
    fenced, lines = False, []
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            lines.append((number, line))
    return lines


def _symbol_problem(code, tree, folder, span):
    """Why a piece of inline code that names a function of this project no longer matches the code, or None when it does
    match or does not name a function of the project (a library call, prose)."""
    named = _FILE_SYMBOL.match(span)
    if named:
        path, name = named["path"], named["name"].rpartition(".")[2]
        if not tree.exists(path, folder):
            return f"{path} does not exist"
        rel = posixpath.normpath(path)
        if rel in code.functions and not code.defines(name, [rel], True):
            return f"{name} is not defined in {path}"
        return None
    call = _CALL.match(span)
    if call:
        name = call[1]
        if hasattr(builtins, name) or name in code.every_word:
            return None
        return f"no function named {name} in the project's code"
    qualified = _QUALIFIED.match(span)
    if qualified:
        owner, member = qualified["owner"].rpartition(".")[2], qualified["member"]
        if member in _FILE_KINDS:
            return None
        files = code.stems.get(owner, set()) | code.owners.get(owner, set())
        if files and not code.defines(member, files, bool(qualified["call"])):
            return f"{owner} has no {member} in {', '.join(sorted(files)[:3])}"
    return None


def _scan_doc(rel, text, root, tree, code_of):
    """The stale references of one doc as rows {"line", "kind", "ref", "why"}.

    Parameters: code_of (callable) the `_Code` of the project, built on first use.
    """
    folder = posixpath.dirname(rel)
    lines = _doc_lines(text)
    paths, symbols, rows = [], [], []
    for number, line in lines:
        for span in (found[1].strip() for found in _SPAN.finditer(line)):
            path = None if _FILE_SYMBOL.match(span) or _CALL.match(span) else tree.candidate(span, folder)
            if path:
                paths.append(path)
            elif _FILE_SYMBOL.match(span) or _CALL.match(span) or _QUALIFIED.match(span):
                symbols.append((number, span))
    if rel.lower().endswith(link_check.EXTENSIONS):
        for item in link_check.review(os.path.join(root, rel), None, text)["broken"]:
            rows.append({"line": item["line"], "kind": "link", "ref": item["link"], "why": "the linked file does not exist"})
    linked = [(row["line"], row["ref"].split("#")[0]) for row in rows]
    for path in dict.fromkeys(paths):
        if tree.exists(path, folder):
            continue
        mentioned = move_preview._mention_test(path.rstrip("/"))
        rows += [{"line": number, "kind": "path", "ref": path, "why": "no file or folder with this path in the project"}
                 for number, line in lines
                 if mentioned(line) and not any(at == number and mentioned(target) for at, target in linked)]
    for number, span in dict.fromkeys(symbols):
        why = _symbol_problem(code_of(), tree, folder, span)
        if why:
            rows.append({"line": number, "kind": "symbol", "ref": span, "why": why})
    return sorted(rows, key=lambda row: (row["line"], row["kind"], row["ref"]))


def scan(root, limit, label, extensions, wanted, note):
    """The stale references of the tracked text files that `wanted` accepts.

    Parameters: root (str) project folder, a git repository; limit (int) stale references listed; label (str) noun for the
    files in the counts and the summary; extensions (tuple of str) suffixes read; wanted (callable) whether a path is read;
    note (str) the caveat returned.
    Returns: the dict `check` documents, with `{label}s_checked` and `{label}s_unreadable` in "counts".
    Raises: ValueError when git cannot list the folder; RuntimeError or OSError when the code analyzers cannot run.
    """
    tree = _Tree(root, project_scan.tracked_files(root))
    built = []

    def code_of():
        if not built:
            built.append(_Code(root))
        return built[0]

    per_doc, unreadable, checked = [], 0, 0
    for rel, text in project_scan.tracked_texts(root, extensions):
        if not wanted(rel):
            continue
        if text is None:
            unreadable += 1
            continue
        checked += 1
        rows = _scan_doc(rel, text, root, tree, code_of)
        if rows:
            per_doc.append((rel, rows))
    per_doc.sort(key=lambda item: item[0])
    total = sum(len(rows) for _rel, rows in per_doc)
    by_kind = {kind: sum(row["kind"] == kind for _rel, rows in per_doc for row in rows)
               for kind in ("path", "link", "symbol")}
    files, left = [], limit
    for rel, rows in per_doc:
        if left <= 0:
            break
        files.append({"path": rel, "stale": rows[:left]})
        left -= len(rows)
    counts = {f"{label}s_checked": checked, f"{label}s_unreadable": unreadable, "files_with_stale": len(per_doc),
              "stale": total,
              "paths": by_kind["path"], "links": by_kind["link"], "symbols": by_kind["symbol"]}
    summary = (f"{total} stale reference(s) in {len(per_doc)} of {checked} {label} file(s)" if total
               else f"No stale reference in {checked} {label} file(s)")
    if total > limit:
        summary += f"; the first {limit} are listed, raise limit for the rest"
    return {"files": files, "counts": counts, "summary": summary + ".", "note": note}


def check(root, limit=DEFAULT_LIMIT):
    """The paths, links and functions the project docs write that no longer exist.

    Parameters: root (str) project folder, a git repository; limit (int) stale references listed.
    Returns: dict {"files": [{"path", "stale": [{"line", "kind": "path", "link" or "symbol", "ref", "why"}]}] with the
    first `limit` references in file and line order, "counts": {"docs_checked", "docs_unreadable", "files_with_stale",
    "stale", "paths", "links", "symbols"} (totals), "summary": str, "note": str}.
    Raises: ValueError when git cannot list the folder; RuntimeError or OSError when the code analyzers cannot run.
    """
    return scan(root, limit, "doc", rename_preview.DOC_EXTENSIONS, rename_preview._is_doc, NOTE)
