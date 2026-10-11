"""Block patterns learned during operation: when the deterministic redirect rule lets a search or read run, the router
model (review_model) reviews the call in the background (the agent never waits for it) and may propose a regex for calls that should
go to Smart Tool. A proposal is checked (compiles, matches the call, how many logged calls it would have blocked; one
that blocks more than BROAD_SHARE of them is discarded) and waits for the user in the setup screen; an accepted pattern
redirects from then on, deterministically, after the rule's own exceptions (single files, commands changing files).
"""
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

import atomic_io
import config
import model_client
import paths

STORE_PATH = os.path.join(paths.DATA_DIR, "block-patterns.json")
REVIEW_TIMEOUT_S = 60
MAX_REVIEWS_PER_HOUR = 20
MAX_PENDING = 20
BROAD_SHARE = 0.25
PATTERN_MAX_CHARS = 300
CALL_MAX_CHARS = 1500
# Only calls whose reach the rule cannot measure are reviewed: bulk reads (globs, xargs, -exec, loops, scripts walking
# folders) and paths it could not resolve. Reading a few listed files is allowed on purpose and never reviewed.
REVIEWED_REASONS = ("The searched path uses a variable the hook cannot resolve.", "No content search over a folder.")
_BULK_READ = re.compile(r"\*|\bxargs\b|-exec\b|\bfor\s+\w+\s+in\b|os\.walk|rglob|glob\(|readdirSync|readdir\(|"
                        r"walkSync|-Recurse\b|Get-ChildItem", re.IGNORECASE)
# A lookaround, backreference or conditional can match by exclusion ("anything but these files").
_OPEN_ENDED = re.compile(r"\(\?<?[=!]|\(\?P=|\(\?\(|\\[1-9]")
# Every hook call runs the accepted patterns, so one that backtracks exponentially would hang the daemon (re holds
# the GIL and cannot be interrupted). Every regex of a proposal runs in a subprocess: first a timing over each prefix
# of the call followed by a run of characters commands are made of, and over each separator of the call itself (1 to
# 3 characters with punctuation, such as "; " or "\\") repeated where it first appears; over SLOW_REGEX_S of regex
# time, or no answer in SLOW_REGEX_S + START_SLACK_S, discards it. Then the checks against the call, the samples and
# the logged history (no answer in HISTORY_TIMEOUT_S discards it too).
SLOW_REGEX_S = 5
START_SLACK_S = 10
HISTORY_TIMEOUT_S = 60
_RUNS = tuple(unit * (200 // len(unit)) for unit in ("a", " ", "/", "_", "1", "a ", "a/", "a.", "$a/", "-a "))
_PREFIXES = 150
_MEASURE_SCRIPT = """
import json, re, sys, time
sys.path.insert(0, sys.argv[1])
import block_patterns as b
tool, pattern, text, metrics = json.loads(sys.stdin.read())
regex = re.compile(pattern)
if sys.argv[2] == "timing":
    start = time.perf_counter()
    step = max(1, len(text) // b._PREFIXES)
    for end in range(0, len(text) + 1, step):
        for run in b._RUNS:
            regex.search(text[:end] + run + "\\x00")
    for unit in b._units(text):
        regex.search(text[:max(0, text.find(unit))] + unit * (200 // len(unit)) + "\\x00")
    print(json.dumps({"seconds": time.perf_counter() - start}))
else:
    allowed = next((x for x in b.ALLOWED_ON_PURPOSE if regex.search(x)), None)
    matches, total = b._history(tool, regex, metrics)
    print(json.dumps({"matches_call": bool(regex.search(text)), "allowed": allowed, "matches": matches,
                      "total": total}))
"""
# Calls the rule lets run on purpose: a pattern matching any of them would block what must run.
ALLOWED_ON_PURPOSE = ("grep -n handler src/app.py", "grep -n \"def load\" -A20 src/app.py | head -40",
                      "sed -n 1,80p src/app.py", "cat README.md", "head -50 src/app.py", "tail -20 logs/app.log",
                      "awk '/def /{print NR\": \"$0}' src/app.py", "git status --short", "git log --oneline -5",
                      "git diff --stat", "npm test", "pytest -q tests/test_app.py", "ls src", "find src -name '*.py'",
                      "Grep pattern=handler path=src/app.py", "cd src && grep -n x app.py",
                      "ls -la src docs | head -20", "wc -l src/app.py src/util.py",
                      "for f in src/app.py src/util.py; do grep -n handler $f; done")

_LOCK = threading.RLock()
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="block-patterns")
_state = {"pending": 0, "recent": [], "seen": set(), "compiled": (None, [])}

_SYSTEM_PROMPT = (
    "Smart Tool saves the context of coding agents: a PreToolUse hook sends raw searches of project code to an indexed "
    "semantic search (smart_search), whose short answer costs far fewer tokens than grep output, and the index gets "
    "better the more it is used. A deterministic rule already redirects content searches over a folder (grep -r, rg, "
    "git grep, the Grep tool on a folder) and searches over more than 20 listed files. It lets run, on purpose: reading "
    "or searching inside one or a few known files, listings (ls, find -name, Glob), commands that change files, run "
    "builds, tests or git operations other than grep, and paths it cannot resolve.\n\n"
    "You get one call the rule let run because it could not measure its reach: a bulk read (glob, xargs, -exec, a "
    "loop, a script walking folders) or a path held in a variable. Answer block=true only when calls like it read the "
    "content of many project source files at once, which smart_search answers in far fewer tokens. Answer block=false "
    "for anything else, which is most calls: reading one or a few named files (grep, sed -n, cat, head, tail, awk on a "
    "file), logs, test or build runs, git commands, registry or system commands, scripts that do not read code. When "
    "block=true, write a Python regex matched with re.search against the call text that catches the same kind of "
    "bulk read (not this exact path) and none of the calls allowed on purpose. Patterns already accepted or rejected "
    "are listed; do not propose them again. Call submit_proposal."
)
_PROPOSAL_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_proposal",
        "description": "Whether calls like this one should be redirected, and the pattern when they should.",
        "parameters": {
            "type": "object",
            "properties": {
                "block": {"type": "boolean"},
                "pattern": {"type": "string", "description": "Python regex over the call text; empty when block is false"},
                "reason": {"type": "string", "description": "One sentence on what the pattern catches and why"},
            },
            "required": ["block", "pattern", "reason"],
        },
    },
}


