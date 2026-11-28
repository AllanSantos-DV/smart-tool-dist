#!/usr/bin/env python3
"""Cota diária ou horária dos provedores sem chave, lembrada entre reinícios do daemon.

Um 429 isolado é rajada e o circuito curto do daemon já o cobre. Três 429 na mesma janela
(dia para o Exa, hora para o Parallel) é cota esgotada: o provedor fica de fora até a janela
virar, em vez de gastar uma chamada real a cada minuto para descobrir de novo.

Sob rajada (varreduras pesadas, cada busca bilíngue gasta duas chamadas) a cota publicada é
contada aqui: a partir de 80% o provedor roda em chamadas alternadas e, ao chegar no teto, fica
de fora até a janela virar. Com chave do usuário o teto é o da conta dele e nada é contado.
"""
import hashlib
import functools
import json
import os
import sys
import threading
import time

import config
import paths

STORE_PATH = os.path.join(paths.DATA_DIR, "web-search-quota.json")
WINDOWS = {"exa": "day", "parallel": "hour"}
KEYLESS_CAPS = {"exa": 150, "parallel": 60}
RESERVE_FRACTION = 0.8
HOLD_AFTER = 3
_LOCK = threading.Lock()


def window_end(window, now):
    """Epoch of the next local midnight (`"day"`) or the next full local hour (`"hour"`) after `now`."""
    parts = time.localtime(now)
    if window == "day":
        return time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    if window == "hour":
        return time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday, parts.tm_hour + 1, 0, 0, 0, 0, -1))
    raise ValueError(f"Unknown quota window: {window}")


def window_start(window, now):
    """Epoch where the window that contains `now` began."""
    parts = time.localtime(now)
    if window == "day":
        return time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday, 0, 0, 0, 0, 0, -1))
    return time.mktime((parts.tm_year, parts.tm_mon, parts.tm_mday, parts.tm_hour, 0, 0, 0, 0, -1))


class StoreBusy(Exception):
    """The store exists but cannot be read right now (a sharing violation); nothing may be written over it."""


_NOTES = []
_NOTES_LOCK = threading.Lock()


def _note(text):
    with _NOTES_LOCK:
        _NOTES.append(text)


def _busy(exc):
    """Queues the line for a store that is locked right now; `_reporting` writes it once the lock is released."""
    _note(f"[smart-tool] web-search-quota store busy ({exc}); this call is neither counted nor held\n")


def _flush_notes():
    with _NOTES_LOCK:
        notes = _NOTES[:]
        _NOTES.clear()
    for note in notes:
        try:
            sys.stderr.write(note)
        except (OSError, ValueError, AttributeError):
            break


