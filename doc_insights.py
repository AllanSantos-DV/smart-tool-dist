"""Documents of the documentation scope that read the project's history and naming: hotspots, hidden coupling,
dead-code candidates, the onboarding tour, the glossary and the decision candidates found in the git log.

Everything is computed from the code graph and `git log`, without a model. The change-coupling and hotspot methods
follow the published definitions of code-maat (shared revisions over average revisions; revisions times size); no
code of that GPL tool is used.
"""
import builtins
import datetime
import functools
import importlib
import itertools
import os
import re
import sys
from collections import Counter, defaultdict

import affected_tests
import doc_scope
import index_views
import project_identity

MAX_COMMITS = 3000
MAX_ROWS = 25
COUPLING_MAX_CHANGESET = 30
COUPLING_MIN_REVISIONS = 5
COUPLING_MIN_SHARED = 5
COUPLING_MIN_DEGREE = 0.3
TOUR_STEPS = 8
TOUR_CALLS = 6
GLOSSARY_TERMS = 40
SIMPLE_TERMS = 12
ADR_CANDIDATES = 25
ADR_DRAFTS = 5
_FIELD, _RECORD = "\x1f", "\x1e"
HISTORIES_KEPT = 8
_histories = {}
_MANIFEST = re.compile(r"(^|/)(package\.json|requirements[\w.-]*\.txt|go\.mod|pom\.xml|pyproject\.toml|build\.gradle(\.kts)?)$")
_DECISION = re.compile(r"\b(decid\w*|replac\w*|switch\w*|migrat\w*|adopt\w*|drop\w*|instead of|introduc\w*|because)\b", re.I)
_HTTP_VERB = re.compile(r"^do_[A-Z]+$")
_FRAMEWORK_STYLE = re.compile(r"^(do_[A-Za-z]+|on_\w+|[a-z]+[A-Z]\w*|run|handle|setup|teardown)$")
_WORDS = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_STOP = frozenset("""get set add is has are was the and for not with from into that this per use run main init new old
    make build create update delete remove check handle process helper helpers util utils self cls args kwargs data
    value values item items list dict map key keys name names text str int bool none true false test tests all any""".split())


def history(root):
    """Commits of the project's git history, newest first, with the project-relative files each touched.

    One `git log` of up to MAX_COMMITS commits is the costliest step of a documentation run, and a run reads the history
    more than once (prose writing generates the scope before and after), so the answer is reused while the Git view's
    stamp (HEAD, index, reflog, packed-refs, branch ref) has not moved; checking it is a stat, no process.

    Returns: (commits, note): commits is a list of {hash, time, subject, body, files}; note is None, or the reason no
    history could be read (not a git repository, git missing), which the page shows in place of the table. The
    returned lists are shared with later calls and must not be changed.
    """
    try:
        view = index_views.describe(root, fresh=True)
    except (RuntimeError, ValueError, OSError):
        view = None
    if not view or not view.get("git"):
        return _read_history(root)
    key = project_identity.canonical_root(root)
    saved = _histories.get(key)
    if saved and saved[0] == view["stamp"]:
        return saved[1]
    result = _read_history(root)
    if not index_views.stamp_changed(view):
        if len(_histories) >= HISTORIES_KEPT:
            _histories.clear()
        _histories[key] = (view["stamp"], result)
    return result


def _read_history(root):
    try:
        raw = affected_tests._git(root, "-c", "core.quotepath=false", "log", "--no-merges", f"-n{MAX_COMMITS}", "--relative",
                                  "--name-status", "-M", f"--pretty=format:{_RECORD}%h{_FIELD}%ct{_FIELD}%s{_FIELD}%b{_FIELD}", "--", ".")
    except (ValueError, OSError) as error:
        return [], f"No git history could be read: {error}"
    commits, current = [], {}
    for chunk in raw.split(_RECORD)[1:]:
        short, stamp, subject, body, names = chunk.split(_FIELD, 4)
        files = []
        for line in filter(None, names.splitlines()):
            fields = line.split("\t")
            if fields[0][0] == "R" and len(fields) == 3:
                current[fields[1]] = current.get(fields[2], fields[2])
            path = fields[-1]
            path = current.get(path, path)
            if path not in files:
                files.append(path)
        commits.append({"hash": short, "time": int(stamp), "subject": subject, "body": body.strip(), "files": files})
    return commits, (None if commits else "The git history holds no commit that touches this project.")


