"""Newer Smart Tool release on npm and the update itself: the tray runs the same install command a user would, in a
visible console, at startup (once per version, when auto_update is on) or on demand from its menu."""
import json
import os
import subprocess
import time
import urllib.request

import atomic_io
import paths
import version

PACKAGE = "@allansantos-dev/smart-tool"
REGISTRY_URL = "https://registry.npmjs.org/@allansantos-dev%2Fsmart-tool/latest"
UPDATE_COMMAND = f"npx --yes {PACKAGE}@latest install"
STATE_PATH = os.path.join(paths.DATA_DIR, "update-state.json")


def _parts(value):
    return tuple(int(part) for part in str(value).split("-")[0].split("."))


def newer_version(timeout=10):
    """The latest version on npm when it is newer than the installed one, else None."""
    request = urllib.request.Request(REGISTRY_URL, headers={"User-Agent": version.user_agent(),
                                                            "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        latest = json.load(response)["version"]
    return latest if _parts(latest) > _parts(version.VERSION) else None


def attempted():
    """Version the last automatic update went for, or None."""
    try:
        with open(STATE_PATH, encoding="utf-8") as stream:
            return json.load(stream).get("attempted")
    except FileNotFoundError:
        return None


def should_auto_update(latest):
    """An automatic update runs once per version: when it fails, the installer restores the previous version and
    restarts the tray, which would otherwise try the same version again on every start."""
    return bool(latest) and attempted() != latest


def run_update(latest, automatic):
    """Starts the install command in its own visible console, detached from the tray (the installer stops and restarts
    the tray). The console closes on success and stays open with the error on failure."""
    if automatic:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        atomic_io.write_secret_text(STATE_PATH, json.dumps({"attempted": latest, "at": time.time()}))
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    command = f'title Smart Tool update to {latest} && {UPDATE_COMMAND} || pause'
    subprocess.Popen(["cmd.exe", "/c", command], creationflags=flags, close_fds=True,
                     cwd=os.path.expanduser("~"))
