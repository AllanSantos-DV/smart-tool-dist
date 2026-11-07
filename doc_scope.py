"""Documentation scope of a project in three levels tied to the code, written to docs/scope in the project:

- technical/: a page per folder, a section per module and an entry per public function anchored on its symbol
  (path::name), with signature, first doc line, layer, callers, callees and the tests that reach it, plus a Mermaid
  flow of the folders grouped by declared layer;
- functional.md: one anchor per entry point (the functions of the highest declared layer, every main and the
  functions a `__main__` block runs) with the functions its static call flow reaches, grouped by layer and linked to
  their technical anchors, and the command lines the code declares with argparse;
- simple.md: the plain-language summary, one block per entry point linked to the functional anchor.

The skeleton comes from the code graph without a model. The prose (a paragraph per module, per entry point and the
simple README) is written between prose markers by the Docs model (doc_prose) or by hand and kept across
regenerations, as terraform-docs keeps the text around its generated block. Each anchor records the signature it was generated from; `check` and the edit
hook flag a document that no longer matches the code, like Swimm's smart tokens. The prose block of an entry point
records the code of the function it was written for (`code="..."`), so `check` and `validate` flag a paragraph left
behind by a body change that kept the signature. The link lives in the documents and in the `anchors` query, never
inside docstrings."""
import hashlib
import html
import json
import os
import posixpath
import re
import tomllib
from collections import defaultdict, deque

import atomic_io
import code_graph
import config
import doc_check
import doc_cli
import index_profile
import index_scope
import layers

DOCS_DIR = "docs/scope"
GENERATED = "<!-- smart-tool:generated: regenerate with project_manage action=docs generate=true; write prose only between prose markers -->"
TEST_DEPTH = 3
FLOW_DEPTH = 6
MAX_FLOW = 60
MAX_LINKS = 12
MAX_VISITED = 400
MAX_FLOW_EDGES = 60
MAX_PAGE_ENTRIES = 150
MIN_ROOT_FLOW = 3
MAX_ROOTS = 40
PROSE = re.compile(r'<!-- smart-tool:prose (.+?)(?: code="[0-9a-f]{12}")? -->\n(.*?)<!-- /smart-tool:prose -->', re.S)
STAMPED_PROSE = re.compile(r'<!-- smart-tool:prose (.+?)(?: code="([0-9a-f]{12})")? -->\n(.*?)<!-- /smart-tool:prose -->', re.S)
MARKER = re.compile(r'<!-- smart-tool:symbol id="([^"]+)" signature="([0-9a-f]{12})" -->')
LINK = re.compile(r"\]\(<([^>#]*)#([^>]+)>\)")
ANCHOR = re.compile(r'<a id="([^"]+)"></a>')
_SPACE = re.compile(r"\s+")
TRUNCATED = ("> The code graph was cut by its analysis limits: some functions and calls are missing from this "
             "document.\n")


def signature_hash(signature):
    """Short hash of a signature with whitespace removed, as stored next to each anchor.

    Parameters: signature (str).
    Returns: str of 12 hex digits.
    """
    return hashlib.sha1(_SPACE.sub("", signature or "").encode("utf-8")).hexdigest()[:12]


def code_stamp(source, function):
    """Short hash of a function's lines with whitespace removed: the version of the code a prose block describes.

    Parameters: source (str) text of the function's file; function (dict) with 'start' and 'end' lines.
    Returns: str of 12 hex digits.
    """
    return signature_hash("\n".join(source.split("\n")[function["start"] - 1:function["end"]]))


def prose_open(key, stamp=None):
    """Opening marker of the prose block `key`, with the code stamp it was written for when there is one."""
    return f"<!-- smart-tool:prose {key}" + (f' code="{stamp}"' if stamp else "") + " -->"


def stale_prose(pages, current):
    """Written entry-point prose of functional.md and simple.md whose code stamp is not the code of its function now,
    a block without a stamp included: the paragraph may describe behavior the function no longer has.

    Parameters: pages (dict) relative file -> text; current (dict) anchor -> code_stamp of the function now (a function
    gone is left out, its signature marker already reports it).
    Returns: list of dicts with file, line and id.
    """
    found = []
    for rel in ("functional.md", "simple.md"):
        text = pages.get(rel) or ""
        for block in STAMPED_PROSE.finditer(text):
            key, stamp, body = block.groups()
            if body.strip() and key in current and stamp != current[key]:
                found.append({"file": rel, "line": text[:block.start()].count("\n") + 1, "id": key})
    return found


def anchor_ids(functions, path):
    """Document anchor of each function of one file: path::name, with @n (n from 2) when the name repeats, so an
    anchor survives lines inserted above it.

    Parameters: functions (list of doc_check function dicts with 'name' and 'start'); path (str) project-relative.
    Returns: dict start line -> anchor id.
    """
    seen, ids = defaultdict(int), {}
    for function in sorted(functions, key=lambda f: f["start"]):
        seen[function["name"]] += 1
        n = seen[function["name"]]
        ids[function["start"]] = f"{path}::{function['name']}" + (f"@{n}" if n > 1 else "")
    return ids


def _slug(folder):
    return folder.replace("/", "--") if folder else "_root"


