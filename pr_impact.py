"""Impact report of a change set, in Markdown, for the description or a comment of a pull request.

It joins what Smart Tool already computes: the functions the diff touches and the tests to run (affected_tests), the
callers of each touched function and the tests that reach it (code_impact), the documentation anchors the change leaves
out of date (doc_scope), the files that usually change with the diff's files and are missing from it (code_health), the decision
notes that name a touched function (dossier) and the notes the edit hook would have added to the whole diff (hook_decision).
Nothing is published: the agent pastes the report where it belongs.
"""
import os
import re

import affected_tests
import code_health
import code_impact
import config
import doc_scope
import dossier
import hook_decision

MAX_FUNCTIONS = 15
MAX_CALLERS = 8
TEST_DEPTH = 3
MAX_CHECKED_FILES = 50
MAX_COUPLED = 10
MAX_NOTES_PER_FUNCTION = 2


def _callers(root, label, view_id):
    """Callers and reaching tests of one touched function, or None when the function is new and nothing calls it yet."""
    try:
        found = code_impact.impact(root, label, view_id, depth=TEST_DEPTH, limit=MAX_CALLERS)
    except ValueError:
        return None
    return found["callers"], found["counts"]["callers"], found["counts"]["tests"]


def _hook_notes(root, base, changes):
    """The edit hook's notes over the changed files of the diff, which commits made without the hook never got.

    Parameters: root (str) project folder; base (str) git revision; changes (dict) the `affected_tests.affected` answer.
    Returns: tuple (str notes, "" when nothing is worth saying; str problem, "" when the checks ran).
    """
    paths = sorted({*changes["changed"]["code"], *changes["changed"]["tests"], *changes["changed"]["other"]})
    triples = []
    for rel in paths[:MAX_CHECKED_FILES]:
        full = os.path.join(root, *rel.split("/"))
        if not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8") as stream:
                after = stream.read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            before = affected_tests._git(root, "show", f"{base}:./{rel}")
        except ValueError:
            before = None
        triples.append((full, before, after))
    try:
        return hook_decision.review_diff(triples, config.load_config()), ""
    except (OSError, ValueError, RuntimeError) as exc:
        return "", f"the edit checks could not run: {exc}"


def _coupled(root, view_id, changes):
    """Files that usually change with the diff's files but are not in it, or the reason none could be computed."""
    paths = sorted({*changes["changed"]["code"], *changes["changed"]["tests"], *changes["changed"]["other"]})
    try:
        found = code_health.coupled_with(root, paths, view_id, MAX_COUPLED)
    except ValueError as exc:
        return [], f"The change coupling was not read: {exc}"
    return found["files"], f"The change coupling was not read: {found['note']}" if found["note"] else None


def _name_pattern(name):
    """How a note must mention a function to count. A compound name (snake_case or camelCase) is distinctive, so the bare
    word counts; a single word such as `load` or `report` is also ordinary prose, so only a code reference counts:
    backticks, or a call with parentheses."""
    escaped = re.escape(name)
    if "_" in name or re.search(r"[a-z][A-Z]", name):
        return re.compile(rf"\b(?:{escaped}|{re.escape(name.replace('_', ' '))})\b", re.I)
    return re.compile(rf"`[\w.]*\b{escaped}\b[\w.()]*`|\b{escaped}\(")


def _decisions(root, labels):
    """Decision notes and commit messages that name each touched function: the dossier search over the project's notes and
    commit history, keeping only hits that contain the function's own name, as a term match alone is too loose.

    Returns: tuple (dict label -> list of hits, str problem, "" when the search ran whole).
    """
    found, reasons = {}, []
    for label in labels:
        name = label.partition("::")[2].rpartition(".")[2]
        if not name:
            continue
        answer = dossier.query(root, name.replace("_", " "), max_hits=MAX_NOTES_PER_FUNCTION * 3, history=True)
        reasons += [reason for reason in answer["reasons"] if reason not in reasons]
        named = _name_pattern(name)
        hits = [hit for hit in answer["hits"] if named.search(f"{hit['title']} {hit['snippet']}")]
        if hits:
            found[label] = hits[:MAX_NOTES_PER_FUNCTION]
    return found, f"The notes search was cut: {'; '.join(reasons)}" if reasons else ""


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
    untested = [label for label in touched if callers[label] is None or callers[label][2] == 0]
    notes, notes_problem = _hook_notes(root, base, changes)
    coupled, coupled_note = _coupled(root, view_id, changes)
    decisions, decisions_note = _decisions(root, touched)
    lines = [f"## Impact of the change against `{base}`", "",
             f"{changes['counts']['changed_files']} changed file(s), {len(changes['touched_functions'])} touched "
             f"function(s), {changes['counts']['tests']} test file(s) to run.", ""]
    if changes["touched_functions"]:
        lines += ["### Touched functions and who calls them", "",
                  "| Function | Callers | Tests | Called from |", "|---|---|---|---|"]
        for label in touched:
            reached = callers[label]
            if reached is None:
                lines.append(f"| `{label}` | 0 | none | new: nothing calls it yet |")
                continue
            sites, total, tests = reached
            shown = ", ".join(f"`{s['function']}` ({s['path']}:{s['line']})" for s in sites)
            more = f" (+{total - len(sites)} more)" if total > len(sites) else ""
            lines.append(f"| `{label}` | {total} | {tests or 'none'} | {shown or '-'}{more} |")
        if len(changes["touched_functions"]) > len(touched):
            lines.append(f"\n{len(changes['touched_functions']) - len(touched)} more touched function(s) not listed.")
        lines.append("")
        lines += ["### Touched functions no test reaches", ""]
        lines += [f"- `{label}`" for label in untested] if untested else ["Every touched function is reached by a test."]
        lines.append("")
    lines += ["### Documentation left out of date", ""]
    if stale_note:
        lines += [stale_note, ""]
    elif stale:
        lines += [f"- `{f['id']}`: {f['issue'].replace('_', ' ')} ({f['file']}:{f['line']})" for f in stale]
        lines += ["", "Regenerate with `project_manage action=docs generate=true` and review the prose of these entries.", ""]
    else:
        lines += ["No documented entry in the changed files is out of date.", ""]
    lines += ["### Edit checks over the whole diff", "", notes or notes_problem or "The edit checks find nothing to add.", ""]
    if touched:
        lines += ["### Decision notes of the touched functions", ""]
        lines += [f"- `{label}`: `{hit['id']}` {hit['title']}" for label, hits in decisions.items() for hit in hits]
        if not decisions:
            lines.append("No decision note or commit message names a touched function.")
        lines += ([decisions_note] if decisions_note else []) + [""]
    lines += ["### Files that usually change with these", ""]
    if coupled_note:
        lines += [coupled_note, ""]
    elif coupled:
        lines += [f"- `{f['path']}` changed in {f['shared']} commits with `{f['with']}` ({f['degree']}%) and is not in this diff"
                  for f in coupled] + [""]
    else:
        lines += ["No file that usually changes with these is missing from the diff.", ""]
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
    if changes.get("truncated"):
        lines += ["", changes["note"]]
    return {"base": base, "markdown": "\n".join(lines).rstrip("\n") + "\n", "touched": touched,
            "callers": {label: (reached[1] if reached else 0) for label, reached in callers.items()},
            "untested": untested, "coupled": coupled, "decisions": decisions, "edit_checks": notes or notes_problem,
            "stale_anchors": stale, "stale_note": stale_note, "tests": changes["tests"],
            "commands": changes["commands"], "run_all": changes["run_all"], "counts": changes["counts"]}
