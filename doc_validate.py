"""Self-validation of the generated documentation scope: every claim the pages make is checked against the code, the
code graph, the command lines the code declares and the manifests, and the result is written as a pass/fail report
with the evidence of each check (docs/scope/validation.md).

Checks, in three groups:

- code to technical: signature markers, links, line claims, callers/calls/tests lists, coverage and its numbers;
- functional to product: entry points and their flows, the features a launcher runs (`__main__` blocks, package.json
  bin, pyproject scripts), the argparse command lines, the prose written by the agent, the tests per entry point;
- coherence: link labels against the headings they point to, counts across pages, the glossary, navigation and
  dependency versions against the manifests.

A failing check is never silent: the verdict is fail, the report lists each failure with file and line, and
`generate` returns the verdict at the top of its answer.
"""
import html
import json
import os
import posixpath
import re
import tomllib
from collections import defaultdict

import atomic_io
import doc_cli
import doc_extras
import doc_insights
import doc_scope

REPORT = "validation.md"
MAX_EVIDENCE = 3
MAX_FINDINGS = 60
MAX_CORPUS_FILES = 4000
_ANY_LINK = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])*)\]\((<[^>]*>|[^)\s]+)\)")
_HEADING = re.compile(r"^#{1,6} (.+)$")
_LINE_CLAIM = re.compile(r"\(line (\d+)\)")
_PATH_LINE = re.compile(r"([\w./@-]+\.\w+):(\d+)")
_UNLINKED = re.compile(r"`([^`]+)` \(([^)]+)\)")
_MORE = re.compile(r"\(\+(\d+) more\)")
_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_OPTION = re.compile(r"(?<![\w`-])(--[a-z][\w-]+)")
_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+)(?!\w|\.\w)")
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


class _Check:
    """One check: the claims it verified (with a few as evidence) and those that failed, each with where and why."""

    def __init__(self, key, title):
        self.key, self.title = key, title
        self.checked, self.pending, self.evidence, self.failures, self.notes = 0, 0, [], [], []

    def ok(self, evidence):
        self.checked += 1
        if len(self.evidence) < MAX_EVIDENCE and evidence not in self.evidence:
            self.evidence.append(evidence)

    def fail(self, where, problem):
        self.checked += 1
        self.failures.append(f"{where}: {problem}")

    def verify(self, condition, where, evidence, problem):
        if condition:
            self.ok(evidence)
        else:
            self.fail(where, problem)

    @property
    def status(self):
        if self.failures:
            return "fail"
        return "pending" if self.pending and not self.checked else "pass"

    def score(self):
        return 100.0 * (self.checked - len(self.failures)) / self.checked if self.checked else None


def _pages(root):
    """Text of each generated page under docs/scope, the previous report left out."""
    folder = os.path.join(root, doc_scope.DOCS_DIR)
    found = {}
    for rel in doc_scope._generated_files(root):
        if rel != REPORT:
            with open(os.path.join(folder, rel), encoding="utf-8") as stream:
                found[rel] = stream.read()
    return found


def _sections(text):
    """Anchored sections of a page: anchor -> {'line', 'heading' (None for an anchor inside a table row), 'body'}."""
    found, current, lines = {}, None, text.split("\n")
    for number, line in enumerate(lines, 1):
        anchors = doc_scope.ANCHOR.findall(line)
        if anchors:
            anchor = html.unescape(anchors[0])
            following = lines[number] if number < len(lines) else ""
            heading = _HEADING.match(following) if line.strip().startswith("<a id=") else None
            current = found[anchor] = {"line": number, "heading": heading.group(1) if heading else None, "body": []}
        if current is not None:
            current["body"].append((number, line))
    return found


def _links(rel, text):
    """Links of a page: (line, label, target path, anchor or None); external links (with a scheme) left out."""
    found = []
    for number, line in enumerate(text.split("\n"), 1):
        for label, target in _ANY_LINK.findall(line):
            target = target[1:-1] if target.startswith("<") else target
            if _SCHEME.match(target):
                continue
            path, _sep, anchor = target.partition("#")
            found.append((number, label, path, html.unescape(anchor) if _sep else None))
    return found


def _resolve(rel, path):
    return posixpath.normpath(posixpath.join(posixpath.dirname(rel), path)) if path else rel


def _function_line(model, function, number):
    """True when the project file's line holds the function's own name (its definition line)."""
    text = model.read(function["path"])
    lines = text.split("\n") if text else []
    name = function["name"].split(".")[-1].split("@")[0]
    return 0 < number <= len(lines) and re.search(rf"\b{re.escape(name)}\b", lines[number - 1]) is not None


