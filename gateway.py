"""Model gateway through adapters: how Smart Tool reaches embedding, chat and (optionally) rerank models.

Pattern copied from OpenCode providers (implementation + options, credentials kept apart; opencode.ai/docs/providers)
and LiteLLM custom handlers (a fixed set of methods registered by name; docs.litellm.ai custom_llm_server).

An adapter is a Python module with:
  NAME    unique id (str)
  LABEL   name shown in the setup screen
  FIELDS  sequence of {"name", "label", optional "secret", "required", "placeholder", "hint"}
  request(options, route, body=None, timeout=15) -> (json, headers)
          route is OpenAI-style: "models", "model/info", "embeddings", "chat/completions", "rerank"; body None = GET.
          HTTP errors propagate as urllib.error.HTTPError.
  verify(options) -> None, raising RuntimeError with a reason a person can act on.

Built-in adapters live in model_adapters/; any .py in ADAPTERS_DIR is loaded as a user adapter. Non-secret options
stay in config.json; secret fields go to the DPAPI key store.
"""
import importlib.util
import os
import threading
import time

import config
import secret_store
from model_adapters import openai_compatible

ADAPTERS_DIR = os.path.join(config.CONFIG_DIR, "adapters")
BUILTIN = (openai_compatible,)
STATUS_TTL_S = 30
_lock = threading.Lock()
_loaded = {"key": None, "adapters": {}, "errors": []}
_status = {"at": 0.0, "key": None, "value": None}


def _check(module, origin):
    for attribute in ("NAME", "LABEL", "FIELDS"):
        if not getattr(module, attribute, None):
            raise ValueError(f"{origin}: missing {attribute}")
    for function in ("request", "verify"):
        if not callable(getattr(module, function, None)):
            raise ValueError(f"{origin}: missing function {function}()")
    for field in module.FIELDS:
        if not isinstance(field, dict) or not field.get("name") or not field.get("label"):
            raise ValueError(f"{origin}: each FIELDS entry needs name and label")
    return module