def _pages(model, public):
    """Technical page of each public function: one page per folder, split into one page per module (in a subfolder
    named after the folder's page) when the folder has more than MAX_PAGE_ENTRIES entries."""
    by_folder = defaultdict(list)
    for anchor in public:
        by_folder[posixpath.dirname(model.function_of[anchor]["path"])].append(anchor)
    page_of, titles = {}, {}
    for folder, anchors in by_folder.items():
        split = len(anchors) > MAX_PAGE_ENTRIES
        for anchor in anchors:
            path = model.function_of[anchor]["path"]
            page = f"technical/{_slug(folder)}/{posixpath.basename(path)}.md" if split else f"technical/{_slug(folder)}.md"
            page_of[anchor] = page
            title = path if split else folder or "(project root)"
            if titles.setdefault(page, title) != title:
                raise ValueError(f"{titles[page]} and {title} map to the same page {page}; rename one of them.")
    return page_of, titles


def _escape(text):
    return html.escape(text, quote=True)


def _link(text, page, anchor):
    return f"[`{text}`](<{page}#{_escape(anchor)}>)"


def registered(function):
    """True when a decorator other than staticmethod or classmethod wraps the function (a property, a route, a
    registration): something outside the call graph calls it."""
    return any(d not in ("staticmethod", "classmethod") for d in function.get("decorators", ()))


def _with_pending_notice(text):
    """The page with a notice under its navigation line while any prose block is empty; the notice sits outside the
    markers, so it is never taken for prose and goes away once every block is written."""
    pending = sum(1 for _key, body in PROSE.findall(text) if not body.strip())
    if not pending:
        return text
    lines = text.split("\n")
    at = next(i for i, line in enumerate(lines) if i and line.startswith("["))
    notice = (f"> Prose pending: {pending} block(s) between the `smart-tool:prose` markers are empty. The text is not "
              "generated with the skeleton: project_manage action=docs write_prose=true writes it with the Docs model, "
              "checking each paragraph against the code, or write it by hand; regenerating keeps what is written and "
              "removes this notice when none is left.")
    return "\n".join(lines[:at + 1] + ["", notice] + lines[at + 1:])


def _prose(key, kept, stamp=None):
    """A prose block: written text keeps the stamp it was written under, and text without one (rewritten by hand, its
    stamp deleted) or an empty block takes stamp, the code as it is now."""
    body, written_for = kept.get(key, ("", None))
    return f"{prose_open(key, (written_for or stamp) if body else stamp)}\n{body}<!-- /smart-tool:prose -->\n"


