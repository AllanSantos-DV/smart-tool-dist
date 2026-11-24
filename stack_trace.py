"""A pasted Python, Node or Java traceback read against the code graph: for each frame inside the project, the function
whose lines hold the frame line, its direct callers and the tests that reach it.

Absolute frame paths are made project-relative by stripping the project folder; frames outside it are counted and
ignored. Java frames carry no folder, so they are matched by package and file name. Nothing is written.
"""
import os
import re
from urllib.parse import unquote

import code_graph
import code_impact
import index_profile
import index_scope
import symbol_history

DEFAULT_LIMIT = 30
TEST_DEPTH = 3
MAX_FRAMES = 100
NOTE = ("A frame's function is the innermost indexed function whose line range holds the frame line, read from the working "
        "tree: a file edited since the trace was printed can place a frame in another function. Callers and tests come "
        "from static calls only: calls made through callbacks, dynamic dispatch or names built at runtime are not seen, "
        "so an empty list means 'none found'.")
HISTORY_NOTE = ("last_commit is the newest commit that changed the function's own lines (`git log -L` on the line range of the "
                "working tree); a frame whose file has uncommitted edits, is not committed or has no readable git history "
                "carries the reason in history_refused instead.")

_PYTHON = re.compile(r'^\s*File "(?P<path>.+)", line (?P<line>\d+)(?:, in (?P<name>.+?))?\s*$')
_NODE = re.compile(r"^\s*at\s+(?:async\s+)?(?:(?P<name>[^()]+?)\s+\()?(?P<path>.+?):(?P<line>\d+):\d+\)?\s*$")
_JAVA = re.compile(r"^\s*at\s+(?:[\w.$-]+/)*(?P<cls>[\w.$]+)\.(?P<name>[\w$<>]+)\((?P<file>[\w$-]+\.java):(?P<line>\d+)\)\s*$")
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_FILE_URL = re.compile(r"^file:///?(?=[A-Za-z]:)|^file://", re.I)


def _frames(text):
    """The stack frames of a text in order, each {"language", "path", "line", "name"} (Java adds "package", "file")."""
    frames = []
    for row in text.splitlines():
        if match := _PYTHON.match(row):
            frames.append({"language": "python", "path": match["path"], "line": int(match["line"]), "name": match["name"]})
        elif match := _JAVA.match(row):
            frames.append({"language": "java", "path": None, "line": int(match["line"]), "name": match["name"],
                           "package": match["cls"].rpartition(".")[0], "file": match["file"]})
        elif match := _NODE.match(row):
            frames.append({"language": "node", "path": match["path"], "line": int(match["line"]), "name": match["name"]})
    return frames


def _inside(root, raw, known):
    """The project-relative path of a frame path, or None when it is not the project's.

    A path under the project folder is the project's whether or not the graph holds the file; a relative one only when
    the graph does, since it cannot be placed otherwise."""
    if raw.lower().startswith("file:"):
        raw = unquote(_FILE_URL.sub("", raw))
    if os.path.isabs(raw) or _DRIVE.match(raw):
        full, base = os.path.normpath(raw), os.path.normpath(root)
        if not os.path.normcase(full).startswith(os.path.normcase(base) + os.sep):
            return None
        rel = full[len(base) + 1:].replace(os.sep, "/")
    else:
        rel = raw.replace("\\", "/").removeprefix("./")
        if os.path.normcase(rel) not in known:
            return None
    return known.get(os.path.normcase(rel), rel)


def _java_files(frame, known):
    """The indexed files of a Java frame's package and file name."""
    suffix = (frame["package"].replace(".", "/") + "/" if frame["package"] else "") + frame["file"]
    return sorted(path for path in known.values() if path == suffix or path.endswith("/" + suffix))


def _enclosing(by_path, paths, line):
    """The innermost function or class of the files whose lines hold `line`, or None."""
    holding = [s for path in paths for s in by_path.get(path, []) if s["start_line"] <= line <= s["end_line"]]
    return min(holding, key=lambda s: (s["end_line"] - s["start_line"], -s["start_line"]), default=None)


def _last_commit(root, view_id, row):
    """The (latest commit that changed the lines of the frame's function, why it could not be read) of a row.

    One of the two is None. A frame in no function has neither."""
    if not row["function"]:
        return None, None
    try:
        answer = symbol_history.report(root, f"{row['path']}::{row['function']}", view_id, limit=1)
    except ValueError as error:
        return None, str(error)
    definition = next((item for item in answer["definitions"] if item["lines"] == row["lines"]), None)
    if definition is None:
        return None, f"the code graph holds no definition of {row['function']} at lines {row['lines']} of {row['path']}."
    return (definition["commits"] or [None])[0], None


