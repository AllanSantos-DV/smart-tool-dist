"""High-confidence unsafe calls in the tracked code, with file and line. Only shapes that are almost never safe are listed,
because a false alarm costs more than a silence: `shell=True` or `os.system` on a command built at run time, `pickle.load`
and `pickle.loads`, `yaml.load` with no loader or an unsafe one, an HTTP call with `verify=False`, `eval` and `exec` on a
non-literal (Python, read from its syntax tree), and an `innerHTML` assignment or an `eval` on a non-literal in JavaScript
and TypeScript (read from the text). The source text of a finding is never returned. Nothing is written. `added` narrows
the findings of one file to the ones an edit or a diff adds.
"""
import ast
import posixpath
import re
import warnings
from collections import Counter

import project_scan

DEFAULT_LIMIT = 30
PYTHON = (".py",)
SCRIPT = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")
NOTE = ("Listed: subprocess calls and os.system with shell=True or a command that is not a literal string, pickle.load and "
        "pickle.loads, yaml.load with no loader or the Loader, FullLoader or UnsafeLoader, an HTTP call (requests, httpx, "
        "a session method) with verify=False, eval and exec on a non-literal, and in JavaScript and TypeScript an innerHTML "
        "assignment or an eval on a non-literal. Python is read from its syntax tree, so strings and comments never match "
        "and a file that does not parse is named in `unparsed`; JavaScript is read from the text, line comments left out. "
        "A name the project defines or imports from elsewhere is not the builtin. Files under tests folders and test-named "
        "files are skipped unless they are the only code, and so are node_modules, vendor, third_party and .min. files. A "
        "call that is unsafe through a variable or a wrapper is not seen.")

_TEST_FOLDERS = {"tests", "test", "__tests__"}
_VENDOR_FOLDERS = {"node_modules", "vendor", "third_party"}
_TEST_NAME = re.compile(r"^(?:test_.*\.py|.*_test\.py|conftest\.py|.*\.(?:test|spec)\.[cm]?[jt]sx?)$")
_SHELL_CALLS = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output", "subprocess.Popen"}
_PICKLE = {"pickle.load", "pickle.loads", "cPickle.load", "cPickle.loads", "_pickle.load", "_pickle.loads"}
_YAML = {"yaml.load", "yaml.load_all"}
_UNSAFE_LOADERS = {"yaml.Loader", "yaml.UnsafeLoader", "yaml.FullLoader", "yaml.CLoader", "yaml.CUnsafeLoader",
                   "yaml.CFullLoader"}
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "send", "stream"}
_HTTP_CLIENTS = {"httpx.Client", "httpx.AsyncClient"}
_INNER_HTML = re.compile(r"\.innerHTML\s*\+?=(?![=>])\s*")
_EVAL = re.compile(r"(?<![\w.$])eval\s*\(\s*")


def _is_test(rel):
    return bool(set(rel.split("/")[:-1]) & _TEST_FOLDERS) or bool(_TEST_NAME.match(posixpath.basename(rel)))


def _is_vendored(rel):
    return bool(set(rel.split("/")[:-1]) & _VENDOR_FOLDERS) or ".min." in posixpath.basename(rel)


def _literal(node):
    """Whether a Python expression is a constant string with nothing built at run time."""
    return isinstance(node, ast.Constant) or (isinstance(node, ast.JoinedStr)
                                              and not any(isinstance(part, ast.FormattedValue) for part in node.values))