def _reporting(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        finally:
            _flush_notes()
    return run


def _load():
    """The stored state. A store that cannot be parsed is moved to `.damaged` and the count starts clean, so one bad
    write does not take the quota tiers down; the line on stderr says it happened."""
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {}
    except (PermissionError, BlockingIOError) as exc:
        raise StoreBusy(type(exc).__name__) from exc
    except ValueError as exc:
        return _set_aside(f"not valid JSON ({exc})")
    if not isinstance(data, dict):
        return _set_aside("not an object")
    return data


def _set_aside(why):
    _note(f"[smart-tool] web-search-quota store {why}; kept as {STORE_PATH}.damaged and counting from zero\n")
    try:
        os.replace(STORE_PATH, STORE_PATH + ".damaged")
    except OSError as exc:
        _note(f"[smart-tool] web-search-quota store could not be set aside: {type(exc).__name__}\n")
    return {}


def _save(data):
    """Writes the state; False when the store is locked right now (nothing is written, the call is not counted)."""
    os.makedirs(os.path.dirname(STORE_PATH), exist_ok=True)
    tmp_path = STORE_PATH + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(tmp_path, STORE_PATH)
    except (PermissionError, BlockingIOError) as exc:
        _busy(type(exc).__name__)
        return False
    return True


@_reporting
def note_rate_limit(tier, config_id, now=None, keyed=False):
    """Counts one provider rate limit. Returns the epoch the tier is held until once the window has
    `HOLD_AFTER` of them, `None` while it is still a burst or when the tier has no quota window.
    `config_id` (bytes) identifies the key in use, so a new key starts clean. With the user's own key (`keyed`) the
    limit is the account's, so nothing is counted or held."""
    window = WINDOWS.get(tier)
    if window is None or keyed:
        return None
    now = time.time() if now is None else now
    identity = config_id.hex()
    with _LOCK:
        try:
            data = _load()
        except StoreBusy as exc:
            _busy(exc)
            return None
        state = data.get(tier) or {}
        if state.get("config") != identity:
            state = {"config": identity, "events": [], "until": 0}
        start = window_start(window, now)
        events = [stamp for stamp in state.get("events", []) if stamp >= start] + [now]
        until = window_end(window, now) if len(events) >= HOLD_AFTER else state.get("until") or 0
        state.update(events=events, until=until)
        data[tier] = state
        if not _save(data):
            return None
    return until if until > now else None


@_reporting
def status(tier, config_id, keyed, now=None):
    """Read-only view of the keyless quota of `tier`: `{used, cap, window, held_until}` (held_until is 0 when free),
    or None for a tier without a quota window and for the user's own key. It never counts a call."""
    window = WINDOWS.get(tier)
    if window is None or keyed:
        return None
    now = time.time() if now is None else now
    with _LOCK:
        try:
            state = _load().get(tier) or {}
        except StoreBusy as exc:
            _busy(exc)
            return None
    if state.get("config") != config_id.hex():
        state = {}
    used = state.get("calls", 0) if state.get("calls_since") == window_start(window, now) else 0
    held = state.get("until") or 0
    return {"used": used, "cap": KEYLESS_CAPS[tier], "window": window, "held_until": held if held > now else 0}


def config_id(tier):
    """Identity (bytes) of the key saved for `tier`: a changed key starts a new quota count."""
    keys = config.load_config().get("api_keys") or {}
    blob = keys.get(tier, "") if isinstance(keys, dict) else ""
    return hashlib.sha256(str(blob).encode("utf-8")).digest()


def provider_status(tier):
    """`status` of `tier` for the key now saved in the config (the user's own key, if any, has no quota)."""
    return status(tier, config_id(tier), config.provider_key_status(tier) == "configured")


@_reporting
def admit(tier, config_id, keyed, now=None):
    """Decides whether the tier may be launched now. Returns `None` to launch it (the call is counted) or the reason
    it stays out: a hold from rate limits, the published keyless cap reached, or the alternate call of the reserve
    zone (80% of the cap). A tier without a quota window, and any tier with the user's own key, is always launched."""
    window = WINDOWS.get(tier)
    if window is None:
        return None
    now = time.time() if now is None else now
    identity = config_id.hex()
    with _LOCK:
        try:
            data = _load()
        except StoreBusy as exc:
            _busy(exc)
            return None
        state = data.get(tier) or {}
        if state.get("config") != identity:
            state = {"config": identity, "events": [], "until": 0}
        if keyed:
            return None
        held = state.get("until") or 0
        if held > now:
            return f"quota used up until {_stamp(held)}"
        start = window_start(window, now)
        calls = state.get("calls", 0) if state.get("calls_since") == start else 0
        cap = KEYLESS_CAPS[tier]
        if calls >= cap:
            state["until"] = window_end(window, now)
            data[tier] = state
            _save(data)
            return f"quota used up until {_stamp(state['until'])}"
        reserve = calls >= int(cap * RESERVE_FRACTION)
        if reserve and state.get("skip_next"):
            state["skip_next"] = False
            data[tier] = state
            _save(data)
            return f"saving quota ({calls} of {cap} calls used)"
        state.update(calls=calls + 1, calls_since=start, skip_next=reserve)
        data[tier] = state
        _save(data)
    return None


def _stamp(epoch):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