def claims_of(body):
    """Claims a prose block makes that the code must back: its code spans, its command-line options outside code spans
    and its version numbers.

    Parameters: body (str) prose text.
    Returns: list of str, in order of appearance by kind.
    """
    return _CODE_SPAN.findall(body) + _OPTION.findall(_CODE_SPAN.sub("", body)) + _VERSION.findall(body)


class Corpus:
    """Names, paths, source text and command-line options of the project a prose claim is checked against, read once."""

    def __init__(self, model):
        self.model = model
        self._known = None

    def _read(self):
        if self._known is None:
            model = self.model
            names = set()
            for function in model.function_of.values():
                names.add(function["name"])
                names.update(function["name"].split("."))
            names.update(s["name"] for s in model.graph.get("symbols") or [])
            files = set(model.files())
            folders = {posixpath.dirname(p) for p in files}
            texts = [model.read(p) or "" for p in sorted(files) if model.kind_of(p) != "doc"][:MAX_CORPUS_FILES]
            options = {f for d in model.command_lines() for c in d["commands"] for a in c["arguments"] for f in a["flags"]}
            self._known = (names, files | folders, "\n".join(texts), options)
        return self._known

    def supports(self, claim):
        """Whether the code backs claim: a name, path, option or text of the code, or several words each of them.

        Parameters: claim (str).
        Returns: bool.
        """
        names, paths, text, options = self._read()
        bare = claim.strip().rstrip("()").removesuffix("()")
        if bare in names or bare.rstrip("/") in paths or bare in options or bare in text:
            return True
        words = [w.strip(".,:;()[]{}'\"") for w in bare.split()]
        return len(words) > 1 and all(w in names or w in paths or w in options or w in text for w in words if w)


