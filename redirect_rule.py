"""Deterministic redirect rule of the PreToolUse hook for native code searches (Grep, Glob, Bash).

A content search over a directory of a project (grep -r, rg, git grep, the Grep tool on a folder) or over more than
LISTED_MAX files goes to smart_search; reading listed files, listings, Glob and commands that change files run. Only
the part of a Bash command that searches is measured, with its own paths: `cd` is carried, `$VAR` set in the command or
in the environment, `~` and globs are expanded, and a path still holding an unresolved variable is left alone.

Measured on 2026-10-09 against 2,789 real decisions of the LLM router it replaced (session cwd recovered from the
transcripts): the model redirected 315, this rule 463 (134 shared); in a sample of 40 redirected by the rule alone, 40
were content searches over a folder or many files; in a sample of 40 redirected by the model alone, ~36 were wrong
(one file, `node -e` over one file, `git show`, listings, commands changing files). Median 0.2 ms against 1.5 s.
"""
import glob
import os
import re
import shlex
import time

import index_scope
import project_store

LISTED_MAX = 20
SCAN_MAX_FILES = 4000
SCAN_MAX_SECONDS = 0.25
# Trees the native tools walk into: their presence is part of the cost, they are counted.
HEAVY_DIRS = frozenset({"node_modules", ".venv", "venv", "vendor", "target", "dist", "build", "out", ".next", ".nuxt",
                        ".gradle", "Pods"})
SEARCHERS = frozenset({"grep", "egrep", "fgrep", "rg", "ripgrep", "ack", "ag", "findstr", "select-string", "sls"})
ALWAYS_RECURSIVE = frozenset({"rg", "ripgrep", "ack", "ag"})
LISTERS = frozenset({"find", "fd", "ls", "dir", "tree", "get-childitem", "gci"})
CD_VERBS = frozenset({"cd", "pushd", "set-location", "sl"})
PATTERN_FLAGS = frozenset({"-e", "--regexp", "-f", "--file"})
VALUE_FLAGS = frozenset({"-A", "-B", "-C", "-m", "--max-count", "--include", "--exclude", "--exclude-dir", "-g",
                         "--glob", "-t", "--type", "-T", "--type-not", "--max-depth", "-d", "--context",
                         "--after-context", "--before-context"})
RECURSIVE_FLAGS = frozenset({"-r", "-R", "--recursive", "-Recurse", "/s", "/S"})
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_VARIABLE = re.compile(r"\$\{(\w+)\}|\$(\w+)")


def resolve_path(raw, base):
    """Path normalized for the disk: relative to base, and the MSYS `/c/...` form agents write in a POSIX shell on
    Windows turned into `C:/...`."""
    if not raw:
        return None
    path = raw.strip().replace("\\", "/")
    msys = re.fullmatch(r"/([A-Za-z])(/.*)?", path)
    if msys:
        path = f"{msys.group(1).upper()}:{msys.group(2) or '/'}"
    if not os.path.isabs(path) and base:
        path = os.path.join(base, path)
    return os.path.normpath(path)


def count_files(path):
    """(files, truncated) under a directory, skipping dot-directories, bounded by count and time."""
    files, deadline = 0, time.monotonic() + SCAN_MAX_SECONDS
    for _dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        files += len(filenames)
        if files > SCAN_MAX_FILES or time.monotonic() > deadline:
            return files, True
    return files, False


def _in_project(target, cwd):
    """A registered project holds target, or target is inside the session's directory."""
    key = os.path.normcase(os.path.abspath(target)) + os.sep
    if any(key.startswith(os.path.normcase(os.path.abspath(p["root"])).rstrip(os.sep) + os.sep)
           for p in project_store.all_projects()):
        return True
    return bool(cwd) and index_scope._within_root(cwd, target)


def _has_files(target):
    """Any file under the folder: the size limit measured made no difference (0 files: 463 redirects, 50: 400), and a
    missed redirect costs more context than a needless one."""
    files, truncated = count_files(target)
    return truncated or files > 0


def segments(command):
    """(text, after_single_pipe) of each part of a shell command split on ; && || | and newlines outside quotes."""
    parts, current, quote, i, after_pipe = [], [], None, 0, False
    while i < len(command):
        ch = command[i]
        if quote:
            current.append(ch)
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif command.startswith(("&&", "||"), i):
            parts.append(("".join(current), after_pipe))
            current, after_pipe = [], False
            i += 2
            continue
        elif ch in ";\n|":
            parts.append(("".join(current), after_pipe))
            current, after_pipe = [], ch == "|"
        else:
            current.append(ch)
        i += 1
    parts.append(("".join(current), after_pipe))
    return [(text.strip(), pipe) for text, pipe in parts if text.strip()]


def _expand(token, variables):
    def substitute(match):
        name = match.group(1) or match.group(2)
        return variables.get(name, os.environ.get(name, match.group(0)))
    return os.path.expanduser(_VARIABLE.sub(substitute, token))


