"""Documents generated beside the three documentation levels from the same code graph, one per key of the config object
`docs_extras` (each on by default, each turned off with `false`):

- before the documentation: `structure` (folders of code, tests and docs), `coverage` (docstring coverage per folder),
  `modules` (Mermaid of the calls between modules);
- other types: `tests` (what each suite reaches), `configuration` (variables, files and ports the code reads),
  `dependencies` (manifests with the license each installed package declares), `adr` (index and template of decisions);
- after the documentation: `flows` (Mermaid sequence per entry point), `features_tests` (entry point x tests matrix),
  `index` (a short block between markers inside an existing AGENTS.md or llms.txt);
- from the history and the names: `hotspots`, `coupling`, `dead_code`, `tour`, `glossary` (see doc_insights); the
  decision candidates from the git log go into the `adr` page.

Everything is deterministic: names, counts and links come from the graph and the files, never from a model. The pages
live under docs/scope/extras and carry the same generated marker, anchors and signature markers as the other pages, so
`check` and the edit hook cover them. The index block is the only text written outside docs/scope, and only into a file
that already exists, between its own markers."""
import glob
import json
import os
import posixpath
import re
import tomllib
from collections import Counter, defaultdict
from importlib import metadata

import atomic_io
import config
import doc_insights
import doc_scope

EXTRAS_DIR = "extras"
INDEX_START = "<!-- smart-tool:index:start -->"
INDEX_END = "<!-- smart-tool:index:end -->"
INDEX_FILES = ("AGENTS.md", "llms.txt")
ADR_DIRS = ("docs/adr", "docs/decisions", "docs/scope/adr")
MAX_ENTRIES = 100
MAX_SEQUENCES = 40
MAX_MESSAGES = 25
MAX_TREE_LINES = 300
TREE_DEPTH = 4
MAX_SOURCE_FILES = 4000
MAX_SOURCE_BYTES = 1_000_000
MAX_HITS = 5
INDEX_ENTRIES = 10
_INDEX_BLOCK = re.compile(re.escape(INDEX_START) + r"\n(.*?)" + re.escape(INDEX_END), re.S)
_ENV = [re.compile(pattern) for pattern in (
    r"os\.environ(?:\.get)?\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", r"os\.environ\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']",
    r"os\.getenv\(\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", r"process\.env\.([A-Za-z_][A-Za-z0-9_]*)",
    r"process\.env\[\s*[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", r"System\.getenv\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"",
    r"os\.Getenv\(\s*\"([A-Za-z_][A-Za-z0-9_]*)\"")]
_CONFIG_FILE = re.compile(r"[\"']([\w./\\-]*\.(?:json|ya?ml|toml|ini|env|cfg|conf|properties))[\"']")
_PORTS = [re.compile(pattern) for pattern in (
    r"(?i)\bport\w*\s*[=:]\s*[\"']?(\d{2,5})\b", r"://[\w.-]+:(\d{2,5})\b", r"listen\(\s*(\d{2,5})\b")]
