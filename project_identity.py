"""Identidade única de raízes e contenção de caminhos, inclusive junctions Windows."""
import hashlib
import os
import time

RESOLVED_TTL_S = 3
RESOLVED_MAX = 4096
_resolved = {}


def display_root(root):
    """Absolute path of `root` with symlinks and junctions resolved. Resolving costs one filesystem call per path
    component on Windows, and the daemon asks for the same few paths thousands of times in a row (per file of an
    index pass, per project of every monitor tick), so an answer is reused for RESOLVED_TTL_S seconds: a junction
    retargeted inside that window is seen after it."""
    if not isinstance(root, (str, os.PathLike)) or not str(root).strip():
        raise ValueError("Provide the project folder.")
    absolute = os.path.abspath(os.path.expanduser(os.fspath(root)))
    now = time.monotonic()
    hit = _resolved.get(absolute)
    if hit and now - hit[0] < RESOLVED_TTL_S:
        return hit[1]
    real = os.path.realpath(absolute)
    if len(_resolved) >= RESOLVED_MAX:
        _resolved.clear()
    _resolved[absolute] = (now, real)
    return real


def canonical_root(root):
    return os.path.normcase(display_root(root))


def project_id(root):
    return hashlib.sha256(canonical_root(root).encode("utf-8")).hexdigest()[:16]


def legacy_ids(root):
    """Chaves antigas conhecidas; a migração copia os dados e preserva a origem."""
    variants = [os.path.abspath(os.fspath(root)), display_root(root),
                os.path.normcase(os.path.abspath(os.fspath(root)))]
    return list(dict.fromkeys(hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
                              for path in variants))


def within_root(root, candidate):
    try:
        base = canonical_root(root)
        resolved = canonical_root(candidate)
        return os.path.commonpath([base, resolved]) == base
    except (OSError, ValueError, TypeError):
        return False


def state_path(path, state_dir):
    """Valida o alvo antes de remover/substituir arquivos de estado."""
    absolute = os.path.abspath(path)
    if not within_root(state_dir, absolute) or canonical_root(absolute) == canonical_root(state_dir):
        raise ValueError("Operation refused: path is outside the state directory.")
    return absolute