class _Model:
    """Graph, layer rule and per-function facts of the project's current view, read once per request."""

    def __init__(self, root, view_id):
        self.root = root
        self._texts = {}
        self._entries = None
        self._files = None
        self._command_lines = None
        self._flows = {}
        self._runners = {}
        self.roots_hidden = 0
        self.graph = code_graph.build_current(root, view_id, shared=True)
        self.scope = index_scope.load_scope(root) or {}
        profile = self.profile = index_profile.current(self.scope.get("profile"))
        self.rule = layers.load(root)
        self.kind = {}
        by_path = defaultdict(list)
        for symbol in self.graph.get("symbols") or []:
            self.kind.setdefault(symbol["path"], index_profile.kind(symbol["path"], profile))
            by_path[symbol["path"]].append(symbol)
        self.anchor_of, self.function_of, self.symbol_of = {}, {}, {}
        for path, symbols in by_path.items():
            if not doc_check.supported(path) or self.kind[path] not in ("code", "test"):
                continue
            source = self.read(path)
            if source is None:
                continue
            found = [f for f in doc_check.functions(path, source, symbols) if f.get("kind", "function")
                     in ("function", "method")]
            ids = anchor_ids(found, path)
            starts = {s["start_line"]: s for s in symbols}
            for function in found:
                symbol = starts.get(function["start"])
                if symbol:
                    anchor = ids[function["start"]]
                    self.anchor_of[symbol["id"]] = anchor
                    self.function_of[anchor] = {**function, "path": path, "id": anchor}
                    self.symbol_of[anchor] = symbol
        self.callers, self.callees = defaultdict(set), defaultdict(set)
        self.used_by_module = set()
        for call in self.graph.get("calls") or []:
            source, target = self.anchor_of.get(call["source"]), self.anchor_of.get(call["target"])
            if source and target and source != target:
                self.callers[target].add(source)
                self.callees[source].add(target)
            elif target and not source and self.kind.get(call["source"].rpartition("::")[0]) == "code":
                self.used_by_module.add(target)
        annotated = bool(self.rule) and any(layer["annotations"] for layer in self.rule.layers)
        self.layer_of = {path: self.rule.assign(path, self.read(path) if annotated else None) if self.rule else None
                         for path in by_path}

    def read(self, path):
        """Text of a project file, read once; None when it cannot be read as UTF-8."""
        if path not in self._texts:
            try:
                with open(os.path.join(self.root, path), encoding="utf-8") as stream:
                    self._texts[path] = stream.read()
            except (OSError, UnicodeDecodeError):
                self._texts[path] = None
        return self._texts[path]

    def files(self):
        """Project-relative paths of the indexed files, resolved once; the files that hold symbols when the project has
        no valid scope."""
        if self._files is None:
            found = (index_scope.resolve_included_files(self.root, self.scope) if index_scope.valid_scope(self.scope)
                     else sorted(self.kind))
            self._files = [p for p in found if not p.startswith(DOCS_DIR + "/")]
        return list(self._files)

    def kind_of(self, path):
        """code, test or doc for a project file under the index profile; None when the profile has no class for it."""
        return self.kind[path] if path in self.kind else index_profile.kind(path, self.profile)

    def public(self):
        """Anchors of the public code functions (the language convention requires their docstring), by file."""
        return [a for a, f in sorted(self.function_of.items(), key=lambda item: (item[1]["path"], item[1]["start"]))
                if f["required"] and self.kind[f["path"]] == "code"]

    def tests_of(self, anchor):
        """Test functions that reach anchor through static calls within TEST_DEPTH hops, nearest first."""
        found, seen, frontier = [], {anchor}, [anchor]
        for _hop in range(TEST_DEPTH):
            reached = []
            for target in frontier:
                for caller in sorted(self.callers[target]):
                    if caller in seen or len(seen) > MAX_VISITED:
                        continue
                    seen.add(caller)
                    if self.kind[self.function_of[caller]["path"]] == "test":
                        found.append(caller)
                    else:
                        reached.append(caller)
            frontier = reached
        return found

    def flow_edges(self, anchor):
        """Calls (caller, callee) that lead from anchor to the code functions within FLOW_DEPTH hops, one per reached
        function, in visiting order."""
        if anchor not in self._flows:
            self._flows[anchor] = self._walk_flow(anchor)
        return list(self._flows[anchor])

    def _walk_flow(self, anchor):
        seen, queue, edges = {anchor}, deque([(anchor, 0)]), []
        while queue and len(edges) < MAX_FLOW:
            current, depth = queue.popleft()
            if depth == FLOW_DEPTH:
                continue
            for callee in sorted(self.callees[current]):
                if callee not in seen and self.kind[self.function_of[callee]["path"]] == "code":
                    seen.add(callee)
                    edges.append((current, callee))
                    queue.append((callee, depth + 1))
        return edges[:MAX_FLOW]

    def flow(self, anchor):
        """Code functions reached from anchor through static calls within FLOW_DEPTH hops, in visiting order."""
        return [callee for _caller, callee in self.flow_edges(anchor)]

    def _python_code(self):
        return [p for p in self.files() if p.endswith(".py") and self.kind_of(p) == "code"]

    def launched(self):
        """Anchors of the functions a `__main__` block of a code file runs (the script's own main or the function a
        launcher script imports and calls)."""
        files, by_name, found = set(self.files()), {}, []
        for anchor, function in sorted(self.function_of.items(), key=lambda i: (i[1]["path"], i[1]["start"])):
            by_name.setdefault((function["path"], function["name"]), anchor)
        for path in self._python_code():
            source = self.read(path)
            for pair in doc_cli.launched(path, source, files) if source else []:
                if pair in by_name and by_name[pair] not in found:
                    found.append(by_name[pair])
        return found

    def command_lines(self):
        """argparse command lines of the code files (see doc_cli.definitions), by file and line, parsed once."""
        if self._command_lines is None:
            found = []
            for path in self._python_code():
                source = self.read(path)
                found += doc_cli.definitions(path, source) if source else []
            self._command_lines = found
        return self._command_lines

    def entry_points(self):
        """Entry points: every function named main, the functions a `__main__` block runs and, when layers are
        declared, the public functions of the highest layer; without layers, the roots of the call graph (see roots)."""
        if self._entries is None:
            top = self.rule.layers[0]["name"] if self.rule else None
            ordered = sorted(self.function_of.items(), key=lambda item: (item[1]["path"], item[1]["start"]))
            code = [(a, f) for a, f in ordered if self.kind[f["path"]] == "code"]
            chosen = {a for a, f in code if f["name"].split(".")[-1] == "main"
                      or top and f["required"] and self.layer_of[f["path"]] == top} | set(self.launched())
            if not self.rule:
                chosen |= set(self.roots(code))
            self._entries = [a for a, _f in code if a in chosen]
        return self._entries

    def roots(self, code):
        """The features of a project without declared layers: public functions no code function calls whose flow
        reaches at least MIN_ROOT_FLOW others (a command, a UI action, a job), the MAX_ROOTS with the widest flow."""
        found = []
        for anchor, function in code:
            if not function["required"] or function["name"].split(".")[-1].startswith("__") or registered(function) \
                    or self.callers_in_code(anchor):
                continue
            reach = len(self.flow(anchor))
            if reach >= MIN_ROOT_FLOW:
                found.append((-reach, function["path"], function["start"], anchor))
        found.sort()
        self.roots_hidden = max(0, len(found) - MAX_ROOTS)
        return [anchor for *_key, anchor in found[:MAX_ROOTS]]

    def callers_in_code(self, anchor):
        """Callers of anchor that are code functions, not tests."""
        return [c for c in self.callers[anchor] if self.kind[self.function_of[c]["path"]] == "code"]

    def code_reached(self, anchor):
        """Code functions reached from anchor (a test) through static calls within TEST_DEPTH hops."""
        seen, queue = {anchor}, deque([(anchor, 0)])
        while queue:
            current, depth = queue.popleft()
            if depth == TEST_DEPTH:
                continue
            for callee in self.callees[current]:
                if callee not in seen:
                    seen.add(callee)
                    queue.append((callee, depth + 1))
        return [a for a in seen if self.kind[self.function_of[a]["path"]] == "code"]


