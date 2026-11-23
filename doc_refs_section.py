"""The `doc_refs` section of a change review: the README and `docs` lines that still name a function the changed files removed or
renamed, or a file they deleted or moved. It narrows the project-wide answer of `doc_refs.check` to the references the diff made
stale. It is advisory: nothing here blocks, and a check that cannot run says why instead of coming back empty.
"""
import posixpath
import re

import doc_check
import doc_refs
import removed_callers

ALL_ROWS = 100000
_NAME = re.compile(r"([A-Za-z_]\w*)(?:\(\))?$")


def _section(checked, findings=(), problem=""):
    return {"checked": checked, "findings": list(findings), "problem": problem}


def describe(finding):
    """One sentence for a finding of `review`: which doc line names what, and what the diff did to it."""
    return f"{finding['path']}:{finding['line']} names `{finding['ref']}`, and {finding['because']}"


def _removed(versions):
    """What the diff took away: ({short function name: path of the file that lost it}, {deleted paths})."""
    names, deleted = {}, set()
    for rel, _full, before, after in versions:
        if before is None:
            continue
        if after is None:
            deleted.add(rel)
        if doc_check.supported(rel):
            for short in removed_callers._gone(rel, before, after or ""):
                names.setdefault(short, rel)
    return names, deleted


def _names_path(ref, deleted):
    """The deleted path a doc reference to a path or a link names, or None. A reference is matched whole, by its tail (a
    relative path climbing out of the doc's folder, a bare file name) or, for a folder, by what was under it."""
    target = posixpath.normpath(ref.split("#")[0].rstrip("/"))
    while target.startswith("../"):
        target = target[3:]
    for path in sorted(deleted):
        if path == target or path.endswith("/" + target) or path.startswith(target + "/"):
            return path
    return None


def _because(row, names, deleted):
    """Why the diff made one stale row stale, or "" when it did not."""
    ref = row["ref"]
    if row["kind"] == "symbol":
        path, _, tail = ref.partition("::")
        if tail and path in deleted:
            return f"{path} is deleted"
        found = _NAME.search(tail or ref)
        if found and found[1] in names:
            return f"{found[1]} was removed or renamed in {names[found[1]]}"
        return ""
    path = _names_path(ref, deleted)
    return f"{path} is deleted" if path else ""


def review(root, versions):
    """The doc lines that still name code the changed files removed, renamed or deleted.

    Parameters: root (str) project folder, a git repository; versions (iterable) of (project-relative path, absolute path,
    text before or None for a new file, text after or None for a deleted one).
    Returns: dict {"checked": bool, "findings": [{"path" (the doc), "line", "kind" (path, link or symbol), "ref", "why" (what
    `doc_refs` says is missing), "because" (what the diff did)}] in doc and line order, "problem": str (why it was not
    checked, "" when it was)}. The docs are read only when the diff removes a function or deletes a file.
    """
    names, deleted = _removed(list(versions))
    if not (names or deleted):
        return _section(True)
    try:
        answer = doc_refs.check(root, limit=ALL_ROWS)
    except (OSError, ValueError, RuntimeError) as exc:
        return _section(False, problem=f"the documentation references could not be read: {exc}")
    found = []
    for entry in answer["files"]:
        for row in entry["stale"]:
            because = _because(row, names, deleted)
            if because:
                found.append({"path": entry["path"], "line": row["line"], "kind": row["kind"], "ref": row["ref"],
                              "why": row["why"], "because": because})
    return _section(True, sorted(found, key=lambda item: (item["path"], item["line"], item["ref"])))