def call_text(tool_name, tool_input):
    """The text a pattern is matched against: the command for Bash, the arguments for Grep and Glob."""
    if tool_name == "Bash":
        return str(tool_input.get("command") or "")
    fields = ("pattern", "path", "glob", "type", "output_mode")
    return f"{tool_name} " + " ".join(f"{f}={tool_input[f]}" for f in fields if tool_input.get(f) not in (None, ""))


def _empty():
    return {"accepted": [], "proposals": [], "rejected": []}


def load():
    """The stored patterns: {"accepted": [...], "proposals": [...], "rejected": [...]}."""
    try:
        with open(STORE_PATH, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return _empty()
    if not isinstance(data, dict) or any(not isinstance(data.get(k), list) for k in _empty()):
        raise ValueError(f"{STORE_PATH} is not a block patterns file: fix or delete it.")
    return data


def _save(data):
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    atomic_io.write_secret_text(STORE_PATH, json.dumps(data, indent=1, ensure_ascii=False))


def matching(tool_name, tool_input):
    """The accepted pattern matching this call, or None. Compiled patterns are cached by the file's mtime."""
    try:
        mtime = os.path.getmtime(STORE_PATH)
    except OSError:
        return None
    with _LOCK:
        cached_mtime, compiled = _state["compiled"]
        if cached_mtime != mtime:
            compiled = [(entry, re.compile(entry["pattern"])) for entry in load()["accepted"]]
            _state["compiled"] = (mtime, compiled)
    text = call_text(tool_name, tool_input)
    return next((entry for entry, regex in compiled if entry["tool"] == tool_name and regex.search(text)), None)


def _units(text, limit=400):
    """Distinct 1-3 character pieces of text holding a non-alphanumeric character, plus each one after a letter: the
    separators a pattern's repetition may hinge on."""
    seen = {}
    for size in (1, 2, 3):
        for start in range(len(text) - size + 1):
            piece = text[start:start + size]
            if not piece.isalnum():
                seen.setdefault(piece, None)
                seen.setdefault("a" + piece, None)
    return list(seen)[:limit]


def _measure(stage, timeout, tool_name, pattern, text, metrics_path):
    """One stage of _MEASURE_SCRIPT ("timing" or "checks") in a subprocess; None when it did not answer in time."""
    try:
        done = subprocess.run([sys.executable, "-I", "-c", _MEASURE_SCRIPT, os.path.dirname(os.path.abspath(__file__)), stage],
                              input=json.dumps([tool_name, pattern, text, metrics_path]), capture_output=True,
                              text=True, encoding="utf-8", timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return None
    if done.returncode:
        raise RuntimeError(f"pattern check failed: {done.stderr.strip()[-300:]}")
    return json.loads(done.stdout)


def _history(tool_name, regex, metrics_path):
    """(matches, total) of the logged calls of this tool the pattern would have blocked."""
    matches = total = 0
    for name in (metrics_path + ".1", metrics_path):
        try:
            with open(name, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if row.get("tool_name") != tool_name or not isinstance(row.get("tool_input"), dict):
                        continue
                    total += 1
                    matches += bool(regex.search(call_text(tool_name, row["tool_input"])))
        except FileNotFoundError:
            continue
    return matches, total


def _ask(tool_name, text, rule_reason, known, model):
    token = model_client.get_token()
    response = model_client.fetch("/v1/chat/completions", token, method="POST", timeout=REVIEW_TIMEOUT_S, body={
        "model": model,
        "messages": [{"role": "system", "content": _SYSTEM_PROMPT},
                     {"role": "user", "content": json.dumps({"tool": tool_name, "call": text[:CALL_MAX_CHARS],
                                                             "rule_allowed_because": rule_reason,
                                                             "known_patterns": known}, ensure_ascii=False)}],
        "tools": [_PROPOSAL_TOOL],
        "tool_choice": {"type": "function", "function": {"name": "submit_proposal"}},
        "temperature": 0,
    })
    return json.loads(response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])


def propose(tool_name, tool_input, rule_reason, model, metrics_path, redact):
    """Asks the model about one allowed call and stores what it proposes. Returns the stored proposal, or None when the
    model did not propose one or the proposal failed a check (the discarded ones are kept as rejected, with why)."""
    text = call_text(tool_name, tool_input)
    with _LOCK:
        data = load()
    known = [e["pattern"] for group in ("accepted", "proposals", "rejected") for e in data[group]
             if e["tool"] == tool_name][-30:]
    answer = _ask(tool_name, call_text(tool_name, redact(tool_input)), rule_reason, known, model)
    pattern = str(answer.get("pattern") or "").strip()
    if not answer.get("block") or not pattern:
        return None
    entry = {"id": hashlib.sha1(f"{tool_name}\0{pattern}".encode()).hexdigest()[:12], "tool": tool_name,
             "pattern": pattern, "reason": " ".join(str(answer.get("reason") or "").split())[:300],
             "example": call_text(tool_name, redact(tool_input))[:300], "proposed_at": time.time()}
    problem = None
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        problem, regex = f"invalid regex: {exc}", None
    if regex and len(pattern) > PATTERN_MAX_CHARS:
        problem = f"longer than {PATTERN_MAX_CHARS} characters"
    elif regex and _OPEN_ENDED.search(pattern):
        problem = "lookaround, backreference or conditional (matches by exclusion)"
    if regex and not problem:
        timing = _measure("timing", SLOW_REGEX_S + START_SLACK_S, tool_name, pattern, text, metrics_path)
        checks = (_measure("checks", HISTORY_TIMEOUT_S, tool_name, pattern, text, metrics_path)
                  if timing and timing["seconds"] <= SLOW_REGEX_S else None)
        if checks is None:
            problem = f"catastrophic backtracking: over {SLOW_REGEX_S}s on a long command or the logged calls"
        elif not checks["matches_call"]:
            problem = "does not match the call it came from"
        elif checks["allowed"]:
            problem = "matches calls the rule lets run on purpose: " + checks["allowed"]
        else:
            matches, total = checks["matches"], checks["total"]
            entry.update(history_matches=matches, history_total=total)
            if total and matches / total > BROAD_SHARE:
                problem = f"too broad: would block {matches} of {total} logged {tool_name} calls"
    with _LOCK:
        data = load()
        if any(e["id"] == entry["id"] for group in data.values() for e in group):
            return None
        if problem:
            data["rejected"].append({**entry, "rejected_by": "check", "why": problem})
            _save(data)
            return None
        data["proposals"].append(entry)
        _save(data)
    return entry


def review_model(cfg):
    """The scope model (a reasoning model by default), else the router model. Measured on 2026-10-09 over 20 real bulk
    reads the rule let run: gpt-4o-mini proposed a pattern for 17 (14 failed the checks, the other 3 copied one session's
    command); gpt-5-mini proposed 2, one of them a real gap (grep over `$(git ls-files)` held in a variable). The review
    runs in the background, so the slower model costs the agent nothing."""
    return cfg.get("scope_model") or cfg.get("router_model")


def reviewable(tool_name, tool_input, rule_reason):
    """Whether an allowed call is worth the model's review: a path the rule could not resolve, or a bulk read."""
    if rule_reason == REVIEWED_REASONS[0]:
        return True
    return rule_reason == REVIEWED_REASONS[1] and bool(_BULK_READ.search(call_text(tool_name, tool_input)))


def review_later(tool_name, tool_input, rule_reason, metrics_path, redact, log):
    """Queues a background review of an allowed call whose reach the rule could not measure (REVIEWED_REASONS, bulk
    reads only), when pattern_proposals is on and a review model is set. The same kind of call is reviewed once per
    daemon run, at most MAX_REVIEWS_PER_HOUR an hour; failures go to log."""
    if not reviewable(tool_name, tool_input, rule_reason):
        return False
    cfg = config.load_config()
    model = review_model(cfg)
    if not model or config.pattern_proposals(cfg) != "on":
        return False
    signature = (tool_name, re.sub(r"\s+", " ", call_text(tool_name, tool_input))[:120])
    now = time.time()
    with _LOCK:
        _state["recent"] = [t for t in _state["recent"] if now - t < 3600]
        if signature in _state["seen"] or len(_state["recent"]) >= MAX_REVIEWS_PER_HOUR or \
                _state["pending"] >= MAX_PENDING:
            return False
        _state["seen"].add(signature)
        _state["recent"].append(now)
        _state["pending"] += 1

    def run():
        try:
            propose(tool_name, tool_input, rule_reason, model, metrics_path, redact)
        except Exception as exc:
            log(f"block pattern review failed: {type(exc).__name__}: {exc}")
        finally:
            with _LOCK:
                _state["pending"] -= 1
    _EXECUTOR.submit(run)
    return True


def decide_proposals(entry_ids, action):
    """accept moves proposals to accepted; reject moves them to rejected; remove deletes accepted patterns. All ids are
    decided in one write, or none when any id is unknown (ValueError naming it)."""
    if action not in ("accept", "reject", "remove"):
        raise ValueError("Invalid action: use accept, reject or remove.")
    wanted = list(dict.fromkeys(entry_ids))
    if not wanted:
        raise ValueError("No pattern selected.")
    with _LOCK:
        data = load()
        source = "accepted" if action == "remove" else "proposals"
        found = {e["id"]: e for e in data[source] if e["id"] in wanted}
        missing = [entry_id for entry_id in wanted if entry_id not in found]
        if missing:
            raise ValueError(f"No {source[:-1]} with id {', '.join(missing)}.")
        data[source] = [e for e in data[source] if e["id"] not in found]
        now = time.time()
        for entry_id in wanted:
            if action == "accept":
                data["accepted"].append({**found[entry_id], "accepted_at": now})
            elif action == "reject":
                data["rejected"].append({**found[entry_id], "rejected_by": "user"})
        _save(data)
        return data