def _kept_prose(root):
    """Prose blocks of the generated documents already in the project: (relative file, key) -> (text, code stamp or
    None)."""
    kept, folder = {}, os.path.join(root, DOCS_DIR)
    for rel in _generated_files(root):
        with open(os.path.join(folder, rel), encoding="utf-8") as stream:
            text = stream.read()
        for key, stamp, body in STAMPED_PROSE.findall(text):
            if body.strip():
                kept[(rel, key)] = (body if body.endswith("\n") else body + "\n", stamp or None)
    return kept


def _generated_files(root):
    """Markdown files under docs/scope written by this generator (first line is the GENERATED marker)."""
    folder, found = os.path.join(root, DOCS_DIR), []
    for current, _dirs, names in os.walk(folder):
        for name in names:
            if name.endswith(".md"):
                full = os.path.join(current, name)
                with open(full, encoding="utf-8") as stream:
                    if stream.readline().rstrip("\n") == GENERATED:
                        found.append(os.path.relpath(full, folder).replace(os.sep, "/"))
    return sorted(found)


def _links(model, anchors, page_of, from_page):
    shown = []
    for anchor in sorted(anchors)[:MAX_LINKS]:
        name = model.function_of[anchor]["name"]
        target = page_of.get(anchor)
        shown.append(_link(name, posixpath.relpath(target, posixpath.dirname(from_page) or "."), anchor) if target
                     else f"`{name}` ({model.function_of[anchor]['path']})")
    more = len(anchors) - MAX_LINKS
    return ", ".join(shown) + (f" (+{more} more)" if more > 0 else "")


def _technical(model, kept):
    """Technical pages by folder and their index with the Mermaid flow by layer: relative file -> text."""
    public = model.public()
    page_of, titles = _pages(model, public)
    truncated = TRUNCATED if model.graph.get("truncated") else ""
    pages, by_page = {}, defaultdict(list)
    for anchor in public:
        by_page[page_of[anchor]].append(anchor)
    for page, anchors in sorted(by_page.items()):
        here = posixpath.dirname(page)
        lines = [GENERATED, f"# `{titles[page]}`", "", f"[Index](<{posixpath.relpath('technical/README.md', here)}>) · "
                 f"[Functional](<{posixpath.relpath('functional.md', here)}>)", ""]
        if truncated:
            lines += [truncated]
        own, modules = {k: v for (f, k), v in kept.items() if f == page}, defaultdict(list)
        for anchor in anchors:
            modules[model.function_of[anchor]["path"]].append(anchor)
        for path, entries in modules.items():
            layer = model.layer_of.get(path)
            lines += [f"## `{path}`", ""] + ([f"Layer: {layer}", ""] if layer else [])
            lines += [_prose(path, own).rstrip("\n"), ""]
            for anchor in entries:
                function = model.function_of[anchor]
                tests = model.tests_of(anchor)
                lines += [f'<a id="{_escape(anchor)}"></a>', f"### `{function['name']}`", "",
                          f'<!-- smart-tool:symbol id="{_escape(anchor)}" signature="{signature_hash(function["signature"])}" -->',
                          "", f"`{function['signature'] or function['name']}` (line {function['start']})", "",
                          function["doc"] or "*No docstring yet.*", ""]
                callers = {c for c in model.callers[anchor] if model.kind[model.function_of[c]["path"]] == "code"}
                if callers:
                    lines.append(f"- Called by: {_links(model, callers, page_of, page)}")
                if model.callees[anchor]:
                    lines.append(f"- Calls: {_links(model, model.callees[anchor], page_of, page)}")
                if tests:
                    lines.append("- Tests: " + ", ".join(f"`{model.function_of[t]['name']}` ({model.function_of[t]['path']})"
                                                         for t in tests[:MAX_LINKS]))
                lines.append("")
        pages[page] = "\n".join(lines).rstrip("\n") + "\n"
    pages["technical/README.md"] = _technical_index(model, by_page, titles, truncated)
    return pages, page_of


def _technical_index(model, by_page, titles, truncated):
    lines = [GENERATED, "# Technical reference", "", "[Scope](../README.md) · [Functional](../functional.md)", ""]
    if truncated:
        lines += [truncated]
    lines += ["## Flow by layer", ""] + _mermaid(model) + [""]
    lines += ["## Pages", "", "| Folder or module | Public functions | Documented |", "|---|---|---|"]
    for page, anchors in sorted(by_page.items()):
        documented = sum(1 for a in anchors if model.function_of[a]["documented"])
        lines.append(f"| [`{titles[page]}`](<{posixpath.relpath(page, 'technical')}>) | {len(anchors)} | {documented} |")
    return "\n".join(lines) + "\n"


