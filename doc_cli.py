"""Command-line interfaces the Python code declares with argparse and the functions its `__main__` blocks run, read
from the syntax tree without running anything."""
import ast
import posixpath

_GROUPS = ("add_argument_group", "add_mutually_exclusive_group")


def _constant(node, bound):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return bound.get(node.id)
    return None


def _text(node, bound):
    """Text of a help or description argument: the literal, an f-string with its holes as {expression}, or the source
    of any other expression in backticks."""
    value = _constant(node, bound)
    if isinstance(value, str):
        return value
    if isinstance(node, ast.JoinedStr):
        return "".join(p.value if isinstance(p, ast.Constant) else "{" + ast.unparse(p.value) + "}" for p in node.values)
    return f"`{ast.unparse(node)}`"


class _Scope:
    """argparse objects of one function body (or of the module's top level), followed statement by statement."""

    def __init__(self, path, function, found):
        self.path, self.function, self.found = path, function, found
        self.names, self.bound, self.definition = {}, {}, None

    def _define(self, line, shared=False):
        self.definition = {"path": self.path, "function": self.function, "line": line, "shared": shared, "commands": {}}
        self.found.append(self.definition)
        return self.definition

    def _command(self, definition, words, help_text, line):
        return definition["commands"].setdefault(words, {"words": list(words), "help": help_text, "line": line,
                                                         "arguments": []})

    def call(self, node):
        """The argparse handle an expression yields: (kind, definition, words) or None."""
        if isinstance(node, ast.Name):
            return self.names.get(node.id)
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        method = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
        keywords = {k.arg: k.value for k in node.keywords if k.arg}
        if method == "ArgumentParser":
            definition = self._define(node.lineno)
            described = keywords.get("description")
            self._command(definition, (), _text(described, self.bound) if described is not None else "", node.lineno)
            return ("parser", definition, ())
        if not isinstance(func, ast.Attribute):
            return None
        base = self.call(func.value)
        if base is None and method == "add_argument" and isinstance(func.value, ast.Name):
            definition = self.definition if self.definition and self.definition["shared"] else self._define(node.lineno, True)
            self._command(definition, (), "", node.lineno)
            base = ("parser", definition, ())
            self.names[func.value.id] = base
        if base is None:
            return None
        kind, definition, words = base
        if kind == "parser" and method == "add_subparsers":
            return ("subparsers", definition, words)
        if kind == "subparsers" and method == "add_parser" and node.args:
            name = _constant(node.args[0], self.bound)
            words = words + (name if isinstance(name, str) else f"<{ast.unparse(node.args[0])}>",)
            told = keywords.get("help", keywords.get("description"))
            self._command(definition, words, _text(told, self.bound) if told is not None else "", node.lineno)
            return ("parser", definition, words)
        if kind in ("parser", "group") and method in _GROUPS:
            return ("group", definition, words)
        if kind in ("parser", "group") and method == "add_argument":
            flags = [_constant(a, self.bound) for a in node.args]
            argument = {"flags": [f for f in flags if isinstance(f, str)], "line": node.lineno,
                        "help": _text(keywords["help"], self.bound) if "help" in keywords else ""}
            for key in ("default", "required", "choices", "action", "nargs", "metavar"):
                if key in keywords:
                    argument[key] = ast.unparse(keywords[key])
            self._command(definition, words, "", node.lineno)["arguments"].append(argument)
        return None

    def block(self, statements):
        for statement in statements:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None:
                handle = self.call(statement.value)
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        if handle:
                            self.names[target.id] = handle
                        else:
                            self.names.pop(target.id, None)
            elif isinstance(statement, (ast.Expr, ast.Return)) and statement.value is not None:
                self.call(statement.value)
            elif isinstance(statement, ast.For):
                self._loop(statement)
            else:
                for field in ("body", "orelse", "finalbody"):
                    self.block(getattr(statement, field, None) or [])
                for handler in getattr(statement, "handlers", None) or []:
                    self.block(handler.body)

    def _loop(self, statement):
        """A loop over a literal list of names runs its body once per item with the loop variables bound."""
        items = statement.iter.elts if isinstance(statement.iter, (ast.Tuple, ast.List)) else None
        target = statement.target
        names = [target.id] if isinstance(target, ast.Name) else \
            [t.id if isinstance(t, ast.Name) else None for t in target.elts] if isinstance(target, ast.Tuple) else None
        if not items or names is None:
            self.block(statement.body)
            return
        for item in items:
            values = [item] if len(names) == 1 else item.elts if isinstance(item, (ast.Tuple, ast.List)) else None
            if values is None or len(values) != len(names):
                self.block(statement.body)
                return
            for name, value in zip(names, values):
                if name:
                    self.bound[name] = value.value if isinstance(value, ast.Constant) else None
            self.block(statement.body)
        for name in names:
            self.bound.pop(name, None)