def report(root, text, view_id=None, limit=DEFAULT_LIMIT, history=False):
    """What each project frame of a traceback is, who calls it and which tests reach it.

    Parameters: root (str) project folder; text (str) the pasted Python, Node or Java traceback; view_id (str or None)
    index view; limit (int) callers and tests listed per frame; history (bool) also give each frame in a function the
    latest commit that changed that function's own lines, through `symbol_history`.
    Returns: dict {"frames": [{"frame": {"language", "path" (project-relative), "line", "name" (as the trace prints it)},
    "function", "path", "lines": "start-end" (function None and lines None when no indexed function holds the line),
    "doc" (first docstring line, when there is one), "callers": [{"function", "path", "line" of the call}], "tests":
    [{"function", "path", "line", "hops"}], "counts": {"callers", "tests"}}] in trace order, "ignored": int (frames
    outside the project or that no indexed file matches), "frames_cut" (only when more than 100 project frames), "view",
    "note": str}. With history each frame also has "last_commit": {"commit", "date", "author", "subject"} or None, and
    "history_refused": str (the reason, only when `symbol_history` refused the frame's function: uncommitted edits, a file
    not committed, no git history); a refusal never fails the call.
    Raises: ValueError when text is blank or holds no frame, history is not a bool, or the code graph is empty.
    """
    if type(history) is not bool:
        raise ValueError(f"history must be true or false, got: {history!r}")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Pass text: the traceback to read.")
    parsed = _frames(text)
    if not parsed:
        raise ValueError("No Python, Node or Java stack frame found in text: expected lines like 'File \"x.py\", line 3, "
                         "in f', 'at f (x.js:3:5)' or 'at pkg.Class.method(Class.java:3)'.")
    data = code_graph.build_current(root, view_id, shared=True)
    if not data.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    by_id = {s["id"]: s for s in data["symbols"]}
    by_path = {}
    for symbol in data["symbols"]:
        by_path.setdefault(symbol["path"], []).append(symbol)
    known = {os.path.normcase(path): path for path in by_path}
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    callers, _callees = code_impact.call_index(data["calls"])

    located, ignored = [], 0
    for frame in parsed:
        paths = _java_files(frame, known) if frame["language"] == "java" else [_inside(root, frame["path"], known)]
        paths = [path for path in paths if path]
        if not paths:
            ignored += 1
            continue
        shown = {key: frame[key] for key in ("language", "line", "name")}
        located.append((shown | {"path": paths[0]}, _enclosing(by_path, paths, frame["line"])))
    cut = max(len(located) - MAX_FRAMES, 0)
    located = located[:MAX_FRAMES]

    rows, ids, sites = [], [], []
    for shown, symbol in located:
        row = {"frame": shown, "function": None, "path": shown["path"], "lines": None, "callers": [], "tests": [],
               "counts": {"callers": 0, "tests": 0}}
        ids.append(symbol["id"] if symbol else None)
        if symbol:
            direct = [code_impact._site(by_id, c["source"], c["line"]) for c in callers[symbol["id"]]
                      if c["source"] != symbol["id"]]
            tests = sorted(code_impact.reaching_tests(by_id, callers, profile, {symbol["id"]}, TEST_DEPTH).values(),
                           key=lambda t: (t["hops"], t["path"], t["line"]))
            row.update(function=symbol["name"], path=symbol["path"], lines=f"{symbol['start_line']}-{symbol['end_line']}",
                       callers=direct[:limit], tests=tests[:limit], counts={"callers": len(direct), "tests": len(tests)})
            sites += row["callers"] + row["tests"]
        rows.append(row)
    code_impact._name_test_callbacks(root, by_id, sites)
    docs = code_impact.summaries(root, data["symbols"], {i for i in ids if i} | {site["id"] for site in sites})
    for row, symbol_id in zip(rows, ids):
        if docs.get(symbol_id):
            row["doc"] = docs[symbol_id]
    for site in sites:
        if docs.get(site["id"]):
            site["doc"] = docs[site["id"]]
        del site["id"]
    if history:
        for row in rows:
            row["last_commit"], refused = _last_commit(root, view_id, row)
            if refused:
                row["history_refused"] = refused
    return {"frames": rows, "ignored": ignored, "view": (data.get("selected") or {}).get("label"),
            "note": NOTE + (f" {code_graph.left_out_sentence(data)} Callers and tests beyond the cut are not listed."
                            if data.get("truncated") else "") + (f" {HISTORY_NOTE}" if history else ""),
            **({"frames_cut": cut} if cut else {})}
