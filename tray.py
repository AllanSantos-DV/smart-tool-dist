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
import urllib.error
import urllib.request
import webbrowser

from PIL import Image, ImageDraw
import pystray

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autostart
import config
import daemon_launcher
import i18n
import paths
import setup_audit
import toast_notice
import update_check
import version

POLL_INTERVAL_S = 45
UPDATE_CHECK_INTERVAL_S = 24 * 3600
NOTICE_INTERVAL_S = 600
NOTICE_RETRY_S = 15
UPDATE_RETRY_S = 300
NOTICE_UNREACHABLE_LOG_S = 600
LOG_PATH = os.path.join(paths.DATA_DIR, "tray.log")
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
    "ok": i18n.N_("Smart Tool - running"),
    "starting": i18n.N_("Smart Tool - starting..."),
    "needs_key": i18n.N_("Smart Tool - model gateway unavailable"),
    "needs_models": i18n.N_("Smart Tool - choose the models in settings"),
    "catalog_unavailable": i18n.N_("Smart Tool - model catalog unavailable"),
    "config_corrupt": i18n.N_("Smart Tool - settings file has an error"),
    "status_error": i18n.N_("Smart Tool - failed to read the settings"),
    "down": i18n.N_("Smart Tool - stopped"),
}


def _set_state(health, icon=None):
    with _STATE_LOCK:
        if _state["health"] == health:
            return
        _state["health"] = health
    if icon is not None:
        icon.icon = _ICONS[health]
        icon.title = i18n.t(_TITLES[health])


def _notify(icon, text, buttons=(), **values):
    """Windows notification, translated (text is an English catalog key; buttons are (catalog key, protocol link)). A
    toast under Smart Tool's own app id, since the tray balloon is shown as "Python"; when Windows refuses the toast,
    the balloon carries the text and the refusal."""
    message = i18n.t(text, **values)
    try:
        toast_notice.show("Smart Tool", [message], [(i18n.t(label), uri) for label, uri in buttons])
    except Exception as exc:
        icon.notify(f"{message} ({exc})", "Smart Tool")


def _follow_language(icon):
    """Redraws the menu and the tooltip after the language changed here or in a settings page."""
    try:
        current = i18n.language()
    except Exception:
        return
    if _state.get("language") != current:
        _state["language"] = current
        icon.title = i18n.t(_TITLES[_state["health"]])
        icon.update_menu()


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
        _follow_language(icon)
        time.sleep(POLL_INTERVAL_S)


def _show_notices(icon, notices):
    """Shows a toast for each pending notice: the new-default-models offer, each model over the failure limit and each
    project whose scheduled setup audit found new errors or warnings."""
    offer = notices.get("upgrade")
    if offer:
        lines = [f"{role}: {change['current']} -> {change['new']}" for role, change in offer["changes"].items()]
        nonce = offer["nonce"]
        toast_notice.show(i18n.t("Smart Tool has new recommended models"), lines, [
            (i18n.t("Apply now"), toast_notice.action_uri("models-apply", n=nonce)),
            (i18n.t("Don't show again"), toast_notice.action_uri("models-decline", n=nonce)),
        ], tag="models-upgrade")
    for row in notices.get("failing") or []:
        last = row.get("last_error") or i18n.t("see the failure log in the settings")
        toast_notice.show(
            i18n.t("{model} is failing", model=row["model"]),
            [i18n.t("{errors} of {calls} calls failed in 24 h as {role}.", errors=row["errors"], calls=row["calls"],
                    role=row["role"]), last[:160]],
            [(i18n.t("Switch model"), toast_notice.action_uri("open-setup", section="models")),
             (i18n.t("Mute 7 days"), toast_notice.action_uri("mute-model", model=row["model"]))],
            tag="model-failing:" + row["model"])
    for row in notices.get("audit") or []:
        title, lines = setup_audit.toast_text(row)
        toast_notice.show(title, lines, [(i18n.t("Open the audit"), toast_notice.action_uri("open-setup", section="audit"))],
                          tag="setup-audit:" + row["project"])


def _log(text):
    """Appends a line to `tray.log` in the data folder: what the tray cannot tell with a notification (a notice that could
    not be fetched or shown) is kept where the user can read it, never turned into another notification."""
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass  # no console and no other channel here; a full disk must not kill the notice thread


