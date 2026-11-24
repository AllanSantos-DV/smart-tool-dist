"""The public functions that a release would break for their users: those removed or given another signature since the latest
tag, each with the callers outside the file that defined it.

The diff against the tag, the removed functions and the changed signatures are those `pr_impact` builds for a pull request,
restricted to public names (`release_notes._public`). Nothing is written, tagged or published.
"""
import affected_tests
import pr_impact
import release_notes
import removed_callers

DEFAULT_LIMIT = 30

RULE = ("Compared with the latest tag reachable from HEAD, working tree included: a public function (no private segment in "
        "its name) is `removed` when a changed code file no longer defines its name (a rename counts), and `resigned` when "
        "the file still defines it once with other parameters, order, defaults or return annotation. `callers` are the calls "
        "the code graph found in other files, by definition or by name, plus the imports of a removed name and the calls a "
        "removed function leaves in its own file; a function nothing calls is listed with no caller. Static analysis: a call "
        "through a framework, a string or a name built at run time is not seen, and only the first "
        f"{pr_impact.MAX_CHECKED_FILES} changed files are read.")
NO_TAG = "The repository has no tag, so there is no release to compare the public API with."


def _latest_tag(root):
    """The latest tag reachable from HEAD, None when the repository has none.

    Raises: ValueError when the folder is not a git repository.
    """
    affected_tests._git(root, "rev-parse", "--git-dir")
    try:
        return affected_tests._git(root, "describe", "--tags", "--abbrev=0").strip()
    except ValueError:
        return None


def _tagged(review):
    """`review` with the file it was asked about added to its answer, which the answer does not carry."""
    def run(root, rel, before, after):
        return {**review(root, rel, before, after), "path": rel}
    return run


def _rows(answers, key):
    """One row per public function an answer names under `key`, with its callers (none when the graph found no call)."""
    rows = []
    for answer in answers:
        found = {item["name"]: item for item in answer["findings"]}
        for name in answer[key]:
            if release_notes._public(f"{answer['path']}::{name}"):
                rows.append(found.get(name) or {"name": name, "path": answer["path"], "callers": [], "total": 0,
                                                "also_defined_in": []})
    return sorted(rows, key=lambda row: (row["path"], row["name"]))


def _read(root, base, view_id):
    """The removed and re-signed answers of every changed code file against `base`, and the notes of what was left out.

    Raises: RuntimeError when the code graph cannot be read.
    """
    changes = affected_tests.affected(root, base, pr_impact.ALL_ROWS, view_id)
    changed_code = set(changes["changed"]["code"])
    versions = pr_impact._versions(root, base, changes)
    removed, problem = pr_impact._reviewed(root, versions, changed_code, _tagged(removed_callers.review), "removed",
                                           "removed functions")
    resigned, resigned_problem = pr_impact._reviewed(root, versions, changed_code, _tagged(removed_callers.signature_review),
                                                     "changed", "functions with a changed signature")
    if problem or resigned_problem:
        raise RuntimeError(problem or resigned_problem)
    notes = list(dict.fromkeys(answer["note"] for answer in removed + resigned if answer["note"]))
    skipped = len({*changes["changed"]["code"], *changes["changed"]["tests"], *changes["changed"]["other"]}) - len(versions)
    if skipped > 0:
        notes.append(f"{skipped} changed file(s) were not compared (past the first {pr_impact.MAX_CHECKED_FILES} or not "
                     "UTF-8 text).")
    return removed, resigned, notes


def report(root, view_id=None, limit=DEFAULT_LIMIT):
    """The public functions removed or re-signed since the latest tag, with their callers outside the defining file.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) rows in each list.
    Returns: dict {"tag": str or None (None when the repository has no tag; both lists are then empty), "removed":
    [{"name", "path", "callers": [{"path", "line"}] (up to 5), "total": int, "also_defined_in": [paths]}], "resigned": same
    rows for the functions with another signature, "counts": {"removed", "resigned", "callers"} (whole totals, before
    `limit`), "note": str (what the graph cut, what was not compared, or that there is no tag; "" otherwise), "rule": str}.
    Raises: ValueError when the folder is not a git repository; RuntimeError when the code graph cannot be read.
    """
    tag = _latest_tag(root)
    if tag is None:
        return {"tag": None, "removed": [], "resigned": [], "counts": {"removed": 0, "resigned": 0, "callers": 0},
                "note": NO_TAG, "rule": RULE}
    removed, resigned, notes = _read(root, tag, view_id)
    gone, signed = _rows(removed, "removed"), _rows(resigned, "changed")
    return {"tag": tag, "removed": gone[:limit], "resigned": signed[:limit],
            "counts": {"removed": len(gone), "resigned": len(signed), "callers": sum(row["total"] for row in gone + signed)},
            "note": " ".join(notes), "rule": RULE}
