"""Tells who updated that this version recommends other models than the ones the config holds, and applies them only on
consent. `model_defaults.ensure_recommended_models` fills empty fields only, so a user who updated keeps the previous
version's defaults forever; this module is the visible, reversible way out.

State lives in `config.json` under `defaults_notice`, tied to DEFAULTS_VERSION: pending (announced once a day until the
user answers), declined (silent for this version), applied (the replaced models are kept for `revert`). Raising
DEFAULTS_VERSION when RECOMMENDED_MODELS changes starts a new notice for everyone. Embedding and rerank are never
offered: changing them invalidates every index.
"""
import secrets
import time

import config
import model_catalog
import model_defaults

DEFAULTS_VERSION = 1
ROLES = ("router_model", "scope_model", "scout_model", "research_model")
REMIND_EVERY_S = 24 * 3600


def _current(cfg):
    """The notice of this defaults version, or a blank one."""
    notice = cfg.get("defaults_notice") or {}
    return notice if notice.get("version") == DEFAULTS_VERSION else {"version": DEFAULTS_VERSION, "state": "none"}


def _differences(cfg, chat):
    """{role: {"current", "new"}} for each role the user has set to something other than the recommended model that
    this gateway's catalog has."""
    found = {}
    for role in ROLES:
        wanted = model_defaults._matching(chat, model_defaults.RECOMMENDED_MODELS[role][1])
        if wanted and cfg.get(role) and cfg[role] != wanted:
            found[role] = {"current": cfg[role], "new": wanted}
    return found


def check():
    """Recomputes what is on offer against the gateway catalog and stores it. Raises when the catalog is unreachable
    (the caller shows that; no answer is invented). Returns `status()`."""
    cfg = config.load_config()
    notice = _current(cfg)
    if notice["state"] in ("declined", "applied"):
        return status()
    found = _differences(cfg, set(model_catalog.list_models("chat")))

    def store(current):
        notice = dict(_current(current))
        if not found:
            notice.update(state="none", changes={}, nonce="")
        else:
            notice.update(state="pending", changes=found, nonce=notice.get("nonce") or secrets.token_hex(8))
        current["defaults_notice"] = notice

    config.update_config(store)
    return status()


def status(cfg=None):
    """What the setup screen and the tray read: state, the model changes on offer, and what `revert` would restore."""
    notice = _current(cfg if cfg is not None else config.load_config())
    return {"version": DEFAULTS_VERSION, "state": notice["state"], "changes": notice.get("changes") or {},
            "previous": notice.get("previous") or {}, "nonce": notice.get("nonce", "")}


def _change(mutate_notice, nonce):
    """Runs `mutate_notice(cfg, notice)` under the config lock on a pending notice; `nonce` None (the setup screen,
    which has its own token) skips the link check, a toast button must carry the nonce it was shown with."""
    result = {}

    def apply(cfg):
        notice = dict(_current(cfg))
        if notice["state"] != "pending":
            raise ValueError(f"No new default models are waiting (state: {notice['state']}).")
        if nonce is not None and not secrets.compare_digest(str(nonce), str(notice.get("nonce", ""))):
            raise ValueError("This notification is out of date; open the settings to see the current offer.")
        result["text"] = mutate_notice(cfg, notice)
        cfg["defaults_notice"] = notice

    config.update_config(apply)
    return result["text"]


def apply(nonce=None):
    """Switches the offered roles to the recommended models and keeps the previous ones for `revert`."""
    def mutate(cfg, notice):
        # A role the user changed since the offer was made is theirs: it is skipped, never overwritten.
        todo = {role: change for role, change in notice["changes"].items() if cfg.get(role) == change["current"]}
        if not todo:
            raise ValueError("The offered roles were changed since the notice; open the settings to see the current state.")
        notice["previous"] = {role: change["current"] for role, change in todo.items()}
        for role, change in todo.items():
            cfg[role] = change["new"]
        notice.update(state="applied", applied_at=time.time())
        return "applied: " + ", ".join(f"{role} {change['current']} -> {change['new']}" for role, change in todo.items())
    return _change(mutate, nonce)


def decline(nonce=None):
    """Stops announcing this defaults version; the user's models stay as they are."""
    def mutate(cfg, notice):
        notice["state"] = "declined"
        return "declined: no more notices for this version of the defaults"
    return _change(mutate, nonce)


def revert():
    """Puts back the models `apply` replaced. Raises when nothing was applied."""
    done = {}

    def put_back(cfg):
        notice = dict(_current(cfg))
        if notice["state"] != "applied" or not notice.get("previous"):
            raise ValueError("Nothing to revert: the recommended models were not applied.")
        for role, model in notice["previous"].items():
            cfg[role] = model
        done["text"] = "reverted: " + ", ".join(f"{role} -> {model}" for role, model in notice["previous"].items())
        notice.update(state="declined", previous={})
        cfg["defaults_notice"] = notice

    config.update_config(put_back)
    return done["text"]


def due(now=None):
    """The offer the tray should announce now (pending and not announced in the last 24 h), or None. Marks it
    announced, so a second call in the same day returns None; closing the toast without choosing brings it back."""
    now = now or time.time()
    taken = {}

    def mark(cfg):
        notice = dict(_current(cfg))
        if notice["state"] == "pending" and notice.get("changes") and now - notice.get("last_notified", 0) >= REMIND_EVERY_S:
            notice["last_notified"] = now
            cfg["defaults_notice"] = notice
            taken["offer"] = {"changes": notice["changes"], "nonce": notice["nonce"]}

    config.update_config(mark)
    return taken.get("offer")
