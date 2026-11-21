"""Impact report of a change set, in Markdown, for the description or a comment of a pull request.

It joins what Smart Tool already computes: the functions the diff touches and the tests to run (affected_tests), the
callers of each touched function and the tests that reach it (code_impact), the documentation anchors the change leaves
out of date (doc_scope), the files that usually change with the diff's files and are missing from it, the import cycles
through the changed files and the changed files that are hotspots (code_health), the new functions no caller or test reaches (code_health), the callers that still name a function
the diff removed or renamed and the callers of those whose signature changed (removed_callers), the decision notes that name a touched function (dossier) and the notes the
edit hook would have added to the whole diff (hook_decision).
Nothing is published: the agent pastes the report where it belongs.
"""
import os

import affected_tests
import code_health
import code_impact
import config
import doc_check
import doc_scope
import dossier
import hook_decision
import removed_callers

MAX_FUNCTIONS = 15
MAX_CALLERS = 8
TEST_DEPTH = 3
MAX_CHECKED_FILES = 50
MAX_COUPLED = 10
MAX_HOTSPOTS = 10
ALL_ROWS = 100000


def _callers(root, label, view_id):
    """Callers and reaching tests of one touched function, or None when the function is new and nothing calls it yet."""
    try:
        found = code_impact.impact(root, label, view_id, depth=TEST_DEPTH, limit=MAX_CALLERS)
    except ValueError:
        return None
    return found["callers"], found["counts"]["callers"], found["counts"]["tests"]


def _versions(root, base, changes):
    """The text of each changed file at the base and now.

    Parameters: root (str) project folder; base (str) git revision; changes (dict) the `affected_tests.affected` answer.
    Returns: list of (project-relative path, absolute path, text at the base or None when the file is new, text now or None
    when the file is gone); files that cannot be read as text are left out.
    """
    paths = sorted({*changes["changed"]["code"], *changes["changed"]["tests"], *changes["changed"]["other"]})
    found = []
    for rel in paths[:MAX_CHECKED_FILES]:
        full = os.path.join(root, *rel.split("/"))
        after = None
        if os.path.isfile(full):
            try:
                with open(full, encoding="utf-8") as stream:
                    after = stream.read()
            except (OSError, UnicodeDecodeError):
                continue
        try:
            before = affected_tests._git(root, "show", f"{base}:./{rel}")
        except ValueError:
            before = None
        found.append((rel, full, before, after))
    return found


def _hook_notes(versions):
    """The edit hook's notes over the changed files of the diff, which commits made without the hook never got.

    Parameters: versions (list) `_versions` of the diff.
    Returns: tuple (str notes, "" when nothing is worth saying; str problem, "" when the checks ran).
    """
    triples = [(full, before, after) for _rel, full, before, after in versions if after is not None]
    try:
        return hook_decision.review_diff(triples, config.load_config()), ""
    except (OSError, ValueError, RuntimeError) as exc:
        return "", f"the edit checks could not run: {exc}"


def _reviewed(root, versions, changed_code, review, key, what):
    """The answers of `review` over the changed code files that have something under `key`, or the reason it could not run.

    Returns: tuple (list of answers; str problem, "" when the check ran).
    """
    answers = []
    try:
        for rel, _full, before, after in versions:
            if rel in changed_code:
                answer = review(root, rel, before, after or "")
                if answer[key]:
                    answers.append(answer)
    except (OSError, ValueError, RuntimeError) as exc:
        return answers, f"the callers of {what} could not be read: {exc}"
    return answers, ""


def _removed_callers(root, versions, changed_code):
    """The callers that still name a function the diff removed or renamed, in the changed code files.

    Returns: tuple (list of answers of `removed_callers.review`, one per file that lost a function; str problem, "" when the
    check ran).
    """
    return _reviewed(root, versions, changed_code, removed_callers.review, "removed", "removed functions")


def _signature_callers(root, versions, changed_code):
    """The callers outside the file of the functions whose signature the diff changed, in the changed code files.

    Returns: tuple (list of answers of `removed_callers.signature_review`, one per file with a changed signature; str problem,
    "" when the check ran).
    """
    return _reviewed(root, versions, changed_code, removed_callers.signature_review, "changed", "functions with a changed signature")


def _also_defined(item):
    return f" A function named {item['name'].rpartition('.')[2]} also exists in {', '.join(item['also_defined_in'])}." \
        if item["also_defined_in"] else ""