def _date(stamp):
    return datetime.datetime.fromtimestamp(stamp, datetime.timezone.utc).strftime("%Y-%m-%d")


def _lines(model, path):
    text = model.read(path)
    return text.count("\n") + 1 if text else 0


def hotspots(model, commits):
    """Rows (path, commits, lines, last change) of the code files ranked by commits times lines, highest first."""
    code = {p for p in model.files() if model.kind_of(p) == "code"}
    churn, last = Counter(), {}
    for commit in commits:
        for path in commit["files"]:
            if path in code:
                churn[path] += 1
                last.setdefault(path, commit["time"])
    rows = [(path, count, _lines(model, path), last[path]) for path, count in churn.items()]
    return sorted((r for r in rows if r[2]), key=lambda r: (-r[1] * r[2], r[0]))


def coupling(model, commits):
    """Pairs of files that change in the same commits: (a, b, shared, degree, linked), strongest first.

    degree is the shared commits over the average commits of the two files; linked tells whether one imports the other.
    Commits that touch more than COUPLING_MAX_CHANGESET files are skipped, as bulk edits couple everything.
    """
    present = set(model.files())
    revisions, shared = Counter(), Counter()
    for commit in commits:
        files = sorted({p for p in commit["files"] if p in present})
        if not files or len(files) > COUPLING_MAX_CHANGESET:
            continue
        revisions.update(files)
        shared.update(itertools.combinations(files, 2))
    imports = {(d["source"], d["target"]) for d in model.graph.get("dependencies") or [] if d.get("target")}
    found = []
    for (a, b), count in shared.items():
        if count < COUPLING_MIN_SHARED or min(revisions[a], revisions[b]) < COUPLING_MIN_REVISIONS:
            continue
        degree = count / ((revisions[a] + revisions[b]) / 2)
        if degree >= COUPLING_MIN_DEGREE:
            found.append((a, b, count, degree, (a, b) in imports or (b, a) in imports))
    return sorted(found, key=lambda r: (-r[3], -r[2], r[0], r[1]))


@functools.lru_cache(maxsize=None)
def _library_defines(base, method):
    """True when the standard-library class named by the dotted path defines the method."""
    module, _dot, name = base.rpartition(".")
    try:
        owner = getattr(importlib.import_module(module), name) if module else getattr(builtins, name)
    except (ImportError, AttributeError, ValueError):
        return False
    return hasattr(owner, method)


def _framework_method(function, method, project):
    """True when a method may be called by the class it extends, outside the call graph: the standard-library base
    defines it (handle, run) or names an HTTP verb handler (do_GET, which the library looks up by name), or a third-party base is extended with a name in that library's style (Qt's
    eventFilter, an on_ handler). Bases defined in the project are not libraries."""
    for base in function.get("bases", ()):
        root = base.split(".")[0]
        if base.startswith(".") or root in project:
            continue
        if root in sys.stdlib_module_names or base in dir(builtins):
            if _library_defines(base, method) or _HTTP_VERB.match(method):
                return True
        elif _FRAMEWORK_STYLE.match(method):
            return True
    return False