class _Validation:
    def __init__(self, model, entries):
        self.model, self.entries, self.root = model, entries, model.root
        self.pages = _pages(model.root)
        self.sections = {rel: _sections(text) for rel, text in self.pages.items()}
        self.public = model.public()
        self.corpus = Corpus(model)
        self.checks = []

    def check(self, key, title):
        found = _Check(key, title)
        self.checks.append(found)
        return found

    def _technical_pages(self):
        return [rel for rel in self.pages if rel.startswith("technical/") and rel != "technical/README.md"]

    def _anchors_of(self, rel):
        return set(self.sections.get(rel, {}))

    def markers(self):
        check = self.check("markers", "Every signature marker and entry paragraph matches its function in the code")
        markers, _anchors, _links = doc_scope._scan(self.root)
        by_path = defaultdict(list)
        for symbol in self.model.graph.get("symbols") or []:
            by_path[symbol["path"]].append(symbol)
        current, stamps = doc_scope._current_code(self.root, {m["id"].rpartition("::")[0] for m in markers}, by_path)
        for marker in markers:
            now = current.get(marker["id"])
            check.verify(now == marker["signature"], f"{marker['file']}:{marker['line']}",
                         f"`{marker['id']}` ({marker['file']}:{marker['line']}) has the signature of the code",
                         f"`{marker['id']}` " + ("no longer exists in the code" if now is None else "changed its signature"))
        for item in doc_scope.stale_prose(self.pages, stamps):
            check.fail(f"{item['file']}:{item['line']}", f"the paragraph of `{item['id']}` was written for other code of "
                       "the function: its body changed since")

    def links(self):
        check = self.check("links", "Every link resolves to an existing page, file and anchor")
        known = set(self.pages) | {REPORT}
        for rel, text in self.pages.items():
            for number, _label, path, anchor in _links(rel, text):
                self._link(check, rel, number, path, anchor, known, _resolve(rel, path))
        block = self._index_block()
        if block:
            name, first, text = block
            for number, _label, path, anchor in _links(name, text):
                target = path[len(doc_scope.DOCS_DIR) + 1:] if path.startswith(doc_scope.DOCS_DIR + "/") else None
                if target is None:
                    check.verify(os.path.exists(os.path.join(self.root, path)), f"{name}:{first + number - 1}",
                                 f"{name} links `{path}`", f"`{path}` does not exist")
                else:
                    self._link(check, name, first + number - 1, target, anchor, known, target, shown=f"{name}:{first + number - 1}")

    def _link(self, check, rel, number, path, anchor, known, target, shown=None):
        where = shown or f"{rel}:{number}"
        if target.startswith("../"):
            exists = os.path.exists(os.path.normpath(os.path.join(self.root, doc_scope.DOCS_DIR, target)))
            check.verify(exists, where, f"{where} links `{path}`, which exists", f"`{path}` does not exist")
            return
        if target not in known:
            check.fail(where, f"`{path or rel}` is not a page of the documentation")
            return
        if anchor is None or target == REPORT:
            check.ok(f"{where} links the page `{target}`")
            return
        check.verify(anchor in self._anchors_of(target), where, f"{where} links `{target}#{anchor}`",
                     f"anchor `{anchor}` is not on `{target}`")

    def _index_block(self):
        name = doc_extras._index_target(self.root)
        if name is None:
            return None
        with open(os.path.join(self.root, name), encoding="utf-8") as stream:
            text = stream.read()
        found = doc_extras._INDEX_BLOCK.search(text)
        return (name, text[:found.start(1)].count("\n") + 1, found.group(1)) if found else None

    def lines(self):
        check = self.check("lines", "Every line number the pages give points at the code it names")
        for rel in self._technical_pages() + ["functional.md"]:
            for anchor, section in self.sections.get(rel, {}).items():
                function = self.model.function_of.get(anchor)
                if function is None:
                    continue
                for number, line in section["body"][:8]:
                    claim = _LINE_CLAIM.search(line)
                    if claim and (line.startswith("`") or line.startswith("Entry point in")):
                        at = int(claim.group(1))
                        check.verify(_function_line(self.model, function, at), f"{rel}:{number}",
                                     f"`{function['name']}` is at {function['path']}:{at}",
                                     f"line {at} of `{function['path']}` does not hold `{function['name']}`")
                        break
        for rel, text in self.pages.items():
            if rel not in ("extras/configuration.md", "extras/dead-code.md"):
                continue
            for number, line in enumerate(text.split("\n"), 1):
                name = _CODE_SPAN.search(line)
                if not name or not (line.startswith("| `") or line.startswith("- `")):
                    continue
                for path, at in _PATH_LINE.findall(line):
                    source = self.model.read(path)
                    rows = source.split("\n") if source else []
                    word = name.group(1).split(".")[-1]
                    held = 0 < int(at) <= len(rows) and word in rows[int(at) - 1]
                    check.verify(held, f"{rel}:{number}", f"`{name.group(1)}` is at {path}:{at}",
                                 f"line {at} of `{path}` does not hold `{name.group(1)}`")

    def relations(self):
        check = self.check("relations", "Callers, calls and tests of each technical entry match the code graph")
        model = self.model
        for rel in self._technical_pages():
            for anchor, section in self.sections[rel].items():
                if anchor not in model.function_of:
                    continue
                callers = set(model.callers_in_code(anchor))
                expected = {"Called by": callers, "Calls": set(model.callees[anchor])}
                listed = {key: None for key in expected}
                tests_line = None
                for number, line in section["body"]:
                    for key in expected:
                        if line.startswith(f"- {key}: "):
                            listed[key] = (number, line)
                    if line.startswith("- Tests: "):
                        tests_line = (number, line)
                name = model.function_of[anchor]["name"]
                for key, want in expected.items():
                    if listed[key] is None:
                        check.verify(not want, f"{rel}:{section['line']}", f"`{name}` lists no {key.lower()}: the graph has none",
                                     f"`{name}` does not list its {len(want)} {key.lower()}")
                        continue
                    number, line = listed[key]
                    linked = {html.unescape(a) for _t, a in doc_scope.LINK.findall(line)}
                    more = _MORE.search(line)
                    total = len(doc_scope.LINK.findall(line)) + len(_UNLINKED.findall(line)) + (int(more.group(1)) if more else 0)
                    wrong = linked - want
                    check.verify(not wrong and total == len(want), f"{rel}:{number}",
                                 f"`{name}`: {total} {key.lower()} as in the graph",
                                 f"`{name}` lists {total} {key.lower()}, the graph has {len(want)}"
                                 + (f"; not in the graph: {', '.join(sorted(wrong))}" if wrong else ""))
                tests = model.tests_of(anchor)
                shown = [t for t, _p in _UNLINKED.findall(tests_line[1])] if tests_line else []
                names = [model.function_of[t]["name"] for t in tests[:doc_scope.MAX_LINKS]]
                check.verify(shown == names, f"{rel}:{tests_line[0] if tests_line else section['line']}",
                             f"`{name}`: {len(shown)} test(s) reach it", f"`{name}` lists tests {shown}, the graph reaches {names}")

    def coverage(self):
        check = self.check("coverage", "Every public function has its technical entry and the coverage numbers are real")
        model = self.model
        documented_at = {}
        for rel in self._technical_pages():
            for anchor in self.sections[rel]:
                documented_at[anchor] = rel
        for anchor in self.public:
            check.verify(anchor in documented_at, f"technical: `{anchor}`", f"`{anchor}` is on {documented_at.get(anchor)}",
                         "public function with no technical entry")
        for anchor, rel in documented_at.items():
            if anchor not in set(self.public):
                check.fail(f"{rel}:{self.sections[rel][anchor]['line']}", f"`{anchor}` is not a public code function")
        gaps = [a for a in self.public if not model.function_of[a]["documented"]]
        check.notes.append(f"Gaps: {len(gaps)} of {len(self.public)} public functions have no docstring"
                           + (f" (first: {', '.join(f'`{a}`' for a in gaps[:5])})" if gaps else "") + ".")
        stats = defaultdict(lambda: [0, 0])
        for anchor in self.public:
            function = model.function_of[anchor]
            row = stats[posixpath.dirname(function["path"]) or "(project root)"]
            row[0] += 1
            row[1] += bool(function["documented"])
        stats["**All**"] = [len(self.public), len(self.public) - len(gaps)]
        text = self.pages.get("extras/coverage.md")
        if text is not None:
            rows = {}
            for number, line in enumerate(text.split("\n"), 1):
                cells = [c.strip() for c in line.strip("|").split("|")]
                if len(cells) == 4 and cells[1].isdigit():
                    rows[cells[0].strip("`")] = (number, int(cells[1]), int(cells[2]))
            for folder, (total, documented) in stats.items():
                number, *shown = rows.get(folder, (None, None, None))
                check.verify(shown == [total, documented], f"extras/coverage.md:{number or 1}",
                             f"coverage of `{folder}`: {documented}/{total}",
                             f"`{folder}` shows {shown}, the code has {total} public and {documented} documented")
        technical = self.pages.get("technical/README.md", "")
        for number, line in enumerate(technical.split("\n"), 1):
            found = re.match(r"\| \[`[^`]*`\]\(<([^>]+)>\) \| (\d+) \| (\d+) \|$", line)
            if found:
                page = posixpath.normpath(posixpath.join("technical", found.group(1)))
                anchors = [a for a in self.sections.get(page, {}) if a in model.function_of]
                documented = sum(1 for a in anchors if model.function_of[a]["documented"])
                check.verify([len(anchors), documented] == [int(found.group(2)), int(found.group(3))],
                             f"technical/README.md:{number}", f"`{page}`: {len(anchors)} entries, {documented} documented",
                             f"`{page}` shows {found.group(2)}/{found.group(3)}, the page holds {len(anchors)}/{documented}")

    def entry_points(self):
        check = self.check("entry_points", "Each entry point of the functional overview is a real entry point and its flow is reachable")
        model, launched = self.model, set(self.model.launched())
        top = model.rule.layers[0]["name"] if model.rule else None
        sections = {a: s for a, s in self.sections.get("functional.md", {}).items() if not a.startswith("cli:")}
        for anchor, section in sections.items():
            where = f"functional.md:{section['line']}"
            function = model.function_of.get(anchor)
            if function is None:
                check.fail(where, f"`{anchor}` is not a function of the code")
                continue
            reasons = [r for r, held in (("named main", function["name"].split(".")[-1] == "main"),
                                         ("run by a __main__ block", anchor in launched),
                                         ("called by no code function", not model.callers_in_code(anchor)),
                                         (f"in the top layer {top}", top and model.layer_of.get(function["path"]) == top)) if held]
            check.verify(reasons, where, f"`{function['name']}` ({function['path']}) is an entry point: {reasons[0] if reasons else ''}",
                         f"`{function['name']}` is called by {', '.join(sorted(model.callers_in_code(anchor)))[:200]}")
            flow = set(model.flow(anchor))
            listed, total = set(), 0
            for _number, line in section["body"]:
                if line.startswith("- ") and ": " in line:
                    listed |= {html.unescape(a) for _t, a in doc_scope.LINK.findall(line)}
                    more = _MORE.search(line)
                    total += len(doc_scope.LINK.findall(line)) + len(_UNLINKED.findall(line)) + (int(more.group(1)) if more else 0)
            check.verify(listed <= flow and total == len(flow), where, f"flow of `{function['name']}`: {total} function(s) reached",
                         f"flow of `{function['name']}` lists {total}, the code reaches {len(flow)}"
                         + (f"; unreachable: {', '.join(sorted(listed - flow))}" if listed - flow else ""))
        for anchor in self.entries:
            if anchor not in sections:
                check.fail("functional.md", f"entry point `{anchor}` of the code has no section")

    def features(self):
        check = self.check("features", "Every feature a launcher runs (__main__ block, package.json bin, pyproject script) is documented")
        model = self.model
        documented = {a for a in self.sections.get("functional.md", {}) if not a.startswith("cli:")}
        files, by_name = set(model.files()), {}
        for anchor, function in model.function_of.items():
            by_name.setdefault((function["path"], function["name"]), anchor)
        for path in model._python_code():
            source = model.read(path)
            if not source or "__main__" not in source:
                continue
            for target in doc_cli.launched(path, source, files):
                anchor = by_name.get(target)
                check.verify(anchor in documented, path, f"`{path}` runs `{target[1]}` ({target[0]}), documented",
                             f"runs `{target[1]}` ({target[0]}), which has no section in functional.md")
        for path in sorted(p for p in files if posixpath.basename(p) in ("package.json", "pyproject.toml")):
            text = model.read(path)
            try:
                data = json.loads(text) if path.endswith(".json") else tomllib.loads(text)
            except (ValueError, TypeError):
                continue
            folder = posixpath.dirname(path)
            if path.endswith(".json"):
                bins = data.get("bin")
                bins = {data.get("name", ""): bins} if isinstance(bins, str) else bins if isinstance(bins, dict) else {}
                for command, script in bins.items():
                    script = posixpath.normpath(posixpath.join(folder, script))
                    if script not in files:
                        check.notes.append(f"{path}: command `{command}` runs `{script}`, which is not a file of the "
                                           "repository (a build output), so it was not checked")
                        continue
                    hit = [a for a in documented if model.function_of.get(a, {}).get("path") == script]
                    check.verify(hit, path, f"command `{command}` runs `{script}`, documented",
                                 f"command `{command}` runs `{script}`, which has no entry point in functional.md")
            for command, spec in ((data.get("project") or {}).get("scripts") or {}).items() if not path.endswith(".json") else ():
                module, _sep, name = str(spec).partition(":")
                target = doc_cli._module_file(path, module, 0, files)
                hit = by_name.get((target, name.split(".")[0])) if target else None
                check.verify(hit in documented, path, f"script `{command}` runs `{spec}`, documented",
                             f"script `{command}` runs `{spec}`, which has no section in functional.md")

    def command_lines(self):
        check = self.check("command_line", "The commands, arguments and options documented are those the code declares")
        model = self.model
        sections = self.sections.get("functional.md", {})
        expected = {doc_scope.cli_anchor(d): d for d in model.command_lines()}
        for anchor, definition in expected.items():
            where = f"{definition['path']}:{definition['line']}"
            section = sections.get(anchor)
            if section is None:
                check.fail(where, f"command line {doc_scope.cli_title(definition)} is missing from functional.md")
                continue
            body = {line: number for number, line in section["body"]}
            want = doc_cli.lines(definition)
            commands = iter(definition["commands"])
            command = None
            for line in want:
                if not line.startswith("  "):
                    command = next(commands)
                shown = f"{doc_cli.command_title(command)} at {definition['path']}:{command['line']}"
                check.verify(line in body, where, shown, f"not documented: {line.strip()}")
            for line, number in body.items():
                if line.startswith(("- ", "  - ")) and line not in want:
                    check.fail(f"functional.md:{number}", f"not in the code: {line.strip()}")
            runners = {html.unescape(a) for _t, a in doc_scope.LINK.findall("\n".join(body))}
            check.verify(runners == set(doc_scope.cli_runners(model, definition, self.entries)), f"functional.md:{section['line']}",
                         f"{doc_scope.cli_title(definition)} is run by {len(runners)} entry point(s)",
                         "the entry points listed as running it differ from the call graph")
        for anchor, section in sections.items():
            if anchor.startswith("cli:") and anchor not in expected:
                check.fail(f"functional.md:{section['line']}", f"command line `{anchor}` is not in the code")

    def prose(self):
        check = self.check("prose", "Each claim of the written prose is backed by the code")
        for rel, text in self.pages.items():
            for block in doc_scope.PROSE.finditer(text):
                key, body = block.groups()
                if not body.strip():
                    check.pending += 1
                    continue
                number = text[:block.start()].count("\n") + 2
                claims = claims_of(body)
                if not claims:
                    check.ok(f"{rel}:{number}: block `{key}` names nothing to check")
                for claim in claims:
                    check.verify(self.corpus.supports(claim), f"{rel}:{number}", f"`{claim}` ({rel}, block `{key}`) is in the code",
                                 f"`{claim}` (block `{key}`) is not found in the code, its files or its command lines")
        if check.pending:
            check.notes.append(f"{check.pending} prose block(s) are empty: the agent writes them (see prose_pending of generate).")

    def entry_tests(self):
        check = self.check("entry_tests", "The tests per entry point match the code graph")
        text = self.pages.get("extras/features-tests.md")
        if text is None:
            return
        rows = 0
        for number, line in enumerate(text.split("\n"), 1):
            marker = doc_scope.MARKER.search(line)
            if not marker or not line.startswith("|"):
                continue
            rows += 1
            anchor = html.unescape(marker.group(1))
            count = int(line.split("|")[2].strip())
            tests = self.model.tests_of(anchor) if anchor in self.model.function_of else []
            check.verify(count == len(tests), f"extras/features-tests.md:{number}", f"`{anchor}`: {count} test(s)",
                         f"`{anchor}` shows {count} test(s), the graph reaches {len(tests)}")
        bare = sum(1 for a in self.entries[:rows] if not self.model.tests_of(a))
        found = re.search(r"^(\d+) of (\d+) entry point", text, re.M)
        check.verify(found and [int(found.group(1)), int(found.group(2))] == [bare, rows], "extras/features-tests.md",
                     f"{bare} of {rows} entry points have no test", f"the summary line does not say {bare} of {rows}")

    def labels(self):
        check = self.check("labels", "Every link that names a section carries the name of its heading")
        headings = {(rel, a): s["heading"] for rel, found in self.sections.items() for a, s in found.items() if s["heading"]}
        for rel, text in self.pages.items():
            for number, label, path, anchor in _links(rel, text):
                heading = headings.get((_resolve(rel, path), anchor)) if anchor else None
                if heading is None or "`" not in label:
                    continue
                check.verify(label == heading, f"{rel}:{number}", f"{rel}:{number} links {heading}",
                             f"label {label} differs from the heading {heading}")

    def counts(self):
        check = self.check("counts", "Counts and names agree across pages")
        entries = [a for a in self.sections.get("functional.md", {}) if not a.startswith("cli:")]
        total = len(entries)
        readme = self.pages.get("README.md", "")
        found = re.search(r"^(\d+) public functions on (\d+) technical pages; (\d+) entry points", readme, re.M)
        technical = self._technical_pages()
        if found:
            check.verify(int(found.group(3)) == total, "README.md", f"README: {total} entry points",
                         f"README says {found.group(3)} entry points, functional.md has {total}")
            check.verify(int(found.group(1)) == len(self.public), "README.md", f"README: {len(self.public)} public functions",
                         f"README says {found.group(1)} public functions, the code has {len(self.public)}")
            check.verify(int(found.group(2)) == len(technical), "README.md", f"README: {len(technical)} technical pages",
                         f"README says {found.group(2)} technical pages, there are {len(technical)}")
        else:
            check.fail("README.md", "the counts line is missing")
        simple = self.pages.get("simple.md", "")
        shown = len(re.findall(r"^### ", simple.split("## What it does", 1)[-1], re.M))
        check.verify(shown == total, "simple.md", f"simple.md: {total} entry points", f"simple.md has {shown} entry points, functional.md {total}")
        for rel, pattern in (("extras/flows.md", r"^(\d+) more entry point\(s\) are not drawn"),
                             ("extras/tour.md", r"^(\d+) more entry point\(s\) in the")):
            text = self.pages.get(rel)
            if text is None or not total:
                continue
            listed = len(re.findall(r"^## ", text, re.M)) if rel.endswith("flows.md") else len(re.findall(r"^\d+\. ", text, re.M))
            more = re.search(pattern, text, re.M)
            check.verify(listed + (int(more.group(1)) if more else 0) == total, rel, f"{rel}: {total} entry points",
                         f"{rel} accounts for {listed + (int(more.group(1)) if more else 0)} entry points, functional.md has {total}")
        block = self._index_block()
        if block:
            found = re.search(r"^(\d+) public functions, (\d+) entry points", block[2], re.M)
            check.verify(found and [int(found.group(1)), int(found.group(2))] == [len(self.public), total], block[0],
                         f"{block[0]}: {len(self.public)} public functions, {total} entry points",
                         f"{block[0]} counts differ from the pages ({len(self.public)} public, {total} entry points)")
        for number, line in enumerate(self.pages.get("extras/hotspots.md", "").split("\n"), 1):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 5 and cells[1].isdigit() and cells[0].startswith("`"):
                path, commits, size, score = cells[0].strip("`"), int(cells[1]), int(cells[2]), int(cells[3])
                lines = doc_insights._lines(self.model, path)
                check.verify(size == lines and score == commits * size, f"extras/hotspots.md:{number}",
                             f"`{path}`: {lines} lines, score {commits} x {lines}",
                             f"`{path}` shows {size} lines and score {score}; the file has {lines} lines")
        name, source = doc_scope.project_name(self.model)
        title = simple.split("\n", 2)[1] if simple.count("\n") > 1 else ""
        check.verify(title == f"# {name} in plain words", "simple.md:2",
                     f"project name `{name}` as declared in {source or 'the folder name'}",
                     f"title {title!r} differs from the project name `{name}` ({source or 'folder'})")

    def glossary(self):
        check = self.check("glossary", "Glossary words and examples come from the code and the summary uses them")
        text = self.pages.get("extras/glossary.md")
        if text is None:
            return
        terms = {w: (n, examples) for w, n, examples in doc_insights.terms(self.model)}
        names = {f["name"].split(".")[-1] for f in self.model.function_of.values()}
        words = set()
        for number, line in enumerate(text.split("\n"), 1):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) != 3 or not cells[1].isdigit():
                continue
            word = cells[0].strip("`")
            words.add(word)
            examples = _CODE_SPAN.findall(cells[2])
            check.verify(word in terms and terms[word][0] == int(cells[1]) and set(examples) <= names,
                         f"extras/glossary.md:{number}", f"`{word}`: {cells[1]} functions, e.g. {', '.join(examples[:2])}",
                         f"`{word}` shows {cells[1]} function(s) and {examples}, the code has {terms.get(word, (0, []))}")
        used = re.search(r"^Words this project uses: (.+) \(\[glossary\]", self.pages.get("simple.md", ""), re.M)
        for word in used.group(1).split(", ") if used else []:
            check.verify(word in words, "simple.md", f"`{word}` is in the glossary", f"`{word}` is not in the glossary")

    def navigation(self):
        check = self.check("navigation", "Every page is reachable from the scope README and links back to it")
        known = set(self.pages) | {REPORT}
        edges = defaultdict(set)
        for rel, text in self.pages.items():
            for _number, _label, path, _anchor in _links(rel, text):
                edges[rel].add(_resolve(rel, path))
        seen, frontier = {"README.md"}, ["README.md"]
        while frontier:
            current = frontier.pop()
            for target in edges[current] & known - seen:
                seen.add(target)
                frontier.append(target)
        for rel in sorted(known):
            check.verify(rel in seen, rel, f"`{rel}` is reachable from README.md", "no link chain from README.md reaches it")
        for rel, text in self.pages.items():
            if rel == "README.md":
                continue
            head = "\n".join(text.split("\n")[:8])
            back = {_resolve(rel, p) for _n, _l, p, _a in _links(rel, head)}
            check.verify(back & {"README.md", "technical/README.md", "extras/adr/README.md"}, rel,
                         f"`{rel}` links back to an index", "no navigation link back to an index in its first lines")

    def versions(self):
        check = self.check("versions", "Dependency versions match the manifests")
        text = self.pages.get("extras/dependencies.md")
        if text is None:
            return
        current, shown = None, defaultdict(dict)
        for line in text.split("\n"):
            section = re.match(r"^## `([^`]+)`$", line)
            if section:
                current = section.group(1)
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if current and len(cells) == 4 and cells[0].startswith("`"):
                shown[current][(cells[0].strip("`"), cells[2])] = cells[1]
        for path, rows in shown.items():
            declared, problem = doc_extras._manifest(self.root, path)
            if problem:
                check.fail(path, f"manifest {problem}")
                continue
            want = {(name, scope): spec or "-" for _e, name, spec, scope in declared}
            for key, spec in want.items():
                check.verify(rows.get(key) == spec, f"extras/dependencies.md ({path})", f"`{key[0]}` {spec} as in {path}",
                             f"`{key[0]}` shows {rows.get(key)!r}, {path} declares {spec!r}")
            for key in set(rows) - set(want):
                check.fail(f"extras/dependencies.md ({path})", f"`{key[0]}` is not declared in {path}")

    def run(self):
        for step in (self.markers, self.links, self.lines, self.relations, self.coverage, self.entry_points,
                     self.features, self.command_lines, self.prose, self.entry_tests, self.labels, self.counts,
                     self.glossary, self.navigation, self.versions):
            step()
        scored = [c.score() for c in self.checks if c.score() is not None]
        failed = [c for c in self.checks if c.status == "fail"]
        pending = sum(c.pending for c in self.checks)
        verdict = "fail" if failed else "incomplete" if pending else "pass"
        return {"verdict": verdict, "score": round(sum(scored) / len(scored), 1) if scored else 0.0,
                "claims_checked": sum(c.checked for c in self.checks),
                "claims_failed": sum(len(c.failures) for c in self.checks), "prose_pending": pending,
                "checks": self.checks}


