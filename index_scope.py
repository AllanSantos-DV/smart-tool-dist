#!/usr/bin/env python3
"""Escopo de indexação decidido por LLM, por repositório (stdlib only).

Cada repositório tem convenções diferentes (ex.: `.gitignore` tira `docs/` do controle de
versão, mas `docs/` é ótima fonte pra busca) — em vez de um classificador mecânico de
exclusão, um LLM analisa a estrutura do repo uma vez e decide o que entra/sai do índice.
A decisão é cacheada por path do projeto; re-scan automático só ocorre se surgirem
diretórios de topo novos desde a última decisão, ou via comando manual (`force=True`).
"""
import fnmatch
import hashlib
import json
import os
import paths
import re
import time

import atomic_io
import project_identity
import project_store
import index_views

import model_client

SCOPE_DIR = paths.DATA_DIR

# Names safe to exclude everywhere without inspection: dependency/tooling caches whose
# purpose does not vary by project convention. Ambiguous names (dist, build, out, bin...)
# are deliberately NOT here — those must be inspected, since their meaning is project-specific.
_ALWAYS_EXCLUDE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", "*.egg-info",
    "site-packages", "bower_components", "jspm_packages",
}

# Folders that are generated output only when the root proves the project type: a root marker file maps to the names
# it makes unambiguous (Cargo's and Maven's `target`, CocoaPods' `Pods`). Excluded mechanically, before any model sees
# the folder list.
_PROJECT_TYPE_EXCLUDES = (
    (("Cargo.toml",), ("target",)),
    (("pom.xml",), ("target",)),
    (("Podfile",), ("Pods",)),
)

# Files a tool writes while it works and replaces or deletes right after (atomic-write temp, backup, editor swap, the
# journal and shared-memory files SQLite keeps beside a database).
TRANSIENT_FILES = {"*.tmp", "*.bak", "*.swp", "*~", "*-journal", "*-wal", "*-shm", "~$*", "*.crdownload", "*.part"}

# Mesmo critério do `_ALWAYS_EXCLUDE`, para arquivo: o significado não varia por
# projeto. Lockfile é grafo de dependência resolvido por ferramenta — nunca é fonte de
# resposta numa busca semântica, e é grande o bastante pra dominar o índice.
_ALWAYS_EXCLUDE_FILES = TRANSIENT_FILES | {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "pdm.lock", "uv.lock", "Pipfile.lock", "Cargo.lock",
    "composer.lock", "go.sum", "Gemfile.lock", "packages.lock.json",
}

_ROOT_DOC_NAMES = ("AGENTS.md", "CLAUDE.md", "README.md", "README")

# Runtime folders a tool keeps inside its own config home (conversation logs, plugin caches, snapshots): never project
# source, written all day by the tool itself. Keyed by the root's folder name; excluded mechanically, whatever the
# scope decision says, so a registered `~/.claude` never indexes or re-scans them.
_TOOL_STATE_DIRS = {
    ".claude": ("backups", "cache", "daemon", "debug", "downloads", "file-history", "ide", "paste-cache", "plugins",
                "projects", "session-env", "sessions", "shell-snapshots", "state", "statsig", "telemetry", "todos"),
}


def tool_state_excludes(root):
    """Names of the tool-runtime folders under `root` (empty unless `root` is a known tool config home)."""
    return _TOOL_STATE_DIRS.get(os.path.basename(os.path.normpath(root)).casefold(), ())


def project_type_excludes(root):
    """Names of the generated-output folders `root`'s marker files make unambiguous (empty for an unknown type)."""
    found = []
    for markers, names in _PROJECT_TYPE_EXCLUDES:
        if any(os.path.isfile(os.path.join(root, marker)) for marker in markers):
            found.extend(names)
    return tuple(found)


def mechanical_excludes(root):
    """Everything excluded without asking a model: the tool-runtime folders of a config home and the generated output
    of the project type. The names of `_ALWAYS_EXCLUDE` and every hidden folder are excluded on top of these."""
    return (*tool_state_excludes(root), *project_type_excludes(root))


def is_hidden(rel_path):
    """Whether any part of the relative path starts with a dot (a tool's folder: .git, .vscode, .crowd, .tokenguard)."""
    return any(part.startswith(".") for part in str(rel_path).replace("\\", "/").split("/") if part not in ("", ".", ".."))


