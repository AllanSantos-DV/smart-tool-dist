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
import time

import atomic_io
import project_identity
import index_views

import model_client

SCOPE_DIR = paths.DATA_DIR

# Names safe to exclude everywhere without inspection: dependency/tooling caches whose
# purpose does not vary by project convention. Ambiguous names (dist, build, out, bin...)
# are deliberately NOT here — those must be inspected, since their meaning is project-specific.
_ALWAYS_EXCLUDE = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", "*.egg-info",
}

# Mesmo critério do `_ALWAYS_EXCLUDE`, para arquivo: o significado não varia por
# projeto. Lockfile é grafo de dependência resolvido por ferramenta — nunca é fonte de
# resposta numa busca semântica, e é grande o bastante pra dominar o índice.
_ALWAYS_EXCLUDE_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "npm-shrinkwrap.json",
    "poetry.lock", "pdm.lock", "uv.lock", "Pipfile.lock", "Cargo.lock",
    "composer.lock", "go.sum", "Gemfile.lock", "packages.lock.json",
}

_ROOT_DOC_NAMES = ("AGENTS.md", "CLAUDE.md", "README.md", "README")


def always_excluded(name):
    """Directory name in _ALWAYS_EXCLUDE, entries may be globs (e.g. *.egg-info)."""
    folded = name.casefold()
    return any(fnmatch.fnmatch(folded, pattern.casefold()) for pattern in _ALWAYS_EXCLUDE)

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
    return sorted(
        name for name in names
        if not name.startswith(".")
        and not always_excluded(name)
        and os.path.isdir(os.path.join(root, name))
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


def save_scope(root, scope):
    if not valid_scope(scope):
        raise ValueError("Scope must contain lists of paths in include/exclude.")
    index_views.assert_current(index_views.describe(root))
    os.makedirs(SCOPE_DIR, exist_ok=True)
    atomic_io.write_secret_text(scope_path(root), json.dumps(
        {**scope, "root": project_identity.canonical_root(root)}, indent=2, ensure_ascii=False))


def _structure(root, max_dirs=1500):
    found = []
    for current, dirs, _files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not always_excluded(d)
                         and _within_root(root, os.path.join(current, d)))
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
    return (not current.issubset(known) or
            not set(_structure(root)).issubset(set(cached_scope.get("structure", []))))


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
        f"whose meaning does not vary by project): {', '.join(sorted(_ALWAYS_EXCLUDE))}. "
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
        "Include sources useful for search even if they are gitignored (e.g. docs/, README, "
        "ADRs). Java, Angular, HTML/CSS, plain text and Markdown are supported sources. "
        "DOCX documents are supported through bounded local text extraction; do not exclude "
        "useful Word documentation merely because its container is binary. "
        "Exclude directories that inspection shows are generated artifacts or "
        "dependencies (compiled binaries, minified bundles, hashed build chunks, vendored "
        "packages)."
    )


def decide_scope(root, router_model, force=False, deadline=None, cancel_check=None):
    cached = load_scope(root)
    if not force and cached is not None and not needs_rescan(root, cached):
        return cached

    top_level_dirs = _top_level_dirs(root)
    ambiguous_dirs = [d for d in top_level_dirs if not always_excluded(d)]
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
    scope = {
        "include": (decision or {}).get("include") or [],
        "exclude": (decision or {}).get("exclude") or [],
        "top_level_dirs": top_level_dirs,
        "scope_version": SCOPE_VERSION,
        "structure": _structure(root),
        "docs_digest": _docs_digest(root),
        "user_exclude": (cached or {}).get("user_exclude", []),
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
        if matcher(name):
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


def resolve_included_files(root, scope, cancel_check=None):
    if not valid_scope(scope):
        raise ValueError("Invalid scope; request a new project analysis.")
    include = scope.get("include") or [""]
    matcher = _exclude_matcher(set(scope.get("exclude") or []) | set(scope.get("user_exclude") or []) |
                               _ALWAYS_EXCLUDE | _ALWAYS_EXCLUDE_FILES)
    files = []
    def walk_error(error):
        raise OSError("Could not list a folder in the scope; try again when it is accessible.") from error
    if not any(base in ("", ".") for base in include):
        files.extend(_root_level_files(root, matcher))
    for base in include:
        base_full = os.path.join(root, base) if base not in ("", ".") else root
        # `include` vem de tool call de um LLM cujo prompt embute documentação do
        # repositório analisado: um valor absoluto ou com `..` faria a varredura sair do
        # projeto e mandar conteúdo de fora pro endpoint de embeddings.
        if not _within_root(root, base_full):
            continue
        if os.path.isfile(base_full):
            rel = os.path.relpath(base_full, root).replace(os.sep, "/")
            if not matcher(rel):
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
                # `os.walk` não desce em symlink de diretório, mas `os.path.islink()` é
                # False para junction do Windows (`mklink /J`, sem admin) — e desce nela.
                and _within_root(root, os.path.join(dirpath, d))
            ]
            for name in filenames:
                rel_path = f"{rel_dir}/{name}" if rel_dir else name
                if not matcher(rel_path) and _within_root(root, os.path.join(root, rel_path)):
                    files.append(rel_path)
    return sorted(set(files))