def _mermaid(model):
    """Mermaid flowchart of code folders (one subgraph per declared layer, in order) and the static calls between them,
    the strongest MAX_FLOW_EDGES edges."""
    folder_of = {a: posixpath.dirname(f["path"]) or "." for a, f in model.function_of.items()
                 if model.kind[f["path"]] == "code"}
    weights = defaultdict(int)
    for source, targets in model.callees.items():
        for target in targets:
            if source in folder_of and target in folder_of and folder_of[source] != folder_of[target]:
                weights[(folder_of[source], folder_of[target])] += 1
    edges = sorted(weights.items(), key=lambda item: (-item[1], item[0]))[:MAX_FLOW_EDGES]
    folders = sorted({f for edge, _n in edges for f in edge} | set(folder_of.values()))
    node = {folder: f"f{i}" for i, folder in enumerate(folders)}
    layer_of_folder = {}
    for anchor, folder in folder_of.items():
        layer = model.layer_of.get(model.function_of[anchor]["path"])
        if layer:
            layer_of_folder.setdefault(folder, layer)
    lines = ["```mermaid", "flowchart TD"]
    for index, layer in enumerate(model.rule.layers if model.rule else []):
        members = [f for f in folders if layer_of_folder.get(f) == layer["name"]]
        if members:
            lines.append(f'  subgraph L{index}["{layer["name"]}"]')
            lines += [f'    {node[f]}["{f}"]' for f in members]
            lines.append("  end")
    lines += [f'  {node[f]}["{f}"]' for f in folders if f not in layer_of_folder]
    lines += [f"  {node[a]} -->|{n}| {node[b]}" for (a, b), n in edges]
    lines.append("```")
    if not model.rule:
        lines.append("\nNo layers declared yet: project_manage action=layers suggests them; once confirmed, this flow "
                     "groups the folders by layer.")
    return lines


def entry_title(model, anchor, entries):
    """Heading text of an entry point: its name, plus its file when another entry point has the same name."""
    function = model.function_of[anchor]
    twins = sum(model.function_of[a]["name"] == function["name"] for a in entries)
    return f"`{function['name']}`" + (f" in `{function['path']}`" if twins > 1 else "")


def entry_link(model, anchor, entries, page):
    """Link to an entry point's section of the functional overview, labelled with its heading."""
    return f"[{entry_title(model, anchor, entries)}](<{page}#{_escape(anchor)}>)"


def cli_anchor(definition):
    """Anchor of a command line in the functional overview."""
    return f"cli:{definition['path']}::{definition['function'] or '<module>'}@{definition['line']}"


def cli_title(definition):
    """Heading text of a command line: the function that declares it and its file."""
    where = f"`{definition['function']}` in `{definition['path']}`" if definition["function"] else f"`{definition['path']}`"
    return where + (" (options added to a parser it receives)" if definition["shared"] else "")


def cli_runners(model, definition, entries):
    """Entry points that run a command line: the function declaring it, or one whose flow reaches it; for a module-level
    parser, the entry points of its file."""
    key = (definition["path"], definition["function"], definition["line"], tuple(entries))
    if key not in model._runners:
        if not definition["function"]:
            model._runners[key] = [a for a in entries if model.function_of[a]["path"] == definition["path"]]
        else:
            declaring = {a for a, f in model.function_of.items() if f["path"] == definition["path"]
                         and f["name"] == definition["function"] and f["start"] <= definition["line"] <= f["end"]}
            model._runners[key] = [a for a in entries if a in declaring or declaring.intersection(model.flow(a))]
    return list(model._runners[key])


def _command_lines(model, entries):
    definitions = model.command_lines()
    if not definitions:
        return []
    lines = ["## Command line", "", "The commands, arguments and options the code declares with argparse, with their "
             "defaults and help text as written in the code.", ""]
    for definition in definitions:
        runners = cli_runners(model, definition, entries)
        lines += [f'<a id="{_escape(cli_anchor(definition))}"></a>', f"### {cli_title(definition)}", "",
                  f"Declared at line {definition['line']}. Run by: "
                  + (", ".join(entry_link(model, a, entries, "") for a in runners) if runners
                     else "no entry point reaches it through static calls") + ".", ""]
        lines += doc_cli.lines(definition) + [""]
    return lines


def project_name(model):
    """Name the project declares in a manifest (pyproject.toml, package.json, Cargo.toml, go.mod), the one nearest the
    root first, without an npm scope; the folder name when no manifest declares one."""
    manifests = sorted((p for p in model.files() if posixpath.basename(p) in ("pyproject.toml", "package.json",
                                                                            "Cargo.toml", "go.mod")),
                       key=lambda p: (p.count("/"), p))
    for path in manifests:
        text = model.read(path)
        if not text:
            continue
        base = posixpath.basename(path)
        try:
            if base == "package.json":
                name = json.loads(text).get("name")
            elif base == "go.mod":
                found = re.search(r"^module\s+(\S+)", text, re.M)
                name = found.group(1).rsplit("/", 1)[-1] if found else None
            else:
                data = tomllib.loads(text)
                name = (data.get("project") or data.get("package") or {}).get("name") \
                    or data.get("tool", {}).get("poetry", {}).get("name")
        except (ValueError, AttributeError):
            continue
        if isinstance(name, str) and name.strip():
            return name.strip().rsplit("/", 1)[-1], path
    return os.path.basename(os.path.normpath(model.root)), None


