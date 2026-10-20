"""Deterministic redirect rule of the PreToolUse hook for native code searches (Grep, Glob, Bash).

A content search over a directory of a project (grep -r, rg, git grep, the Grep tool on a folder) or over more than
LISTED_MAX files goes to smart_search; reading listed files, listings, Glob and commands that change files run. Only
the part of a Bash command that searches is measured, with its own paths: `cd` is carried, `$VAR` set in the command or
in the environment, `~` and globs are expanded, and a path still holding an unresolved variable is left alone. Shell
shapes that run the same search are decided as the search itself: wrappers and subshells, `bash -c`/`powershell
-Command`/`cmd /c` scripts, `$(...)` substitutions, loop variables, listings fed to a reader (`find -exec`, `xargs`,
`while read`, PowerShell pipelines), `cat` over many files and inline python/node scripts that walk a folder and match
text. Replayed on 2026-10-12 over 3 days of transcripts (14,580 Grep and shell calls): 37 more redirects, all folder
searches, and none of the previous ones lost.

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
READERS = frozenset({"cat", "head", "tail", "type", "get-content", "gc", "less", "more", "bat", "nl", "sed", "awk"})
READER_VALUE_FLAGS = {"head": frozenset({"-n", "-c", "--lines", "--bytes"}),
                      "tail": frozenset({"-n", "-c", "--lines", "--bytes", "-s", "--pid", "--sleep-interval"}),
                      "sed": frozenset({"-l", "--line-length"}), "awk": frozenset({"-v", "-F", "--assign"}),
                      "get-content": frozenset({"-totalcount", "-tail", "-head", "-first", "-last", "-encoding",
                                                "-readcount", "-delimiter", "-stream"})}
READER_VALUE_FLAGS["gc"] = READER_VALUE_FLAGS["get-content"]
READER_SCRIPT_FLAGS = {"sed": frozenset({"-e", "-f", "--expression", "--file"}), "awk": frozenset({"-f", "--file"})}
# PowerShell cmdlets that read the content of the files piped into them (a listing piped to grep only filters names).
PIPED_CONTENT_READERS = frozenset({"select-string", "sls", "get-content", "gc"})
CAPPING_VERBS = frozenset({"head", "tail", "select-object", "select"})
LISTER_VALUE_FLAGS = frozenset({"-filter", "-include", "-exclude", "-depth", "-attributes", "-e", "--extension", "-t",
                                "--type", "-d", "--max-depth", "--exclude"})
PREFIX_WORDS = frozenset({"do", "then", "else", "elif", "if", "while", "until", "!", "time", "exec", "nohup", "sudo",
                          "command", "builtin", "{", "("})
GIT_FLAGS = frozenset({"--no-pager", "-P", "--paginate", "--no-replace-objects", "--literal-pathspecs", "--bare"})
MAX_NESTING = 3
LISTING = "__smart_tool_listing__"
SUBSTITUTION = "$__smart_tool_substitution__"
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.DOTALL)
_INLINE_INTERPRETER = re.compile(r"(?:^|[\s;&|(`/\\])(?:python[\d.]*|py|node|deno|bun)(?:\.exe)?\b[^\n]*?"
                                 r"(?:\s-[A-Za-z]*[ce]\s|\s--eval\b|\s-p\s|\s-(?:\s|$)|<<)", re.IGNORECASE)
_WALK = re.compile(r"os\.walk\(|\.rglob\(|glob\.i?glob\([^)]*recursive\s*=\s*True|['\"][^'\"\n]*\*\*[/\\]|"
                   r"readdirSync\([^)]*recursive|readdir\([^)]*recursive|\bwalk(?:Sync)?\(|globSync\(|"
                   r"Get-ChildItem[^|\n]*-Recurse", re.IGNORECASE)
_CONTENT_READ = re.compile(r"\bopen\(|read_text\(|read_bytes\(|readFileSync\(|readFile\(|\.read\(\)|Get-Content",
                           re.IGNORECASE)
_TEXT_MATCH = re.compile(r"\bre\.(?:search|findall|finditer|match|fullmatch|compile)\(|\.includes\(|\.indexOf\(|"
                         r"\.find\(|\.match(?:All)?\(|\.test\(|\.search\(|['\"][^'\"\n]+['\"]\s+in\s+\w+|Select-String",
                         re.IGNORECASE)
_SCRIPT_WRITES = re.compile(r"\.write\(|write_text\(|writeFileSync|writeFile\(|appendFileSync|os\.remove|os\.rename|"
                            r"shutil\.|unlink\(|Set-Content|Out-File", re.IGNORECASE)
_CWD_WALK = re.compile(r"os\.getcwd\(\)|process\.cwd\(\)|Path\(\)|os\.walk\(\s*\)")
# Scanned apart: a script quoted one way holds its string literals quoted the other way.
_SINGLE_QUOTED = re.compile(r"'([^'\n]+)'")
_DOUBLE_QUOTED = re.compile(r'"([^"\n]+)"')
_PS_READ = re.compile(r"\b(?:get-content|gc|select-string|sls|cat|type)\b|\[io\.file\]", re.IGNORECASE)
_MATCHED_LITERAL = re.compile(r"""(?:\bre\.(?:search|findall|finditer|match|fullmatch|compile)\(\s*r?|"""
                              r"""\.(?:includes|indexOf|match|matchAll|search|test)\(\s*)(['"])(.+?)\1|"""
                              r"""(['"])([^'"\n]+)\3\s+in\s+\w+""")
QUERY_MAX_CHARS = 400
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


def _folder(target, cwd):
    """Whether target is a folder of a project holding files: the reach a native search should not take."""
    return bool(target) and os.path.isdir(target) and _in_project(target, cwd) and _has_files(target)


def _split(text, powershell):
    """Words of one simple command. PowerShell keeps backslashes (Windows paths), so its quotes are only stripped."""
    try:
        if powershell:
            return [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "'\"" else t
                    for t in shlex.split(text, posix=False)]
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def _unwrap(tokens, variables):
    """The command a prefix runs: subshell and group brackets, loop keywords, wrappers (sudo, time, timeout, nice,
    env, nohup...) and leading `NAME=value` assignments, which are stored in variables."""
    tokens = list(tokens)
    while tokens:
        head, low = tokens[0], tokens[0].lower()
        if len(head) > 1 and head[0] in "({" and head != "{}":
            tokens[0] = head.lstrip("({")
        elif low in PREFIX_WORDS:
            tokens = tokens[1:]
        elif _ASSIGN.match(head):
            name, value = _ASSIGN.match(head).groups()
            variables[name] = _expand(value, variables)
            tokens = tokens[1:]
        elif low in ("env", "timeout", "nice", "ionice", "stdbuf"):
            tokens = tokens[1:]
            while tokens and tokens[0].startswith("-"):
                flag = tokens.pop(0)
                if flag in ("-s", "-k", "-n", "-c", "-u", "--signal", "--kill-after", "--unset") and tokens:
                    tokens.pop(0)
            if low == "timeout" and tokens:
                tokens.pop(0)
        else:
            break
    while tokens and tokens[-1] in (")", "}", "done", "fi", "esac"):
        tokens = tokens[:-1]
    if tokens and tokens[-1].endswith(")") and tokens[-1].count(")") > tokens[-1].count("("):
        tokens[-1] = tokens[-1].rstrip(")") or "."
    return tokens


def _strip_heredocs(command):
    """The command without heredoc bodies: their lines are data for the command that reads them, not commands."""
    return _HEREDOC.sub(lambda m: m.group(0)[:m.group(0).index("\n") + 1], command)


def _substitutions(command, powershell):
    """(command with each `$(...)` and backtick substitution replaced by one word, inner commands). The word is LISTING
    when the inner command lists the files of a tree (its output names files to read), else an unresolved variable."""
    out, inner, i, quote = [], [], 0, None
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = None if ch == "'" else quote
        elif ch == "'" and quote is None:
            quote = "'"
        elif ch == '"':
            quote = None if quote == '"' else '"'
        if quote != "'" and command.startswith("$(", i) and not command.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(command) and depth:
                depth += {"(": 1, ")": -1}.get(command[j], 0)
                j += 1
            text = command[i + 2:j - 1]
        elif quote != "'" and ch == "`" and not powershell:
            j = command.find("`", i + 1)
            j = len(command) if j < 0 else j + 1
            text = command[i + 1:j - 1]
        else:
            out.append(ch)
            i += 1
            continue
        inner.append(text)
        out.append(LISTING if _lists_tree(text, powershell) else SUBSTITUTION)
        i = j
    return "".join(out), inner


def _lister_path(verb, tokens):
    """The folder a listing command walks (`.` when none is named)."""
    rest = tokens[1:]
    if verb == "find":
        return next((t for t in rest[:1] if not t.startswith("-") and t not in ("(", "!")), ".")
    positional, skip = [], False
    for index, token in enumerate(rest):
        low = token.lower()
        if skip:
            skip = False
        elif low in ("-path", "-literalpath") and index + 1 < len(rest):
            return rest[index + 1]
        elif low in LISTER_VALUE_FLAGS:
            skip = True
        elif not token.startswith("-"):
            positional.append(token)
    if verb == "fd":
        positional = positional[1:]
    return positional[0] if positional else "."


def _lister_recursive(verb, tokens):
    lows = [t.lower() for t in tokens[1:]]
    if verb in ("find", "fd", "tree"):
        return True
    if verb in ("get-childitem", "gci") and any(len(t) > 1 and "-recurse".startswith(t) for t in lows):
        return True
    return "-recurse" in lows or "/s" in lows or any(re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", t) for t in tokens[1:])


def _lists_tree(text, powershell):
    """Whether a command prints the files of a whole tree, uncapped: a recursive listing, `git ls-files`, `rg --files`."""
    parts = segments(_strip_heredocs(text))
    if not parts:
        return False
    tokens = _unwrap(_split(parts[0][0], powershell), {})
    if not tokens:
        return False
    verb = tokens[0].lower()
    tree = (verb in LISTERS and _lister_recursive(verb, tokens)) or (verb in ("rg", "ripgrep") and "--files" in tokens) \
        or (verb == "git" and "ls-files" in tokens)
    capped = any(_split(part, powershell)[:1] and _split(part, powershell)[0].lower() in CAPPING_VERBS
                 for part, _pipe in parts[1:]) or (verb == "find" and any(
                     flag in ("-name", "-iname") and not any(ch in value for ch in "*?[")
                     for flag, value in zip(tokens, tokens[1:])))
    return tree and not capped


def _with_lists(tokens, lists):
    """Tokens with each `$name` of a loop variable replaced by the words the loop runs over."""
    out = []
    for token in tokens:
        match = re.fullmatch(r"\$\{?(\w+)\}?", token)
        out.extend(lists[match.group(1)] if match and match.group(1) in lists else [token])
    return out


def _inner_script(tokens):
    """The script a shell invoker runs: `bash -c S`, `sh -lc S`, `powershell -Command S`, `cmd /c S`; else None."""
    verb, rest = re.sub(r"\.exe$", "", tokens[0].lower()), tokens[1:]
    for index, token in enumerate(rest):
        low = token.lower()
        if verb in ("bash", "sh", "zsh", "dash", "ksh") and re.fullmatch(r"-[a-z]*c[a-z]*", token):
            return rest[index + 1] if index + 1 < len(rest) else None
        if verb in ("powershell", "pwsh") and low in ("-command", "-c") or verb == "cmd" and low in ("/c", "/k"):
            return " ".join(rest[index + 1:]) or None
    return None


def _search_args(verb, tokens):
    """(path arguments, recursive, patterns) of a grep-like command: the paths without the pattern and flag values,
    whether it walks folders, and the text it looks for (`-e`/`-Pattern`/`/c:` values, else the first argument)."""
    if verb in ("select-string", "sls"):
        recursive = False
    else:
        recursive = verb in ALWAYS_RECURSIVE or any(
            t in RECURSIVE_FLAGS or (re.fullmatch(r"-[a-zA-Z]+", t) and ("r" in t or "R" in t)) for t in tokens)
    args, paths, patterns, skip, path_next, pattern_given = [], [], [], None, False, False
    for token in tokens:
        low = token.lower()
        if skip:
            if skip == "pattern":
                patterns.append(token)
            skip = None
        elif path_next:
            paths.append(token)
            path_next = False
        elif token in PATTERN_FLAGS or low == "-pattern":
            skip, pattern_given = ("value" if token in ("-f", "--file") else "pattern"), True
        elif low in ("-path", "-literalpath"):
            path_next = True
        elif token in VALUE_FLAGS or low in ("-include", "-exclude", "-context", "-encoding"):
            skip = "value"
        elif verb == "findstr" and low.startswith("/c:"):
            patterns.append(token[3:])
            pattern_given = True
        elif not token.startswith("-") and not (verb == "findstr" and token.startswith("/")):
            args.append(token)
    return paths + (args if pattern_given else args[1:]), recursive, patterns if pattern_given else args[:1]


def _reader_files(verb, tokens):
    """File arguments of a command that prints file contents (cat, head, tail, sed -n, awk, Get-Content...)."""
    files, skip, path_next, script_given = [], False, False, False
    values = READER_VALUE_FLAGS.get(verb, frozenset())
    for token in tokens:
        low = token.lower()
        if skip:
            skip = False
        elif path_next:
            files.append(token)
            path_next = False
        elif low in ("-path", "-literalpath") and verb in ("get-content", "gc", "type"):
            path_next = True
        elif token in READER_SCRIPT_FLAGS.get(verb, ()):
            skip, script_given = True, True
        elif token in values or (verb in ("get-content", "gc") and low in values):
            skip = True
        elif not token.startswith("-") and not token.startswith("+"):
            files.append(token)
    return files[1:] if verb in ("sed", "awk") and not script_given else files


def _inline_script(command, base, cwd):
    """Redirect for an inline python/node script (`-c`, `-e`, heredoc) that walks a folder of a project, reads the
    files and matches their text: a content search written as a script."""
    if not (_INLINE_INTERPRETER.search(command) and _WALK.search(command) and _CONTENT_READ.search(command)
            and _TEXT_MATCH.search(command)) or _SCRIPT_WRITES.search(command):
        return None
    for literal in _SINGLE_QUOTED.findall(command) + _DOUBLE_QUOTED.findall(command):
        prefix = re.split(r"[*?\[]", literal, maxsplit=1)[0]
        if not prefix or prefix in (".", "./"):
            target = base
        elif "\n" in prefix or len(prefix) > 260:
            continue
        else:
            target = resolve_path(_expand(prefix, {}), base)
        if _folder(target, cwd):
            return "redirect", target, "content search over a folder in an inline script"
    if _CWD_WALK.search(command) and _folder(base, cwd):
        return "redirect", base, "content search over a folder in an inline script"
    return None


def _bash(command, cwd, powershell=False):
    found = _inline_script(command, cwd, cwd)
    if found:
        return found
    return _shell(command, cwd, {"base": cwd, "variables": {}, "lists": {}}, powershell, 0)


def _shell(command, cwd, state, powershell, depth):
    """(decision, target, reason) of one shell script; substitutions and `bash -c`/`powershell -Command` scripts are
    decided with the same state (folder, variables, loop variables)."""
    verdict = ("allow", None, "No content search over a folder.")
    text, inner = _substitutions(_strip_heredocs(command), powershell)
    for script in inner if depth < MAX_NESTING else ():
        found = _shell(script, cwd, state, powershell, depth + 1)
        if found[0] == "redirect":
            return found
    variables, lists, listing = state["variables"], state["lists"], None
    for segment, after_pipe in segments(text):
        words = _split(segment, powershell)
        if words[:2] == ["while", "read"]:
            name = next((t for t in words[2:] if not t.startswith("-")), None)
            if name and listing and listing[1] and not listing[2]:
                lists[name] = [LISTING]
            continue
        tokens = _unwrap(words, variables)
        if not tokens:
            continue
        if len(tokens) >= 3 and re.fullmatch(r"\$\w+", tokens[0]) and tokens[1] == "=":
            variables[tokens[0][1:]] = _expand(tokens[2], variables)
            continue
        tokens = _with_lists(tokens, lists)
        verb, base = tokens[0].lower(), state["base"]
        if not after_pipe:
            listing = None
        if verb == "for" and len(tokens) > 2 and tokens[2] == "in":
            lists[tokens[1]] = [_expand(t, variables) for t in tokens[3:]]
            continue
        script = _inner_script(tokens) if depth < MAX_NESTING else None
        if script:
            windows = re.sub(r"\.exe$", "", verb) in ("powershell", "pwsh", "cmd")
            found = _shell(script, cwd, state, windows, depth + 1)
            if found[0] == "redirect":
                return found
            continue
        if verb in CD_VERBS and len(tokens) > 1:
            state["base"] = resolve_path(_expand(tokens[1], variables), base) or base
            continue
        if verb == "xargs":
            consumer = _xargs_command(tokens)
            source = listing[0] if listing else base
            if consumer in SEARCHERS and _folder(source, cwd):
                return "redirect", source, "content search over the files of a listing"
            if consumer in READERS and listing and listing[1] and not listing[2] and _folder(source, cwd):
                return "redirect", source, "reading every file of a listing"
            continue
        if after_pipe:
            if listing and verb in CAPPING_VERBS:
                listing = (listing[0], listing[1], True)
            elif listing and not listing[2] and (verb in PIPED_CONTENT_READERS or (
                    verb in ("foreach-object", "%", "foreach") and _PS_READ.search(segment))) and \
                    _folder(listing[0], cwd):
                return "redirect", listing[0], "content search over the files of a listing"
            continue
        if verb == "git":
            found = _git(tokens, variables, base, cwd)
            if found:
                return found
            continue
        if verb in LISTERS:
            recursive = _lister_recursive(verb, tokens)
            targets = _paths(_expand(_lister_path(verb, tokens), variables), base) or []
            listing = (targets[0], recursive, False) if len(targets) == 1 and os.path.isdir(targets[0]) else None
            execs = [i for i, t in enumerate(tokens) if t in ("-exec", "-execdir", "-ok", "-okdir")]
            if verb == "find" and listing and any(i + 1 < len(tokens) and tokens[i + 1].lower() in SEARCHERS | READERS
                                                  for i in execs) and _folder(listing[0], cwd):
                return "redirect", listing[0], "content search over the files of a listing"
            for target in targets if recursive else []:
                if os.path.isdir(target) and _in_project(target, cwd) and count_files(target)[1]:
                    return "redirect", target, "recursive listing of a very large tree"
            continue
        if verb in SEARCHERS:
            if verb in ("rg", "ripgrep") and "--files" in tokens:
                continue
            found, verdict = _search(verb, tokens, variables, base, cwd, verdict)
            if found:
                return found
            continue
        if verb in READERS:
            files = _reader_files(verb, tokens[1:])
            if LISTING in files and _folder(base, cwd):
                return "redirect", base, "reading every file of a listing"
            listed = sum(len([p for p in found if os.path.isfile(p)])
                         for found in (_paths(_expand(raw, variables), base) for raw in files) if found)
            if listed > LISTED_MAX:
                return "redirect", base, f"reading {listed} files"
    return verdict


def _xargs_tokens(tokens):
    """The command xargs runs, past its own flags."""
    rest = tokens[1:]
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in ("-I", "-n", "-P", "-d", "-L", "-s", "-E", "-a", "--delimiter", "--max-args", "--max-procs") and rest:
            rest.pop(0)
    return rest


def _xargs_command(tokens):
    """The verb xargs runs, past its own flags."""
    rest = _xargs_tokens(tokens)
    return rest[0].lower() if rest else None


def _git(tokens, variables, base, cwd):
    """Redirect for `git grep` over the repository (global options such as -C, -c and --no-pager skipped)."""
    rest, repo = tokens[1:], base
    while rest:
        if rest[0] in ("-C", "-c") and len(rest) > 1:
            if rest[0] == "-C":
                repo = resolve_path(_expand(rest[1], variables), repo) or repo
            rest = rest[2:]
        elif rest[0] in GIT_FLAGS or rest[0].startswith(("--git-dir=", "--work-tree=", "--namespace=", "-c")):
            rest = rest[1:]
        else:
            break
    if not rest or rest[0] != "grep":
        return None
    specs = rest[rest.index("--") + 1:] if "--" in rest else []
    found = [_paths(_expand(raw, variables), repo) for raw in specs]
    only_files = specs and all(f and all(os.path.isfile(p) for p in f) for f in found)
    listed = sum(len(f) for f in found if f) if only_files else 0
    if (not only_files or listed > LISTED_MAX) and _folder(repo, cwd):
        return "redirect", repo, "git grep over the repository"
    return None


def _search(verb, tokens, variables, base, cwd, verdict):
    """(redirect or None, verdict so far) for a grep-like command."""
    args, recursive, _patterns = _search_args(verb, tokens[1:])
    if LISTING in args:
        return (("redirect", base, "content search over the files of a listing") if _folder(base, cwd) else None), verdict
    if not args:
        if recursive and _folder(base, cwd):
            return ("redirect", base, "recursive content search of the working folder"), verdict
        return None, verdict
    listed = 0
    for raw in args:
        found = _paths(_expand(raw, variables), base)
        if found is None:
            verdict = ("allow", None, "The searched path uses a variable the hook cannot resolve.")
            continue
        for target in found:
            if os.path.isdir(target):
                if recursive and _in_project(target, cwd) and _has_files(target):
                    return ("redirect", target, "recursive content search over a folder"), verdict
            else:
                listed += 1
    if listed > LISTED_MAX:
        return ("redirect", base, f"content search over {listed} files"), verdict
    if listed:
        verdict = ("allow", None, "Search inside listed files.")
    return None, verdict


def _command_patterns(command, powershell, depth):
    """Patterns of the grep-like commands, `git grep`, `xargs grep` and `find -exec grep` of a shell command, inner
    scripts and substitutions included."""
    text, inner = _substitutions(_strip_heredocs(command), powershell)
    found = []
    for script in inner if depth < MAX_NESTING else ():
        found += _command_patterns(script, powershell, depth + 1)
    for segment, _after_pipe in segments(text):
        tokens = _unwrap(_split(segment, powershell), {})
        if not tokens:
            continue
        verb = tokens[0].lower()
        script = _inner_script(tokens) if depth < MAX_NESTING else None
        if script:
            found += _command_patterns(script, re.sub(r"\.exe$", "", verb) in ("powershell", "pwsh", "cmd"), depth + 1)
            continue
        if verb == "xargs":
            tokens = _xargs_tokens(tokens)
        elif verb == "find":
            start = next((i + 1 for i, t in enumerate(tokens) if t in ("-exec", "-execdir", "-ok", "-okdir")), None)
            tokens = tokens[start:] if start else []
        elif verb == "git":
            tokens = tokens[tokens.index("grep"):] if "grep" in tokens else []
            tokens = tokens[:tokens.index("--")] if "--" in tokens else tokens
        if tokens and tokens[0].lower() in SEARCHERS | {"grep"}:
            found += _search_args(tokens[0].lower(), tokens[1:])[2]
    return found


def _regex_words(pattern):
    """The literal words of a regular expression: classes, quantifiers, escapes and operators become spaces."""
    text = re.sub(r"\[[^\]]*\]|\{\d*,?\d*\}|\\[a-zA-Z]|\(\?[:=!<]*", " ", pattern)
    return re.sub(r"[\\^$.*+?()|{}\[\]]", " ", text)


def search_terms(tool_name, tool_input):
    """The smart_search query of a redirected call: the words of the Grep pattern, of the patterns a shell command
    searches for, or else of the string literals an inline script matches against. Empty when none is found."""
    if tool_name == "Grep":
        patterns = [str(tool_input.get("pattern") or "")]
    else:
        command = str(tool_input.get("command") or "")
        patterns = _command_patterns(command, tool_name == "PowerShell", 0) or [
            match.group(2) or match.group(4) for match in _MATCHED_LITERAL.finditer(command)]
    words = []
    for pattern in patterns:
        words += [word for word in _regex_words(pattern).split() if word not in words]
    return " ".join(words)[:QUERY_MAX_CHARS]


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
    if tool_name in ("Bash", "PowerShell"):
        return _bash(tool_input.get("command") or "", cwd, powershell=tool_name == "PowerShell")
    return "allow", None, "Tool not redirected."
