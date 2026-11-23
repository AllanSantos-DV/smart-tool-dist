"""The tests to run and the touched functions no test reaches, for the STAGED change: the two lines the pre-push summary
prints for a pushed range, over what `git diff --cached` holds. It joins `affected_tests` (the staged form) and the caller
and test counts `pr_impact` uses for its own "no test reaches" list. It is advisory: nothing here blocks, and a check that
cannot run says why instead of coming back empty.
"""
import affected_tests
import index_inventory
import pr_impact

LIMIT = 15
NOT_READY = ("the code graph of the project is not built yet, so the tests that reach the staged functions cannot be "
             "found (project_manage action=index)")


def _section(checked=True, problem="", note="", answer=None, untested=()):
    answer = answer or {}
    return {"checked": checked, "tests": answer.get("tests", []), "total": (answer.get("counts") or {}).get("tests", 0),
            "commands": answer.get("commands", []), "run_all": answer.get("run_all", False), "problem": problem,
            "note": note, "untested": list(untested)}


def _also_unstaged(root, paths):
    """The sentence naming the staged files that have unstaged edits too (the graph reads a file from disk, not from the
    index), or "" when there are none."""
    unstaged = set(affected_tests._git(root, "diff", "--name-only", "-z", "--relative", "--", ".").split("\0"))
    both = sorted(unstaged.intersection(paths))
    return (f"{', '.join(both)} also ha{'ve' if len(both) != 1 else 's'} unstaged edits: the graph reads the file as it is "
            "on disk, so its functions and tests may differ from the staged text.") if both else ""


def review(root, view_id=None):
    """The tests to run and the staged functions no test reaches.

    Parameters: root (str) project folder, inside a git repository; view_id (str or None) index view.
    Returns: dict {"checked": bool, "tests": [{"path", "why"}] most likely failures first (at most LIMIT), "total": int
    (test files selected), "commands": the commands that run them, "run_all": bool (a staged file can change every test),
    "untested": [path::name] the touched functions, at most `pr_impact.MAX_FUNCTIONS`, that no test reaches through static
    calls (a function the graph does not know yet counts), "problem": str (why it was not checked, "" when it was), "note":
    str (a limit of the answer)}. Nothing staged is a quiet, checked section that asks nothing of the graph; a project
    without a code graph, or a check that fails, is not checked and says why.
    Raises: ValueError when git cannot read the index.
    """
    staged = affected_tests._changes(root, "HEAD", staged=True)
    if not staged:
        return _section()
    try:
        if not index_inventory.inspect(root, view_id)["selected"]:
            return _section(False, NOT_READY)
        answer = affected_tests.affected(root, "HEAD", LIMIT, view_id, staged=True)
        reached = {label: pr_impact._callers(root, label, view_id)
                   for label in answer["touched_functions"][:pr_impact.MAX_FUNCTIONS]}
        note = _also_unstaged(root, staged)
    except (OSError, ValueError, RuntimeError) as exc:
        return _section(False, f"the check of the tests to run could not run: {exc}")
    untested = [label for label, found in reached.items() if found is None or found[2] == 0]
    return _section(note=note, answer=answer, untested=untested)