_GROUPS = (("Code to technical documentation", ("markers", "links", "lines", "relations", "coverage")),
           ("Functional documentation to the product", ("entry_points", "features", "command_line", "prose", "entry_tests")),
           ("Coherence", ("labels", "counts", "glossary", "navigation", "versions")))


def _report(result):
    by_key = {c.key: c for c in result["checks"]}
    verdict = {"fail": "FAIL", "incomplete": "PASS with prose pending", "pass": "PASS"}[result["verdict"]]
    lines = [doc_scope.GENERATED, "# Documentation validation", "",
             "[Scope](README.md) · [Functional](functional.md) · [Technical](technical/README.md)", "",
             f"**Verdict: {verdict}** · score {result['score']} / 100 · {result['claims_checked']} claims checked, "
             f"{result['claims_failed']} failed · {result['prose_pending']} prose block(s) pending", "",
             "Each check reads the generated pages and verifies their claims against the code on disk, the code graph, "
             "the command lines the code declares and the manifests. The score is the mean, over the checks, of the "
             "share of claims that hold. Not checked claim by claim: the change coupling and the commit counts (they "
             "come from the git history), the Mermaid diagrams, the module and folder trees and the decision records.",
             "", "| Check | Status | Claims | Failed |", "|---|---|---|---|"]
    lines += [f"| {c.title} | {c.status} | {c.checked} | {len(c.failures)} |" for c in result["checks"]]
    for group, keys in _GROUPS:
        lines += ["", f"## {group}"]
        for key in keys:
            check = by_key[key]
            lines += ["", f"### {check.title}", "", f"Status: **{check.status}** · {check.checked} claim(s) checked, "
                                                    f"{len(check.failures)} failed.", ""]
            lines += [f"- {note}" for note in check.notes]
            lines += [f"- Evidence: {item}" for item in check.evidence]
            if check.failures:
                lines += ["", f"Failures ({len(check.failures)}):", ""]
                lines += [f"- {item}" for item in check.failures[:MAX_FINDINGS]]
                if len(check.failures) > MAX_FINDINGS:
                    lines.append(f"- ... and {len(check.failures) - MAX_FINDINGS} more")
    return "\n".join(lines) + "\n"