def normalize_include(include):
    """`(bases, dropped)` from the include entries of a scope. A base is a folder or file path relative to the root, with
    the shapes a model emits for "everything under it" (`docs/**`, `docs/*`, `docs/`) reduced to the folder; `*`, `**`
    and `**/*` mean the whole project and become ``. An entry that is absolute, climbs out with `..`, sits inside a hidden
    folder or keeps a wildcard in the middle is dropped: it can never name something indexable, and it is returned so the
    caller can report it. No entries means no folder: only the loose files at the root are indexed."""
    bases, dropped = [], []
    for raw in include or []:
        base = str(raw).replace("\\", "/").strip()
        while base.startswith("./"):
            base = base[2:]
        for tail in ("/**/*", "/**", "/*"):
            if base.endswith(tail):
                base = base[:-len(tail)]
        base = base.strip("/")
        if base in ("*", "**", "**/*", "."):
            base = ""
        if (os.path.isabs(str(raw)) or str(raw).strip().startswith(("/", "\\")) or ".." in base.split("/") or is_hidden(base)
                or any(ch in base for ch in "*?[")):
            dropped.append(raw)
        elif base not in bases:
            bases.append(base)
    return bases, dropped


def is_transient(name):
    """Whether a file name is a temporary, backup or editor-swap file (`*.tmp`, `*.bak`, `*.swp`, `*~`, SQLite `*-journal`)."""
    return any(fnmatch.fnmatch(name.casefold(), pattern) for pattern in TRANSIENT_FILES)


def always_excluded(name):
    """Directory name in _ALWAYS_EXCLUDE, entries may be globs (e.g. *.egg-info)."""
    folded = name.casefold()
    return any(fnmatch.fnmatch(folded, pattern.casefold()) for pattern in _ALWAYS_EXCLUDE)


def venv_name(name):
    """Whether a folder name marks a Python virtual environment: it holds `venv` (venv, venv-ov, gate-venv, .devenv)."""
    return "venv" in name.casefold()


def is_venv_folder(path):
    """Whether the folder at path is a Python virtual environment, by its name (`venv_name`) or because it holds
    `pyvenv.cfg`, which every virtual environment writes whatever its folder is called."""
    return venv_name(os.path.basename(path)) or os.path.isfile(os.path.join(path, "pyvenv.cfg"))


def _is_exempt(rel_path, bases):
    """Whether rel_path is an include base or a folder above one: the scope names it, so no exclusion rule judges it."""
    folded = rel_path.casefold()
    return any(base.casefold() == folded or base.casefold().startswith(folded + "/") for base in bases if base)


def _inside_venv(root, parts, is_dir, bases):
    """Whether a folder on the way to the path (its last part counts only when it is a folder) is a virtual
    environment, leaving out the folders `_is_exempt` spares."""
    for depth in range(1, (len(parts) if is_dir else len(parts) - 1) + 1):
        if not _is_exempt("/".join(parts[:depth]), bases) and is_venv_folder(os.path.join(root, *parts[:depth])):
            return True
    return False


def _gitignore_source(pattern):
    """Regular expression source of one `.gitignore` pattern, already stripped of `!` and the closing `/`, matched
    against a path relative to the folder of the file: `*` and `?` stay inside a name, `**` crosses folders, `[...]`
    is a class, `\\x` is a literal, and a pattern with a `/` is anchored to that folder."""
    anchored = "/" in pattern
    pattern = pattern.lstrip("/")
    out, index = [], 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            out.append(".*")
            index += 2
            continue
        close = pattern.find("]", index + 2) if char == "[" else -1
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif close != -1:
            body = pattern[index + 1:close]
            out.append("[" + ("^" + body[1:] if body.startswith("!") else body).replace("\\", "\\\\") + "]")
            index = close
        elif char == "\\" and index + 1 < len(pattern):
            index += 1
            out.append(re.escape(pattern[index]))
        else:
            out.append(re.escape(char))
        index += 1
    return ("" if anchored else "(?:.*/)?") + "".join(out)


