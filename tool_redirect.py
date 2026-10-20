"""Transparent redirect of native searches and web calls to the Smart Tool.

The PreToolUse hook starts the Smart Tool call that replaces a native one (smart_search for a folder search, web_fetch
or web_search for WebFetch/WebSearch) under the call's `tool_use_id`, and the PostToolUse hook puts its text in the
native tool's output, in the shape that tool returns: the agent reads the Smart Tool answer as the result of the call it
made. Measured on Claude Code 2.1.300: `updatedInput` without `permissionDecision` is applied (the user's permission
rules still judge the rewritten call) and `updatedToolOutput` replaces the output of Grep, Bash, WebFetch and WebSearch
when it keeps the tool's shape; a PostToolUseFailure cannot replace anything, so a shell search whose answer is ready in
the PreToolUse is rewritten to a no-op that always succeeds (a grep with no match exits 1). Codex has no output
replacement: its shell call is rewritten to print a file holding the answer, which Codex only applies with `allow`.
"""
import concurrent.futures
import os
import threading
import time
import uuid

import paths

RESULT_TTL_S = 600
RESULTS_DIR = os.path.join(paths.DATA_DIR, "redirects")
NO_OP = {"Bash": "true", "PowerShell": "$null"}
CODEX_PREFIX = "cat '"
_POOL = concurrent.futures.ThreadPoolExecutor(4, thread_name_prefix="redirect")


class Jobs:
    """Smart Tool calls started for native tool calls, by `tool_use_id`; each is dropped once read or after
    RESULT_TTL_S."""

    def __init__(self, runners):
        self._runners = runners
        self._futures = {}
        self._lock = threading.Lock()

    def start(self, key, kind, arguments):
        """Runs `runners[kind](arguments)` in the background under key (a second start of the same key is ignored)."""
        now = time.monotonic()
        with self._lock:
            for old in [k for k, (at, _f) in self._futures.items() if now - at > RESULT_TTL_S]:
                del self._futures[old]
            if key not in self._futures:
                self._futures[key] = (now, _POOL.submit(self._runners[kind], arguments))

    def wait(self, key, timeout):
        """The text of key's call when it finished within timeout seconds, else None (the call keeps running and a
        later wait may still get it). Raises what the call raised."""
        with self._lock:
            entry = self._futures.get(key)
        if entry is None:
            return None
        try:
            return entry[1].result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return None

    def forget(self, key):
        """Drops key's call once its text was used or will never be read."""
        with self._lock:
            self._futures.pop(key, None)


def output_for(tool_name, response, text):
    """`updatedToolOutput` for a native tool: its own response with the Smart Tool text in the field the model reads,
    or None when the response has an unknown shape."""
    if not isinstance(response, dict):
        return None
    if tool_name == "Grep":
        return {**response, "mode": "content", "content": text, "numLines": len(text.splitlines()), "numFiles": 0,
                "filenames": []}
    if tool_name in NO_OP:
        return {**response, "stdout": text, "stderr": "", "interrupted": False}
    if tool_name == "WebFetch":
        return {**response, "result": text}
    if tool_name == "WebSearch":
        return {**response, "results": [text]}
    return None


def codex_command(text):
    """Shell command that prints text from a file: `cat` with a quoted forward-slash path runs the same in bash and
    PowerShell (an alias of Get-Content there), the shells Codex uses."""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    now = time.time()
    for name in os.listdir(RESULTS_DIR):
        path = os.path.join(RESULTS_DIR, name)
        try:
            if now - os.path.getmtime(path) > RESULT_TTL_S:
                os.remove(path)
        except OSError:
            pass
    path = os.path.join(RESULTS_DIR, f"{uuid.uuid4().hex}.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return f"{CODEX_PREFIX}{path.replace(os.sep, '/')}'"


def is_codex_command(command):
    """Whether a shell command is one codex_command wrote: the only input the hook approves with `allow`."""
    if not isinstance(command, str) or not command.startswith(CODEX_PREFIX) or not command.endswith("'"):
        return False
    path = os.path.normpath(command[len(CODEX_PREFIX):-1])
    return os.path.dirname(os.path.normcase(path)) == os.path.normcase(os.path.normpath(RESULTS_DIR)) and "'" not in path