def _daemon_not_ready(exc):
    """The daemon is not accepting connections yet (refused, reset or no answer in time), as right after boot, when the
    registry still holds the address of the previous run. An HTTP answer, even an error, is not that."""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    return isinstance(reason, (ConnectionError, TimeoutError))


def _notice_loop(icon):
    """Asks the daemon every 10 minutes whether a toast is due. The daemon decides (once a day per notice) and marks it
    announced, so a restart of the tray never repeats one. While the daemon is still starting the question is asked again
    every few seconds and nothing is shown: the tray icon already says "starting". Any other failure goes to `tray.log`."""
    waiting_since = None
    while True:
        delay = NOTICE_INTERVAL_S
        try:
            daemon_url = (daemon_launcher.read_registry() or {}).get("url")
            if daemon_url:
                request = urllib.request.Request(daemon_url.rstrip("/") + "/setup/notices", data=b"{}", method="POST",
                                                 headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    notices = json.load(response)
                waiting_since = None
                _show_notices(icon, notices)
            else:
                delay = NOTICE_RETRY_S
        except Exception as exc:
            if _daemon_not_ready(exc):
                delay = NOTICE_RETRY_S
                waiting_since = waiting_since if waiting_since is not None else time.monotonic()
                if time.monotonic() - waiting_since >= NOTICE_UNREACHABLE_LOG_S and not _state.get("notice_unreachable"):
                    _state["notice_unreachable"] = True
                    _log(f"notices: the daemon has not answered for {NOTICE_UNREACHABLE_LOG_S // 60} min: {exc}")
            elif str(exc) != _state.get("notice_error"):
                _state["notice_error"] = str(exc)
                _log(f"notices: could not fetch or show them: {type(exc).__name__}: {exc}")
        time.sleep(delay)


def _announce_start(icon):
    """Tells the user, once per start, that Smart Tool was just updated or that the last automatic update failed."""
    updated = update_check.update_for_tray()
    if updated:
        changes = (i18n.N_("What changed"), toast_notice.action_uri("open-changes"))
        _notify(icon, "Smart Tool updated from {old} to {new}.", [changes], old=updated["from"], new=updated["to"])
    failed = update_check.failed_update()
    if failed:
        log = (i18n.N_("View log"), toast_notice.action_uri("open-update-log"))
        _notify(icon, "The update to {target} failed; Smart Tool {version} is still installed. It is tried again in "
                "24 h, or now from this menu.", [log], target=failed, version=version.VERSION)


def _update_loop(icon):
    """Checks npm at start and every 24 h (every 5 min while npm cannot be reached, as when the network is not up yet right
    after boot; nothing is shown for that). A newer version is installed right away when automatic updates are on (once
    per version, again 24 h after a failed try); otherwise the user is told and the menu offers it."""
    try:
        _announce_start(icon)
    except Exception as exc:
        _notify(icon, "Could not read the update state: {error}", error=exc)
    while True:
        unreachable = False
        try:
            latest = update_check.newer_version()
        except Exception:
            latest = None
            unreachable = True
        else:
            try:
                update_check.record_check(latest)
            except Exception as exc:
                _notify(icon, "Could not save the update check: {error}", error=exc)
        if latest:
            known = latest == _state["update"]
            _state["update"] = latest
            icon.update_menu()
            try:
                automatic = update_check.automatic_enabled() and update_check.should_auto_update(latest)
            except Exception as exc:
                automatic = False
                _notify(icon, "Automatic update skipped: {error}", error=exc)
            if automatic:
                _notify(icon, "Updating Smart Tool from {old} to {new}...", old=version.VERSION, new=latest)
                update_check.run_update(latest, automatic=True)
            elif not known:
                _notify(icon, "Smart Tool {version} is available: use \"Update to {version}\" in this menu.",
                        version=latest)
        time.sleep(UPDATE_RETRY_S if unreachable else UPDATE_CHECK_INTERVAL_S)


def _update_label(item):
    return i18n.t("Update to {version}", version=_state["update"]) if _state["update"] else i18n.t("Check for updates")


_update_busy = threading.Lock()


def _update_now(icon, item):
    """Menu action: installs the known newer version, or checks npm first (telling the user it is checking) and says
    when there is none."""
    if not _update_busy.acquire(blocking=False):
        return

    def run():
        try:
            latest = _state["update"]
            if not latest:
                _notify(icon, "Checking for updates…")
                latest = update_check.newer_version()
            if not latest:
                _notify(icon, "Smart Tool {version} is the latest version.", version=version.VERSION)
                return
            _state["update"] = latest
            icon.update_menu()
            _notify(icon, "Updating Smart Tool from {old} to {new}...", old=version.VERSION, new=latest)
            update_check.run_update(latest, automatic=False)
        except Exception as exc:
            _notify(icon, "Could not update: {error}", error=exc)
        finally:
            _update_busy.release()
    threading.Thread(target=run, daemon=True).start()


def _auto_update_checked(item):
    try:
        return update_check.automatic_enabled()
    except Exception:
        return False


def _toggle_auto_update(icon, item):
    if update_check.env_opted_out():
        _notify(icon, "{name}=off is set: automatic updates stay off until it is removed.", name=update_check.ENV_OPT_OUT)
        return
    try:
        config.update_config(lambda cfg: cfg.__setitem__("auto_update", not config.auto_update(cfg)))
    except Exception as exc:
        _notify(icon, "Could not change automatic updates: {error}", error=exc)
    icon.update_menu()


def _language_checked(lang):
    def checked(item):
        try:
            return i18n.language() == lang
        except Exception:
            return False
    return checked


def _choose_language(lang):
    def choose(icon, item):
        try:
            i18n.set_language(lang)
        except Exception as exc:
            _notify(icon, "Could not change the language: {error}", error=exc)
        _follow_language(icon)
    return choose


def _open_setup(icon, item):
    registry = daemon_launcher.read_registry()
    webbrowser.open((registry["url"].rstrip("/") + "/setup") if registry else SETUP_URL)


def _open_projects(icon, item):
    def run():
        try:
            url = daemon_launcher.ensure_daemon_running()
            webbrowser.open(url.rstrip("/") + "/projects")
        except Exception as exc:
            _notify(icon, "Could not open the projects: {error}", error=i18n.t(str(exc)))
    threading.Thread(target=run, daemon=True).start()


def _add_project(icon, item):
    def run():
        try:
            import folder_picker
            folder_picker.register_from_tray()
        except Exception as exc:
            _notify(icon, "Could not select the folder: {error}", error=exc)
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
                    _notify(icon, "Scheduled Task unavailable ({reason}); using the Startup folder.",
                            reason=result["fallback_reason"])
        except Exception as exc:
            _notify(icon, "Failed to change automatic start: {error}", error=exc)
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
    _state["language"] = i18n.language()
    icon = pystray.Icon(
        "smart-tool",
        icon=_ICONS["starting"],
        title=i18n.t(_TITLES["starting"]),
        menu=pystray.Menu(
            pystray.MenuItem(lambda item: i18n.t("Projects and indexing…"), _open_projects, default=True),
            pystray.MenuItem(lambda item: i18n.t("Index a folder…"), _add_project),
            pystray.MenuItem(lambda item: i18n.t("Open settings"), _open_setup),
            pystray.MenuItem(lambda item: i18n.t("Start with Windows"), _toggle_autostart, checked=_autostart_checked),
            pystray.MenuItem(_update_label, _update_now),
            pystray.MenuItem(lambda item: i18n.t("Update automatically"), _toggle_auto_update,
                             checked=_auto_update_checked),
            pystray.MenuItem("Idioma / Language", pystray.Menu(
                pystray.MenuItem("Português (Brasil)", _choose_language("pt-BR"), checked=_language_checked("pt-BR"),
                                 radio=True),
                pystray.MenuItem("English", _choose_language("en"), checked=_language_checked("en"), radio=True),
            )),
            pystray.MenuItem(lambda item: i18n.t("Quit"), _quit),
        ),
    )
    threading.Thread(target=_poll_loop, args=(icon,), daemon=True).start()
    threading.Thread(target=_update_loop, args=(icon,), daemon=True).start()
    threading.Thread(target=_notice_loop, args=(icon,), daemon=True).start()
    try:
        icon.run()
    finally:
        if handle:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


if __name__ == "__main__":
    main()