def _paths(raw, base):
    """Existing paths of a token (globs expanded), or None when it still holds an unresolved variable."""
    if "$" in raw or "`" in raw:
        return None
    path = resolve_path(raw, base)
    if any(ch in raw for ch in "*?["):
        return [os.path.normpath(p) for p in glob.glob(path)]
    return [path] if path and os.path.exists(path) else []


def _search_args(verb, tokens):
    """Path arguments of a grep-like command (the pattern and flag values dropped) and whether it is recursive."""
    recursive = verb in ALWAYS_RECURSIVE or any(
        t in RECURSIVE_FLAGS or (re.fullmatch(r"-[a-zA-Z]+", t) and ("r" in t or "R" in t)) for t in tokens)
    args, skip, pattern_given = [], False, False
    for token in tokens:
        if skip:
            skip = False
        elif token in PATTERN_FLAGS:
            skip, pattern_given = True, True
        elif token in VALUE_FLAGS:
            skip = True
        elif not token.startswith("-") and not (verb == "findstr" and token.startswith("/")):
            args.append(token)
    return (args if pattern_given else args[1:]), recursive


def _bash(command, cwd):
    base, variables, verdict = cwd, {}, ("allow", "No content search over a folder.")
    for text, after_pipe in segments(command):
        try:
            tokens = shlex.split(text, posix=True)
        except ValueError:
            tokens = text.split()
        while tokens and _ASSIGN.match(tokens[0]):
            name, value = _ASSIGN.match(tokens[0]).groups()
            variables[name] = _expand(value, variables)
            tokens = tokens[1:]
        if not tokens:
            continue
        verb = tokens[0].lower()
        if verb in CD_VERBS and len(tokens) > 1:
            base = resolve_path(_expand(tokens[1], variables), base) or base
            continue
        xargs = verb == "xargs" and len(tokens) > 1 and tokens[1].lower() in SEARCHERS
        if after_pipe and not xargs:
            continue
        if xargs:
            if base and os.path.isdir(base) and _in_project(base, cwd) and _has_files(base):
                return "redirect", base, "content search over the files of a listing"
            continue
        if verb == "git":
            rest, repo = tokens[1:], base
            while len(rest) > 1 and rest[0] == "-C":
                repo = resolve_path(_expand(rest[1], variables), repo) or repo
                rest = rest[2:]
            if rest and rest[0] == "grep":
                specs = rest[rest.index("--") + 1:] if "--" in rest else []
                found = [_paths(_expand(raw, variables), repo) for raw in specs]
                only_files = specs and all(f and all(os.path.isfile(p) for p in f) for f in found)
                listed = sum(len(f) for f in found if f) if only_files else 0
                if (not only_files or listed > LISTED_MAX) and repo and os.path.isdir(repo) and \
                        _in_project(repo, cwd) and _has_files(repo):
                    return "redirect", repo, "git grep over the repository"
            continue
        if verb in LISTERS:
            recursive = verb in ("find", "fd", "tree") or "-recurse" in (t.lower() for t in tokens) or any(
                re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", t) for t in tokens[1:])
            first = next((t for t in tokens[1:] if not t.startswith("-")), ".")
            for target in (_paths(_expand(first, variables), base) or []) if recursive else []:
                if os.path.isdir(target) and _in_project(target, cwd) and count_files(target)[1]:
                    return "redirect", target, "recursive listing of a very large tree"
            continue
        if verb not in SEARCHERS:
            continue
        args, recursive = _search_args(verb, tokens[1:])
        if not args:
            if recursive and base and os.path.isdir(base) and _in_project(base, cwd) and _has_files(base):
                return "redirect", base, "recursive content search of the working folder"
            continue
        listed = 0
        for raw in args:
            found = _paths(_expand(raw, variables), base)
            if found is None:
                verdict = ("allow", "The searched path uses a variable the hook cannot resolve.")
                continue
            for target in found:
                if os.path.isdir(target):
                    if recursive and _in_project(target, cwd) and _has_files(target):
                        return "redirect", target, "recursive content search over a folder"
                else:
                    listed += 1
        if listed > LISTED_MAX:
            return "redirect", base, f"content search over {listed} files"
        if listed:
            verdict = ("allow", "Search inside listed files.")
    return verdict[0], None, verdict[1]


def decide(tool_name, tool_input, cwd):
    """(decision, target, reason) for a Grep, Glob or Bash call: decision is redirect or allow, target the folder that
    would be searched (None when allowed)."""
    if tool_name == "Glob":
        return "allow", None, "Glob lists file names; smart_search does not replace a listing."
    if tool_name == "Grep":
        target = resolve_path(tool_input.get("path") or cwd, cwd)
        if not target or not os.path.exists(target):
            return "allow", None, "The path does not exist: the native tool's error is the useful answer."
        if os.path.isfile(target):
            return "allow", None, "Search inside one file."
        if not _in_project(target, cwd):
            return "allow", None, "The folder is outside every project: no index covers it."
        if not _has_files(target):
            return "allow", None, "The folder has no files."
        return "redirect", target, "content search over a folder"
    if tool_name == "Bash":
        return _bash(tool_input.get("command") or "", cwd)
    return "allow", None, "Tool not redirected."