def _gitignore_runs(text):
    """The rules of one `.gitignore` as runs of consecutive rules with the same polarity, in file order:
    `[(negated, regex_for_any_entry, regex_for_files_or_None)]`. A pattern that is not a valid glob matches nothing,
    as in Git."""
    flags = re.IGNORECASE if os.name == "nt" else 0
    runs = []
    for raw in text.splitlines():
        line = raw if raw.endswith("\\ ") else raw.rstrip(" \t")
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line[1:] if negated else line
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        source = _gitignore_source(line)
        try:
            re.compile(source, flags)
        except re.error:
            continue
        if not runs or runs[-1][0] != negated:
            runs.append((negated, [], []))
        runs[-1][1].append(source)
        if not dir_only:
            runs[-1][2].append(source)

    def combine(sources):
        return re.compile("(?:" + "|".join(f"(?:{source})" for source in sources) + r")\Z", flags) if sources else None

    return [(negated, combine(anywhere), combine(files)) for negated, anywhere, files in runs]


class GitIgnore:
    """What the `.gitignore` files inside a project folder ignore: the file of each folder applies to that folder and
    below, a deeper file overrides a shallower one, and within a file the last matching rule wins. Read from the
    project folder only (not Git's global excludes, `.git/info/exclude` or the files of a repository above the
    project), a file is read when first needed and again after `TTL_S`, so one instance can serve a burst of file
    events without going stale."""

    TTL_S = 5

    def __init__(self, root):
        self.root = root
        self._folders = {}

    def _runs(self, folder):
        now = time.monotonic()
        hit = self._folders.get(folder)
        if hit and now - hit[0] < self.TTL_S:
            return hit[1]
        try:
            with open(os.path.join(self.root, folder, ".gitignore"), encoding="utf-8-sig", errors="replace") as stream:
                text = stream.read()
        except (FileNotFoundError, NotADirectoryError):
            text = ""
        runs = _gitignore_runs(text)
        self._folders[folder] = (now, runs)
        return runs

    def matches(self, rel_path, is_dir):
        """Whether the rules ignore this entry by themselves, which is the question during a walk that never enters an
        ignored folder. `rel_path` uses `/`."""
        parts = rel_path.split("/")
        verdict = False
        for depth in range(len(parts)):
            target = "/".join(parts[depth:])
            for negated, anywhere, files in self._runs("/".join(parts[:depth])):
                pattern = anywhere if is_dir else files
                if pattern and pattern.match(target):
                    verdict = not negated
        return verdict

    def ignored(self, rel_path, is_dir, bases=()):
        """Whether the entry or any folder above it is ignored, for a path met out of a walk (a file event). The
        include `bases` and the folders above them are spared: the scope names them."""
        parts = rel_path.split("/")
        for depth in range(1, len(parts) + 1):
            sub = "/".join(parts[:depth])
            if not _is_exempt(sub, bases) and self.matches(sub, depth < len(parts) or is_dir):
                return True
        return False

# Versão do contrato de decisão de escopo. Mudar o que é perguntado ao decisor exige
# incrementar: sem isso, um repositório com escopo já cacheado nunca é reavaliado, porque
# `needs_rescan` só olha diretório de topo novo.
SCOPE_VERSION = 4


def _within_root(root, candidate):
    """`candidate` está dentro de `root` depois de resolver link/junction. Comparar só
    `abspath` (sem resolver) deixa passar junction do Windows, que aponta pra fora sem
    que `os.path.islink()` acuse."""
    return project_identity.within_root(root, candidate)

_INSPECT_TOOL = {
    "type": "function",
    "function": {
        "name": "inspect_directory",
        "description": (
            "Inspect a directory's real contents (file count, extension histogram, sample "
            "filenames) before deciding whether to include or exclude it from the search "
            "index. Call this for every directory whose purpose is not already settled by "
            "the project documentation — never decide from the directory name alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the project root"},
            },
            "required": ["path"],
        },
    },
}

_SCOPE_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_scope",
        "description": "Submit the include/exclude scope decision for the search index.",
        "parameters": {
            "type": "object",
            "properties": {
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["include", "exclude"],
        },
    },
}


def _project_hash(root):
    return index_views.storage_key(root)


def scope_path(root):
    return os.path.join(SCOPE_DIR, _project_hash(root) + ".scope.json")


def _top_level_dirs(root):
    try:
        names = os.listdir(root)
    except OSError:
        return []
    skipped = mechanical_excludes(root)
    return sorted(
        name for name in names
        if not name.startswith(".")
        and not always_excluded(name)
        and name not in skipped
        and os.path.isdir(os.path.join(root, name))
        and not is_venv_folder(os.path.join(root, name))
        and _within_root(root, os.path.join(root, name))
    )