def _callers_section(answers, problem, title, key, finding, quiet_text):
    """The Markdown lines of one caller section, none when there is nothing to say.

    Parameters: answers (list) of `removed_callers` answers; problem (str) why the check could not run, "" when it ran;
    title (str) the `###` heading; key (str) the answer key holding the names of the functions concerned; finding (str)
    format of a caller line over `item` and `sites`; quiet_text (str) format of the line for names with no caller.
    """
    if not answers and not problem:
        return []
    lines = [f"### {title}", ""]
    for answer in answers:
        for item in answer["findings"]:
            lines.append(finding.format(item=item, sites=removed_callers.sites_text(item), also=_also_defined(item)))
        quiet = [name for name in answer[key] if name not in {item["name"] for item in answer["findings"]}]
        if quiet:
            lines.append(quiet_text.format(names=", ".join(f"`{name}`" for name in quiet)))
    lines += list(dict.fromkeys(answer["note"] for answer in answers if answer["note"]))
    return lines + ([problem] if problem else []) + [""]


def _removed_lines(answers, problem):
    """The Markdown lines of the section on removed or renamed functions, none when the diff removes no function."""
    return _callers_section(answers, problem, "Callers of removed or renamed functions", "removed",
                            "- `{item[name]}` ({item[path]}) is gone and is still called from {sites}.{also}",
                            "No caller was found for the removed {names}.")


def _signature_lines(answers, problem):
    """The Markdown lines of the section on functions whose signature changed, none when the diff changes no signature."""
    return _callers_section(answers, problem, "Callers of functions whose signature changed", "changed",
                            "- `{item[name]}` ({item[path]}) has a new signature and is called from {sites} outside the file.{also}",
                            "No caller outside the file was found for {names}, whose signature changed.")


def _coupled(root, view_id, changes):
    """Files that usually change with the diff's files but are not in it, or the reason none could be computed."""
    paths = sorted({*changes["changed"]["code"], *changes["changed"]["tests"], *changes["changed"]["other"]})
    try:
        found = code_health.coupled_with(root, paths, view_id, MAX_COUPLED)
    except ValueError as exc:
        return [], f"The change coupling was not read: {exc}"
    return found["files"], f"The change coupling was not read: {found['note']}" if found["note"] else None


def _cycles(root, view_id, changed_code):
    """Import cycles that contain a changed code file, or the reason none could be computed.

    Returns: tuple (list of `code_health.import_cycles` entries; str note, what the graph cut; str problem, "" when the
    cycles were read).
    """
    try:
        found = code_health.import_cycles(root, view_id, limit=ALL_ROWS)
    except (ValueError, RuntimeError) as exc:
        return [], "", f"The import cycles were not read: {exc}"
    return [c for c in found["cycles"] if changed_code.intersection(c["files"])], found["note"], ""


def _hotspots(root, view_id, changed_code):
    """Changed code files among the hotspots of the project, or the reason none could be computed.

    Returns: tuple (list of `code_health.hotspots` rows, riskiest first; str problem, "" when the history was read).
    """
    try:
        found = code_health.hotspots(root, view_id, limit=ALL_ROWS)
    except (ValueError, RuntimeError) as exc:
        return [], f"The hotspots were not read: {exc}"
    if found["note"]:
        return [], f"The hotspots were not read: {found['note']}"
    return [f for f in found["files"] if f["path"] in changed_code][:MAX_HOTSPOTS], ""


def _added_functions(versions, changed_code):
    """The (path, qualified name) pairs of the functions the diff adds: those a changed code file defines now and did not at
    the base, every function of a new file included."""
    added = set()
    for rel, _full, before, after in versions:
        if rel in changed_code and after is not None and doc_check.supported(rel):
            known = {f["name"] for f in doc_check.functions(rel, before)} if before is not None else set()
            added.update((rel, f["name"]) for f in doc_check.functions(rel, after) if f["name"] not in known)
    return added


def _new_dead(root, view_id, versions, changed_code):
    """The functions the diff adds that no function and no test calls, from `code_health.dead_code`.

    Returns: tuple (list of `dead_code` entries; str note, what the graph cut; str problem, "" when the check ran). Nothing is
    asked of the graph when the diff adds no function.
    """
    added = _added_functions(versions, changed_code)
    if not added:
        return [], "", ""
    try:
        found = code_health.dead_code(root, view_id, limit=ALL_ROWS)
    except (ValueError, RuntimeError) as exc:
        return [], "", f"The new functions without a caller were not read: {exc}"
    return [f for f in found["no_caller"] if (f["path"], f["name"]) in added], found["graph_note"], ""


