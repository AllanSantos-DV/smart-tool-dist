#!/usr/bin/env python3
"""Configuração de modelos do Smart Tool, compartilhada por todos os hosts MCP.

Chaves de API ficam em `api_keys` sempre cifradas com DPAPI; texto em claro é recusado na gravação.
"""
import json
import os
import paths
import threading

import atomic_io
import secret_store

# Alias de compatibilidade: a implementação mora em `atomic_io`.
atomic_replace = atomic_io.atomic_replace

CONFIG_DIR = paths.HOME_DIR
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

# RLock: update_config() chama load/save sob o mesmo lock; Lock travaria a própria thread.
_LOCK = threading.RLock()

DEFAULT_CONFIG = {
    "router_model": "",
    "scope_model": "",
    "embedding_model": "",
    "rerank_model": "",
    "research_model": "",
    "research_max_iterations": 3,
    "quick_search_timeout_s": 120,
    "site_memory_similarity_threshold": 0.35,
    "local_models": "fallback",
    "hook_mode": "redirect",
    "doc_mode": "remind",
    "duplicate_mode": "warn",
    "auto_update": True,
    "contact": "",
    "model_adapter": "",
    "model_adapter_options": {},
}

# Local models from the mcp-memory sidecar: fallback = only when the gateway has no model; off = never;
# prefer = even when the gateway has one (embedding quality measured equal on the rerank gold set).
LOCAL_MODEL_MODES = ("fallback", "off", "prefer")
# PreToolUse hook: redirect = deny the native tool and point to Smart Tool; advise = let it run and add the tip to the
# agent's context; off = no routing at all.
HOOK_MODES = ("redirect", "advise", "off")
# Edit hook on functions without a docstring: require = deny the edit until it adds one; remind = let it run and tell
# the agent; off = say nothing. Independent of hook_mode, which only routes searches and reads.
DOC_MODES = ("require", "remind", "off")
# Edit hook on functions that copy one already indexed (exact or near-identical body): warn = let the edit run and
# point to the existing function; off = say nothing. Never blocks: near matches are leads, not defects.
DUPLICATE_MODES = ("warn", "off")


def doc_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("doc_mode") or DEFAULT_CONFIG["doc_mode"]
    if value not in DOC_MODES:
        raise ValueError(f"Invalid doc_mode ({value!r}) in {CONFIG_PATH}: use require, remind or off.")
    return value


def duplicate_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("duplicate_mode") or DEFAULT_CONFIG["duplicate_mode"]
    if value not in DUPLICATE_MODES:
        raise ValueError(f"Invalid duplicate_mode ({value!r}) in {CONFIG_PATH}: use warn or off.")
    return value


def auto_update(cfg=None):
    """Whether the tray installs a newer release by itself when it starts (once per version)."""
    value = (cfg if cfg is not None else load_config()).get("auto_update", DEFAULT_CONFIG["auto_update"])
    if not isinstance(value, bool):
        raise ValueError(f"Invalid auto_update ({value!r}) in {CONFIG_PATH}: use true or false.")
    return value


def hook_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("hook_mode") or DEFAULT_CONFIG["hook_mode"]
    if value not in HOOK_MODES:
        raise ValueError(f"Invalid hook_mode ({value!r}) in {CONFIG_PATH}: use redirect, advise or off.")
    return value


def local_models_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("local_models") or DEFAULT_CONFIG["local_models"]
    if value not in LOCAL_MODEL_MODES:
        raise ValueError(f"Invalid local_models ({value!r}) in {CONFIG_PATH}: use fallback, off or prefer.")
    return value


# Único lugar com os limites/recomendados de research_max_iterations e
# site_memory_similarity_threshold — configure.py, setup_ui.py e smart_tool_daemon.py leem
# daqui em vez de cada um definir seu próprio intervalo (mesmo padrão de fonte única de
# web_search_adapters.py pros tiers de busca).
RESEARCH_MAX_ITERATIONS_RANGE = (1, 10)
QUICK_SEARCH_TIMEOUT_RANGE = (30, 360)
SITE_MEMORY_SIMILARITY_THRESHOLD_RANGE = (0.0, 1.0)
WEB_PROVIDER_NAMES = ("exa", "parallel", "keenable", "tavily", "firecrawl")


def _within(value, cast, bounds, key):
    lo, hi = bounds
    try:
        n = cast(value)
    except (TypeError, ValueError):
        return DEFAULT_CONFIG[key]
    return n if lo <= n <= hi else DEFAULT_CONFIG[key]


def clamp_research_max_iterations(value):
    return _within(value, int, RESEARCH_MAX_ITERATIONS_RANGE, "research_max_iterations")


def clamp_quick_search_timeout(value):
    return _within(value, int, QUICK_SEARCH_TIMEOUT_RANGE, "quick_search_timeout_s")


def clamp_site_memory_similarity_threshold(value):
    return _within(value, float, SITE_MEMORY_SIMILARITY_THRESHOLD_RANGE, "site_memory_similarity_threshold")


class ConfigCorruptedError(Exception):
    """O arquivo existe mas não é JSON de objeto legível. Distinto de "não existe":
    `load_config()` pode cair pros defaults num arquivo ausente, mas gravar sobre
    um arquivo ilegível apagaria configurações que ainda poderiam ser recuperadas."""


