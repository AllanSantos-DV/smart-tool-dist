"""Newer Smart Tool release on npm and the update itself: the tray checks npm at start and every 24 h and, when
automatic updates are on (the default), runs the same install command a user would, in a visible console. A failed
version is tried again after 24 h. What happened (newer version available, updated, update failed) is told to the
user by the tray and to agent sessions by the daemon, from the state file, without contacting npm again.

Automatic updates are turned off by the tray switch (config `auto_update`) or by SMART_TOOL_AUTO_UPDATE=off; the
newer-version notice still shows then, with the command to update by hand."""
import json
import os
import subprocess
import time
import urllib.request

import atomic_io
import config
import paths
import version

PACKAGE = "@allansantos-dev/smart-tool"
REGISTRY_URL = "https://registry.npmjs.org/@allansantos-dev%2Fsmart-tool/latest"
UPDATE_COMMAND = f"npx --yes {PACKAGE}@latest install"
CHANGELOG_URL = "https://github.com/AllanSantos-DV/smart-tool-dist/blob/main/CHANGELOG.md"
STATE_PATH = os.path.join(paths.DATA_DIR, "update-state.json")
ENV_OPT_OUT = "SMART_TOOL_AUTO_UPDATE"
RETRY_AFTER_S = 24 * 3600
UPDATED_NOTICE_S = 24 * 3600


def _parts(value):
    return tuple(int(part) for part in str(value).split("-")[0].split("."))


def newer_version(timeout=10):
    """The latest version on npm when it is newer than the installed one, else None."""
    request = urllib.request.Request(REGISTRY_URL, headers={"User-Agent": version.user_agent(),
                                                            "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        latest = json.load(response)["version"]
    return latest if _parts(latest) > _parts(version.VERSION) else None


def env_opted_out():
    """SMART_TOOL_AUTO_UPDATE set to off, 0, false or no."""
    return os.environ.get(ENV_OPT_OUT, "").strip().lower() in ("off", "0", "false", "no")


def automatic_enabled(cfg=None):
    """Automatic updates are on: the tray switch (config `auto_update`, on by default) and no environment opt-out."""
    return not env_opted_out() and config.auto_update(cfg)


def _state():
    try:
        with open(STATE_PATH, encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return {}


def _save(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    atomic_io.write_secret_text(STATE_PATH, json.dumps(state))


def attempted():
    """Version the last automatic update went for, or None."""
    return _state().get("attempted")


def should_auto_update(latest, now=None):
    """An automatic update runs once per version and again 24 h after a failed try: when it fails, the installer
    restores the previous version and restarts the tray, which would otherwise try the same version on every start."""
    if not latest:
        return False
    state = _state()
    if state.get("attempted") != latest:
        return True
    return (now or time.time()) - state.get("at", 0) >= RETRY_AFTER_S


def record_check(latest):
    """Stores the result of an npm check (the newer version, or None) for the notices read without network."""
    state = _state()
    state.update(latest=latest, checked_at=time.time())
    _save(state)


def note_running_version(now=None):
    """Called when the tray and the daemon start: a version different from the one that ran last is recorded as an
    update (from, to, at) for the updated notice; the very first run records nothing. Returns the update record when
    this call found it, else None."""
    state = _state()
    running = state.get("running")
    if running == version.VERSION:
        return None
    state["running"] = version.VERSION
    found = None
    if running:
        found = state["updated"] = {"from": running, "to": version.VERSION, "at": now or time.time()}
    _save(state)
    return found


def failed_update():
    """The version an automatic update went for and did not install (the previous version is still running), else
    None."""
    target = _state().get("attempted")
    return target if target and _parts(target) > _parts(version.VERSION) else None


def notices(now=None):
    """[(key, text)] about updates, for the user and the agent: Smart Tool updated in the last 24 h, the last
    automatic update failed, or a newer version is on npm. Reads only the state file."""
    now = now or time.time()
    state = _state()
    found = []
    updated = state.get("updated") or {}
    if updated.get("to") == version.VERSION and now - updated.get("at", 0) < UPDATED_NOTICE_S:
        found.append((f"updated:{version.VERSION}",
                      f"Smart Tool was updated from {updated['from']} to {version.VERSION}. Changes: {CHANGELOG_URL}"))
    failed = failed_update()
    latest = state.get("latest")
    if failed:
        retry = ("the tray tries again 24 h after the failed try" if automatic_enabled()
                 else "automatic updates are now off")
        found.append((f"failed:{failed}",
                      f"The automatic update of Smart Tool to {failed} failed; {version.VERSION} is still installed "
                      f"and {retry}. To update now, run: {UPDATE_COMMAND}"))
    elif latest and _parts(latest) > _parts(version.VERSION):
        how = ("the tray installs it automatically" if automatic_enabled()
               else f"automatic updates are off (tray switch or {ENV_OPT_OUT}=off); to update, run: {UPDATE_COMMAND}")
        found.append((f"available:{latest}",
                      f"Smart Tool {latest} is available on npm (installed: {version.VERSION}); {how}."))
    return found


def run_update(latest, automatic):
    """Starts the install command in its own visible console, detached from the tray (the installer stops and restarts
    the tray). The console closes on success and stays open with the error on failure."""
    if automatic:
        state = _state()
        state.update(attempted=latest, at=time.time())
        _save(state)
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    command = f'title Smart Tool update to {latest} && {UPDATE_COMMAND} || pause'
    subprocess.Popen(["cmd.exe", "/c", command], creationflags=flags, close_fds=True,
                     cwd=os.path.expanduser("~"))
