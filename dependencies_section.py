"""The `dependencies` section of a change review: the imports the changed code files make that no manifest declares, and the
dependencies a changed manifest declares that no code imports, taken from `unused_deps.report` and narrowed to the files and
manifests of the diff. Each finding keeps the confidence and the reason `unused_deps` gives it. It is advisory: nothing here
blocks, and a check that cannot run says why instead of coming back empty.
"""
import posixpath

import dependency_notice
import unused_deps

ALL_ROWS = 100000
MORE = " (+{} more)"


def _section(checked, findings=(), problem="", notes=()):
    return {"checked": checked, "findings": list(findings), "problem": problem, "notes": list(notes)}


def describe(finding):
    """One sentence for a finding of `review`: the package, where it is imported or declared, how far to trust it and why."""
    if finding["kind"] == "undeclared":
        files = ", ".join(finding["files"]) + (MORE.format(finding["count"] - len(finding["files"]))
                                               if finding["count"] > len(finding["files"]) else "")
        what = f"is imported by {files} and no manifest declares it"
    else:
        what = f"is declared in {finding['manifest']} and no import uses it"
    return f"`{finding['name']}` {what} (confidence {finding['confidence']}): {finding['reason']}"


def _manifest(rel):
    return dependency_notice.ecosystem_of(rel) in unused_deps.LANGUAGES


def review(root, versions, view_id=None):
    """The dependency findings of the changed files and manifests.

    Parameters: root (str) project folder; versions (iterable) of (project-relative path, absolute path, text before or None
    for a new file, text after or None for a deleted one); view_id (str or None) index view of the code graph.
    Returns: dict {"checked": bool, "findings": [{"kind": "undeclared", "folder", "language", "name", "files", "count",
    "confidence", "reason"} or {"kind": "unused", "manifest", "ecosystem", "name", "specifier", "confidence", "reason"}, with
    "alias_candidates" when `unused_deps` gives them] undeclared first, "problem": str (why it was not checked, "" when it
    was), "notes": [str] (what the code graph cut)}. An undeclared import is kept when one of the files `unused_deps` lists for
    it is changed, an unused dependency when its manifest is changed; the graph is read only when the diff changes code or a
    manifest of a kind `unused_deps` reads and the project has such a manifest.
    """
    present = {rel for rel, _full, _before, after in versions if after is not None}
    manifests = {rel for rel in present if _manifest(rel)}
    code = {rel for rel in present if posixpath.splitext(rel)[1] in unused_deps._EXTENSIONS}
    if not (manifests or code) or not any(_manifest(rel) for rel in dependency_notice.manifests(root)):
        return _section(True)
    try:
        answer = unused_deps.report(root, view_id, limit=ALL_ROWS)
    except (OSError, ValueError, RuntimeError) as exc:
        return _section(False, problem=f"the dependencies could not be compared: {exc}")
    found = [{"kind": "undeclared", **item} for item in answer["undeclared"] if code.intersection(item["files"])]
    found += [{"kind": "unused", **item} for item in answer["unused"] if item["manifest"] in manifests]
    found.sort(key=lambda item: (item["kind"], item["name"]))
    return _section(True, found, notes=answer["notes"] if found else ())
