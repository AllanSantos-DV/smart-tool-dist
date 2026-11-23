"""Lines of the git diff that no executed line covers, read from a coverage report the user's own last test run wrote.

The report is a Cobertura `coverage.xml`, an `lcov.info` or a coverage.py JSON found in the project root or a common
report folder; the newest one is used. Tests are never run and nothing is installed or written. The changed lines are
those `affected_tests` reads (`git diff -U0` against a base, which covers staged and unstaged edits, plus untracked files),
and each uncovered line is named after the innermost function of the code graph that holds it.
"""
import datetime
import json
import os
import posixpath
import re
import xml.etree.ElementTree as ET

import affected_tests
import code_graph

CANDIDATES = (
    "coverage.xml", "cobertura.xml", "cobertura-coverage.xml", "lcov.info", "coverage.json",
    "coverage/coverage.xml", "coverage/cobertura-coverage.xml", "coverage/lcov.info", "coverage/coverage.json",
    "reports/coverage.xml", "report/coverage.xml", "build/coverage.xml", "build/reports/coverage.xml",
    "target/site/cobertura/coverage.xml", "test-results/coverage.xml", "test-results/lcov.info",
)
FORMATS = {".xml": "cobertura", ".info": "lcov", ".json": "coverage.py json"}
MAX_BYTES = 200_000_000
FUNCTION_KINDS = ("function", "method")
NOTE = ("Only lines the report lists as executable are judged: a changed comment, blank line or declaration the report "
        "does not list is neither covered nor uncovered. Line numbers are the ones of the working tree, so a report older "
        "than the edits can point at the wrong lines; run the tests again when `stale` is true. Other report formats "
        "(JaCoCo, Istanbul JSON, a `.coverage` database) are not read.")


def _utc(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat(timespec="seconds")


def _find(root):
    """The newest candidate report as (project-relative path, modified timestamp), or None."""
    found = []
    for rel in CANDIDATES:
        path = os.path.join(root, *rel.split("/"))
        if os.path.isfile(path):
            found.append((os.stat(path).st_mtime, rel))
    if not found:
        return None
    modified, rel = max(found, key=lambda item: item[0])
    return rel, modified


def _read(root, rel):
    path = os.path.join(root, *rel.split("/"))
    if os.path.getsize(path) > MAX_BYTES:
        raise ValueError(f"{rel} is larger than {MAX_BYTES // 1_000_000} MB: not read.")
    with open(path, "rb") as stream:
        return stream.read()


def _add(files, name, aliases, number, hits):
    entry = files.setdefault(name, {"names": {name}, "lines": {}})
    entry["names"].update(aliases)
    entry["lines"][number] = entry["lines"].get(number, 0) + hits


def _cobertura(rel, data):
    if b"<!ENTITY" in data[:8192]:
        raise ValueError(f"{rel} declares XML entities: not read.")
    try:
        tree = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f"{rel} is not valid XML: {exc}") from exc
    if tree.tag != "coverage":
        raise ValueError(f"{rel} is not a Cobertura coverage report (root element <{tree.tag}>).")
    sources = [source.text.strip() for source in tree.iter("source") if source.text and source.text.strip()]
    files = {}
    for element in tree.iter("class"):
        name = (element.get("filename") or "").replace("\\", "/")
        if not name:
            continue
        aliases = {posixpath.join(source.replace("\\", "/"), name) for source in sources}
        for line in element.findall("lines/line"):
            try:
                _add(files, name, aliases, int(line.get("number")), int(float(line.get("hits") or 0)))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{rel} has a line element without a valid number or hits.") from exc
    return files


def _lcov(rel, data):
    files, name = {}, None
    for text in data.decode("utf-8", "replace").splitlines():
        if text.startswith("SF:"):
            name = text[3:].strip().replace("\\", "/")
            files.setdefault(name, {"names": {name}, "lines": {}})
        elif text.startswith("DA:") and name is not None:
            try:
                number, hits = text[3:].split(",")[:2]
                _add(files, name, (), int(number), int(float(hits)))
            except ValueError as exc:
                raise ValueError(f"{rel} has a malformed DA record.") from exc
    if not files:
        raise ValueError(f"{rel} is not an lcov report (no SF record).")
    return files


def _coverage_json(rel, data):
    try:
        report = json.loads(data)
    except ValueError as exc:
        raise ValueError(f"{rel} is not valid JSON: {exc}") from exc
    listed = report.get("files") if isinstance(report, dict) else None
    if not isinstance(listed, dict) or not all(
            isinstance(item, dict) and "executed_lines" in item and "missing_lines" in item for item in listed.values()):
        raise ValueError(f"{rel} is not a coverage.py JSON report (a `files` map with executed_lines and "
                         "missing_lines per file).")
    files = {}
    for name, item in listed.items():
        name = name.replace("\\", "/")
        files[name] = {"names": {name}, "lines": {**{n: 0 for n in item["missing_lines"]},
                                                  **{n: 1 for n in item["executed_lines"]}}}
    return files


def _parse(root, rel):
    """The files of a report as {name: {"names": {every path it may be known by}, "lines": {line: hits}}}."""
    kind = FORMATS[os.path.splitext(rel)[1].lower()]
    parser = {"cobertura": _cobertura, "lcov": _lcov}.get(kind, _coverage_json)
    return parser(rel, _read(root, rel))


def _score(name, path, absolute):
    """How well a report path names a project file: 3 exact, 2 the report path is longer, 1 shorter, 0 not at all."""
    if os.path.isabs(name):
        return 3 if os.path.normcase(os.path.normpath(name)) == os.path.normcase(absolute) else 0
    name = posixpath.normpath(name)
    if name == path:
        return 3
    if name.endswith("/" + path):
        return 2
    return 1 if path.endswith("/" + name) else 0