_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*([^;#]*)")
_GO_REQUIRE = re.compile(r"^\s*(?:require\s+)?([\w.-]+\.[\w.-]+/[\w./-]+|[\w.-]+\.[\w.-]+)\s+(v[\w.+-]+)")
_POM = re.compile(r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>(?:\s*<version>([^<]+)</version>)?", re.S)
_ADR_STATUS = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?Status(?:\*\*)?\s*:?\s*(.+)$", re.I | re.M)
ADR_TEMPLATE = """# NNNN. Title of the decision

Status: proposed | accepted | superseded by NNNN
Date: YYYY-MM-DD

## Context

What forces are at play: the problem, the constraints, what was tried.

## Decision

What was chosen, in the active voice ("We use ...").

## Consequences

What becomes easier, what becomes harder, what is left to decide."""
FILES = {"tests": "extras/tests.md", "configuration": "extras/configuration.md", "dependencies": "extras/dependencies.md",
         "adr": "extras/adr/README.md", "structure": "extras/structure.md", "coverage": "extras/coverage.md",
         "modules": "extras/modules.md", "flows": "extras/flows.md", "features_tests": "extras/features-tests.md",
         "hotspots": "extras/hotspots.md", "coupling": "extras/coupling.md", "dead_code": "extras/dead-code.md",
         "tour": "extras/tour.md", "glossary": "extras/glossary.md"}
TITLES = {"hotspots": "Hotspots", "coupling": "Change coupling", "dead_code": "Dead-code candidates",
          "tour": "Onboarding tour", "glossary": "Glossary","tests": "Tests", "configuration": "Configuration", "dependencies": "Dependencies",
          "adr": "Decision records", "structure": "Folder structure", "coverage": "Docstring coverage",
          "modules": "Modules", "flows": "Flow per entry point", "features_tests": "Entry points x tests"}


def _head(model, title, depth=1):
    """First lines of an extra page: generated marker, title, navigation and the truncation warning when it applies."""
    up = "../" * depth
    lines = [doc_scope.GENERATED, f"# {title}", "", f"[Scope]({up}README.md) · [Functional]({up}functional.md) · "
             f"[Technical]({up}technical/README.md)", ""]
    if model.graph.get("truncated"):
        lines += [doc_scope.TRUNCATED]
    return lines


def _done(lines):
    return "\n".join(lines).rstrip("\n") + "\n"


def _folder(path):
    return posixpath.dirname(path) or "(project root)"


def _source(root, path):
    """Text of a project file, or None when it is too large or not UTF-8."""
    full = os.path.join(root, path)
    try:
        if os.path.getsize(full) > MAX_SOURCE_BYTES:
            return None
        with open(full, encoding="utf-8") as stream:
            return stream.read()
    except (OSError, UnicodeDecodeError):
        return None


def _entries(model, entries):
    """Entry points paired with the tests that reach each, computed once per page set."""
    return [(anchor, model.tests_of(anchor)) for anchor in entries[:MAX_ENTRIES]]


def _tests(model, covering, entries):
    """README of the tests: one row per suite with its functions and the entry points they reach."""
    by_file, reached = defaultdict(list), defaultdict(set)
    for anchor, function in model.function_of.items():
        if model.kind[function["path"]] == "test":
            by_file[function["path"]].append(anchor)
    for entry, tests in covering:
        for test in tests:
            reached[model.function_of[test]["path"]].add(entry)
    lines = _head(model, "Tests")
    if not by_file:
        return _done(lines + ["No test function in the indexed code."])
    lines += ["What each suite reaches through static calls (up to three hops) from its functions: the code files "
              "(with how many functions of each) and the entry points of the functional overview.", "",
              "| Suite | Functions | Code files it reaches | Entry points it reaches |", "|---|---|---|---|"]
    for path in sorted(by_file):
        names = sorted(reached[path], key=lambda a: (model.function_of[a]["path"], model.function_of[a]["start"]))
        shown = [doc_scope.entry_link(model, a, entries, "../functional.md") for a in names[:doc_scope.MAX_LINKS]]
        more = f" (+{len(names) - doc_scope.MAX_LINKS} more)" if len(names) > doc_scope.MAX_LINKS else ""
        files = Counter(model.function_of[a]["path"] for test in by_file[path] for a in model.code_reached(test))
        top = [f"`{f}` ({n})" for f, n in sorted(files.items(), key=lambda i: (-i[1], i[0]))[:3]]
        rest = f" (+{len(files) - 3} more)" if len(files) > 3 else ""
        lines.append(f"| `{path}` | {len(by_file[path])} | {', '.join(top) + rest if top else 'none'} "
                     f"| {', '.join(shown) + more if shown else 'none'} |")
    return _done(lines)


def _configuration(model):
    """README of the configuration the code reads: environment variables, config files and ports found by pattern."""
    found = {"Environment variables": defaultdict(list), "Files": defaultdict(list), "Ports": defaultdict(list)}
    paths = [p for p in model.files() if model.kind_of(p) == "code"][:MAX_SOURCE_FILES]
    for path in paths:
        text = _source(model.root, path)
        if text is None:
            continue
        for number, line in enumerate(text.split("\n"), 1):
            for pattern in _ENV:
                for name in pattern.findall(line):
                    found["Environment variables"][name].append(f"{path}:{number}")
            for name in _CONFIG_FILE.findall(line):
                found["Files"][name].append(f"{path}:{number}")
            for pattern in _PORTS:
                for port in pattern.findall(line):
                    if int(port) <= 65535:
                        found["Ports"][port].append(f"{path}:{number}")
    lines = _head(model, "Configuration")
    lines += ["Found by pattern in the code files: a name built at run time is not listed.", ""]
    for title, hits in found.items():
        lines += [f"## {title}", ""]
        if not hits:
            lines += ["None found.", ""]
            continue
        lines += ["| Name | Read at |", "|---|---|"]
        for name in sorted(hits):
            where = sorted(set(hits[name]))
            lines.append(f"| `{name}` | {', '.join(where[:MAX_HITS])}{f' (+{len(where) - MAX_HITS} more)' if len(where) > MAX_HITS else ''} |")
        lines.append("")
    return _done(lines)


def _normal(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _python_licenses(root):
    """Normalized package name -> license text declared by the packages installed in the project's own virtualenv."""
    folders = []
    for env in (".venv", "venv"):
        folders += glob.glob(os.path.join(root, env, "Lib", "site-packages"))
        folders += glob.glob(os.path.join(root, env, "lib", "python*", "site-packages"))
    licenses = {}
    for dist in metadata.distributions(path=folders) if folders else []:
        info, name = dist.metadata, dist.metadata["Name"]
        text = (info.get("License-Expression") or "").strip()
        if not text:
            classified = [c.split("::")[-1].strip() for c in info.get_all("Classifier", []) if c.startswith("License ::")]
            text = ", ".join(classified) or (info.get("License") or "").strip()
            if "\n" in text or len(text) > 80:
                text = "declared in the package metadata"
        if name:
            licenses[_normal(name)] = text or "none declared"
    return licenses


def _node_license(root, folder, name):
    for base in (folder, ""):
        try:
            with open(os.path.join(root, base, "node_modules", name, "package.json"), encoding="utf-8") as stream:
                data = json.load(stream)
        except (OSError, ValueError):
            continue
        value = data.get("license") or data.get("licenses")
        return value if isinstance(value, str) else json.dumps(value) if value else "none declared"
    return None


def _manifest(root, path):
    """Dependencies declared by one manifest: (rows of ecosystem, name, spec, scope), or a note when unreadable."""
    base = posixpath.basename(path)
    text = _source(root, path)
    if text is None:
        return [], "could not be read as UTF-8 within the size limit"
    rows = []
    try:
        if re.fullmatch(r"requirements[\w.-]*\.txt", base):
            for line in text.splitlines():
                found = _REQUIREMENT.match(line)
                if found and not line.lstrip().startswith(("-", "#")):
                    rows.append(("python", found.group(1), found.group(2).strip().rstrip("\\").strip(), "runtime"))
        elif base == "pyproject.toml":
            data = tomllib.loads(text)
            project = data.get("project", {})
            groups = [("runtime", project.get("dependencies", []))] + \
                     [(f"extra {k}", v) for k, v in project.get("optional-dependencies", {}).items()]
            for scope, items in groups:
                for item in items:
                    found = _REQUIREMENT.match(item)
                    if found:
                        rows.append(("python", found.group(1), found.group(2).strip(), scope))
            for name, spec in data.get("tool", {}).get("poetry", {}).get("dependencies", {}).items():
                if name.lower() != "python":
                    rows.append(("python", name, spec if isinstance(spec, str) else json.dumps(spec), "runtime"))
        elif base == "package.json":
            data = json.loads(text)
            for key, scope in (("dependencies", "runtime"), ("devDependencies", "dev"), ("peerDependencies", "peer"),
                               ("optionalDependencies", "optional")):
                rows += [("node", name, spec, scope) for name, spec in (data.get(key) or {}).items()]
        elif base == "go.mod":
            for line in text.splitlines():
                found = _GO_REQUIRE.match(line)
                if found:
                    rows.append(("go", found.group(1), found.group(2), "runtime"))
        elif base == "pom.xml":
            rows += [("java", f"{g.strip()}:{a.strip()}", (v or "").strip(), "runtime") for g, a, v in _POM.findall(text)]
    except (ValueError, AttributeError, TypeError) as exc:
        return [], f"could not be parsed ({exc})"
    return rows, ""


def _dependencies(model):
    """Map of the external dependencies of every manifest, with the license each installed package declares."""
    manifests = [p for p in model.files() if re.fullmatch(r"requirements[\w.-]*\.txt|pyproject\.toml|package\.json|go\.mod|pom\.xml",
                                                          posixpath.basename(p))][:50]
    lines = _head(model, "Dependencies")
    if not manifests:
        return _done(lines + ["No dependency manifest found (requirements*.txt, pyproject.toml, package.json, go.mod, pom.xml)."])
    python = _python_licenses(model.root)
    lines += ["Licenses come from the packages installed next to the manifest (the project's virtualenv or node_modules); "
              "a package that is not installed there is marked, never guessed. Go and Maven licenses are not resolved.", ""]
    for path in manifests:
        rows, problem = _manifest(model.root, path)
        lines += [f"## `{path}`", ""]
        if problem:
            lines += [f"Manifest {problem}.", ""]
            continue
        if not rows:
            lines += ["No dependency declared.", ""]
            continue
        lines += ["| Package | Version | Scope | License |", "|---|---|---|---|"]
        for ecosystem, name, spec, scope in sorted(rows, key=lambda r: (r[3], r[1].lower())):
            if ecosystem == "python":
                license_ = python.get(_normal(name), "not installed in the project")
            elif ecosystem == "node":
                license_ = _node_license(model.root, posixpath.dirname(path), name) or "not installed in the project"
            else:
                license_ = "not resolved"
            lines.append(f"| `{name}` | {spec or '-'} | {scope} | {license_} |")
        lines.append("")
    return _done(lines)


def _decisions(model):
    """Markdown files of decision records already in the project (outside the generated ones): (path, title, status)."""
    found = []
    for folder in ADR_DIRS:
        for path in sorted(glob.glob(os.path.join(model.root, folder, "*.md"))):
            text = _source(model.root, os.path.relpath(path, model.root).replace(os.sep, "/"))
            if text is None or text.startswith(doc_scope.GENERATED):
                continue
            title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.startswith("#")), os.path.basename(path))
            status = _ADR_STATUS.search(text)
            found.append((os.path.relpath(path, model.root).replace(os.sep, "/"), title, status.group(1).strip() if status else "-"))
    return found


def _adr(model, commits, note):
    """Index of the project's decision records, the template to copy and the decision candidates in the git log."""
    lines = _head(model, "Decision records", depth=2)
    here = "docs/scope/extras/adr"
    records = _decisions(model)
    lines += ["## Records", ""]
    if records:
        lines += ["| Record | Status |", "|---|---|"]
        lines += [f"| [{title}](<{posixpath.relpath(path, here)}>) | {status} |" for path, title, status in records]
    else:
        lines += ["No record yet in " + ", ".join(f"`{d}`" for d in ADR_DIRS) + "."]
    lines += ["", "## Template", "", "Copy it to a new file named `NNNN-title.md` in `docs/adr`; this page is regenerated "
              "and lists what that folder holds.", "", "```markdown", ADR_TEMPLATE, "```"]
    return _done(lines + doc_insights.adr_section(commits, note))


def _structure(model):
    """Folder tree of the indexed files with the count of code, test and doc files under each folder."""
    totals, children = defaultdict(Counter), defaultdict(set)
    for path in model.files():
        kind = model.kind_of(path) or "other"
        parts = path.split("/")[:-1]
        totals[""][kind] += 1
        for depth in range(1, len(parts) + 1):
            folder = "/".join(parts[:depth])
            totals[folder][kind] += 1
            children["/".join(parts[:depth - 1])].add(folder)

    def counts(folder):
        shown = [f"{label} {totals[folder][kind]}" for kind, label in (("code", "code"), ("test", "tests"), ("doc", "docs"))
                 if totals[folder][kind]]
        return f"  ({', '.join(shown)})" if shown else ""

    body, hidden = [], 0

    def walk(folder, prefix, depth):
        nonlocal hidden
        kids = sorted(children[folder])
        for index, child in enumerate(kids):
            if len(body) >= MAX_TREE_LINES or depth >= TREE_DEPTH:
                hidden += sum(1 for _ in kids[index:])
                return
            last = index == len(kids) - 1
            body.append(f"{prefix}{'└── ' if last else '├── '}{posixpath.basename(child)}/{counts(child)}")
            walk(child, prefix + ("    " if last else "│   "), depth + 1)

    walk("", "", 0)
    lines = _head(model, "Folder structure")
    lines += ["Folders of the indexed files, with how many code, test and doc files each holds (files in subfolders "
              "included).", "", "```text", f"(project root){counts('')}"] + body + ["```"]
    if hidden:
        lines += ["", f"{hidden} folder(s) are not drawn: the tree stops at {TREE_DEPTH} levels or {MAX_TREE_LINES} lines."]
    return _done(lines)


def _coverage(model):
    """Docstring coverage of the public code functions per folder, as a Markdown table."""
    stats = defaultdict(lambda: [0, 0])
    for anchor in model.public():
        function = model.function_of[anchor]
        row = stats[_folder(function["path"])]
        row[0] += 1
        row[1] += bool(function["documented"])
    lines = _head(model, "Docstring coverage")
    lines += ["| Folder | Public functions | Documented | Coverage |", "|---|---|---|---|"]
    for folder in sorted(stats):
        total, documented = stats[folder]
        lines.append(f"| `{folder}` | {total} | {documented} | {100 * documented // total}% |")
    total = sum(row[0] for row in stats.values())
    documented = sum(row[1] for row in stats.values())
    lines.append(f"| **All** | {total} | {documented} | {100 * documented // total if total else 0}% |")
    return _done(lines)


def _modules(model):
    """Mermaid flowchart of the calls between code modules (files), the strongest edges first."""
    weights = defaultdict(int)
    for source, targets in model.callees.items():
        for target in targets:
            a, b = model.function_of[source]["path"], model.function_of[target]["path"]
            if a != b and model.kind[a] == "code" and model.kind[b] == "code":
                weights[(a, b)] += 1
    edges = sorted(weights.items(), key=lambda item: (-item[1], item[0]))[:doc_scope.MAX_FLOW_EDGES]
    names = sorted({m for edge, _n in edges for m in edge})
    node = {name: f"m{i}" for i, name in enumerate(names)}
    lines = _head(model, "Modules")
    if not edges:
        return _done(lines + ["No call between two code modules in the graph."])
    lines += ["The calls between modules, the strongest " + str(doc_scope.MAX_FLOW_EDGES) + " edges; the number is how "
              "many functions call across.", "", "```mermaid", "flowchart LR"]
    lines += [f'  {node[name]}["{name}"]' for name in names]
    lines += [f"  {node[a]} -->|{n}| {node[b]}" for (a, b), n in edges]
    lines.append("```")
    return _done(lines)


def _sequence(model, entry):
    """Mermaid sequence diagram of the calls the flow of one entry point makes, depth first, at most MAX_MESSAGES.

    Returns: (diagram lines, calls of the flow left out, whether any call is drawn).
    """
    edges = model.flow_edges(entry)
    children = defaultdict(list)
    for caller, callee in edges:
        children[caller].append(callee)
    order, stack = [], [entry]
    while stack and len(order) < MAX_MESSAGES:
        current = stack.pop()
        for callee in reversed(children[current]):
            stack.append(callee)
        if current != entry:
            order.append(current)
    parent = {callee: caller for caller, callee in edges}
    modules = []
    for anchor in [entry] + order:
        path = model.function_of[anchor]["path"]
        if path not in modules:
            modules.append(path)
    lines = ["```mermaid", "sequenceDiagram"] + [f"  participant P{i} as {path}" for i, path in enumerate(modules)]
    for anchor in order:
        source = model.function_of[parent[anchor]]["path"]
        lines.append(f"  P{modules.index(source)}->>P{modules.index(model.function_of[anchor]['path'])}: "
                     f"{model.function_of[anchor]['name']}")
    return lines + ["```"], len(edges) - len(order), bool(order)


def _flows(model, entries):
    """One Mermaid sequence diagram per entry point, anchored and signed like the functional overview."""
    lines = _head(model, "Flow per entry point")
    if not entries:
        return _done(lines + ["No entry point found: declare the layers (project_manage action=layers)."])
    lines += ["The static calls each entry point reaches, as a sequence between modules, in the order a depth-first "
              "read of the call graph meets them.", ""]
    for anchor in entries[:MAX_SEQUENCES]:
        function = model.function_of[anchor]
        diagram, left, drawn = _sequence(model, anchor)
        lines += [f'<a id="{doc_scope._escape(anchor)}"></a>', f"## {doc_scope.entry_title(model, anchor, entries)}", "",
                  f'<!-- smart-tool:symbol id="{doc_scope._escape(anchor)}" signature="{doc_scope.signature_hash(function["signature"])}" -->',
                  "", f"Entry point in `{function['path']}`: {doc_scope.entry_link(model, anchor, entries, '../functional.md')}.", ""]
        lines += diagram if drawn else ["No call from this entry point reaches another code function."]
        if left > 0:
            lines += ["", f"{left} more call(s) of this flow are not drawn."]
        lines.append("")
    if len(entries) > MAX_SEQUENCES:
        lines.append(f"{len(entries) - MAX_SEQUENCES} more entry point(s) are not drawn.")
    return _done(lines)


def _features_tests(model, covering, entries):
    """Matrix of entry points against the tests that reach them, with the entry points no test reaches."""
    lines = _head(model, "Entry points x tests")
    if not covering:
        return _done(lines + ["No entry point found: declare the layers (project_manage action=layers)."])
    lines += ["| Entry point | Tests | Reached by |", "|---|---|---|"]
    for anchor, tests in covering:
        function = model.function_of[anchor]
        shown = ", ".join(f"`{model.function_of[t]['name']}`" for t in tests[:3]) + (f" (+{len(tests) - 3} more)" if len(tests) > 3 else "")
        marker = (f'<a id="{doc_scope._escape(anchor)}"></a><!-- smart-tool:symbol id="{doc_scope._escape(anchor)}" '
                  f'signature="{doc_scope.signature_hash(function["signature"])}" -->')
        lines.append(f"| {marker}{doc_scope.entry_link(model, anchor, entries, '../functional.md')} | {len(tests)} | "
                     f"{shown or 'no test reaches it'} |")
    bare = [a for a, tests in covering if not tests]
    lines += ["", f"{len(bare)} of {len(covering)} entry point(s) have no test reaching them through static calls."]
    return _done(lines)


def _index(model, entries, files, counts):
    """The short block for AGENTS.md or llms.txt: where the documentation is and the entry points to start from."""
    lines = ["## Project documentation (generated by Smart Tool)", "",
             f"{counts['public']} public functions, {counts['entry_points']} entry points; layers: {counts['layers']}.", ""]
    if model.graph.get("truncated"):
        lines += [doc_scope.TRUNCATED]
    lines += ["- [Plain words](<docs/scope/simple.md>)", "- [Functional overview](<docs/scope/functional.md>)",
              "- [Technical reference](<docs/scope/technical/README.md>)"]
    lines += [f"- [{TITLES[key]}](<docs/scope/{FILES[key]}>)" for key in config.DOCS_EXTRAS if FILES.get(key) in files]
    if entries:
        lines += ["", "Entry points:"]
        lines += [f"- {doc_scope.entry_link(model, a, entries, 'docs/scope/functional.md')} (`{model.function_of[a]['path']}`)"
                  for a in entries[:INDEX_ENTRIES]]
    return "\n".join(lines) + "\n"


def build(model, entries, counts, enabled):
    """The enabled extra documents and the index block.

    Parameters: model (doc_scope._Model); entries (list) entry-point anchors; counts (dict) of the scope index;
    enabled (dict) config docs_extras. Returns: (files: relative path under docs/scope -> text, block: str or None).
    """
    files = {}
    covering = _entries(model, entries) if enabled["tests"] or enabled["features_tests"] else []
    commits, note = doc_insights.history(model.root) if any(enabled[k] for k in ("hotspots", "coupling", "adr")) else ([], None)
    builders = {"hotspots": lambda: doc_insights.hotspots_page(model, _head, _done, commits, note),
                "coupling": lambda: doc_insights.coupling_page(model, _head, _done, commits, note),
                "dead_code": lambda: doc_insights.dead_code_page(model, _head, _done, entries),
                "tour": lambda: doc_insights.tour_page(model, _head, _done, entries),
                "glossary": lambda: doc_insights.glossary_page(model, _head, _done),"tests": lambda: _tests(model, covering, entries), "configuration": lambda: _configuration(model),
                "dependencies": lambda: _dependencies(model), "structure": lambda: _structure(model),
                "coverage": lambda: _coverage(model), "modules": lambda: _modules(model),
                "flows": lambda: _flows(model, entries), "features_tests": lambda: _features_tests(model, covering, entries),
                "adr": lambda: _adr(model, commits, note)}
    for key, make in builders.items():
        if enabled[key]:
            files[FILES[key]] = make()
    block = _index(model, entries, files, counts) if enabled["index"] else None
    return files, block


def _index_target(root):
    return next((name for name in INDEX_FILES if os.path.isfile(os.path.join(root, name))), None)


def write_index(root, block):
    """Put block between the index markers of the existing AGENTS.md (else llms.txt), or take the markers out when block
    is None. A file without markers gets the block appended; nothing else in it is touched.

    Returns: the file name written, or None when neither file exists.
    """
    target = _index_target(root)
    if target is None:
        return None
    full = os.path.join(root, target)
    with open(full, encoding="utf-8") as stream:
        text = stream.read()
    starts, ends = text.count(INDEX_START), text.count(INDEX_END)
    if starts != ends or starts > 1 or (starts and text.index(INDEX_START) > text.index(INDEX_END)):
        raise ValueError(f"{full} has unbalanced Smart Tool index markers; keep one start and one end marker, or remove both.")
    wrapped = f"{INDEX_START}\n{block}{INDEX_END}" if block is not None else ""
    if starts:
        pattern = re.compile(re.escape(INDEX_START) + r".*?" + re.escape(INDEX_END) + (r"\n?" if block is None else ""), re.S)
        updated = pattern.sub(lambda _m: wrapped, text)
    elif block is None:
        return target
    else:
        updated = (text if not text or text.endswith("\n") else text + "\n") + ("\n" if text else "") + wrapped + "\n"
    if updated != text:
        atomic_io.write_text(full, updated)
    return target


def index_links(root):
    """Links of the index block into docs/scope: list of {file, line, target, anchor}, target relative to docs/scope."""
    target = _index_target(root)
    if target is None:
        return []
    with open(os.path.join(root, target), encoding="utf-8") as stream:
        text = stream.read()
    found = _INDEX_BLOCK.search(text)
    if not found:
        return []
    first = text[:found.start(1)].count("\n") + 1
    prefix, links = doc_scope.DOCS_DIR + "/", []
    for offset, line in enumerate(found.group(1).splitlines()):
        for path, anchor in doc_scope.LINK.findall(line):
            if path.startswith(prefix):
                links.append({"file": target, "line": first + offset, "target": path[len(prefix):],
                              "anchor": doc_scope.html.unescape(anchor)})
    return links
