"""Tests to run for the current changes: git diff against a base (default HEAD, so staged, unstaged and untracked
files), then every test file that imports a changed file directly or through other files, plus changed tests, ordered
so the most likely failures come first (tests calling a touched function, test file named after the function or the
module, import distance). Test support files (conftest.py, helpers other tests import) carry the selection but are
not listed to run. Selection by file imports, not by calls: in fault-injection measurements on Python,
JavaScript, TypeScript and Java projects static calls alone found 2 to 37% of the failing test files."""
import ast
import json
import os
import re
import subprocess
from collections import defaultdict, deque

import code_graph
import index_inventory
import index_profile
import index_scope

RUN_ALL = re.compile(
    r"(^|/)(package\.json|package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|pnpm-workspace\.yaml|"
    r"bun\.lockb?|pyproject\.toml|setup\.cfg|setup\.py|pytest\.ini|tox\.ini|requirements[^/]*\.txt|poetry\.lock|"
    r"uv\.lock|Pipfile(\.lock)?|tsconfig[^/]*\.json|jsconfig\.json|(jest|vitest|vite|babel)\.config\.[^/]+|"
    r"\.babelrc|\.mocharc[^/]*|pom\.xml|build\.gradle(\.kts)?|settings\.gradle(\.kts)?|gradle\.properties|"
    r"\.env[^/]*)$")
CI_CONFIG = re.compile(r"(^|/)(\.github|\.circleci|\.buildkite)/|(^|/)(\.gitlab-ci\.ya?ml|azure-pipelines\.ya?ml|"
                       r"Jenkinsfile|\.travis\.ya?ml|bitbucket-pipelines\.ya?ml|\.gitignore|\.gitattributes|"
                       r"\.editorconfig|\.mailmap|CODEOWNERS|LICENSE(\.[^/]*)?|\.npmignore)$")
NOTE = ("Selection from the indexed import graph: tests that load code through strings (mock.patch targets, "
        "importlib, require(variable), reflection, Spring scanning) or read data files are not seen. Run the full "
        "suite before committing; run_all is true when a changed file can change every test (config, lockfile, "
        "unanalyzed code). Changed files and files committed after the last indexing are read from disk, so new "
        "files and imports count before reindexing; pytest fixtures link a test to the conftest.py that defines them.")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.M)


