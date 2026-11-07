"""Impact report of a change set, in Markdown, for the description or a comment of a pull request.

It joins what Smart Tool already computes: the functions the diff touches and the tests to run (affected_tests), the
callers of each touched function (code_impact) and the documentation anchors the change leaves out of date (doc_scope).
Nothing is published: the agent pastes the report where it belongs.
"""
import os

import affected_tests
import code_impact
import doc_scope

MAX_FUNCTIONS = 15
MAX_CALLERS = 8


def _callers(root, label, view_id):
    """Callers of one touched function, or None when the function is new and nothing calls it yet."""
    try:
        found = code_impact.impact(root, label, view_id, depth=1, limit=MAX_CALLERS)
    except ValueError:
        return None
    return found["callers"], found["counts"]["callers"]


def _stale(root, view_id, paths):
    """Documentation findings that sit on the changed files, or the reason none could be computed."""
    if not os.path.isdir(os.path.join(root, doc_scope.DOCS_DIR)):
        return [], "docs/scope is not generated in this project; run project_manage action=docs generate=true."
    found = doc_scope.check(root, view_id, limit=200)["findings"]
    return [f for f in found if f["issue"] != "broken_link" and f["id"].rpartition("::")[0] in paths], None


def report(root, base="HEAD", limit=30, view_id=None):
    """Impact of the changes since base.

    Parameters: root (str) project folder; base (str) git revision; limit (int) rows per list; view_id (str or None).
    Returns: dict with 'markdown' and the data behind it (touched, tests, stale_anchors, commands, run_all).
    """
    changes = affected_tests.affected(root, base, limit, view_id)
    changed_code = set(changes["changed"]["code"])
    touched = changes["touched_functions"][:MAX_FUNCTIONS]
    callers = {label: _callers(root, label, view_id) for label in touched}
    stale, stale_note = _stale(root, view_id, changed_code)
    lines = [f"## Impact of the change against `{base}`", "",
             f"{changes['counts']['changed_files']} changed file(s), {len(changes['touched_functions'])} touched "
             f"function(s), {changes['counts']['tests']} test file(s) to run.", ""]
    if changes["touched_functions"]:
        lines += ["### Touched functions and who calls them", "", "| Function | Callers | Called from |", "|---|---|---|"]
        for label in touched:
            reached = callers[label]
            if reached is None:
                lines.append(f"| `{label}` | 0 | new: nothing calls it yet |")
                continue
            sites, total = reached
            shown = ", ".join(f"`{s['function']}` ({s['path']}:{s['line']})" for s in sites)
            more = f" (+{total - len(sites)} more)" if total > len(sites) else ""
            lines.append(f"| `{label}` | {total} | {shown or '-'}{more} |")
        if len(changes["touched_functions"]) > len(touched):
            lines.append(f"\n{len(changes['touched_functions']) - len(touched)} more touched function(s) not listed.")
        lines.append("")
    lines += ["### Documentation left out of date", ""]
    if stale_note:
        lines += [stale_note, ""]
    elif stale:
        lines += [f"- `{f['id']}`: {f['issue'].replace('_', ' ')} ({f['file']}:{f['line']})" for f in stale]
        lines += ["", "Regenerate with `project_manage action=docs generate=true` and review the prose of these entries.", ""]
    else:
        lines += ["No documented entry in the changed files is out of date.", ""]
    lines += ["### Tests to run", ""]
    if changes["tests"]:
        lines += [f"- `{t['path']}`: {t['why']}" for t in changes["tests"]]
        if changes.get("omitted"):
            lines.append(f"- ... and {changes['omitted']['tests']} more")
        lines += [""] + [f"`{c['command']}` (in `{c['cwd']}`)" if c.get("command") else f"{c['reason']} ({', '.join(c['files'])})"
                         for c in changes["commands"]]
    else:
        lines.append("No test reaches the changed code through imports or static calls.")
    if changes["run_all"]:
        lines += ["", "Run the whole suite: " + "; ".join(changes["run_all_reasons"]) + "."]
    return {"base": base, "markdown": "\n".join(lines).rstrip("\n") + "\n", "touched": touched,
            "callers": {label: (reached[1] if reached else 0) for label, reached in callers.items()},
            "stale_anchors": stale, "stale_note": stale_note, "tests": changes["tests"],
            "commands": changes["commands"], "run_all": changes["run_all"], "counts": changes["counts"]}