def run(model, entries, limit=30):
    """Validate the documentation already written under docs/scope and write the report.

    Parameters: model (doc_scope._Model) of the current code; entries (list) its entry-point anchors; limit (int)
    failures listed per check in the answer.
    Returns: dict with 'verdict' (pass | incomplete | fail), 'score', claim counts, 'report' (path) and 'checks'.
    """
    result = _Validation(model, entries).run()
    path = os.path.normpath(os.path.join(model.root, doc_scope.DOCS_DIR, REPORT))
    if os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            if stream.readline().rstrip("\n") != doc_scope.GENERATED:
                raise ValueError(f"{path} exists and was not written by Smart Tool; move it before validating.")
    atomic_io.write_text(path, _report(result))
    checks = [{"check": c.key, "status": c.status, "claims": c.checked, "failed": len(c.failures),
               **({"failures": c.failures[:limit]} if c.failures else {})} for c in result["checks"]]
    return {**{k: v for k, v in result.items() if k != "checks"}, "report": path, "checks": checks}


def validate(root, view_id=None, limit=30):
    """Validate the generated documentation of a project against its current code (the standalone action).

    Parameters: root (str) project folder; view_id (str or None) index view; limit (int) failures listed per check.
    Returns: the dict of run.
    """
    if not os.path.isdir(os.path.join(root, doc_scope.DOCS_DIR)):
        raise ValueError(f"No {doc_scope.DOCS_DIR} in the project yet: generate it with project_manage action=docs generate=true.")
    model = doc_scope._Model(root, view_id)
    if not model.graph.get("symbols"):
        raise ValueError(f"The code graph of {root} is empty: index the project first (project_manage action=index).")
    return run(model, model.entry_points(), limit)