def _functional(model, kept, page_of):
    entries = model.entry_points()
    lines = [GENERATED, "# Functional overview", "", "[Scope](README.md) · [Simple](simple.md) · "
             "[Technical](technical/README.md)", ""]
    if model.graph.get("truncated"):
        lines += [TRUNCATED]
    if not entries:
        lines += ["No entry point found: declare the layers (project_manage action=layers) so the functions of the "
                  "highest layer become entry points.", ""]
    elif not model.rule:
        lines += ["Without declared layers the entry points are every `main`, the functions a `__main__` block runs "
                  "and the public functions no code function "
                  f"calls whose flow reaches {MIN_ROOT_FLOW} or more others"
                  + (f" ({model.roots_hidden} more with a narrower flow are not listed)" if model.roots_hidden else "")
                  + "; declare the layers (project_manage action=layers) to choose them by layer.", ""]
    own = {k: v for (f, k), v in kept.items() if f == "functional.md"}
    order = [l["name"] for l in model.rule.layers] if model.rule else []
    for anchor in entries:
        function = model.function_of[anchor]
        lines += [f'<a id="{_escape(anchor)}"></a>', f"## {entry_title(model, anchor, entries)}", "",
                  f'<!-- smart-tool:symbol id="{_escape(anchor)}" signature="{signature_hash(function["signature"])}" -->',
                  "", f"Entry point in `{function['path']}` (line {function['start']})."
                  + (f" Technical entry: {_link(function['name'], page_of[anchor], anchor)}." if anchor in page_of else ""),
                  "", _prose(anchor, own, code_stamp(model.read(function["path"]), function)).rstrip("\n"), ""]
        groups = defaultdict(list)
        for reached in model.flow(anchor):
            groups[model.layer_of.get(model.function_of[reached]["path"]) or "other"].append(reached)
        for layer in order + ["other"]:
            if groups.get(layer):
                lines.append(f"- {layer}: {_links(model, groups[layer], page_of, 'functional.md')}")
        lines.append("")
    lines += _command_lines(model, entries)
    return "\n".join(lines).rstrip("\n") + "\n", entries


def _simple(model, kept, entries, vocabulary=()):
    own = {k: v for (f, k), v in kept.items() if f == "simple.md"}
    lines = [GENERATED, f"# {project_name(model)[0]} in plain words", "", "[Scope](README.md) · [Functional](functional.md)", "",
             _prose("overview", own).rstrip("\n"), ""]
    if vocabulary:
        lines += [f"Words this project uses: {', '.join(vocabulary)} ([glossary](<extras/glossary.md>)).", ""]
    lines += ["## What it does", ""]
    for anchor in entries:
        function = model.function_of[anchor]
        lines += [f"### {entry_title(model, anchor, entries)}", "",
                  _prose(anchor, own, code_stamp(model.read(function["path"]), function)).rstrip("\n"),
                  f"[Details](<functional.md#{_escape(anchor)}>)", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _index(model, counts, extras=()):
    lines = [GENERATED, "# Documentation scope", "",
             "- [Simple](simple.md): what the project does, in plain words.",
             "- [Functional](functional.md): one section per entry point and the functions its flow reaches.",
             "- [Technical](technical/README.md): one entry per public function with callers, calls, tests and layer.",
             "- [Validation](validation.md): every claim of these pages checked against the code, with the verdict."]
    lines += [f"- [{title}](<{rel}>)" for title, rel in extras]
    lines += ["", f"{counts['public']} public functions on {counts['pages']} technical pages; "
                 f"{counts['entry_points']} entry points; layers: {counts['layers']}.", ""]
    if model.graph.get("truncated"):
        lines.append(TRUNCATED)
    return "\n".join(lines).rstrip("\n") + "\n"


def generate(root, view_id=None, limit=30, model=None):
    """Write the three documents to docs/scope, keeping the prose already written between prose markers.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) pending prose blocks listed;
    model (_Model or None) a snapshot of the current code to reuse, built here when None.
    Returns: dict led by 'validation' (verdict, score, claim counts, report path and the failed checks of
    doc_validate.run over the pages just written), then the files written and removed, counts, the prose blocks still
    empty and those dropped because their module or entry point is gone.
    """
    folder = os.path.join(root, DOCS_DIR)
    owned = set(_generated_files(root)) if os.path.isdir(folder) else set()
    kept = _kept_prose(root) if owned else {}
    model = model or _Model(root, view_id)
    if not model.graph.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index), "
                         "then generate again; no file was written.")
    files, page_of = _technical(model, kept)
    files["functional.md"], entries = _functional(model, kept, page_of)
    import doc_extras
    import doc_insights
    enabled = config.docs_extras()
    files["simple.md"] = _simple(model, kept, entries, doc_insights.simple_terms(model) if enabled["glossary"] else ())
    counts = {"public": len(page_of), "pages": len(files) - 3, "entry_points": len(entries),
              "layers": ", ".join(l["name"] for l in model.rule.layers) if model.rule else "none declared"}
    extras, block = doc_extras.build(model, entries, counts, enabled)
    files["README.md"] = _index(model, counts, [(doc_extras.TITLES[k], doc_extras.FILES[k]) for k in config.DOCS_EXTRAS
                                                if doc_extras.FILES.get(k) in extras])
    files.update(extras)
    for rel in files:
        if rel in ("simple.md", "functional.md") or rel.startswith("technical/") and rel != "technical/README.md":
            files[rel] = _with_pending_notice(files[rel])
    for rel in files:
        full = os.path.join(folder, rel)
        if os.path.exists(full) and rel not in owned:
            raise ValueError(f"{full} exists and was not written by Smart Tool; move it before generating.")
    for rel, text in files.items():
        full = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        atomic_io.write_text(full, text)
    import doc_validate
    removed = sorted(owned - set(files) - {doc_validate.REPORT})
    for rel in removed:
        os.remove(os.path.join(folder, rel))
        parent = os.path.dirname(os.path.join(folder, rel))
        if parent != os.path.join(folder, "technical") and not os.listdir(parent):
            os.rmdir(parent)
    pending = [{"file": f"{DOCS_DIR}/{rel}", "block": key} for rel, text in sorted(files.items())
               for key, body in PROSE.findall(text) if not body.strip()]
    index_file = doc_extras.write_index(root, block)
    model._texts.pop(index_file, None)
    present = {(rel, key) for rel, text in files.items() for key, _body in PROSE.findall(text)}
    dropped = sorted(f"{DOCS_DIR}/{rel} {key}" for rel, key in kept if (rel, key) not in present)
    validation = doc_validate.run(model, entries, limit)
    summary = {k: validation[k] for k in ("verdict", "score", "claims_checked", "claims_failed", "prose_pending", "report")}
    summary["failed_checks"] = [c for c in validation["checks"] if c["status"] == "fail"]
    return {"validation": summary, "folder": folder, "written": len(files), "removed": [f"{DOCS_DIR}/{r}" for r in removed], **counts,
            "prose_pending_total": len(pending), "prose_pending": pending[:limit], "prose_dropped": dropped[:limit],
            "truncated": bool(model.graph.get("truncated")),
            "extras": sorted(f"{DOCS_DIR}/{rel}" for rel in extras), "index_file": index_file if block is not None else None,
            "guidance": (f"Validation FAILED: {summary['claims_failed']} claim(s) do not hold; read {validation['report']} "
                         "and fix the generator or the code before trusting these pages. " if summary["verdict"] == "fail"
                         else "") + "write_prose=true writes the empty prose blocks with the Docs model of the setup, checking each "
                        "paragraph against the code; to write by hand, keep the markers: a paragraph per module "
                        "(what it is for) on the technical pages, a paragraph per entry point (the feature, in the "
                        "user's words) in functional.md, and simple.md without technical terms. Regenerate after code "
                        "changes; check=true lists anchors whose function changed or is gone; validate=true checks "
                        "every claim again, the prose included."}