def _new_dead_lines(dead, note, problem):
    """The Markdown section on new functions nothing calls; left out when the diff adds none or all have a caller, unless
    the graph was cut or the check could not run, which are said."""
    if not (dead or note or problem):
        return []
    lines = ["### New functions nothing calls", ""]
    lines += [f"- `{f['name']}` ({f['path']}:{f['line']})" + ("" if f["public"] else " - private") for f in dead]
    if dead:
        lines.append("Candidates from the static call graph (Python and Go only): a call through a framework, a decorator or a "
                     "name built at run time is not seen.")
    elif not problem:
        lines.append(f"No new function without a caller was found. {note}".rstrip())
        note = ""
    lines += [note] if note else []
    return lines + ([problem] if problem else []) + [""]


def _shape_lines(cycles, cycles_note, cycles_problem, hotspots, hotspots_problem, changed_code):
    """The Markdown sections on import cycles and hotspots of the changed files; a section with nothing to say is left out
    unless the graph was cut or the check could not run, which are said."""
    lines = []
    if cycles or cycles_note or cycles_problem:
        lines += ["### Import cycles through the changed files", ""]
        lines += [f"- {c['size']} files import each other, directly or through a chain: "
                  + ", ".join(f"`{f}`" for f in c["files"]) + "; changed: "
                  + ", ".join(f"`{f}`" for f in c["files"] if f in changed_code) for c in cycles]
        if not cycles and not cycles_problem:
            lines.append(f"No cycle through the changed files was found. {cycles_note}")
        lines += [cycles_problem] if cycles_problem else []
        lines.append("")
    if hotspots or hotspots_problem:
        lines += ["### Changed files that are hotspots", ""]
        lines += [f"- `{f['path']}`: {f['commits']} commits, {f['lines']} lines, last changed {f['last_change']}" for f in hotspots]
        lines += [hotspots_problem] if hotspots_problem else []
        lines.append("")
    return lines


def _decisions(root, labels):
    """Decision notes and commit messages that name each touched function (`dossier.notes_about`, whose rule keeps a
    single word out of ordinary prose).

    Returns: tuple (dict label -> list of hits, str problem, "" when the search ran whole).
    """
    found, reasons = {}, []
    for label in labels:
        rel, _, qualified = label.partition("::")
        if not qualified:
            continue
        answer = dossier.notes_about(root, rel, qualified)
        reasons += [reason for reason in answer["reasons"] if reason not in reasons]
        if answer["hits"]:
            found[label] = answer["hits"]
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
    versions = _versions(root, base, changes)
    notes, notes_problem = _hook_notes(versions)
    removed, removed_problem = _removed_callers(root, versions, changed_code)
    resigned, resigned_problem = _signature_callers(root, versions, changed_code)
    coupled, coupled_note = _coupled(root, view_id, changes)
    decisions, decisions_note = _decisions(root, touched)
    cycles, cycles_note, cycles_problem = _cycles(root, view_id, changed_code) if changed_code else ([], "", "")
    hotspots, hotspots_problem = _hotspots(root, view_id, changed_code) if changed_code else ([], "")
    new_dead, new_dead_note, new_dead_problem = _new_dead(root, view_id, versions, changed_code)
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
    lines += _removed_lines(removed, removed_problem)
    lines += _signature_lines(resigned, resigned_problem)
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
    lines += _shape_lines(cycles, cycles_note, cycles_problem, hotspots, hotspots_problem, changed_code)
    lines += _new_dead_lines(new_dead, new_dead_note, new_dead_problem)
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
            "untested": untested, "coupled": coupled, "cycles": cycles, "hotspots": hotspots, "new_without_caller": new_dead, "decisions": decisions, "decisions_note": decisions_note,
            "edit_checks": notes or notes_problem,
            "removed_callers": [item for answer in removed for item in answer["findings"]],
            "signature_callers": [item for answer in resigned for item in answer["findings"]],
            "stale_anchors": stale, "stale_note": stale_note, "tests": changes["tests"],
            "commands": changes["commands"], "run_all": changes["run_all"], "counts": changes["counts"]}