def dead_code(model, entries):
    """Anchors of code functions nothing calls (static calls only) and of those only tests call, in file order. A call
    or a reference (the function handed over as a value) from another function or from module-level code counts as a
    use. A function with a decorator other than staticmethod or classmethod is left out: property, route and
    registration decorators call it from outside the graph, as are the unnamed functions the parser calls `callback` or
    `anonymous`, which run as arguments of the call that holds them."""
    entry = set(entries)
    project = {p.split("/")[0].split(".")[0] for p in model.files()}
    alone, tested = [], []
    for anchor, function in sorted(model.function_of.items(), key=lambda i: (i[1]["path"], i[1]["start"])):
        name = function["name"].split(".")[-1]
        if model.kind[function["path"]] != "code" or anchor in entry or (name.startswith("__") and name.endswith("__")) \
                or name in ("main", "callback", "anonymous") or anchor in model.used_by_module \
                or doc_scope.registered(function) or _framework_method(function, name, project):
            continue
        callers = model.callers[anchor]
        if not callers:
            alone.append(anchor)
        elif all(model.kind[model.function_of[c]["path"]] == "test" for c in callers):
            tested.append(anchor)
    return alone, tested


def terms(model):
    """Words of the code identifiers, as (word, functions using it, example identifiers), most used first."""
    used, examples = defaultdict(set), defaultdict(list)
    for anchor, function in model.function_of.items():
        if model.kind[function["path"]] != "code":
            continue
        name = function["name"].split(".")[-1]
        for word in {w.lower() for w in _WORDS.findall(name)}:
            if len(word) >= 3 and word not in _STOP and not word.isdigit():
                used[word].add(anchor)
                if name not in examples[word] and len(examples[word]) < 3:
                    examples[word].append(name)
    ranked = sorted(used, key=lambda w: (-len(used[w]), w))
    return [(w, len(used[w]), examples[w]) for w in ranked if len(used[w]) >= 2]


def adr_candidates(commits):
    """Commits that look like a decision: a subject with decision wording, or a change to a dependency manifest."""
    found = []
    for commit in commits:
        signals = []
        if _DECISION.search(commit["subject"]):
            signals.append("subject")
        if any(_MANIFEST.search(p) for p in commit["files"]):
            signals.append("dependencies")
        if signals:
            found.append({**commit, "signals": signals})
    return found


def _table(lines, head, rows):
    return lines + [f"| {' | '.join(head)} |", f"|{'---|' * len(head)}"] + [f"| {' | '.join(map(str, r))} |" for r in rows]


def hotspots_page(model, head, done, commits, note):
    lines = head(model, "Hotspots")
    if note:
        return done(lines + [note])
    rows = hotspots(model, commits)[:MAX_ROWS]
    lines += [f"Code files ranked by the commits that touched them times their lines (the last {len(commits)} commits): "
              "the files that change most and weigh most are the riskiest to modify.", ""]
    if not rows:
        return done(lines + ["No code file of the index appears in the history."])
    return done(_table(lines, ["File", "Commits", "Lines", "Score", "Last change"],
                       [(f"`{p}`", c, n, c * n, _date(t)) for p, c, n, t in rows]))


def coupling_page(model, head, done, commits, note):
    lines = head(model, "Change coupling")
    if note:
        return done(lines + [note])
    pairs = coupling(model, commits)
    hidden = [p for p in pairs if not p[4]]
    lines += ["Files that changed in the same commits without an import between them, which the call graph does not show. "
              f"Degree is the shared commits over the average commits of the two files; shown from {COUPLING_MIN_SHARED} "
              f"shared commits and {int(COUPLING_MIN_DEGREE * 100)}%, in commits of at most {COUPLING_MAX_CHANGESET} files.", ""]
    if hidden:
        lines = _table(lines, ["File", "Changes with", "Shared commits", "Degree"],
                       [(f"`{a}`", f"`{b}`", n, f"{int(d * 100)}%") for a, b, n, d, _l in hidden[:MAX_ROWS]]) + [""]
    else:
        lines += ["No pair of files without an import between them changes together often enough.", ""]
    return done(lines + [f"{len(pairs) - len(hidden)} more pair(s) change together but already import each other."])