def _scan(root):
    """Anchors, signature markers and links of the generated documents: (markers, anchors per file, links)."""
    markers, anchors, links = [], defaultdict(set), []
    folder = os.path.join(root, DOCS_DIR)
    for rel in _generated_files(root):
        with open(os.path.join(folder, rel), encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                for anchor, digest in MARKER.findall(line):
                    markers.append({"file": rel, "line": number, "id": html.unescape(anchor), "signature": digest})
                anchors[rel].update(html.unescape(a) for a in ANCHOR.findall(line))
                links += [{"file": rel, "line": number, "target": target, "anchor": html.unescape(anchor)}
                          for target, anchor in LINK.findall(line)]
    return markers, anchors, links


def _current_code(root, paths, symbols_by_path):
    """Signature hash and code stamp of the functions in paths as they are on disk now: two dicts by anchor id."""
    current, stamps = {}, {}
    for path in paths:
        try:
            with open(os.path.join(root, path), encoding="utf-8") as stream:
                source = stream.read()
        except (OSError, UnicodeDecodeError):
            continue
        found = [f for f in doc_check.functions(path, source, symbols_by_path.get(path))
                 if f.get("kind", "function") in ("function", "method")]
        ids = anchor_ids(found, path)
        current.update({ids[f["start"]]: signature_hash(f["signature"]) for f in found})
        stamps.update({ids[f["start"]]: code_stamp(source, f) for f in found})
    return current, stamps


def check(root, view_id=None, limit=30):
    """Drift of the generated documents: anchors whose function is gone or changed its signature since generation, and
    links whose target anchor does not exist.

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) findings listed.
    Returns: dict with 'ok', 'findings' (file, line, id, issue: missing | signature_changed | broken_link) and counts.
    """
    if not os.path.isdir(os.path.join(root, DOCS_DIR)):
        raise ValueError(f"No {DOCS_DIR} in the project yet: generate it with project_manage action=docs generate=true.")
    markers, anchors, links = _scan(root)
    graph = code_graph.build_current(root, view_id, shared=True)
    by_path = defaultdict(list)
    for symbol in graph.get("symbols") or []:
        by_path[symbol["path"]].append(symbol)
    paths = {m["id"].rpartition("::")[0] for m in markers}
    current, stamps = _current_code(root, paths, by_path)
    findings = []
    for marker in markers:
        now = current.get(marker["id"])
        if now != marker["signature"]:
            findings.append({"file": f"{DOCS_DIR}/{marker['file']}", "line": marker["line"], "id": marker["id"],
                             "issue": "missing" if now is None else "signature_changed"})
    for link in links:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(link["file"]), link["target"])) \
            if link["target"] else link["file"]
        if link["anchor"] not in anchors.get(target, ()):
            findings.append({"file": f"{DOCS_DIR}/{link['file']}", "line": link["line"], "id": link["anchor"],
                             "issue": "broken_link"})
    import doc_extras
    for link in doc_extras.index_links(root):
        links.append(link)
        if link["anchor"] not in anchors.get(link["target"], ()):
            findings.append({"file": link["file"], "line": link["line"], "id": link["anchor"], "issue": "broken_link"})
    stale = stale_prose(_read_pages(root, ("functional.md", "simple.md")), stamps)
    findings += [{**item, "file": f"{DOCS_DIR}/{item['file']}", "issue": "prose_stale"} for item in stale]
    guidance = []
    if len(stale) < len(findings):
        guidance.append("Regenerate with project_manage action=docs generate=true: the skeleton follows the code and "
                        "the prose is kept; review the prose of the entries listed.")
    if stale:
        guidance.append("prose_stale: the code of the entry changed after its paragraph was written. Empty the block and "
                        "run docs write_prose=true, or rewrite it by hand and delete its code=\"...\" stamp, then "
                        "regenerate so the block records the code it now describes.")
    return {"ok": not findings, "anchors": len(markers), "links": len(links), "findings_total": len(findings),
            "findings": findings[:limit],
            "guidance": " ".join(guidance) if findings else
                        "Every anchor matches the code, every link resolves and every entry paragraph was written for "
                        "the code of its function."}