def adapters():
    """({name: module}, [load errors]); user adapters are re-imported only when their files change."""
    errors, stamped = [], []
    try:
        names = sorted(name for name in os.listdir(ADAPTERS_DIR) if name.endswith(".py"))
    except FileNotFoundError:
        names = []
    except OSError as exc:
        names, errors = [], [f"{ADAPTERS_DIR}: {type(exc).__name__}: {exc}"]
    for name in names:
        path = os.path.join(ADAPTERS_DIR, name)
        try:
            stamped.append((path, os.path.getmtime(path)))
        except OSError:
            continue
    key = (tuple(stamped), tuple(errors))
    with _lock:
        if _loaded["key"] == key:
            return dict(_loaded["adapters"]), list(_loaded["errors"])
    # Imported outside the lock: an adapter that calls back into gateway at import time must not deadlock the daemon.
    found = {module.NAME: module for module in BUILTIN}
    for path, _mtime in stamped:
        try:
            spec = importlib.util.spec_from_file_location(
                "smart_tool_adapter_" + os.path.splitext(os.path.basename(path))[0], path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _check(module, path)
            if module.NAME in found:
                raise ValueError(f"{path}: NAME {module.NAME!r} already exists")
            found[module.NAME] = module
        except Exception as exc:
            errors.append(f"{os.path.basename(path)}: {type(exc).__name__}: {exc}")
    with _lock:
        _loaded.update(key=key, adapters=found, errors=errors)
    return dict(found), list(errors)


def _secret_name(adapter, field):
    return f"adapter:{adapter}:{field}"


def selected():
    """(adapter module or None, options with secrets in clear). Unknown adapter in config fails loudly."""
    cfg = config.load_config()
    name = cfg.get("model_adapter") or ""
    if not name:
        return None, {}
    found, errors = adapters()
    if name not in found:
        raise RuntimeError(f"Model adapter {name!r} not found" + (f" ({'; '.join(errors)})" if errors else ""))
    module = found[name]
    options = dict((cfg.get("model_adapter_options") or {}).get(name) or {})
    for field in module.FIELDS:
        if field.get("secret"):
            options[field["name"]] = config.get_provider_api_key(_secret_name(name, field["name"]))
    return module, options


def describe():
    """Setup screen view: adapters with fields, stored non-secret values and secret state; never the secrets."""
    cfg = config.load_config()
    found, errors = adapters()
    stored = cfg.get("model_adapter_options") or {}
    listing = []
    for name, module in found.items():
        fields = []
        for field in module.FIELDS:
            entry = {key: field.get(key) for key in ("name", "label", "secret", "required", "placeholder", "hint")}
            if field.get("secret"):
                entry["state"] = config.provider_key_status(_secret_name(name, field["name"]))
            else:
                entry["value"] = (stored.get(name) or {}).get(field["name"], "")
            fields.append(entry)
        listing.append({"name": name, "label": module.LABEL, "builtin": module in BUILTIN, "fields": fields})
    return {"selected": cfg.get("model_adapter") or "", "adapters": listing, "errors": errors, "dir": ADAPTERS_DIR}


def save(name, values):
    """Verifies with the adapter before storing. Secret field absent/None keeps the stored value; "" removes it.
    name "" disconnects."""
    if not name:
        config.update_config(lambda cfg: cfg.update(model_adapter=""))
        forget_status()
        return
    found, _errors = adapters()
    if name not in found:
        raise ValueError(f"Adapter {name!r} does not exist.")
    module, values = found[name], values or {}
    plain, secrets, options = {}, {}, {}
    for field in module.FIELDS:
        key = field["name"]
        value = values.get(key)
        if field.get("secret"):
            if value is None:
                try:
                    value = config.get_provider_api_key(_secret_name(name, key))
                except secret_store.SecretProtectionError:
                    raise ValueError(f"The saved value of {field['label']} cannot be opened in this Windows profile; "
                                     "paste it again.") from None
            else:
                secrets[key] = str(value).strip()
                value = secrets[key]
                if value:
                    config.check_provider_key_format(value)
        else:
            value = str(value or "").strip()
            plain[key] = value
        if field.get("required") and not value:
            raise ValueError(f"Fill in {field['label']}.")
        options[key] = value
    module.verify(options)

    def mutate(cfg):
        stored = dict(cfg.get("model_adapter_options") or {})
        stored[name] = plain
        cfg.update(model_adapter=name, model_adapter_options=stored)
    config.update_config(mutate)
    for key, value in secrets.items():
        config.set_provider_api_key(_secret_name(name, key), value)
    forget_status()


def remove_secret(name, field):
    """Deletes one stored secret without testing the connection (a revoked key must be removable)."""
    found, _errors = adapters()
    module = found.get(name)
    if module is None or not any(f["name"] == field and f.get("secret") for f in module.FIELDS):
        raise ValueError(f"{name}/{field} is not a secret adapter field.")
    config.set_provider_api_key(_secret_name(name, field), "")
    forget_status()


def forget_status():
    with _lock:
        _status.update(at=0.0, key=None, value=None)


def status():
    """{"status": "ready" | "not_configured" | "error", "adapter", "label", "error"}, checked every STATUS_TTL_S."""
    try:
        module, options = selected()
    except Exception as exc:
        return {"status": "error", "adapter": config.load_config().get("model_adapter"), "label": "",
                "error": f"{type(exc).__name__}: {exc}"}
    if module is None:
        return {"status": "not_configured", "adapter": "", "label": "",
                "error": "No model gateway configured."}
    key = (module.NAME, tuple(sorted(options.items())))
    with _lock:
        if _status["key"] == key and time.monotonic() - _status["at"] < STATUS_TTL_S:
            return dict(_status["value"])
    try:
        module.verify(options)
        value = {"status": "ready", "adapter": module.NAME, "label": module.LABEL, "error": ""}
    except Exception as exc:
        value = {"status": "error", "adapter": module.NAME, "label": module.LABEL, "error": str(exc)}
    with _lock:
        _status.update(at=time.monotonic(), key=key, value=value)
    return dict(value)


def fingerprint():
    """Changes whenever the adapter or its options change (cache key for the model catalog)."""
    module, options = selected()
    return (module.NAME if module else "", tuple(sorted(options.items())))


def request(route, body=None, timeout=15):
    module, options = selected()
    if module is None:
        raise RuntimeError("No model gateway configured: choose an adapter in the setup screen.")
    return module.request(options, route, body=body, timeout=timeout)
