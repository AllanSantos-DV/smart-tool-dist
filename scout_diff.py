"""smart_scout over the current git diff: the candidates are the changed hunks, the callers of the changed functions and
the tests that reach them, in place of whole files found by search.

It reuses the diff reading of `affected_tests` (changed lines, touched functions) and the call graph of `code_impact`
(callers and tests per function, as `pr_impact` asks for them), and hands the rows to `scout.evidence`, so every line
shown is still read from disk now. Outside a git repository, or with a base git does not know, it raises.
"""
import affected_tests
import code_impact
import pr_impact
import scout

CONTEXT_LINES = 2
CALLER_LINES = 3
TEST_LINES = 12
HUNKS_PER_FILE = 6
WINDOWS_PER_FILE = 12
WHOLE_FILE = 100000
MAX_NAMED = 6


def source(base):
    """The `source:` line of an answer read from the diff against `base`."""
    return f"git diff against {base}: the changed lines, plus the callers and the tests of the changed functions"


def _runs(lines):
    """Inclusive `(first, last)` ranges of the consecutive numbers of `lines`, or the whole file when `lines` is None."""
    if lines is None:
        return [(1, WHOLE_FILE)]
    runs = []
    for number in sorted(lines):
        if runs and number == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], number)
        else:
            runs.append((number, number))
    return runs


def _checked(root, base):
    """The changed paths of the diff against `base` (`affected_tests._changes`), after checking that `root` is inside a
    git repository and that `base` names a commit; raises ValueError saying which."""
    if not scout.BASE_REVISION.fullmatch(base) or base.startswith("-"):
        raise ValueError("base must be a git revision such as HEAD, main or origin/main")
    try:
        affected_tests._git(root, "rev-parse", "--show-toplevel")
    except ValueError as exc:
        raise ValueError(f"smart_scout with diff=true needs a git repository, and {root} is not inside one ({exc})") from None
    try:
        affected_tests._git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    except ValueError:
        raise ValueError(f"smart_scout with diff=true: base {base!r} is not a commit of this repository") from None
    return affected_tests._changes(root, base)


def _score(root, path, first, last, terms):
    """How many times the `terms` occur in lines `first..last` of `path` as it is on disk (0 when unreadable)."""
    lines = scout.disk_lines(root, path, first, last) or []
    return sum(text.casefold().count(term) for _number, text in lines for term in terms)


def _named(paths):
    return ", ".join(paths[:MAX_NAMED]) + (f" and {len(paths) - MAX_NAMED} more" if len(paths) > MAX_NAMED else "")


def _hunk_rows(root, base, changes, terms, max_files):
    """Rows `{path, lines, snippet, symbols}` of the changed hunks (each with CONTEXT_LINES around it) of the
    `max_files` changed files holding most `terms`, at most HUNKS_PER_FILE per file, plus the warnings naming what was
    left out and why."""
    warnings, files = [], []
    deleted = [path for path, change in sorted(changes.items()) if change["status"] == "deleted"]
    unreadable = []
    for path, change in sorted(changes.items()):
        if change["status"] == "deleted":
            continue
        if scout.disk_lines(root, path, 1, 1) is None:
            unreadable.append(path)
            continue
        hunks = []
        for first, last in _runs(change["lines"]):
            start, end = max(1, first - CONTEXT_LINES), min(last + CONTEXT_LINES, WHOLE_FILE)
            hunks.append((_score(root, path, start, end, terms), start, {
                "path": path, "lines": f"{start}-{end}", "snippet": "", "symbols": f"changed against {base}"}))
        files.append((sum(score for score, _start, _row in hunks), path, hunks))
    kept = sorted(files, key=lambda item: (-item[0], item[1]))[:max_files]
    left = sorted(path for _total, path, _hunks in files if path not in {item[1] for item in kept})
    if left:
        warnings.append(f"{len(left)} more changed file(s) not shown (max_files={max_files}): {_named(left)}.")
    rows = []
    for _total, path, hunks in sorted(kept, key=lambda item: item[1]):
        best = sorted(sorted(hunks, key=lambda hunk: -hunk[0])[:HUNKS_PER_FILE], key=lambda hunk: hunk[1])
        if len(hunks) > len(best):
            warnings.append(f"{len(hunks) - len(best)} more hunk(s) of {path} not shown (the {HUNKS_PER_FILE} holding "
                            "most terms are).")
        rows += [row for _score_, _start, row in best]
    if deleted:
        warnings.append(f"Deleted, so nothing to read: {_named(deleted)}.")
    if unreadable:
        warnings.append(f"Not readable as text lines by the index rules (binary, secret, too large, empty): {_named(unreadable)}.")
    return rows, warnings