def dead_code_page(model, head, done, entries):
    lines = head(model, "Dead-code candidates")
    alone, tested = dead_code(model, entries)
    lines += ["Functions no other function or module-level code calls or hands over as a value (Python and Go only; in "
              "other languages a function passed as a value counts as uncalled), from the static call graph only: a call "
              "through a framework, a decorator registration, reflection or a name built at run time is not seen, so "
              "read each one before deleting it.", ""]
    for title, anchors in (("No caller", alone), ("Called only from tests", tested)):
        lines += [f"## {title} ({len(anchors)})", ""]
        if not anchors:
            lines += ["None.", ""]
            continue
        lines += [f"- `{model.function_of[a]['name']}` ({model.function_of[a]['path']}:{model.function_of[a]['start']})"
                  + ("" if model.function_of[a]["required"] else " - private") for a in anchors[:100]]
        lines += [f"- ... and {len(anchors) - 100} more" if len(anchors) > 100 else "", ""]
    return done(lines)


def tour_page(model, head, done, entries):
    lines = head(model, "Onboarding tour")
    if not entries:
        return done(lines + ["No entry point found: declare the layers (project_manage action=layers)."])
    lines += ["Read the entry points in this order, highest layer first; each step lists the functions it calls next.", ""]
    for number, anchor in enumerate(entries[:TOUR_STEPS], 1):
        function = model.function_of[anchor]
        calls = [model.function_of[c] for _caller, c in model.flow_edges(anchor)][:TOUR_CALLS]
        title = doc_scope.entry_title(model, anchor, entries)
        lines += [f"{number}. {doc_scope.entry_link(model, anchor, entries, '../functional.md')}"
                  + ("" if title.endswith(f"in `{function['path']}`") else f" in `{function['path']}`")
                  + (f": {function['doc']}" if function.get("doc") else "")]
        lines += [f"   - then `{c['name']}` ({c['path']})" for c in calls]
    if len(entries) > TOUR_STEPS:
        lines += ["", f"{len(entries) - TOUR_STEPS} more entry point(s) in the [functional overview](../functional.md)."]
    return done(lines)


def glossary_page(model, head, done):
    lines = head(model, "Glossary")
    found = terms(model)[:GLOSSARY_TERMS]
    lines += ["Words the code uses in function names, by how many functions use them. Use the same words in the "
              "documentation.", ""]
    if not found:
        return done(lines + ["No word is shared by two functions."])
    return done(_table(lines, ["Word", "Functions", "Examples"], [(f"`{w}`", n, ", ".join(f"`{e}`" for e in ex)) for w, n, ex in found]))


def simple_terms(model):
    """The most used words of the code, for the plain-words summary."""
    return [w for w, _n, _e in terms(model)[:SIMPLE_TERMS]]


def adr_section(commits, note):
    """Lines for the decision-records page: commits that look like decisions with a draft for the latest ones."""
    lines = ["", "## Candidates from the git log", ""]
    if note:
        return lines + [note]
    found = adr_candidates(commits)[:ADR_CANDIDATES]
    if not found:
        return lines + ["No commit with decision wording or a dependency change in the history."]
    lines += ["Commits that look like a decision, by their subject or by a change to a dependency manifest. Each is a "
              "candidate for a record, never a record: only the person who made the decision knows the reasons.", ""]
    lines = _table(lines, ["Date", "Commit", "Subject", "Signal"],
                   [(_date(c["time"]), f"`{c['hash']}`", c["subject"].replace("|", "\\|"), ", ".join(c["signals"])) for c in found])
    for commit in found[:ADR_DRAFTS]:
        lines += ["", f"### Draft: {commit['subject']}", "", "```markdown", f"# NNNN. {commit['subject']}", "", "Status: proposed", "",
                  "## Context", "", commit["body"] or "To write: why the change was needed.", "", "## Decision", "",
                  f"Commit `{commit['hash']}` ({_date(commit['time'])}): {commit['subject']}", "", "## Consequences", "",
                  f"To write. The commit touched {len(commit['files'])} file(s).", "```"]
    return lines
