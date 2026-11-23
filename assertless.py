"""Test functions whose body holds no assertion, so a test that passes whatever the code does is visible.

The tests are the functions of the test files in the code graph: Python and Go by name, Java by its `@Test` annotation,
JavaScript and TypeScript by the `it` or `test` call that receives the callback. The body is read from the file and
searched for the assertion forms of the language. Nothing is written.
"""
import re

import code_graph
import code_health
import comment_check
import doc_check

FUNCTION_KINDS = ("function", "method")
NOTE = ("These are candidates only: a test whose assertion sits in a helper it calls, or in a form this scan does not know, "
        "looks assertion-less here. Read each one before adding an assertion.")

_PYTHON = re.compile(r"\bassert\b|\bassert[A-Z_]\w*\s*\(|\bself\.fail\s*\(|\bpytest\.(?:raises|warns|fail)\b|\.assert_\w+\s*\(")
_SCRIPT = re.compile(r"\bexpect\w*\s*\(|\bassert(?:\.\w+)?\s*\(|\.should\b|\bshould\.|\bt\.(?:is|not|true|false|truthy|falsy|"
                     r"deepEqual|equal|throws|notThrows|rejects|snapshot|pass|fail|assert)\w*\s*\(|\bfail\s*\(")
_GO = re.compile(r"\bt\.(?:Error|Errorf|Fatal|Fatalf|Fail|FailNow)\s*\(|\b(?:assert|require)\.\w+\s*\(|\bExpect\s*\(|\.Should\w*\s*\(")
_JAVA = re.compile(r"\b(?:assert\w*|fail|verify\w*|expect\w*|andExpect)\s*\(|\bAssertions\.|@Test\s*\([^)]*expected")
_JAVA_TEST = re.compile(r"@(?:Test|ParameterizedTest|RepeatedTest|TestFactory)\b")
_SCRIPT_TEST = re.compile(r"\b(?:it|test)\b(?:\.\w+)*(?:\([^)]*\))?\s*\(")
_GO_TEST = re.compile(r"^func\s+Test\w*\s*\(.*\btesting\.T\b")
_LANGUAGES = {doc_check.PYTHON: _PYTHON, doc_check.SCRIPT: _SCRIPT, doc_check.JAVA: _JAVA, doc_check.GO: _GO}


def _language(path):
    lowered = path.lower()
    return next((exts for exts in _LANGUAGES if lowered.endswith(exts)), None)


def _annotated(lines, start):
    """True when the declaration at 1-based line `start` carries a JUnit test annotation, on its line or the lines of
    annotations right above it."""
    if _JAVA_TEST.search(lines[start - 1]):
        return True
    number = start - 2
    while number >= 0 and lines[number].lstrip().startswith("@"):
        if _JAVA_TEST.search(lines[number]):
            return True
        number -= 1
    return False


def _script_call(lines, start):
    """True when the callback starting at 1-based line `start` is handed to `it(` or `test(` on that line or in the three
    lines above it, not past the end of an earlier statement."""
    for number in range(start - 1, max(start - 5, -1), -1):
        text = lines[number]
        if number < start - 1 and text.rstrip().endswith((");", "}", "},")):
            return False
        if _SCRIPT_TEST.search(text):
            return True
    return False


def _is_test(symbol, exts, lines):
    """Whether a function of a test file is a test the way its language declares one."""
    name = symbol["name"].split(".")[-1]
    start = symbol["start_line"]
    if exts == doc_check.PYTHON:
        return name.startswith("test")
    if exts == doc_check.GO:
        return bool(_GO_TEST.match(lines[start - 1]))
    if exts == doc_check.JAVA:
        return _annotated(lines, start)
    return name == "callback" and _script_call(lines, start)


def _without_comments(path, text):
    """The file's lines with the comment lines emptied, so an assertion named in a comment does not count."""
    python = path.lower().endswith(doc_check.PYTHON)
    go = path.lower().endswith(doc_check.GO)
    comments = comment_check._python_comments(text) if python else comment_check._braced_comments(text, go)
    lines = text.split("\n")
    for comment in comments:
        if not comment["trailing"]:
            for number in range(comment["start"], comment["end"] + 1):
                lines[number - 1] = ""
    return lines


def _folder(folder):
    """The project-relative prefix a folder argument names, "" for the whole project."""
    return (folder or "").replace("\\", "/").strip("/")


def _inside(path, prefix):
    return not prefix or path == prefix or path.startswith(prefix + "/")


def candidates(root, view_id=None, limit=code_health.DEFAULT_LIMIT, folder=None):
    """The test functions of the indexed project whose body holds no assertion.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) candidates returned; folder
    (str or None) project-relative folder, only the test files under it are read.
    Returns: dict {"candidates": [{"name", "path", "line", "lines"}] in path and line order, "counts": {"candidates",
    "tests"}, "note": str, "graph_note": str (what the analysis limits cut, "" when nothing)}; `note` states that
    these are candidates. A test file that cannot be read is skipped.
    Raises: ValueError when the code graph is empty.
    """
    model = code_health._model(root, view_id)
    prefix = _folder(folder)
    by_path = {}
    for symbol in model.graph["symbols"]:
        if (symbol.get("kind") in FUNCTION_KINDS and model.kind.get(symbol["path"]) == "test"
                and _inside(symbol["path"], prefix) and _language(symbol["path"])):
            by_path.setdefault(symbol["path"], []).append(symbol)
    found, tests = [], 0
    for path in sorted(by_path):
        text = model.read(path)
        if not text:
            continue
        exts = _language(path)
        raw = text.split("\n")
        clean = _without_comments(path, text)
        for symbol in sorted(by_path[path], key=lambda s: s["start_line"]):
            if symbol["end_line"] > len(raw) or not _is_test(symbol, exts, raw):
                continue
            tests += 1
            body = "\n".join(clean[symbol["start_line"] - 1:symbol["end_line"]])
            if not _LANGUAGES[exts].search(body):
                found.append({"name": symbol["name"], "path": path, "line": symbol["start_line"],
                              "lines": symbol["end_line"] - symbol["start_line"] + 1})
    note = NOTE
    if prefix and not tests:
        note = f"No indexed test file under {prefix}. " + note
    return {"candidates": found[:limit], "counts": {"candidates": len(found), "tests": tests}, "note": note,
            "graph_note": code_graph.left_out_sentence(model.graph) or ""}
