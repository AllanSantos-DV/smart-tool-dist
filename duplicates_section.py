"""The `duplicates` section of a change review: the functions a set of changed files adds or edits whose body copies code the
project already has, found by the same check the edit hook runs (`duplicates.on_write`) over each file's text before and after.
It is advisory: nothing here blocks, and a check that cannot run says why instead of coming back empty.
"""
import code_graph
import config
import doc_check
import duplicates

NOT_READY = ("the code graph of the project is not built yet, so there is no index of existing functions to compare with "
             "(project_manage action=index)")


def _section(checked, findings=(), problem="", files=0):
    return {"checked": checked, "findings": list(findings), "problem": problem, "files": files}


def describe(finding):
    """One sentence for a finding of `review`: which function copies which, exact or near."""
    existing = finding["existing"]
    where = f"{existing['name']} in {existing['path']}:{existing['lines']}"
    here = f"{finding['name']} ({finding['path']}:{finding['lines']})"
    if finding["type"] == "exact":
        return f"{here} has the same body as {where}"
    return f"{here} is near-identical to {where} (score {finding['score']})"


def review(root, versions):
    """The duplicates among the functions the changed files add or edit.

    Parameters: root (str) project folder; versions (iterable) of (project-relative path, absolute path, text before or None
    for a new file, text after or None for a deleted one).
    Returns: dict {"checked": bool, "findings": [{"path", "name", "lines", "type" (exact or near), "score", "existing":
    {"name", "path", "lines"}}], "problem": str (why it was not checked, "" when it was), "files": int (changed code files
    looked at)}. Not checked when `duplicate_mode` is off, when the project has no code graph yet or when the check fails.
    """
    candidates = [(rel, full, before, after) for rel, full, before, after in versions
                  if after is not None and doc_check.supported(full)]
    if not candidates:
        return _section(True)
    try:
        if config.duplicate_mode() == "off":
            return _section(False, problem="the duplicates check is off (duplicate_mode in the setup)")
        found = []
        with code_graph.pending_scope():
            if duplicates._write_pool(root) is None:
                return _section(False, problem=NOT_READY)
            for rel, full, before, after in candidates:
                edited = doc_check.written(full, before, after)
                if edited["touched"]:
                    found += [{"path": rel, **item} for item in duplicates.on_write(root, full, before, after, edited)]
    except (OSError, ValueError, RuntimeError) as exc:
        return _section(False, problem=f"the duplicates check could not run: {exc}")
    return _section(True, sorted(found, key=lambda item: (item["path"], int(item["lines"].split("-")[0]))),
                    files=len(candidates))
