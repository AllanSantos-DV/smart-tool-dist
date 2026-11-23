"""Client side of every command hook that calls the Smart Tool daemon (PreToolUse, PostToolUse, SubagentStart, Stop, and the
opt-in git pre-commit and pre-push).

The daemon address is read from its run file on every call, never from a port written into a settings file. A refused or
reset connection (a daemon restarting) is retried with backoff for `RESTART_WINDOW_S`, so a restart produces no message at
all; a daemon that stays unreachable fails open with one plain message, and the next calls stay silent until the daemon
answers again. Imports stay light (stdlib and the run-file reader) so a hook process starts fast.
"""
import http.client
import json
import os
import socket
import time
import urllib.parse

import daemon_launcher
import paths

ORIGIN = "smart-tool"
RESTART_WINDOW_S = 10
WINDOW_ENV = "SMART_TOOL_HOOK_WINDOW_S"
DOWN_MEMORY_S = 60
CONNECT_TIMEOUT_S = 2
QUIET_CONNECT_TIMEOUT_S = 0.4
BACKOFF_S = (0.1, 0.2, 0.4, 0.8, 1.0)
ROUTE_TIMEOUT_S = {"pretool": 14, "posttool": 59, "subagent-start": 4, "stop": 110, "commit-check": 55,
                   "pre-push": 100}
DOWN_MARKER = os.path.join(paths.DATA_DIR, "hook-down")
LOG_NAME = "daemon.log"
RETRYABLE = (ConnectionError, http.client.RemoteDisconnected, http.client.BadStatusLine)


class Unreachable(Exception):
    """The daemon could not answer: `cause` is a plain-words reason."""

    def __init__(self, cause):
        """`cause`: the plain-words reason the daemon could not answer."""
        super().__init__(cause)
        self.cause = cause


def _post(url, route, query, raw, timeout, connect_timeout):
    """The JSON the daemon answered to one POST of `raw` on /hooks/<route>, or raises ConnectionError, TimeoutError,
    Unreachable (an HTTP error status). A connect that times out is a ConnectionError: a closed port on Windows may
    take seconds to refuse, and that is a daemon not listening, not a slow one."""
    parsed = urllib.parse.urlsplit(url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=connect_timeout)
    try:
        try:
            connection.connect()
        except socket.timeout as exc:
            raise ConnectionError(f"connect timed out: {exc}") from None
        connection.sock.settimeout(timeout)
        suffix = f"?{urllib.parse.urlencode(query)}" if query else ""
        connection.request("POST", f"/hooks/{route}{suffix}", body=raw, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        body = response.read()
    except socket.timeout as exc:
        raise TimeoutError(str(exc)) from None
    except OSError as exc:
        if isinstance(exc, ConnectionError):
            raise
        raise ConnectionError(str(exc)) from None
    finally:
        connection.close()
    if response.status != 200:
        raise Unreachable(f"answered HTTP {response.status}")
    return json.loads(body or b"{}")


def _down_recently():
    try:
        return time.time() - os.path.getmtime(DOWN_MARKER) < DOWN_MEMORY_S
    except OSError:
        return False


def _note_down():
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    with open(DOWN_MARKER, "w", encoding="utf-8") as stream:
        stream.write(str(time.time()))


def _clear_down():
    try:
        os.remove(DOWN_MARKER)
    except OSError:
        pass


def call(route, raw, client="", window_s=None, sleep=time.sleep):
    """`(output, first_failure)` of one hook call. `output` is the daemon's JSON answer ({} when it stayed
    unreachable); `first_failure` is the cause in plain words the first time the daemon is found unreachable, None when
    it answered or when an earlier call already reported the same outage. The restart window is `RESTART_WINDOW_S`, or the
    seconds in the environment variable `SMART_TOOL_HOOK_WINDOW_S` (tests shorten it; the native client reads the same)."""
    query = {"client": client} if client else {}
    window = float(os.environ.get(WINDOW_ENV) or RESTART_WINDOW_S) if window_s is None else window_s
    quiet = _down_recently()
    deadline = time.monotonic() + (0 if quiet else window)
    attempt = 0
    cause = "the daemon is not running"
    while True:
        info = daemon_launcher.read_registry()
        try:
            if info:
                output = _post(info["url"], route, query, raw, ROUTE_TIMEOUT_S[route],
                               QUIET_CONNECT_TIMEOUT_S if quiet else CONNECT_TIMEOUT_S)
                _clear_down()
                return (output if isinstance(output, dict) else {}), None
            cause = "the daemon is not running (no run file)"
        except TimeoutError:
            return {}, _report(f"the daemon did not answer within {ROUTE_TIMEOUT_S[route]} s (timed out)", quiet)
        except Unreachable as exc:
            return {}, _report(f"the daemon {exc.cause}", quiet)
        except RETRYABLE:
            cause = "the daemon is restarting or not running"
        if time.monotonic() >= deadline:
            return {}, _report(cause, quiet)
        sleep(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])
        attempt += 1


def _report(cause, quiet):
    _note_down()
    return None if quiet else cause


AFTER_DOWN = ("Nothing to do: it comes back by itself and further calls stay silent until it does; if it keeps "
              "happening, see {log}.")


def message(event, cause, effect, action=None):
    """The one visible line of a hook that let a call through: which hook, who owns it, why, what happened to the call
    and what the user does."""
    action = action or AFTER_DOWN.format(log=os.path.join(paths.DATA_DIR, LOG_NAME))
    return f"[hook-origin={ORIGIN}] Smart Tool (project smart-tool) {event} hook: {cause}. {effect} {action}"
