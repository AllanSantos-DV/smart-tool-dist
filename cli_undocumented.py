"""The command lines the Python code defines with argparse against the README and `docs` text, in both directions: the flags
and subcommands that no doc names, and the flags and subcommands a doc gives to one of the project's scripts that no
definition has. The definitions come from `doc_cli.definitions` (the syntax tree, nothing is run) and the docs are the ones
`undocumented_env` reads. Nothing is written.
"""
import re

import doc_cli
import env_unread
import project_scan
import risky_calls
import undocumented_env

DEFAULT_LIMIT = 30
NOTE = ("Only argparse is seen: a command line built with click or typer, or an argparse parser the syntax tree cannot "
        "follow, is not. Definitions come from the tracked Python files outside test folders and vendored folders; a flag "
        "or subcommand counts as documented when the git-tracked README or `docs` text (the generated docs/scope is left "
        "out) names it: any flag of an argument (`-p` or `--port`) named anywhere in the text, a subcommand only in inline "
        "code or a fenced line, as its words in order. In the other direction only a command line that runs one of those "
        "scripts is read (a token that is the script's path, or its module after `-m`), up to a pipe, `&&` or `;`: the "
        "flags after it must be defined by that script, and the word right after it must be one of its subcommands when "
        "it has any; a flag of another program, a line that names no script of the project and `-h` or `--help` are "
        "left alone. A name abbreviated the way argparse allows is not matched.")

_FLAG = re.compile(r"(?<![\w-])(--[A-Za-z0-9][\w-]*|-[A-Za-z])(?![\w-])")
_SUBCOMMAND = re.compile(r"[A-Za-z][\w-]*")
_STOP = {"|", "||", "&&", ";", ">", ">>", "&", "2>&1"}
_IMPLICIT = {"-h", "--help"}


def _definitions(root):
    """The argparse definitions of the tracked Python files outside test and vendored folders, and how many files hold any."""
    found, files = [], 0
    for rel, text in project_scan.tracked_texts(root, risky_calls.PYTHON):
        if text is None or risky_calls._is_test(rel) or risky_calls._is_vendored(rel):
            continue
        defined = doc_cli.definitions(rel, text)
        files += bool(defined)
        found += defined
    return found, files


def _inventory(definitions):
    """The flag arguments and the subcommands of definitions, as rows with their file and line.

    Returns: tuple (flags, commands); a flag row has "flags", "command" (the words of its subcommand, '' at the top) and
    "path", "line"; a command row has "words", "path", "line". A subcommand named by an expression is left out.
    """
    flags, commands = [], []
    for definition in definitions:
        for command in definition["commands"]:
            words = tuple(command["words"])
            if words and not words[-1].startswith("<"):
                commands.append({"words": words, "path": definition["path"], "line": command["line"]})
            for argument in command["arguments"]:
                names = [flag for flag in argument["flags"] if flag.startswith("-")]
                if names:
                    flags.append({"flags": names, "command": " ".join(words), "path": definition["path"],
                                  "line": argument["line"]})
    return flags, commands


def _command_lines(texts):
    """The (path, first line, text) of every fenced line, with backslash continuations joined, and inline code span of the docs."""
    for rel, text in texts:
        fence, start, joined = None, None, ""
        for number, line in enumerate(text.splitlines(), 1):
            marker = env_unread._FENCE.match(line)
            if marker:
                if joined:
                    yield rel, start, joined.strip()
                fence, start, joined = (None if fence is not None else marker[1].lower() or ""), None, ""
            elif fence is None:
                for span in env_unread._SPAN.findall(line):
                    yield rel, number, span
            else:
                start = start or number
                if line.rstrip().endswith("\\"):
                    joined += " " + line.rstrip()[:-1]
                else:
                    yield rel, start, (joined + " " + line).strip()
                    start, joined = None, ""


def _runs(path, token, previous):
    """Whether a doc token runs the Python file `path`: its path, or its module right after `-m`."""
    token = token.strip("'\"").replace("\\", "/").removeprefix("./")
    if path == token or path.endswith("/" + token):
        return True
    if previous != "-m":
        return False
    module = path[:-3].replace("/", ".")
    package = module.removesuffix(".__main__")
    return token in (module, package) or module.endswith("." + token)


