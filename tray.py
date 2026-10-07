#!/usr/bin/env python3
"""Ícone na bandeja do sistema do Smart Tool — visibilidade de que o daemon está de
pé, e ponto único de "se caiu, sobe de novo" que não depende de nenhum host MCP
cooperar. Processo separado do daemon (mesma relação que `daemon_launcher` já tem com
`smart_tool_daemon.py`): matar o tray não mata o daemon.

Uso: `pythonw.exe tray.py` (sem console) — entrypoint apontado pelo mecanismo de
autostart (Tarefa Agendada ou atalho de inicialização).
"""
import json
import os
import sys
import threading
import time
import urllib.request
import webbrowser

from PIL import Image, ImageDraw
import pystray

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autostart
import daemon_launcher
import update_check

POLL_INTERVAL_S = 45
UPDATE_CHECK_INTERVAL_S = 24 * 3600
SETUP_URL = "http://127.0.0.1:8765/setup"

_STATE_LOCK = threading.Lock()
_state = {"health": "starting", "update": None}


def _make_icon_image(color):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 8
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color)
    return img


_GREEN = _make_icon_image((47, 138, 76, 255))
_YELLOW = _make_icon_image((219, 164, 26, 255))
_RED = _make_icon_image((194, 59, 59, 255))
_ICONS = {
    "ok": _GREEN,
    "starting": _YELLOW,
    "needs_key": _YELLOW,
    "needs_models": _YELLOW,
    "catalog_unavailable": _YELLOW,
    "config_corrupt": _YELLOW,
    "status_error": _YELLOW,
    "down": _RED,
}

_TITLES = {
    "ok": "Smart Tool - daemon running",
    "starting": "Smart Tool - starting the daemon...",
    "needs_key": "Smart Tool - model gateway unavailable",
    "needs_models": "Smart Tool - choose the models in settings",
    "catalog_unavailable": "Smart Tool - model catalog unavailable",
    "config_corrupt": "Smart Tool - settings file has an error",
    "status_error": "Smart Tool - failed to read the settings",
    "down": "Smart Tool - daemon down",
}


def _set_state(health, icon=None):
    with _STATE_LOCK:
        if _state["health"] == health:
            return
        _state["health"] = health
    if icon is not None:
        icon.icon = _ICONS[health]
        icon.title = _TITLES[health]


def _configuration_state(url):
    with urllib.request.urlopen(url.rstrip("/") + "/setup/status", timeout=5) as response:
        payload = json.load(response)
    return payload["models_status"]


def _indicator_state(model_status):
    if model_status == "ready":
        return "ok"
    return model_status if model_status in _TITLES else "status_error"


def _poll_loop(icon):
    while True:
        try:
            url = daemon_launcher.read_registry()
            # Timeout maior que o default: aqui um falso negativo custa um spawn inútil de
            # daemon, e o polling é a cada 45s — nada espera por esta resposta.
            if url and daemon_launcher.health(url["url"], timeout=3):
                daemon_url = url["url"]
            else:
                _set_state("starting", icon)
                daemon_url = daemon_launcher.ensure_daemon_running()
            try:
                status = _configuration_state(daemon_url)
                _set_state(_indicator_state(status), icon)
            except Exception:
                _set_state("status_error", icon)
        except Exception:
            _set_state("down", icon)
        time.sleep(POLL_INTERVAL_S)


def _update_loop(icon):
    while True:
        try:
            latest = update_check.newer_version()
        except Exception:
            latest = None
        if latest and latest != _state["update"]:
            _state["update"] = latest
            icon.update_menu()
            icon.notify(f"Smart Tool {latest} is available. Update with: {update_check.UPDATE_COMMAND}", "Smart Tool")
        time.sleep(UPDATE_CHECK_INTERVAL_S)


def _update_label(item):
    return f"Update to {_state['update']} (copy command)"


def _copy_update_command(icon, item):
    import subprocess
    subprocess.run(["clip.exe"], input=update_check.UPDATE_COMMAND.encode("ascii"), check=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    icon.notify(f"Copied: {update_check.UPDATE_COMMAND}. Paste it in a terminal.", "Smart Tool")


def _open_setup(icon, item):
    registry = daemon_launcher.read_registry()
    webbrowser.open((registry["url"].rstrip("/") + "/setup") if registry else SETUP_URL)


def _open_projects(icon, item):
    def run():
        try:
            url = daemon_launcher.ensure_daemon_running()
            webbrowser.open(url.rstrip("/") + "/projects")
        except Exception as exc:
            icon.notify(f"Could not open the projects: {exc}", "Smart Tool")
    threading.Thread(target=run, daemon=True).start()


def _add_project(icon, item):
    def run():
        try:
            import folder_picker
            folder_picker.register_from_tray()
        except Exception as exc:
            icon.notify(f"Could not select the folder: {exc}", "Smart Tool")
    threading.Thread(target=run, daemon=True).start()


def _autostart_checked(item):
    try:
        return autostart.status()["installed"]
    except Exception:
        return False


_autostart_busy = threading.Lock()


def _toggle_autostart(icon, item):
    if not _autostart_busy.acquire(blocking=False):
        return  # já tem uma troca em andamento — clique duplo ignorado, não enfileirado

    def _run():
        try:
            if autostart.status()["installed"]:
                autostart.remove()
            else:
                result = autostart.install()
                if result.get("method") == "startup_folder" and result.get("fallback_reason"):
                    icon.notify(
                        f"Scheduled Task unavailable ({result['fallback_reason']}); using the Startup folder.",
                        "Smart Tool",
                    )
        except Exception as exc:
            icon.notify(f"Failed to change automatic start: {exc}", "Smart Tool")
        finally:
            _autostart_busy.release()
            icon.update_menu()

    threading.Thread(target=_run, daemon=True).start()


def _quit(icon, item):
    icon.stop()


def _single_instance_handle():
    """Impede dois ícones quando o atalho e o autostart rodam juntos."""
    if sys.platform != "win32":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\SmartToolTray")
    if not handle:
        raise OSError(ctypes.get_last_error(), "Could not start the Smart Tool icon")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    return handle


def main():
    handle = _single_instance_handle()
    if handle is False:
        return
    icon = pystray.Icon(
        "smart-tool",
        icon=_ICONS["starting"],
        title=_TITLES["starting"],
        menu=pystray.Menu(
            pystray.MenuItem("Projects and indexing…", _open_projects, default=True),
            pystray.MenuItem("Index a folder…", _add_project),
            pystray.MenuItem("Open settings", _open_setup),
            pystray.MenuItem("Start with Windows", _toggle_autostart, checked=_autostart_checked),
            pystray.MenuItem(_update_label, _copy_update_command, visible=lambda item: bool(_state["update"])),
            pystray.MenuItem("Quit", _quit),
        ),
    )
    threading.Thread(target=_poll_loop, args=(icon,), daemon=True).start()
    threading.Thread(target=_update_loop, args=(icon,), daemon=True).start()
    try:
        icon.run()
    finally:
        if handle:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


if __name__ == "__main__":
    main()
