#!/usr/bin/env python3
"""Lifecycle do daemon HTTP do Smart Tool: self-start com lock. Ao contrário do Brain
Server (client-pure, nunca spawna, depende de instalador externo — o daemon que está
caído neste ambiente por isso mesmo), este cliente sobe o processo sozinho se estiver
fora do ar, protegido por lockfile contra corrida entre sessões simultâneas.

Contrato de registry (mesmo formato usado pelo probe de grafo em `capabilities.py`):
`~/.smart-tool/data/daemon.json` = `{"url": "http://127.0.0.1:<porta>", "pid": <pid>, "port": <porta>}` —
`url` é a base do daemon; o endpoint MCP em si é `<url>/mcp`.
"""
import json
import os
import paths
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import venv_launch

RUN_DIR = paths.DATA_DIR
REGISTRY_PATH = os.path.join(RUN_DIR, "daemon.json")
LOG_PATH = os.path.join(RUN_DIR, "daemon.log")
SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INFINITE = 0xFFFFFFFF
LOCK_PATH = os.path.join(RUN_DIR, "daemon.lock")
LOCK_STALE_SECONDS = 30
UPDATE_MARKER = os.path.join(RUN_DIR, "updating")
UPDATE_STALE_SECONDS = 900
DAEMON_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smart_tool_daemon.py")


def read_registry():
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
            info = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if isinstance(info, dict) and info.get("url"):
        return info
    return None


def health(url, timeout=1):
    """Timeout curto porque só se fala com o loopback: um daemon vivo responde `/health`
    em milissegundos (o servidor é multi-thread, indexação em curso não bloqueia), e o
    caso lento é justamente o daemon morto cuja URL ainda está no registry — tempo que
    toda tool call do agente paga antes de seguir."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout) as resp:
            return resp.status == 200 and json.loads(resp.read()).get("status") == "ok"
    except (urllib.error.URLError, OSError, ValueError, AttributeError):
        return False


def _acquire_lock():
    os.makedirs(RUN_DIR, exist_ok=True)
    try:
        if time.time() - os.path.getmtime(LOCK_PATH) > LOCK_STALE_SECONDS:
            os.remove(LOCK_PATH)
    except OSError:
        pass
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


def _spawn_daemon():
    executable = Path(sys.executable)
    if sys.platform == "win32":
        # O venv pythonw.exe pode morrer sem iniciar o daemon. O python.exe iniciado
        # direto abre uma aba no Windows Terminal; fechá-la mata o serviço e o tray
        # tenta subir outra a cada polling. WScript cria um processo independente com
        # a janela oculta, como no Passtrough.
        if executable.name.lower() == "pythonw.exe":
            executable = executable.with_name("python.exe")
        fd, launcher = tempfile.mkstemp(prefix="smart-tool-hidden-", suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-16") as stream:
                stream.write(venv_launch.hidden_run_vbs(executable, DAEMON_SCRIPT, Path(DAEMON_SCRIPT).parent))
            result = subprocess.run(
                ["wscript.exe", "//B", "//NoLogo", launcher],
                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise RuntimeError("Windows could not start the Smart Tool daemon without a window.")
        finally:
            Path(launcher).unlink(missing_ok=True)
        return
    subprocess.Popen(
        [str(executable), DAEMON_SCRIPT],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
    )


def updating():
    """Whether the installer is replacing the files (its marker, younger than UPDATE_STALE_SECONDS): a hook or the
    tray starting the daemon then would run half-copied code and hold the port the installer waits for."""
    try:
        return time.time() - os.path.getmtime(UPDATE_MARKER) < UPDATE_STALE_SECONDS
    except OSError:
        return False


def ensure_daemon_running(wait_seconds=20):
    """Retorna a URL base do daemon (sem `/mcp`), subindo o processo se necessário."""
    info = read_registry()
    if info and health(info["url"]):
        return info["url"]
    if updating():
        raise RuntimeError("Smart Tool is being updated; the daemon starts again when the update finishes.")

    if not _acquire_lock():
        url = _wait_for_daemon(wait_seconds)
        if url:
            return url
        raise RuntimeError("Timed out waiting for another session to start the Smart Tool daemon")

    try:
        _spawn_daemon()
        url = _wait_for_daemon(wait_seconds)
        if url:
            return url
        raise RuntimeError("Timed out starting the Smart Tool daemon")
    finally:
        _release_lock()


def daemon_url_if_up():
    info = read_registry()
    if info and health(info["url"], timeout=0.5):
        return info["url"]
    return None


def start_daemon_if_down():
    """`True` se o daemon já responde; `False` se acabou de disparar o start, sem esperar.

    Para quem está num caminho sensível a latência (o hook PreToolUse roda antes de toda
    Grep/Glob/Read/Bash): esperar o processo subir pode custar segundos — spawn de
    processo em máquina com EDR varia muito — e quem chama tem um desfecho válido sem o
    daemon. A garantia de daemon único não depende do lock ser mantido aqui: um segundo
    processo falha no bind exclusivo da porta e morre sozinho."""
    if daemon_url_if_up():
        return True
    if updating():
        return False
    if _acquire_lock():
        # O lock NÃO é liberado aqui: ele é o freio de taxa. Expirando por idade
        # (`LOCK_STALE_SECONDS`), um daemon que não consegue subir — porta tomada, import
        # quebrado — resulta em uma tentativa a cada 30s, e não num processo órfão por
        # tool call. Quem perde a corrida do bind morre sozinho (`allow_reuse_address`
        # False), então soltar o lock não acrescentaria segurança nenhuma.
        _spawn_daemon()
    return False


def note_stop(message):
    """Appends a stop line to the daemon log, for stops the daemon cannot write itself: the installer closing it and
    the supervisor seeing it end without a word (killed, crashed)."""
    os.makedirs(RUN_DIR, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as stream:
        stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")


def wait_for_exit(pid):
    """Blocks until process `pid` ends and returns its exit code; None when it cannot be watched (already gone, not
    ours to open, or not Windows), so the caller falls back to polling."""
    if os.name != "nt" or not pid:
        return None
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        kernel32.WaitForSingleObject(wintypes.HANDLE(handle), INFINITE)
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code)):
            return None
        return code.value
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))


def _wait_for_daemon(wait_seconds, poll_interval=0.1):
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        info = read_registry()
        if info and health(info["url"], timeout=0.5):
            return info["url"]
        time.sleep(poll_interval)
    return None