def valid_scope(scope):
    return (isinstance(scope, dict) and all(
        isinstance(scope.get(key, []), list) and all(isinstance(item, str) for item in scope.get(key, []))
        for key in ("include", "exclude", "user_exclude", "top_level_dirs", "structure")))


def load_scope(root):
    paths = [scope_path(root)] + [os.path.join(SCOPE_DIR, key + ".scope.json")
                                  for key in project_identity.legacy_ids(root)]
    for path in dict.fromkeys(paths):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if valid_scope(data):
                return data
        except (OSError, ValueError, UnicodeError):
            continue
    return None


def sibling_scope(root):
    """Scope of the most recently saved other Git view of the same project, or None. A new branch or a worktree starts
    without a scope; asking the scope and profile models again cost ~28 s of a 52 s first search (measured 2026-10-09
    on a branch at the same commit). The caller still checks it with needs_rescan before using it."""
    if not index_views.describe(root).get("git"):
        return None
    own = scope_path(root)
    prefix = project_identity.project_id(root) + ".v-"
    try:
        names = [n for n in os.listdir(SCOPE_DIR) if n.startswith(prefix) and n.endswith(".scope.json")]
    except OSError:
        return None
    paths = sorted((os.path.join(SCOPE_DIR, n) for n in names), key=os.path.getmtime, reverse=True)
    for path in paths:
        if os.path.normcase(path) == os.path.normcase(own):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError, UnicodeError):
            continue
        if valid_scope(data):
            return data
    return None


def save_scope(root, scope):
    if not valid_scope(scope):
        raise ValueError("Scope must contain lists of paths in include/exclude.")
    index_views.assert_current(index_views.describe(root))
    os.makedirs(SCOPE_DIR, exist_ok=True)
    atomic_io.write_secret_text(scope_path(root), json.dumps(
        {**scope, "root": project_identity.canonical_root(root)}, indent=2, ensure_ascii=False))


def _structure(root, max_dirs=1500, keep=None):
    """Relative folders of `root` (at most `max_dirs`). `keep(rel)` prunes the walk: a folder it rejects is neither
    listed nor entered."""
    found = []
    for current, dirs, _files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not always_excluded(d)
                         and not is_venv_folder(os.path.join(current, d))
                         and _within_root(root, os.path.join(current, d))
                         and (keep is None or keep(os.path.relpath(os.path.join(current, d), root).replace(os.sep, "/"))))
        for name in dirs:
            found.append(os.path.relpath(os.path.join(current, name), root).replace(os.sep, "/"))
            if len(found) >= max_dirs:
                return found
    return found


def _docs_digest(root):
    return hashlib.sha256(_read_root_docs(root).encode("utf-8")).hexdigest()


def needs_rescan(root, cached_scope):
    if not valid_scope(cached_scope):
        return True
    if cached_scope.get("manual"):
        return False
    if cached_scope.get("scope_version") != SCOPE_VERSION:
        return True
    if cached_scope.get("docs_digest") != _docs_digest(root):
        return True
    known = set(cached_scope.get("top_level_dirs", []))
    current = set(_top_level_dirs(root))
    # A folder the scope excludes (a cache that grows all day) is not a structure change: the walk skips it.
    return (not current.issubset(known) or
            not set(_structure(root, keep=scope_filter(cached_scope, root))).issubset(
                set(cached_scope.get("structure", []))))