def definitions(path, source):
    """argparse command lines declared in one Python file.

    Parameters: path (str) project-relative path; source (str) the file's text.
    Returns: list of {'path', 'function' (None at module level), 'line', 'shared' (options added to a parser the function
    receives), 'commands': [{'words', 'help', 'line', 'arguments': [{'flags', 'help', 'line', 'default', ...}]}]}.
    """
    if "argparse" not in source and "add_argument" not in source:
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    found = []
    _Scope(path, None, found).block(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    child._owner = node.name
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = getattr(node, "_owner", None)
            _Scope(path, f"{owner}.{node.name}" if owner else node.name, found).block(node.body)
    for definition in found:
        definition["commands"] = sorted(definition["commands"].values(), key=lambda c: (c["line"], c["words"]))
    return [d for d in found if any(c["arguments"] or c["words"] for c in d["commands"])]


def _is_main_guard(test):
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def _module_file(path, module, level, files):
    """Project path of the module an import names, or None when it is not a project file."""
    base = posixpath.dirname(path).split("/") if path.count("/") else []
    if level:
        base = base[:len(base) - (level - 1)] if level > 1 else base
        parts = base + (module.split(".") if module else [])
    else:
        parts = module.split(".") if module else []
    stem = "/".join(p for p in parts if p)
    for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
        if candidate in files:
            return candidate
    return None


def launched(path, source, files):
    """Functions a `__main__` block of a Python file runs: (project path, function name) pairs.

    Parameters: path (str) project-relative path; source (str) the file's text; files (set) project-relative paths.
    """
    if "__main__" not in source:
        return []
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _module_file(path, node.module, node.level, files)
            if target:
                imported.update({(a.asname or a.name): (target, a.name) for a in node.names})
    local = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found = []
    for node in tree.body:
        if isinstance(node, ast.If) and _is_main_guard(node.test):
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name):
                    name = inner.func.id
                    pair = (path, name) if name in local else imported.get(name)
                    if pair and pair not in found:
                        found.append(pair)
    return found


def _argument(argument):
    details = []
    for key, label in (("default", "default"), ("choices", "one of"), ("nargs", "nargs"), ("metavar", "shown as")):
        if key in argument:
            details.append(f"{label} `{argument[key]}`")
    if argument.get("required") == "True":
        details.append("required")
    if argument.get("action") in ("'store_true'", "'store_false'", "'count'", "'version'"):
        details.append(argument["action"].strip("'").replace("store_", "sets "))
    help_text = " ".join(argument["help"].split())
    return (f"  - {', '.join(f'`{f}`' for f in argument['flags']) or '(no name)'}"
            + (f" ({'; '.join(details)})" if details else "") + (f": {help_text}" if help_text else ""))


def command_title(command):
    """How the documentation names a command: its words, or 'without a command' for the parser itself."""
    return f"`{' '.join(command['words'])}`" if command["words"] else "Without a command"


def lines(definition):
    """Markdown list of a command line's commands, each with its arguments and options and their defaults."""
    out = []
    for command in definition["commands"]:
        help_text = " ".join(command["help"].split())
        out.append(f"- {command_title(command)} (line {command['line']})" + (f": {help_text}" if help_text else ""))
        out += [_argument(a) for a in command["arguments"]]
    return out