def _read_pages(root, names):
    """Text of the named generated documents that exist: relative file -> text."""
    pages = {}
    for rel in names:
        path = os.path.join(root, DOCS_DIR, rel)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as stream:
                pages[rel] = stream.read()
    return pages


def anchors(root, symbol, view_id=None):
    """Where a function is documented: its anchors in the generated documents with the drift state of each.

    Parameters: root (str) project folder; symbol (str) name, Class.method or path::name; view_id (str or None).
    Returns: dict with 'anchors' (file, line, id, level, current) and guidance when the documents do not exist.
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("Pass symbol: a function, Class.method or path::name.")
    if not os.path.isdir(os.path.join(root, DOCS_DIR)):
        return {"symbol": symbol, "anchors": [],
                "guidance": f"No {DOCS_DIR} yet: generate it with project_manage action=docs generate=true."}
    path, _sep, name = symbol.strip().rpartition("::")
    path = path.replace("\\", "/").removeprefix("./")
    markers, _anchors, _links = _scan(root)

    def wanted(anchor):
        where, _s, full = anchor.rpartition("::")
        base = full.split("@")[0]
        return (not path or where == path or where.endswith("/" + path)) and (base == name or base.split(".")[-1] == name)

    found = [m for m in markers if wanted(m["id"])]
    graph = code_graph.build_current(root, view_id, shared=True)
    by_path = defaultdict(list)
    for item in graph.get("symbols") or []:
        by_path[item["path"]].append(item)
    current, _stamps = _current_code(root, {m["id"].rpartition("::")[0] for m in found}, by_path)
    return {"symbol": symbol.strip(),
            "anchors": [{"file": f"{DOCS_DIR}/{m['file']}", "line": m["line"], "id": m["id"],
                         "level": "technical" if m["file"].startswith("technical/") else m["file"][:-3],
                         "current": current.get(m["id"]) == m["signature"]} for m in found]}


_MARKERS_CACHE = {}


def _markers_for(root, path):
    """Signature markers of path's functions in the generated documents, cached by the documents' modification times."""
    folder = os.path.join(root, DOCS_DIR)
    if not os.path.isdir(folder):
        return []
    files = _generated_files(root)
    stamp = tuple((rel, os.stat(os.path.join(folder, rel)).st_mtime_ns) for rel in files)
    cached = _MARKERS_CACHE.get(root)
    if not cached or cached[0] != stamp:
        cached = (stamp, _scan(root)[0])
        _MARKERS_CACHE[root] = cached
    return [m for m in cached[1] if m["id"].rpartition("::")[0] == path]


def on_edit(root, rel, before, after_functions):
    """Anchors of the edited file that this edit makes stale (matched the code before, not after).

    Parameters: root (str) project folder; rel (str) project-relative path; before (str or None) text before the edit;
    after_functions (list) doc_check functions of the file after the edit.
    Returns: list of {'file', 'id', 'issue'}.
    """
    markers = _markers_for(root, rel)
    if not markers:
        return []
    absolute = os.path.join(root, rel)
    old = [f for f in doc_check.functions(absolute, before) if f.get("kind", "function") in ("function", "method")] \
        if before else []
    new = [f for f in after_functions if f.get("kind", "function") in ("function", "method")]
    old_ids, new_ids = anchor_ids(old, rel), anchor_ids(new, rel)
    then = {old_ids[f["start"]]: signature_hash(f["signature"]) for f in old}
    now = {new_ids[f["start"]]: signature_hash(f["signature"]) for f in new}
    stale = []
    for marker in markers:
        if then.get(marker["id"]) == marker["signature"] and now.get(marker["id"]) != marker["signature"]:
            stale.append({"file": f"{DOCS_DIR}/{marker['file']}", "id": marker["id"],
                          "issue": "missing" if marker["id"] not in now else "signature_changed"})
    return stale