def _read_root_docs(root, max_chars=4000):
    parts = []
    budget = max_chars
    for name in _ROOT_DOC_NAMES:
        if budget <= 0:
            break
        full = os.path.join(root, name)
        if not os.path.isfile(full) or not _within_root(root, full):
            continue
        try:
            with open(full, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read(budget)
        except OSError:
            continue
        parts.append(f"--- {name} ---\n{text}")
        budget -= len(text)
    return "\n\n".join(parts)


def _inspect_directory(root, rel_path, max_files=4000, sample_size=15, top_ext=12):
    root_full = os.path.normpath(os.path.abspath(root))
    base_full = os.path.normpath(os.path.abspath(os.path.join(root, rel_path)))
    if not _within_root(root_full, base_full):
        return {"error": "path outside the project root"}
    if not os.path.isdir(base_full):
        return {"error": f"directory not found: {rel_path}"}

    ext_counts = {}
    samples = []
    total = 0
    total_size = 0
    max_depth = 0
    for dirpath, _dirnames, filenames in os.walk(base_full):
        _dirnames[:] = [name for name in _dirnames if not name.startswith(".")
                       and not always_excluded(name)
                       and not is_venv_folder(os.path.join(dirpath, name))
                       and _within_root(root, os.path.join(dirpath, name))]
        depth = os.path.relpath(dirpath, base_full).count(os.sep) + (0 if dirpath == base_full else 1)
        max_depth = max(max_depth, depth)
        for name in filenames:
            total += 1
            if total > max_files:
                break
            ext = os.path.splitext(name)[1].lower() or "(no extension)"
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
            full = os.path.join(dirpath, name)
            try:
                total_size += os.path.getsize(full)
            except OSError:
                pass
            if len(samples) < sample_size:
                samples.append(os.path.relpath(full, base_full).replace(os.sep, "/"))
        if total > max_files:
            break

    top_extensions = dict(sorted(ext_counts.items(), key=lambda kv: -kv[1])[:top_ext])
    return {
        "path": rel_path,
        "file_count": total,
        "truncated": total > max_files,
        "total_size_bytes": total_size,
        "max_depth": max_depth,
        "extensions": top_extensions,
        "sample_files": samples,
    }


def _build_scope_prompt(root, ambiguous_dirs, root_files, doc_context):
    return (
        "You are deciding what to include in a semantic code-search index for the "
        f"repository at {root}.\n\n"
        "These directories are already excluded automatically (dependency/tooling caches "
        f"whose meaning does not vary by project): {', '.join(sorted({*_ALWAYS_EXCLUDE, *mechanical_excludes(root)}))}. "
        "Every hidden folder (name starting with a dot: .git, .vscode, .crowd, .tokenguard...) is also never indexed. "
        "Do not decide on them.\n\n"
        f"Directories that still need a decision: {', '.join(ambiguous_dirs) or '(none)'}\n\n"
        "Loose files at the repository ROOT are indexed automatically, because in a flat "
        "repository they are the whole codebase and no directory in `include` can stand "
        f"for them. These are the ones found: {', '.join(root_files) or '(none)'}. "
        "If any of them is generated output, a data dump, a duplicate of another file, or "
        "otherwise useless for code search, list it in `exclude` by name — `exclude` "
        "accepts file paths and glob patterns (e.g. 'notes/*.log'), not only directory "
        "names. Files holding credentials are dropped mechanically elsewhere; you do not "
        "need to decide on those.\n\n"
        "Project documentation found at the root (may state project-specific conventions):\n"
        f"{doc_context or '(none found)'}\n\n"
        "IMPORTANT: names like 'dist', 'build', 'out', 'bin', 'target' do NOT reliably mean "
        "'generated build output' — some projects use them to hold real hand-written source. "
        "Never decide from the directory name alone. For every directory listed above whose "
        "purpose is not already settled by the documentation, call inspect_directory(path) "
        "and look at the real extensions/sample filenames before deciding. If the project "
        "documentation above explicitly states what a directory is for, that statement "
        "overrides any inference you would otherwise draw from file extensions or the "
        "directory's name — do not exclude a directory the documentation identifies as "
        "source, even if it also contains a few generated/log files. Only call submit_scope "
        "once every non-obvious directory is grounded in inspection data or in the "
        "documentation.\n\n"
        "`include` lists folder paths relative to the root, without wildcards; a folder you leave out of `include` is "
        "not indexed, and an empty `include` indexes only the loose root files. "
        "Whatever .gitignore ignores, and every folder named like *venv* or holding pyvenv.cfg (a Python virtual "
        "environment), is not indexed, unless `include` names that folder or file: name the gitignored ones that are "
        "useful for search (e.g. docs/, ADRs, CLAUDE.md). "
        "Java, Angular, HTML/CSS, plain text and Markdown are supported sources. "
        "DOCX, PPTX, XLSX and PDF documents are supported through bounded local text extraction; do not exclude "
        "useful Office or PDF documentation merely because its container is binary. "
        "Exclude directories that inspection shows are generated artifacts or "
        "dependencies (compiled binaries, minified bundles, hashed build chunks, vendored "
        "packages)."
    )


def decide_scope(root, router_model, force=False, deadline=None, cancel_check=None):
    cached = load_scope(root)
    if not force and cached is not None and not needs_rescan(root, cached):
        return cached

    top_level_dirs = _top_level_dirs(root)
    ambiguous_dirs = list(top_level_dirs)
    root_files = _root_level_files(root, _exclude_matcher(_ALWAYS_EXCLUDE_FILES))
    doc_context = _read_root_docs(root)

    token = model_client.get_token()
    messages = [{"role": "user", "content": _build_scope_prompt(root, ambiguous_dirs, root_files, doc_context)}]
    tools = [_INSPECT_TOOL, _SCOPE_TOOL]
    decision = None

    for _ in range(min(len(ambiguous_dirs) + 5, 30)):
        if cancel_check:
            cancel_check()
        remaining = deadline - time.monotonic() if deadline is not None else 60
        if remaining <= 0:
            raise TimeoutError("Scope analysis deadline exceeded.")
        response = model_client.fetch(
            "/v1/chat/completions",
            token,
            method="POST",
            timeout=min(60, remaining),
            body={
                "model": router_model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "required",
                "temperature": 0,
            },
        )
        message = response["choices"][0]["message"]
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            break
        messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls})

        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            try:
                args = json.loads(tool_call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            if name == "submit_scope":
                if isinstance(args, dict) and "include" in args and "exclude" in args and valid_scope(args):
                    decision = args
                    result_text = "ok"
                else:
                    result_text = "include/exclude must be arrays of strings"
            elif name == "inspect_directory":
                result_text = json.dumps(_inspect_directory(root, args.get("path", "")), ensure_ascii=False)
            else:
                result_text = json.dumps({"error": f"tool desconhecida: {name}"})
            messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result_text})

        if decision is not None:
            break

    if decision is None:
        raise RuntimeError("The model did not define a valid scope. Check the model or set the scope manually.")
    include, dropped = normalize_include(decision.get("include"))
    final = {"include": include,
             "exclude": [*(decision.get("exclude") or []), *mechanical_excludes(root)],
             "user_exclude": (cached or {}).get("user_exclude", [])}
    scope = {
        "include": final["include"],
        "dropped_include": [str(item) for item in dropped],
        "exclude": final["exclude"],
        "top_level_dirs": top_level_dirs,
        "scope_version": SCOPE_VERSION,
        "structure": _structure(root, keep=scope_filter(final, root)),
        "docs_digest": _docs_digest(root),
        "user_exclude": final["user_exclude"],
        "decided_at": time.time(),
    }
    save_scope(root, scope)
    return scope