def _read_stored():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            stored = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigCorruptedError(
            f"{CONFIG_PATH} is not valid JSON ({exc}); fix or delete the file manually."
        )
    if not isinstance(stored, dict):
        raise ConfigCorruptedError(f"{CONFIG_PATH} does not contain a JSON object; fix or delete the file manually.")
    return stored


def load_config():
    """Defaults mesclados com o que está em disco. Um arquivo corrompido devolve os
    defaults em vez de levantar: leitura é caminho quente (todo tool call passa por
    aqui) e não pode derrubar o daemon — quem for gravar usa `update_config`, que
    recusa escrever nesse estado."""
    with _LOCK:
        try:
            stored = _read_stored() or {}
        except ConfigCorruptedError:
            stored = {}
        merged = dict(DEFAULT_CONFIG)
        if "web_provider_keys" in stored:
            raise ConfigCorruptedError(f"{CONFIG_PATH} uses the old 'web_provider_keys' format: delete that field and "
                                       "enter the keys again in the setup screen.")
        merged.update(stored)
        return merged


def save_config(config):
    with _LOCK:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # Mantém o gravador atômico: a instalação antiga guardava segredo aqui.
        clean = dict(config)
        provider_keys = clean.get("api_keys") or {}
        if (not isinstance(provider_keys, dict) or any(
            not _known_key(name) or not secret_store.is_protected(value)
            for name, value in provider_keys.items()
        )):
            raise ValueError("Keys must be protected by DPAPI.")
        atomic_io.write_secret_text(
            CONFIG_PATH, json.dumps(clean, indent=2, ensure_ascii=False)
        )


def corrupted_reason():
    """Motivo pelo qual o arquivo em disco é ilegível, ou `None` se está legível/ausente.
    `load_config` cai nos defaults nesse caso, então sem isto uma config truncada é
    indistinguível de uma config vazia na hora de explicar a falha ao usuário."""
    with _LOCK:
        try:
            _read_stored()
        except ConfigCorruptedError as exc:
            return str(exc)
        return None


def update_config(mutate):
    """Load + mutate + save como uma única seção crítica, pra dois handlers HTTP
    concorrentes nunca perderem a escrita um do outro. Levanta `ConfigCorruptedError`
    se o arquivo em disco estiver ilegível, em vez de substituí-lo pelos defaults."""
    with _LOCK:
        stored = _read_stored() or {}
        cfg = dict(DEFAULT_CONFIG)
        cfg.update(stored)
        # Serializado (não `dict(cfg)`): a comparação tem que enxergar mutação in-place
        # de um valor aninhado, que uma cópia rasa esconderia. Mesmo contrato de
        # serialização de `save_config`, pra um valor que só a comparação aceitaria não
        # passar da guarda e estourar na gravação.
        before = json.dumps(cfg, sort_keys=True)
        mutate(cfg)
        # Mutação que não muda nada não regrava.
        if json.dumps(cfg, sort_keys=True) == before and os.path.isfile(CONFIG_PATH):
            return cfg
        save_config(cfg)
        return cfg


def _known_key(name):
    """Web providers by name; model adapter secrets as "adapter:<adapter>:<field>" (see gateway.py)."""
    return name in WEB_PROVIDER_NAMES or (isinstance(name, str) and name.startswith("adapter:"))


def _provider_name(name):
    if not _known_key(name):
        raise ValueError(f"Unknown key: {name!r}")
    return name


def get_provider_api_key(name):
    """Devolve a chave opcional em claro só no processo que fará a chamada HTTP."""
    name = _provider_name(name)
    stored = (load_config().get("api_keys") or {}).get(name) or ""
    if not stored:
        return ""
    if not secret_store.is_protected(stored):
        raise secret_store.SecretProtectionError("Provider key is not protected by DPAPI.")
    return secret_store.unprotect(stored)


def provider_key_status(name):
    """Estado para a UI, sem revelar a chave nem o blob DPAPI."""
    name = _provider_name(name)
    stored = (load_config().get("api_keys") or {}).get(name) or ""
    if not stored:
        return "keyless"
    try:
        get_provider_api_key(name)
    except secret_store.SecretProtectionError:
        return "unreadable"
    return "configured"


def check_provider_key_format(key):
    if len(key) > 4096 or any(ord(char) < 33 for char in key) or secret_store.is_protected(key):
        raise ValueError("Paste the provider's original API key, without spaces or line breaks.")


def set_provider_api_key(name, key):
    """Grava/remove chave externa sob DPAPI; nunca persiste texto puro."""
    name = _provider_name(name)
    if key is not None and not isinstance(key, str):
        raise ValueError("API key must be text.")
    key = (key or "").strip()
    if key:
        check_provider_key_format(key)
    protected = secret_store.protect(key) if key else ""

    def mutate(cfg):
        keys = dict(cfg.get("api_keys") or {})
        if protected:
            keys[name] = protected
        else:
            keys.pop(name, None)
        if keys:
            cfg["api_keys"] = keys
        else:
            cfg.pop("api_keys", None)

    update_config(mutate)