def _impact_rows(root, labels, max_files, view_id):
    """Rows of the callers (CALLER_LINES around the call) and of the tests (the first TEST_LINES of the test function)
    of the touched functions `labels`, at most `max_files` files of each kind, plus the warnings. A function nothing
    calls yet has no row."""
    callers, tests = [], []
    for label in labels:
        try:
            found = code_impact.impact(root, label, view_id, depth=pr_impact.TEST_DEPTH, limit=pr_impact.MAX_CALLERS)
        except ValueError:
            continue
        reaching = {(site["path"], site["function"]) for site in found["tests"]}
        callers += [{"path": site["path"], "lines": f"{max(1, site['line'] - CALLER_LINES)}-{site['line'] + CALLER_LINES}",
                     "snippet": "", "symbols": f"{site['function']} calls {label}"} for site in found["callers"]
                    if site.get("path") and (site["path"], site["function"]) not in reaching]
        tests += [{"path": site["path"], "lines": f"{site['line']}-{site['line'] + TEST_LINES - 1}", "snippet": "",
                   "symbols": f"test {site['function']} reaches {label} in {site['hops']} call(s)"}
                  for site in found["tests"] if site.get("path")]
    warnings, rows = [], []
    for kind, found in (("caller", callers), ("test", tests)):
        files = list(dict.fromkeys(row["path"] for row in found))
        if len(files) > max_files:
            warnings.append(f"{len(files) - max_files} more {kind} file(s) not shown (max_files={max_files}): "
                            f"{_named(files[max_files:])}.")
        rows += [row for row in found if row["path"] in files[:max_files]]
    return rows, warnings


def candidates(root, base, terms, max_files, view_id=None):
    """The rows smart_scout reads for the diff of the working tree against `base`.

    Parameters: root (str) project folder, inside a git repository; base (str) git revision; terms (list) the lowercase
    words of the question, which choose among files and hunks when there are more than `max_files`; max_files (int) files
    shown per kind (changed, caller, test); view_id (str or None).
    Returns: dict with `rows` (changed hunks first, then callers, then tests; each `{path, lines, snippet, symbols}` as
    `scout.evidence` takes them) and `warnings` (what was left out and why; an empty diff is said).
    Raises: ValueError when `root` is not inside a git repository or `base` is not a commit of it.
    """
    changes = _checked(root, base)
    rows, warnings = _hunk_rows(root, base, changes, terms, max_files)
    if not changes:
        return {"rows": [], "warnings": [f"No changes against {base}: there is no diff to read."]}
    labels = affected_tests.affected(root, base, pr_impact.MAX_FUNCTIONS, view_id)["touched_functions"]
    more, more_warnings = _impact_rows(root, labels, max_files, view_id)
    return {"rows": rows + more, "warnings": warnings + more_warnings}


def evidence(root, rows, terms, window_lines):
    """Evidence items of `candidates` rows: `scout.evidence` with every file kept and up to WINDOWS_PER_FILE windows per
    file, since the rows were already limited per kind."""
    return scout.evidence(root, rows, terms, len({row["path"] for row in rows}) or 1, window_lines, WINDOWS_PER_FILE)