def _git(root, *args):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError(f"git {' '.join(args[:2])} failed: {result.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return result.stdout.decode("utf-8", "replace")


def _changes(root, base):
    """Project-relative changed paths with their status and the new-side changed lines (None = whole file)."""
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    prefix = os.path.relpath(os.path.realpath(root), os.path.realpath(top)).replace(os.sep, "/")
    prefix = "" if prefix == "." else prefix + "/"
    _git(root, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    changes = {}
    fields = _git(root, "diff", "--name-status", "-z", "--no-renames", base, "--", ".").split("\0")
    for status, path in zip(fields[0::2], fields[1::2]):
        if path.startswith(prefix):
            changes[path[len(prefix):]] = {"status": "deleted" if status == "D" else "changed", "lines": set()}
    for path in _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--full-name", "--", ".").split("\0"):
        if path and path.startswith(prefix):
            changes[path[len(prefix):]] = {"status": "added", "lines": None}
    diff = _git(root, "diff", "-U0", "--no-renames", "--relative", base, "--", ".")
    for block in re.split(r"^diff --git ", diff, flags=re.M)[1:]:
        target = re.search(r"^\+\+\+ b/(.+)$", block, re.M)
        if target and target.group(1) in changes and changes[target.group(1)]["lines"] is not None:
            for start, count in _HUNK.findall(block):
                first, size = int(start), int(count or 1)
                changes[target.group(1)]["lines"].update(range(first, first + max(size, 1)))
    return changes


def _depths(start, reverse):
    seen, frontier = {node: 0 for node in start}, deque(start)
    while frontier:
        node = frontier.popleft()
        for nxt in reverse.get(node, ()):
            if nxt not in seen:
                seen[nxt] = seen[node] + 1
                frontier.append(nxt)
    return seen


def _nearest(root, start, names):
    folder = os.path.dirname(start)
    while True:
        for name in names:
            if os.path.isfile(os.path.join(root, folder, name)):
                return folder, name
        if not folder:
            return None, None
        folder = os.path.dirname(folder)


def _read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except (OSError, UnicodeDecodeError):
        return ""


def _commands(root, tests):
    """One command per runner and folder for the selected test files, or the reason none could be built."""
    groups = defaultdict(list)
    for path in tests:
        lower = path.lower()
        if lower.endswith(".py"):
            folder, marker = _nearest(root, path, ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"))
            pytest = (marker and "pytest" in _read(os.path.join(root, folder or "", marker))) \
                or _nearest(root, path, ("conftest.py",))[1] \
                or re.search(r"^\s*(import pytest|from pytest import)", _read(os.path.join(root, path)), re.M)
            test_dir = os.path.dirname(path)
            if pytest:
                groups[(folder or "", "python -m pytest")].append(path)
            elif os.path.isfile(os.path.join(root, test_dir, "__init__.py")):
                groups[("", "unittest-package")].append(path)
            else:
                groups[(test_dir, "unittest-folder")].append(path)
        elif lower.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts")):
            folder, _name = _nearest(root, path, ("package.json",))
            try:
                package = json.loads(_read(os.path.join(root, folder or "", "package.json")) or "{}")
            except ValueError:
                package = {}
            tools = " ".join([str((package.get("scripts") or {}).get("test", ""))]
                             + list(package.get("devDependencies") or {}) + list(package.get("dependencies") or {}))
            runner = next((cmd for key, cmd in (("vitest", "npx vitest run"), ("jest", "npx jest"),
                                                ("mocha", "npx mocha"), ("node --test", "node --test"))
                           if key in tools), None)
            groups[(folder or "", runner)].append(path)
        elif lower.endswith(".java"):
            folder, name = _nearest(root, path, ("pom.xml", "build.gradle", "build.gradle.kts"))
            groups[(folder or "", "maven" if name == "pom.xml" else "gradle" if name else None)].append(path)
        else:
            groups[("", None)].append(path)
    commands = []
    for (folder, runner), files in sorted(groups.items(), key=lambda item: (item[0][0], str(item[0][1]))):
        local = [os.path.relpath(f, folder or ".").replace(os.sep, "/") for f in files]
        if runner is None:
            commands.append({"cwd": folder or ".", "command": None, "files": local,
                             "reason": "No known test runner configured for these files."})
            continue
        if runner == "maven":
            names = ",".join(sorted({os.path.basename(f).rsplit(".", 1)[0] for f in files}))
            line = f"mvn test -Dtest={names} -Dsurefire.failIfNoSpecifiedTests=false"
        elif runner == "unittest-package":
            line = "python -m unittest " + " ".join(sorted(f[:-3].replace("/", ".") for f in files))
        elif runner == "unittest-folder":
            line = "python -m unittest " + " ".join(sorted(os.path.basename(f)[:-3] for f in files))
        elif runner == "gradle":
            wrapper = "./gradlew" if os.path.isfile(os.path.join(root, folder, "gradlew")) else "gradle"
            line = wrapper + " test " + " ".join(f"--tests {os.path.basename(f).rsplit('.', 1)[0]}" for f in sorted(files))
        else:
            line = runner + " " + " ".join(sorted(local))
        commands.append({"cwd": folder or ".", "command": line})
    return commands


def _fixtures(source):
    """Fixture names a conftest defines and whether one of them is autouse."""
    names, autouse = set(), False
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                text = ast.unparse(decorator)
                if "fixture" in text:
                    names.add(node.name)
                    autouse |= "autouse=True" in text.replace(" ", "")
    return names, autouse


def _parameters(source):
    return {arg.arg for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs}


def _fixture_users(root, test_files):
    """conftest.py -> Python test files (and nested conftests) below it that take one of its fixtures as a parameter,
    or all of them when it has an autouse fixture: pytest injects fixtures by name, without an import."""
    users = defaultdict(set)
    python = [p for p in test_files if p.endswith(".py")]
    params = {}
    for conftest in (p for p in python if os.path.basename(p) == "conftest.py"):
        try:
            names, autouse = _fixtures(_read(os.path.join(root, conftest)))
        except SyntaxError:
            continue
        folder = os.path.dirname(conftest)
        for path in python:
            if path == conftest or folder and not path.startswith(folder + "/"):
                continue
            if path not in params:
                try:
                    params[path] = _parameters(_read(os.path.join(root, path)))
                except SyntaxError:
                    params[path] = set()
            if autouse or names & params[path]:
                users[conftest].add(path)
    return users


def affected(root, base="HEAD", limit=30, view_id=None):
    """Test files to run for the changes since base, most likely failures first, with the command to run them."""
    if not isinstance(base, str) or not re.fullmatch(r"[\w./@^~{}-]{1,200}", base) or base.startswith("-"):
        raise ValueError("base must be a git revision such as HEAD, main or origin/main.")
    changes = _changes(root, base)
    selected = index_inventory.inspect(root, view_id)["selected"] or {}
    pending = code_graph.pending_changes(root, selected["path"]) if selected.get("current") else {}
    data = code_graph.build_overlay(root, {**pending, **{path: change["status"] for path, change in changes.items()}},
                                    view_id)
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    kind = {}
    for path in set(changes) | {f["path"] for f in data.get("files") or []}:
        kind[path] = index_profile.kind(path, profile)
    parsed = {f["path"] for f in data.get("files") or [] if f.get("analysis") == "parsed"}
    test_files = {f["path"] for f in data.get("files") or [] if kind.get(f["path"]) == "test"}
    importers, callers = defaultdict(set), defaultdict(set)
    for dep in data.get("dependencies") or []:
        if dep.get("target"):
            importers[dep["target"]].add(dep["source"])
    for call in data.get("calls") or []:
        callers[call["target"]].add(call["source"])
    for conftest, users in _fixture_users(root, test_files).items():
        importers[conftest] |= users
    by_id = {s["id"]: s for s in data.get("symbols") or []}
    run_all, not_analyzed, changed_code, changed_tests, conftests = [], [], [], [], []
    for path, change in sorted(changes.items()):
        if CI_CONFIG.search(path):
            continue
        if RUN_ALL.search(path):
            run_all.append(f"{path} changed (configuration or dependencies)")
        elif os.path.basename(path) == "conftest.py":
            conftests.append(path)
        elif kind[path] == "test":
            changed_tests.append(path)
        elif kind[path] == "code":
            changed_code.append(path)
            if path not in parsed:
                not_analyzed.append({"path": path, "status": change["status"],
                                     "reason": "not analyzed (unsupported language, data file or outside the "
                                               "index scope)"})
    if not_analyzed:
        run_all.append(f"{len(not_analyzed)} changed code file(s) outside the import graph; see not_analyzed")
    touched = [s for s in by_id.values() if s["path"] in changes and s.get("kind") not in ("module", "class", "interface")
               and (changes[s["path"]]["lines"] is None
                    or changes[s["path"]]["lines"].intersection(range(s["start_line"], s["end_line"] + 1)))]
    touched = [s for s in touched if not any(o is not s and o["path"] == s["path"] and o in touched
                                             and o["start_line"] >= s["start_line"] and o["end_line"] <= s["end_line"]
                                             for o in touched)] or touched
    import_depth = {f: d for f, d in _depths([p for p in changed_code if p in parsed], importers).items()
                    if f in test_files}
    call_hops = {}
    for sid, hops in _depths([s["id"] for s in touched], callers).items():
        symbol = by_id.get(sid)
        if symbol and symbol["path"] in test_files and hops:
            call_hops[symbol["path"]] = min(hops, call_hops.get(symbol["path"], hops))
    names = {s["name"].split("(")[0].split(".")[-1].lower() for s in touched}
    names = {n for n in names if len(n) > 2 and n not in ("callback", "anonymous", "constructor")}
    stems = {os.path.basename(p).split(".")[0].lower().lstrip("_") for p in changed_code}
    selected = {p: "changed" for p in changed_tests}
    for conftest in conftests:
        folder = os.path.dirname(conftest)
        for test in test_files:
            if not folder or test.startswith(folder + "/"):
                selected.setdefault(test, f"{conftest} changed")
    for test in set(import_depth) | set(call_hops):
        selected.setdefault(test, None)
    support = {p for p in test_files if os.path.basename(p) == "conftest.py"
               or any(source in test_files for source in importers.get(p, ()))}
    selected = {test: reason for test, reason in selected.items() if test not in support}

    def why(test):
        if selected[test]:
            return selected[test]
        if test in call_hops:
            return f"calls a changed function ({call_hops[test]} hop{'s' if call_hops[test] > 1 else ''})"
        return f"imports a changed file ({import_depth[test]} step{'s' if import_depth[test] > 1 else ''})"

    def rank(test):
        base_name = os.path.basename(test).lower()
        words = set(re.split(r"[^a-z0-9]+", base_name.rsplit(".", 1)[0]))
        named = 0 if names & words or {n.replace("_", "") for n in names} & words else \
            1 if any(stem and stem in re.sub(r"[^a-z0-9]", "", base_name) for stem in stems) else 2
        return (0 if selected[test] == "changed" else 1, call_hops.get(test, 99), named, import_depth.get(test, 99), test)

    ordered = sorted(selected, key=rank)
    result = {
        "base": base,
        "changed": {"code": changed_code[:limit], "tests": changed_tests[:limit],
                    "other": sorted(p for p in changes if p not in changed_code and p not in changed_tests)[:limit]},
        "touched_functions": [f"{s['path']}::{s['name']}" for s in touched][:limit],
        "tests": [{"path": t, "why": why(t)} for t in ordered[:limit]],
        "counts": {"changed_files": len(changes), "tests": len(ordered), "test_files": len(test_files)},
        "run_all": bool(run_all),
        "run_all_reasons": run_all,
        "not_analyzed": not_analyzed[:limit],
        "commands": _commands(root, ordered) if ordered else [],
        "diagnostics": [d.get("reason") for d in data.get("diagnostics") or [] if d.get("reason")],
        "view": (data.get("selected") or {}).get("label"),
        "note": NOTE,
    }
    if len(ordered) > limit:
        result["omitted"] = {"tests": len(ordered) - limit}
    if not changes:
        result["note"] = "No changes since " + base + ". " + NOTE
    return result