def _root_level_files(root, matcher):
    """Arquivos soltos na raiz do projeto, sem descer em nenhum diretório.

    Num repositório de fontes planos (Python/PowerShell sem `src/`) eles são o código
    todo, e `include` lista diretórios — então nenhuma entrada de `include` os
    representa. `_build_scope_prompt` mostra esta mesma lista ao decisor, que pode
    nomeá-los em `exclude`."""
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        raise OSError("Could not list the root; the existing index was kept.") from exc
    out = []
    for name in names:
        if matcher(name) or name.startswith("."):
            continue
        full = os.path.join(root, name)
        # `isfile` segue link: sem a checagem de contenção, um link na raiz apontando
        # pra fora do projeto entraria com um `rel_path` que parece interno.
        if os.path.isfile(full) and _within_root(root, full):
            out.append(name)
    return out


def _exclude_matcher(exclude):
    """Predicado "está excluído?" tolerante ao que um LLM de fato emite: glob
    (`logs/*.log`), separador do Windows, caixa diferente da do disco e prefixo
    de diretório. Uma exclusão que não casa é pior que nenhuma — o decisor acredita ter
    tirado o arquivo do índice."""
    patterns = set()
    for raw in exclude:
        norm = str(raw).replace("\\", "/").strip("/").lower()
        if norm.startswith("./"):
            norm = norm[2:]
        if norm:
            patterns.add(norm)

    def matches(rel_path):
        target = rel_path.replace("\\", "/").lower()
        name = target.rsplit("/", 1)[-1]
        for pattern in patterns:
            if target == pattern or name == pattern:
                return True
            if target.startswith(pattern + "/"):
                return True
            if ("*" in pattern or "?" in pattern or "[" in pattern) and (
                fnmatch.fnmatch(target, pattern) or fnmatch.fnmatch(name, pattern)
            ):
                return True
        return False

    return matches


