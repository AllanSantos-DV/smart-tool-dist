"""Imports between Go files, read from the package clause and the import declarations of the indexed snapshot.

A Go package is a directory, so a file that imports a package depends on every non-test file of that directory, and an
internal test file (`package x` in `x_test.go`) depends on every non-test file of its own directory. Import paths are
resolved against the nearest `go.mod` module path; with no `go.mod` in the snapshot an import resolves to the longest
directory whose path ends the import path. No symbols or calls are produced: the doc and duplicate hooks stay silent
for Go until their thresholds are measured on it, and `affected_tests` selects by package import."""
import posixpath
import re

_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_PACKAGE = re.compile(r"^package\s+(\w+)", re.M)
_IMPORT_BLOCK = re.compile(r"^import\s*\((.*?)^\)", re.M | re.S)
_IMPORT_ONE = re.compile(r'^import\s+(?:[\w.]+\s+)?"([^"]+)"', re.M)
_IMPORT_SPEC = re.compile(r'^\s*(?:[\w.]+\s+)?"([^"]+)"', re.M)
_MODULE = re.compile(r"^module\s+(\S+)", re.M)


def _module_roots(sources):
    """Folder of each go.mod in the snapshot -> its module path."""
    roots = {}
    for path, text in sources.items():
        if posixpath.basename(path) == "go.mod":
            match = _MODULE.search(_COMMENT.sub("", text))
            if match:
                roots[posixpath.dirname(path)] = match.group(1).strip('"')
    return roots


def _enclosing_module(folder, roots):
    """(module folder, module path) of the nearest go.mod above `folder`, or (None, None)."""
    while True:
        if folder in roots:
            return folder, roots[folder]
        if not folder:
            return None, None
        folder = posixpath.dirname(folder)


def _imports(text):
    """Import paths of one Go file (comments already blanked)."""
    found = [match.group(1) for match in _IMPORT_ONE.finditer(text)]
    for block in _IMPORT_BLOCK.finditer(text):
        found.extend(spec.group(1) for spec in _IMPORT_SPEC.finditer(block.group(1)))
    return found


def analyze(sources):
    go_files = {path: text for path, text in sources.items() if path.endswith(".go")}
    parsed = [path for path in sources if path.endswith(".go") or posixpath.basename(path) == "go.mod"]
    if not go_files:
        return {"parsed": parsed}
    roots = _module_roots(sources)
    packages = {}
    for path in go_files:
        if not path.endswith("_test.go"):
            packages.setdefault(posixpath.dirname(path), []).append(path)

    def package_dir(path, spec):
        folder, module = _enclosing_module(posixpath.dirname(path), roots)
        if module is not None:
            if spec != module and not spec.startswith(module + "/"):
                return None
            return folder if spec == module else posixpath.normpath(posixpath.join(folder, spec[len(module) + 1:]))
        candidates = [d for d in packages if d and (spec == d or spec.endswith("/" + d))]
        return max(candidates, key=len, default=None)

    dependencies = []
    for path, text in sorted(go_files.items()):
        bare = _COMMENT.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)
        for spec in _imports(bare):
            folder = package_dir(path, spec)
            line = bare[:bare.find(f'"{spec}"')].count("\n") + 1
            targets = [t for t in packages.get(folder, ()) if t != path] if folder is not None else []
            if not targets:
                dependencies.append({"source": path, "target": None, "specifier": spec, "line": line, "kind": "import",
                                     "resolution": "external"})
            dependencies.extend({"source": path, "target": target, "specifier": spec, "line": line, "kind": "import",
                                 "resolution": "resolved"} for target in targets)
        clause = _PACKAGE.search(bare)
        if path.endswith("_test.go") and clause and not clause.group(1).endswith("_test"):
            dependencies.extend({"source": path, "target": target, "specifier": posixpath.dirname(path) or ".",
                                 "line": 1, "kind": "package", "resolution": "resolved"}
                                for target in packages.get(posixpath.dirname(path), ()))
    return {"dependencies": dependencies, "parsed": parsed}
