"""Brings the shared engines Smart Tool consumes (the embeddings home and the graph engine) up through the engine
registry: installs one that is missing, updates one that is outdated with the signed asset checked by the registry kit,
and reports why it could not when it cannot. Nothing here raises for a machine without Node, without network or without
the engine: that is a status the searches and the setup screen show.
"""
import glob
import json
import os
import subprocess
import threading
import time

import node_locator

ENGINES = ("casa-de-embeddings", "motor-de-grafo")
HOME_DIRS = {"casa-de-embeddings": os.path.join(os.path.expanduser("~"), ".casa-de-embeddings")}
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_adapters", "node", "engine-home.mjs")
TIMEOUT_S = 900
CHECK_EVERY_S = 24 * 3600
RETRY_AFTER_S = 600

_lock = threading.Lock()
_last = {}
_installing = set()


def forget():
    """Drops what was learned about every engine (tests)."""
    with _lock:
        _last.clear()
        _installing.clear()


def installed(name):
    """Whether the engine `name` has an executable on this machine, running or not (the engine's own folder holds
    `bin/<name>.exe` or one `versions/<version>/<name>.exe`). Raises: ValueError for an engine without a known folder."""
    if name not in HOME_DIRS:
        raise ValueError(f"no home folder is known for {name!r}")
    base = HOME_DIRS[name]
    return (os.path.isfile(os.path.join(base, "bin", f"{name}.exe"))
            or bool(glob.glob(os.path.join(glob.escape(base), "versions", "*", f"{name}.exe"))))


def ensure_in_background(name):
    """Starts `ensure_when_due(name)` on a daemon thread unless one is already running for `name`; the first embedder
    use of a machine without the home calls this instead of installing it at boot. Returns: True when a thread started."""
    with _lock:
        if name in _installing:
            return False
        _installing.add(name)

    def run():
        try:
            ensure_when_due(name)
        except Exception as exc:
            remember(name, {"ok": False, "reason": f"{name}: the background install failed ({type(exc).__name__}: {exc})"})
        finally:
            with _lock:
                _installing.discard(name)

    threading.Thread(target=run, daemon=True, name=f"engine-home-{name}").start()
    return True


def remember(name, status):
    """Keeps `status` as the last answer for `name`, with the time it was kept."""
    with _lock:
        _last[name] = {**status, "at": time.time()}


def status(name):
    """The last status of `name` (`ok`, `url`, `version`, `reason`, `at`), or None when none was asked yet."""
    with _lock:
        return dict(_last[name]) if name in _last else None


def ensure(name):
    """Installs or updates `name`, starts it and waits for it to announce itself.

    Parameters: name (one of ENGINES). Returns: `{"ok": True, "url", "version", ...}` or `{"ok": False, "reason"}`;
    the result is also kept for `status`. Raises: ValueError for a name that is not an engine of this module."""
    if name not in ENGINES:
        raise ValueError(f"unknown engine {name!r}; Smart Tool consumes {' and '.join(reversed(ENGINES))}")
    result = _run(name)
    remember(name, result)
    return result


def _run(name):
    """One run of the Node script for `name`, as a status."""
    node = node_locator.node()
    if not node:
        return {"ok": False, "reason": f"Node.js was not found, and {name} is installed and updated through it: "
                                       "install Node.js or let Smart Tool download its own."}
    try:
        done = subprocess.run([node, SCRIPT, name], capture_output=True, text=True, timeout=TIMEOUT_S,
                              cwd=os.path.dirname(SCRIPT), env=node_locator.environment(prefer=node),
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"{name} did not finish installing or starting in {TIMEOUT_S}s."}
    except OSError as exc:
        return {"ok": False, "reason": f"{name} could not be started through Node ({exc})."}
    if done.returncode:
        tail = (done.stderr.strip().splitlines() or ["no error text"])[-1]
        return {"ok": False, "reason": f"{name}: the install step failed (exit {done.returncode}): {tail[:300]}"}
    try:
        answer = json.loads(done.stdout)
        if not isinstance(answer, dict) or type(answer.get("ok")) is not bool:
            raise ValueError("not a status")
    except ValueError:
        return {"ok": False, "reason": f"{name}: the install step gave an answer that is not a status."}
    return answer


def ensure_when_due(name):
    """`ensure(name)` unless it ran recently: a working engine is looked at again after CHECK_EVERY_S, a failed one after
    RETRY_AFTER_S, so a search never waits on an install that just failed. Returns: the status in force."""
    last = status(name)
    wait = CHECK_EVERY_S if last and last["ok"] else RETRY_AFTER_S
    if last and time.time() - last["at"] < wait:
        return last
    return ensure(name)


def explain(name):
    """A sentence for a warning: why `name` is not answering."""
    last = status(name)
    if last is None:
        return f"{name} is being installed or started; it has not answered yet."
    return f"{name} could not be brought up: {last.get('reason') or 'it did not answer'}."
