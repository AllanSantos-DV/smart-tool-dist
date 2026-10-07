#!/usr/bin/env python3
"""Lifecycle do daemon HTTP do Smart Tool: self-start com lock. Ao contrário do Brain
Server (client-pure, nunca spawna, depende de instalador externo — o daemon que está
caído neste ambiente por isso mesmo), este cliente sobe o processo sozinho se estiver
fora do ar, protegido por lockfile contra corrida entre sessões simultâneas.

Contrato de registry (mesmo formato usado pelo probe de grafo em `capabilities.py`):
`~/.smart-tool/data/daemon.json` = `{"url": "http://127.0.0.1:<porta>"}` —
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

RUN_DIR = paths.DATA_DIR
REGISTRY_PATH = os.path.join(RUN_DIR, "daemon.json")
LOCK_PATH = os.path.join(RUN_DIR, "daemon.lock")
LOCK_STALE_SECONDS = 30
DEFAULT_PORT = int(os.environ.get("SMART_TOOL_PORT", "8765"))
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
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
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
            command = (f'Chr(34) & "{executable}" & Chr(34) & " " & '
                       f'Chr(34) & "{DAEMON_SCRIPT}" & Chr(34)')
            with os.fdopen(fd, "w", encoding="utf-16") as stream:
                stream.write('Set sh = CreateObject("WScript.Shell")\n')
                stream.write(f'sh.CurrentDirectory = "{Path(DAEMON_SCRIPT).parent}"\n')
                stream.write(f'sh.Run {command}, 0, False\n')
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


def ensure_daemon_running(wait_seconds=10):
    """Retorna a URL base do daemon (sem `/mcp`), subindo o processo se necessário."""
    info = read_registry()
    if info and health(info["url"]):
        return info["url"]

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
    if _acquire_lock():
        # O lock NÃO é liberado aqui: ele é o freio de taxa. Expirando por idade
        # (`LOCK_STALE_SECONDS`), um daemon que não consegue subir — porta tomada, import
        # quebrado — resulta em uma tentativa a cada 30s, e não num processo órfão por
        # tool call. Quem perde a corrida do bind morre sozinho (`allow_reuse_address`
        # False), então soltar o lock não acrescentaria segurança nenhuma.
        _spawn_daemon()
    return False


def _wait_for_daemon(wait_seconds, poll_interval=0.1):
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        info = read_registry()
        if info and health(info["url"], timeout=0.5):
            return info["url"]
        time.sleep(poll_interval)
    return None