class _Names:
    """What each name of a Python module stands for, from its imports, and the names the module defines itself."""

    def __init__(self, tree):
        self.origin, self.defined = {}, set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.origin[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for alias in node.names:
                    self.origin[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defined.add(node.name)

    def dotted(self, node):
        """The dotted name an expression of names and attributes stands for, or None when it is not one or is the project's."""
        if isinstance(node, ast.Name):
            if node.id in self.origin:
                return self.origin[node.id]
            return None if node.id in self.defined else node.id
        if isinstance(node, ast.Attribute):
            base = self.dotted(node.value)
            return f"{base}.{node.attr}" if base else None
        return None


def _keyword(call, name):
    return next((item.value for item in call.keywords if item.arg == name), None)


def _first_argument(call):
    return call.args[0] if call.args else _keyword(call, "args")


def _yaml_unsafe(call, names):
    """Whether a `yaml.load` call has no loader or an unsafe one; a loader it cannot resolve is not judged."""
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(item.arg is None for item in call.keywords):
        return False
    loader = call.args[1] if len(call.args) > 1 else _keyword(call, "Loader")
    return loader is None or names.dotted(loader) in _UNSAFE_LOADERS


def _call_kind(call, names):
    """The kind of unsafe call a Python call node is, or None."""
    name = names.dotted(call.func)
    final = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
    shell, verify = _keyword(call, "shell"), _keyword(call, "verify")
    command = _first_argument(call)
    if name in _SHELL_CALLS and isinstance(shell, ast.Constant) and shell.value is True and command is not None \
            and not _literal(command):
        return "shell=True"
    if name == "os.system" and command is not None and not _literal(command):
        return "os.system"
    if name in _PICKLE:
        return f"pickle.{name.rpartition('.')[2]}"
    if name in _YAML and _yaml_unsafe(call, names):
        return "yaml.load"
    if isinstance(verify, ast.Constant) and verify.value is False and (final in _HTTP_METHODS or name in _HTTP_CLIENTS):
        return "verify=False"
    if name in ("eval", "exec") and call.args and not _literal(call.args[0]):
        return name
    return None


def _python(text):
    """The (line, kind) of the unsafe calls of a Python file, in line order.

    Raises: SyntaxError, ValueError or RecursionError when the file does not parse.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(text)
    names = _Names(tree)
    found = [(node.lineno, _call_kind(node, names)) for node in ast.walk(tree) if isinstance(node, ast.Call)]
    return sorted((line, kind) for line, kind in found if kind)


def _plain_literal(text, start, closers):
    """Whether the expression at `start` is one string literal (a template without `${`) that ends where a `closers`
    character, a line comment or the end of the text comes next."""
    quote = text[start:start + 1]
    if quote not in ("'", '"', "`"):
        return False
    at = start + 1
    while at < len(text) and text[at] != quote:
        if text[at] == "\\":
            at += 1
        elif quote == "`" and text.startswith("${", at) or quote != "`" and text[at] == "\n":
            return False
        at += 1
    at += 1
    while text[at:at + 1] in (" ", "\t"):
        at += 1
    return at >= len(text) or text[at] in closers or text.startswith("//", at)


def _commented(text, offset):
    line_start = text.rfind("\n", 0, offset) + 1
    before = text[line_start:offset]
    return before.lstrip().startswith(("//", "/*", "*")) or re.search(r"(?<!:)//", before) is not None


def _script(text):
    """The (line, kind) of the unsafe calls of a JavaScript or TypeScript file, in line order."""
    found = []
    for match in _INNER_HTML.finditer(text):
        if not _plain_literal(text, match.end(), ";\n}),") and not _commented(text, match.start()):
            found.append((match.start(), "innerHTML"))
    for match in _EVAL.finditer(text):
        if text[match.end():match.end() + 1] != ")" and not _plain_literal(text, match.end(), ")") \
                and not _commented(text, match.start()):
            found.append((match.start(), "eval"))
    return sorted((text.count("\n", 0, offset) + 1, kind) for offset, kind in found)


def _scan_file(rel, text):
    return _python(text) if rel.lower().endswith(PYTHON) else _script(text)


def _keyed(rel, text):
    """The findings of a file as ((kind, stripped text of its line), line): the text tells a finding that only moved from one
    the edit wrote, and it never leaves this module."""
    lines = text.split("\n")
    return [((kind, "".join(lines[line - 1:line]).strip()), line) for line, kind in _scan_file(rel, text)]


def added(rel, before, after):
    """The unsafe calls of a file that its new text has and the old one did not.

    A finding of `after` is old when `before` has one of the same kind on a line with the same text, once per such finding, so
    a call that moved or whose file gained lines elsewhere is not reported, and a copy of an old call is.

    Parameters: rel (str) file path, whose suffix picks the language; before (str or None) text before the edit, None for a
    new file; after (str) text after it.
    Returns: list of {"line" (1-based, in `after`), "kind"} in line order; empty for a file that is not Python, JavaScript or
    TypeScript.
    Raises: ValueError naming the file when either text does not parse.
    """
    if not rel.lower().endswith(PYTHON + SCRIPT):
        return []
    try:
        old = Counter(key for key, _line in _keyed(rel, before)) if before else Counter()
        new = _keyed(rel, after)
    except (SyntaxError, ValueError, RecursionError) as error:
        raise ValueError(f"{rel} could not be read for unsafe calls ({type(error).__name__})") from error
    found = []
    for key, line in new:
        if old[key]:
            old[key] -= 1
        else:
            found.append({"line": line, "kind": key[0]})
    return found


def scan(root, limit=DEFAULT_LIMIT):
    """The high-confidence unsafe calls of the tracked Python, JavaScript and TypeScript files.

    Parameters: root (str) project folder, a git repository; limit (int) findings returned.
    Returns: dict {"findings": [{"path", "line", "kind"}] in file and line order, "total": int, "by_kind": {kind: count},
    "files_checked": int, "files_skipped": int (not readable text), "tests_skipped": int (test files left out because
    other code exists), "unparsed": [{"path", "why"}] (Python files that did not parse), "note": str}.
    Raises: ValueError when git cannot list the folder.
    """
    code, tests, skipped = [], [], 0
    for rel, text in project_scan.tracked_texts(root, PYTHON + SCRIPT):
        if _is_vendored(rel):
            continue
        if text is None:
            skipped += 1
        else:
            (tests if _is_test(rel) else code).append((rel, text))
    chosen, left_out = (code, len(tests)) if code else (tests, 0)
    findings, unparsed = [], []
    for rel, text in chosen:
        try:
            findings += [{"path": rel, "line": line, "kind": kind} for line, kind in _scan_file(rel, text)]
        except (SyntaxError, ValueError, RecursionError) as error:
            where = f" at line {error.lineno}" if getattr(error, "lineno", None) else ""
            unparsed.append({"path": rel, "why": type(error).__name__ + where})
    by_kind = {}
    for item in findings:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
    return {"findings": findings[:limit], "total": len(findings), "by_kind": by_kind, "files_checked": len(chosen) - len(unparsed),
            "files_skipped": skipped, "tests_skipped": left_out, "unparsed": unparsed, "note": NOTE}
