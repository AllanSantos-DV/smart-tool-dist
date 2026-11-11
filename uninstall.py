#!/usr/bin/env python3
"""Silent uninstall: removes what install.py and the agent-side setup added, never a user's project folder.

Usage: python uninstall.py [--target DIR] [--keep-data]   (npm: npx @allansantos-dev/smart-tool@<version> uninstall)

It never asks anything. Exit code 0 means everything is gone (or was never there); 1 means a step failed, each failure
named on stderr, and the steps that could run did run.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import autostart
import browser_control
import client_hooks
import endpoint_sync
import install
import paths

CLIENTS = ("claude", "codex")
RUNTIME_DIR = Path(paths.HOME_DIR) / "runtime"
DELETE_RETRY_S = 15


def _delete(folder):
    """Deletes folder, retrying while a process that was just stopped still holds a file; raises the last OSError."""
    deadline = time.monotonic() + DELETE_RETRY_S
    while True:
        try:
            shutil.rmtree(folder)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def _inside(path, folder):
    try:
        return Path(path).resolve().is_relative_to(Path(folder).resolve())
    except OSError:
        return False


def _delete_after_exit(folders):
    """Windows cannot delete the interpreter that is running this script: a detached, windowless cmd removes folders
    once the process has ended."""
    commands = " & ".join(f'rmdir /s /q "{folder}"' for folder in folders)
    subprocess.Popen(["cmd.exe", "/c", f"ping -n 4 127.0.0.1 >nul & {commands}"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW)


def remove_hooks():
    """Our hook entries (redirect and session-start) out of Claude Code's and Codex's hooks files."""
    for client in CLIENTS:
        for result in (client_hooks.remove(client), client_hooks.audit_remove(client)):
            if result["changed"]:
                print(f"Hooks removed from {result['file']} (backup {result['backup']}).")


def remove_registrations():
    """The smart-tool MCP entry, and the playwright one Smart Tool registered, out of each client."""
    for client in CLIENTS:
        print(endpoint_sync.unregister(client))
        print(browser_control.unregister(client))


def remove_state(keep_data):
    """~/.smart-tool: everything unless keep_data; the downloaded runtime always, afterwards when this very
    interpreter runs from it."""
    home = Path(paths.HOME_DIR)
    if not home.is_dir():
        return
    pending = []
    for entry in sorted(home.iterdir()):
        if entry == RUNTIME_DIR:
            if _inside(sys.executable, entry):
                pending.append(entry)
            else:
                _delete(entry)
        elif not keep_data:
            _delete(entry) if entry.is_dir() else entry.unlink()
    if not keep_data and not pending and not any(home.iterdir()):
        home.rmdir()
    if pending:
        _delete_after_exit([home] if not keep_data else pending)
        print(f"{pending[0]} is in use by this run; it is removed as soon as the run ends.")


def run(target, keep_data):
    """Runs every step, collecting failures instead of stopping at the first. Returns the list of failure messages."""
    failures = []
    steps = [("stop the running Smart Tool", lambda: install.stop_processes(target)),
             ("remove the autostart", lambda: print("Autostart removed:", autostart.remove()["removed"])),
             ("remove the agent hooks", remove_hooks),
             ("remove the MCP registrations", remove_registrations)]
    install.mark_updating(True)
    try:
        for name, step in steps:
            try:
                step()
            except Exception as exc:
                failures.append(f"Could not {name}: {type(exc).__name__}: {exc}")
        if failures:
            return failures
        for name, step in (("delete the program folder", lambda: _delete(target)),
                           ("delete the old copies beside it", lambda: install.prune_backups(target)),
                           ("delete Smart Tool's state", lambda: remove_state(keep_data))):
            try:
                step()
            except Exception as exc:
                failures.append(f"Could not {name}: {type(exc).__name__}: {exc}")
    finally:
        if keep_data or failures:
            install.mark_updating(False)
    return failures


def main():
    parser = argparse.ArgumentParser(description="Uninstall Smart Tool silently.")
    parser.add_argument("--target", type=Path, default=install.DEFAULT_TARGET)
    parser.add_argument("--keep-data", action="store_true", help="keep ~/.smart-tool (settings, indexes, saved keys)")
    args = parser.parse_args()
    paths.refuse_overridden_folders("uninstall")
    target = args.target.resolve()
    if target == install.SOURCE:
        raise SystemExit("The target cannot be the folder this script runs from.")
    failures = run(target, args.keep_data)
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        raise SystemExit(1)
    print(f"Smart Tool removed from {target}.")


if __name__ == "__main__":
    main()