def _assign(root, files, paths):
    """Each report file goes to the changed project file it names best: {changed path: its report entry}."""
    chosen = {}
    absolute = {path: os.path.normcase(os.path.normpath(os.path.join(root, *path.split("/")))) for path in paths}
    for entry in files.values():
        best = max(((max(_score(name, path, absolute[path]) for name in entry["names"]), len(path), path)
                    for path in paths), default=(0, 0, None))
        if best[0] and (best[2] not in chosen or chosen[best[2]][0] < best[0]):
            chosen[best[2]] = (best[0], entry)
    return {path: entry for path, (_, entry) in chosen.items()}


def _ranges(lines):
    """`7`, `7-8` or `2, 7-9` for a sorted list of line numbers."""
    spans, start = [], None
    for number in lines:
        if start is not None and number == previous + 1:
            previous = number
            continue
        if start is not None:
            spans.append((start, previous))
        start = previous = number
    if start is not None:
        spans.append((start, previous))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


def _functions(root, view_id):
    """The functions of the code graph by path, and why none could be read ('' when they were)."""
    try:
        data = code_graph.build_current(root, view_id, shared=True)
    except ValueError as exc:
        return {}, f"Functions are not named: {exc}"
    by_path = {}
    for symbol in data["symbols"]:
        if symbol.get("kind") in FUNCTION_KINDS:
            by_path.setdefault(symbol["path"], []).append(symbol)
    return by_path, code_graph.left_out_sentence(data) or ""


def _innermost(symbols, line):
    holding = [s for s in symbols if s["start_line"] <= line <= s["end_line"]]
    return min(holding, key=lambda s: s["end_line"] - s["start_line"], default=None)


def report(root, base="HEAD", limit=30, view_id=None):
    """The changed lines no executed line of the last test run's coverage report covers.

    Parameters: root (str) project folder; base (str) git revision the changes are read against (staged, unstaged and
    untracked files count); limit (int) rows returned; view_id (str or None) index view used to name the functions.
    Returns: dict {"found": bool, "message": str, "report": {"path", "format", "modified", "head_commit_time", "stale",
    "warning"} or None, "uncovered": [{"path", "function" (None outside every function), "line", "lines", "count"}] by
    path and line, "counts": {"changed_files", "uncovered_lines", "covered_lines", "not_in_report"}, "not_in_report":
    [path] changed code files the report does not list, "searched": [path], "graph_note": str, "note": str}.
    Raises: ValueError when base is not a git revision, git cannot read the folder, or the report cannot be read.
    """
    if not isinstance(base, str) or not re.fullmatch(r"[\w./@^~{}-]{1,200}", base) or base.startswith("-"):
        raise ValueError("base must be a git revision such as HEAD, main or origin/main.")
    changes = {path: change for path, change in affected_tests._changes(root, base).items() if change["status"] != "deleted"}
    answer = {"found": True, "message": "", "report": None, "uncovered": [], "searched": list(CANDIDATES),
              "counts": {"changed_files": len(changes), "uncovered_lines": 0, "covered_lines": 0, "not_in_report": 0},
              "not_in_report": [], "graph_note": "", "note": NOTE}
    located = _find(root)
    if located is None:
        answer.update(found=False, message=(
            "No coverage report found. This action reads a report your own last test run wrote (Cobertura XML, lcov or "
            "coverage.py JSON) and never runs the tests; it looked for " + ", ".join(CANDIDATES) + "."))
        return answer
    rel, modified = located
    head_time = int(affected_tests._git(root, "log", "-1", "--format=%ct", "HEAD").strip())
    stale = modified < head_time
    answer["report"] = {"path": rel, "format": FORMATS[os.path.splitext(rel)[1].lower()], "modified": _utc(modified),
                        "head_commit_time": _utc(head_time), "stale": stale,
                        "warning": (f"The report ({_utc(modified)}) is older than the HEAD commit ({_utc(head_time)}): "
                                    "lines may have moved since the tests ran." if stale else "")}
    if not changes:
        answer["message"] = f"No changes since {base}: nothing to check against the report."
        return answer
    files = _parse(root, rel)
    matched = _assign(root, files, sorted(changes))
    extensions = {os.path.splitext(name)[1].lower() for entry in files.values() for name in entry["names"]}
    functions, answer["graph_note"] = _functions(root, view_id)
    rows, covered = [], 0
    for path in sorted(matched):
        hits = matched[path]["lines"]
        changed = changes[path]["lines"]
        judged = sorted(n for n in hits if changed is None or n in changed)
        covered += sum(1 for n in judged if hits[n] > 0)
        groups = {}
        for number in (n for n in judged if hits[n] == 0):
            symbol = _innermost(functions.get(path, ()), number)
            groups.setdefault(symbol["name"] if symbol else None, []).append(number)
        rows += [{"path": path, "function": function, "line": lines[0], "lines": _ranges(lines), "count": len(lines)}
                 for function, lines in groups.items()]
    rows.sort(key=lambda row: (row["path"], row["line"]))
    missing = sorted(path for path in changes if path not in matched and os.path.splitext(path)[1].lower() in extensions)
    answer["uncovered"] = rows[:limit]
    answer["not_in_report"] = missing[:limit]
    answer["counts"].update(uncovered_lines=sum(row["count"] for row in rows), covered_lines=covered,
                            not_in_report=len(missing))
    answer["message"] = (f"{answer['counts']['uncovered_lines']} changed line(s) no executed line covers in "
                         f"{len({row['path'] for row in rows})} file(s); {covered} changed line(s) are covered.")
    return answer
