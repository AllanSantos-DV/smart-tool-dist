#!/usr/bin/env python3
"""Gravação atômica de arquivo pequeno de estado/credencial, compartilhada pelos módulos
do Smart Tool."""
import glob
import os
import time
import uuid

_REPLACE_ATTEMPTS = 4
_REPLACE_BACKOFF_S = 0.05
TMP_SUFFIX = ".tmp."
ORPHAN_AGE_S = 300


def rotate(path, max_bytes):
    """Keeps one previous generation (`path.1`) once an append-only log reaches max_bytes. Best effort: a log
    locked by another process is rotated on a later append, and the append itself still happens."""
    try:
        if os.path.getsize(path) >= max_bytes:
            os.replace(path, path + ".1")
    except OSError:
        pass


def atomic_replace(tmp_path, final_path):
    """`os.replace` com retry curto: no Windows, `MoveFileEx` falha com `PermissionError`
    enquanto outro processo mantém o destino aberto — e estes arquivos têm vários leitores
    concorrentes (hook do router, tray, configure.py). A janela é de
    milissegundos, então insistir resolve; a exceção só sobe na última tentativa."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp_path, final_path)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_S)


def tmp_path_for(final_path):
    """Sufixo único por chamada (não por pid): duas threads/processos podem gravar o mesmo
    arquivo quase ao mesmo tempo."""
    return final_path + TMP_SUFFIX + uuid.uuid4().hex


def sweep_orphans(final_path, max_age_s=ORPHAN_AGE_S):
    """Remove tmp de gravação interrompida (kill -9, queda de energia) do mesmo arquivo.
    Esses tmp carregam o mesmo conteúdo do destino — credencial, no caso — com nome
    aleatório e ninguém mais os apagaria. Best-effort: nunca impede a gravação em curso."""
    limite = time.time() - max_age_s
    for candidate in glob.glob(glob.escape(final_path) + TMP_SUFFIX + "*"):
        try:
            if os.path.getmtime(candidate) < limite:
                os.remove(candidate)
        except OSError:
            pass


def _write_and_replace(final_path, data, encoding=None):
    sweep_orphans(final_path)
    tmp_path = tmp_path_for(final_path)
    try:
        # `O_EXCL` garante tmp novo (nunca aproveita um abandonado, que pode ser de outro
        # conteúdo); `0o600` é o pedido de permissão restrita onde o SO honra (no Windows
        # só liga o atributo read-only — lá a confidencialidade vem da ACL do diretório).
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp_path, flags, 0o600)
        mode = "wb" if encoding is None else "w"
        extra = {} if encoding is None else {"encoding": encoding, "newline": ""}
        with os.fdopen(fd, mode, **extra) as f:
            f.write(data)
            # `os.replace` só garante que o nome aparece inteiro; sem o flush+fsync uma
            # queda de energia pode persistir o rename sem os dados e deixar o destino —
            # que era o único lugar com o conteúdo anterior — vazio ou com lixo.
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, final_path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_secret_bytes(final_path, data):
    """Grava bytes em `final_path` com o tmp restrito, `fsync` dos dados antes do rename
    (a durabilidade da entrada de diretório fica com o SO) e replace atômico."""
    _write_and_replace(final_path, data)


def write_secret_text(final_path, text, encoding="utf-8"):
    """Mesmo contrato de `write_secret_bytes`, para conteúdo textual (JSON de token)."""
    _write_and_replace(final_path, text, encoding=encoding)
