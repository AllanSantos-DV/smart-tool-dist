"""Source files read through the shell (`cat`, `head`, `sed -n`), counted per session so a run of them can be answered
with a tip.

One read of a known file is what the shell is for, and `smart_search` cannot return its exact lines, so no single read
is touched. A burst of them is an investigation done by reading, which `smart_scout` answers with only the lines that
matter: the third read inside the window carries the tip, then the tip waits before it repeats."""
import collections
import os
import threading
import time

import redirect_rule

READERS = frozenset({"cat", "head", "sed"})
SOURCE_SUFFIXES = frozenset({
    ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".scala", ".vue", ".svelte", ".sh", ".ps1", ".sql", ".lua", ".dart"})
BURST_READS = 3
WINDOW_S = 600
TIP_EVERY_S = 600
MAX_SESSIONS = 256
TIP = ("You have read {count} source files through the shell in the last minutes. For a question that spans several "
       "files call mcp__smart-tool__smart_scout(project_root, question) (it returns only the lines that answer); keep "
       "cat, head and sed -n for a file you already know and need verbatim.")

_LOCK = threading.Lock()
_READS = collections.OrderedDict()
_TIPPED = {}


def source_files(command, cwd):
    """The existing source files, inside the session's reach, that the reading commands of a shell command print: `cat`,
    `head` and `sed -n` at the start of a command, not a filter after a pipe."""
    found = []
    for segment, after_pipe in redirect_rule.segments(command or ""):
        if after_pipe:
            continue
        tokens = redirect_rule._unwrap(redirect_rule._split(segment, False), {})
        if not tokens or tokens[0].lower() not in READERS or (tokens[0].lower() == "sed" and "-n" not in tokens):
            continue
        for raw in redirect_rule._reader_files(tokens[0].lower(), tokens[1:]):
            path = redirect_rule.resolve_path(os.path.expanduser(raw), cwd)
            if path and os.path.splitext(path)[1].lower() in SOURCE_SUFFIXES and os.path.isfile(path) \
                    and redirect_rule._in_project(path, cwd):
                found.append(path)
    return found


def note(session_id, command, cwd, now=None):
    """Counts the source reads of one shell call for the session and returns the tip when this call is the third inside
    the window and no tip went out in the last TIP_EVERY_S; None otherwise."""
    files = source_files(command, cwd)
    if not files or not session_id:
        return None
    now = time.time() if now is None else now
    with _LOCK:
        reads = _READS.setdefault(session_id, collections.deque())
        _READS.move_to_end(session_id)
        while len(_READS) > MAX_SESSIONS:
            _TIPPED.pop(_READS.popitem(last=False)[0], None)
        reads.extend([now] * len(files))
        while reads and now - reads[0] > WINDOW_S:
            reads.popleft()
        if len(reads) < BURST_READS or now - _TIPPED.get(session_id, -TIP_EVERY_S) < TIP_EVERY_S:
            return None
        _TIPPED[session_id] = now
        return TIP.format(count=len(reads))