def _nested_matcher(root):
    """Predicate "is this inside a registered project nested under root?", by exact path prefix: that project is
    indexed on its own, so the folder holding it leaves it out. A folder elsewhere with the same name stays in."""
    nested = tuple(path.lower() for path in project_store.nested_projects(root)) if root else ()

    def inside(rel_path):
        target = rel_path.replace("\\", "/").strip("/").lower()
        return any(target == path or target.startswith(path + "/") for path in nested)

    return inside


def scope_filter(scope, root=None):
    """Predicate `keep(rel_path)`: whether resolve_included_files would list rel_path (a file) or walk it (a folder) —
    under an include base, outside the exclusions, outside hidden folders. Built once so a burst of file events does not
    rebuild the matcher per path; `root` adds the tool-runtime folders of a known config home."""
    bases, _dropped = normalize_include(scope.get("include"))
    matcher = _exclude_matcher(set(scope.get("exclude") or []) | set(scope.get("user_exclude") or []) |
                               set(mechanical_excludes(root) if root else ()) | _ALWAYS_EXCLUDE | _ALWAYS_EXCLUDE_FILES)
    whole = "" in bases
    nested = _nested_matcher(root)
    ignore = GitIgnore(root) if root else None

    def keep(rel_path):
        rel_path = rel_path.replace("\\", "/")
        parts = rel_path.split("/")
        if matcher(rel_path) or is_hidden(rel_path) or nested(rel_path):
            return False
        if not (whole or len(parts) == 1 or any(rel_path == base or rel_path.startswith(base + "/") for base in bases)):
            return False
        if not root:
            return True
        is_dir = os.path.isdir(os.path.join(root, *parts))
        return not (_inside_venv(root, parts, is_dir, bases) or ignore.ignored(rel_path, is_dir, bases))

    return keep


def in_scope(scope, rel_path, root=None):
    """Whether resolve_included_files would list rel_path: under an include base, outside the exclusions and outside
    hidden folders."""
    return scope_filter(scope, root)(rel_path)


def resolve_included_files(root, scope, cancel_check=None):
    if not valid_scope(scope):
        raise ValueError("Invalid scope; request a new project analysis.")
    include, _dropped = normalize_include(scope.get("include"))
    excluded = _exclude_matcher(set(scope.get("exclude") or []) | set(scope.get("user_exclude") or []) |
                                set(mechanical_excludes(root)) | _ALWAYS_EXCLUDE | _ALWAYS_EXCLUDE_FILES)
    nested = _nested_matcher(root)
    ignore = GitIgnore(root)

    def matcher(rel_path):
        return excluded(rel_path) or nested(rel_path)

    files = []
    def walk_error(error):
        raise OSError(f"Could not list {error.filename} ({error.strerror}); try again when it is accessible.") from error
    if "" not in include:
        files.extend(_root_level_files(root, lambda rel: matcher(rel) or ignore.matches(rel, False)))
    for base in include:
        base_full = os.path.join(root, base) if base else root
        # `include` vem de tool call de um LLM cujo prompt embute documentação do
        # repositório analisado: um valor absoluto ou com `..` faria a varredura sair do
        # projeto e mandar conteúdo de fora pro endpoint de embeddings.
        if not _within_root(root, base_full):
            continue
        if not os.path.lexists(base_full):
            continue
        if os.path.isfile(base_full):
            rel = os.path.relpath(base_full, root).replace(os.sep, "/")
            if not matcher(rel) and not is_hidden(rel):
                files.append(rel)
            continue
        for dirpath, dirnames, filenames in os.walk(base_full, onerror=walk_error):
            if cancel_check:
                cancel_check()
            rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
            rel_dir = "" if rel_dir == "." else rel_dir
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".")
                and not matcher(f"{rel_dir}/{d}" if rel_dir else d)
                and not is_venv_folder(os.path.join(dirpath, d))
                and not ignore.matches(f"{rel_dir}/{d}" if rel_dir else d, True)
                # `os.walk` não desce em symlink de diretório, mas `os.path.islink()` é
                # False para junction do Windows (`mklink /J`, sem admin) — e desce nela.
                and _within_root(root, os.path.join(dirpath, d))
            ]
            for name in filenames:
                rel_path = f"{rel_dir}/{name}" if rel_dir else name
                if (not name.startswith(".") and not matcher(rel_path) and not ignore.matches(rel_path, False)
                        and _within_root(root, os.path.join(root, rel_path))):
                    files.append(rel_path)
    return sorted(set(files))
