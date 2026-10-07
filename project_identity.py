"""Identidade única de raízes e contenção de caminhos, inclusive junctions Windows."""
import hashlib
import os


def display_root(root):
    if not isinstance(root, (str, os.PathLike)) or not str(root).strip():
        raise ValueError("Provide the project folder.")
    return os.path.realpath(os.path.abspath(os.path.expanduser(os.fspath(root))))


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