def _segments(tokens, paths):
    """The (script paths, tokens after them) of each run of a project script on one command line."""
    index = 0
    while index < len(tokens) and not tokens[index].startswith("#"):
        runs ={path for path in paths if _runs(path, tokens[index], tokens[index - 1] if index else "")}
        index += 1
        if not runs:
            continue
        end = index
        while end < len(tokens) and tokens[end] not in _STOP and not tokens[end].startswith("#"):
            end += 1
        yield runs, tokens[index:end]
        index = end


def _unknown(runs, tokens, valid_flags, commands):
    """The (kind, name) of the flags and subcommands of one segment that none of the scripts in `runs` defines."""
    words = {c["words"] for c in commands if c["path"] in runs}
    prefix, position, found = (), 0, []
    while position < len(tokens):
        children = {w[len(prefix)] for w in words if w[:len(prefix)] == prefix and len(w) > len(prefix)}
        word = tokens[position].strip("'\"")
        if not children or word.startswith("-") or not _SUBCOMMAND.fullmatch(word) or word.isupper():
            break
        if word not in children:
            found.append(("subcommand", " ".join(prefix + (word,))))
            break
        prefix, position = prefix + (word,), position + 1
    for token in tokens:
        flag = token.strip("'\"").split("=")[0]
        if _FLAG.fullmatch(flag) and flag not in valid_flags and flag not in _IMPLICIT:
            found.append(("flag", flag))
    return found


def _not_defined(texts, definitions, flags, commands):
    """The flags and subcommands the docs give to a project script that no definition of that script has."""
    paths = {d["path"] for d in definitions}
    allowed = {path: {flag for row in flags if row["path"] == path for flag in row["flags"]} for path in paths}
    rows = {}
    for rel, line, text in _command_lines(texts):
        for runs, tokens in _segments(text.split(), paths):
            valid = set().union(*(allowed[path] for path in runs))
            for kind, name in _unknown(runs, tokens, valid, commands):
                rows.setdefault((rel, line, kind, name), min(runs))
    return [{"kind": kind, "name": name, "path": rel, "line": line, "program": program}
            for (rel, line, kind, name), program in sorted(rows.items())]


def _undocumented(texts, flags, commands):
    """The flag arguments and subcommands of the code that no doc names."""
    named_flags = {flag for _rel, text in texts for flag in _FLAG.findall(text)}
    sizes = {len(row["words"]) for row in commands}
    named_words = set()
    for _rel, _line, text in _command_lines(texts):
        tokens = [token.strip("'\"") for token in text.split()]
        named_words.update(tuple(tokens[i:i + size]) for size in sizes for i in range(len(tokens) - size + 1))
    rows = [{"kind": "flag", "name": next((f for f in row["flags"] if f.startswith("--")), row["flags"][0]),
             "command": row["command"], "path": row["path"], "line": row["line"]}
            for row in flags if not named_flags.intersection(row["flags"])]
    rows += [{"kind": "subcommand", "name": " ".join(row["words"]), "command": " ".join(row["words"][:-1]),
              "path": row["path"], "line": row["line"]} for row in commands if row["words"] not in named_words]
    return sorted(rows, key=lambda row: (row["path"], row["line"], row["kind"], row["name"]))


def report(root, limit=DEFAULT_LIMIT):
    """The argparse flags and subcommands no README or `docs` file names, and the ones the docs name that no definition has.

    Parameters: root (str) project folder, a git repository; limit (int) rows returned in each list.
    Returns: dict {"undocumented": [{"kind": "flag" | "subcommand", "name", "command" (the subcommand a flag belongs to, ''
    at the top; the parent words of a subcommand), "path", "line"}] in file and line order, "not_defined": [{"kind",
    "name", "path", "line" (the doc site), "program" (the script the command line runs)}], "counts": {"flags",
    "subcommands", "undocumented", "not_defined", "argparse_files", "docs_checked"}, "note": str}.
    Raises: ValueError when git cannot list the folder.
    """
    definitions, files = _definitions(root)
    flags, commands = _inventory(definitions)
    texts = undocumented_env.doc_texts(root)
    undocumented = _undocumented(texts, flags, commands)
    not_defined = _not_defined(texts, definitions, flags, commands)
    counts = {"flags": len(flags), "subcommands": len(commands), "undocumented": len(undocumented),
              "not_defined": len(not_defined), "argparse_files": files, "docs_checked": len(texts)}
    return {"undocumented": undocumented[:limit], "not_defined": not_defined[:limit], "counts": counts, "note": NOTE}
