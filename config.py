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

# The config fields that select a gateway model; every per-role table (recommended models, failure log) keys on these.
MODEL_ROLES = ("router_model", "scope_model", "scout_model", "research_model", "docs_model", "embedding_model",
               "rerank_model")

# Extra documents written by `project_manage action=docs generate=true` beside docs/scope's three levels; each key is a
# document and true (the default) writes it, false skips it and removes the file Smart Tool wrote earlier.
DOCS_EXTRAS = ("structure", "coverage", "modules", "tests", "configuration", "dependencies", "adr", "flows",
               "features_tests", "hotspots", "coupling", "dead_code", "tour", "glossary", "index")

DEFAULT_CONFIG = {
    "router_model": "",
    "scope_model": "",
    "scout_model": "",
    "embedding_model": "",
    "rerank_model": "",
    "research_model": "",
    "docs_model": "",
    "research_max_iterations": 3,
    "quick_search_timeout_s": 120,
    "site_memory_similarity_threshold": 0.35,
    "embedder": "home",
    "embeddings_home_url": "",
    "hook_mode": "redirect",
    "doc_mode": "require",
    "comment_mode": "require",
    "duplicate_mode": "warn",
    "dependency_mode": "warn",
    "layer_mode": "warn",
    "link_mode": "warn",
    "pr_comment": "off",
    "decision_mode": "warn",
    "risky_mode": "warn",
    "document_converter": "off",
    "document_converter_python": "",
    "docs_extras": {key: True for key in DOCS_EXTRAS},
    "auto_update": True,
    "language": "",
    "pattern_proposals": "on",
    "contact": "",
    "model_adapter": "",
    "model_adapter_options": {},
    "structured_output": {
        "mode": "auto",
        "output_config_routes": ["vertex_ai"],
        "unsupported_keywords": ["maxItems", "minItems", "minLength", "maxLength", "minimum", "maximum", "pattern"],
    },
}

# Embedder of the project indexes: home = the embeddings home daemon of this machine (casa-de-embeddings), whose rerank
# connector forwards to the host configured in it; gateway = the gateway's embedding_model and rerank_model, only when
# the user chooses it. Neither replaces the other when it fails.
EMBEDDERS = ("home", "gateway")
# PreToolUse hook: redirect = deny the native tool and point to Smart Tool; advise = let it run and add the tip to the
# agent's context; off = no routing at all.
HOOK_MODES = ("redirect", "advise", "off")
# Edit hook on functions without a docstring: require = deny the edit until it adds one; remind = let it run and tell
# the agent; off = say nothing. Independent of hook_mode, which only routes searches and reads.
DOC_MODES = ("require", "remind", "off")
# Edit hook on comments an edit adds in the wrong place (commented-out code, ticket or history tags, a loose block above
# a function instead of its docstring, a long block inside a body): require = deny the edit until they are gone;
# remind = let it run and tell the agent; off = say nothing. A short comment explaining a non-obvious reason is allowed.
COMMENT_MODES = ("require", "remind", "off")
# Edit hook on functions that copy one already indexed (exact or near-identical body): warn = let the edit run and
# point to the existing function; off = say nothing. Never blocks: near matches are leads, not defects.
DUPLICATE_MODES = ("warn", "off")
# Edit hook on package.json, requirements*.txt and go.mod: warn = let the edit run and list the added or changed dependencies that have OSV advisories;
# off = say nothing and make no network call.
DEPENDENCY_MODES = ("warn", "off")
LAYER_MODES = ("warn", "off")
# Edit hook on HTML and Markdown files: warn = let the edit run and name the local links it adds or changes that point to a
# file that does not exist, and the ones that cannot be checked; off = say nothing. Never blocks.
LINK_MODES = ("warn", "off")
# Posting the pr_impact report as a comment on a pull request through the gh CLI, a write to GitHub with the user's own
# login: off = never; on = project_manage action=pr_impact_comment may post. Only the user turns it on, in the setup screen.
PR_COMMENT_MODES = ("off", "on")
# Edit hook on the functions an edit touches: warn = let the edit run and name the notes and commit messages of the project
# that record a decision about them; off = say nothing. Never blocks.
DECISION_MODES = ("warn", "off")
# Edit hook on the unsafe calls an edit adds (shell=True, os.system, pickle.load, yaml.load, verify=False, eval, exec, innerHTML on
# a non-literal): warn = let the edit run and name file, line and kind of each added call; off = say nothing. Never blocks.
RISKY_MODES = ("warn", "off")
# External converter read_document falls back to for a file type or a scanned PDF its native reader refuses: off = never;
# auto = the first of kreuzberg and docling found installed; or one of them by name. The engine is looked for in the
# interpreter named by `document_converter_python` (empty = the one Smart Tool runs on).
DOCUMENT_CONVERTERS = ("off", "auto", "kreuzberg", "docling")
# Router model reviewing, in the background, searches the deterministic redirect rule let run: on = propose block
# patterns for the user to accept in the setup screen; off = no review.
PATTERN_PROPOSAL_MODES = ("on", "off")
# Structured output of the model calls that must return JSON (research, scout, freshness): response_format = the
# OpenAI-style `response_format` json_schema (applied on every gateway route); output_config = Anthropic `output_config`
# in the chat body (also reports truncation as finish_reason "length"); auto = output_config for the Claude models whose route
# (the id prefix before the first dot) is in `output_config_routes`, response_format for all others (Gemini on
# vertex_ai ignores output_config and answers loose text).
STRUCTURED_OUTPUT_MODES = ("auto", "response_format", "output_config")


def doc_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("doc_mode") or DEFAULT_CONFIG["doc_mode"]
    if value not in DOC_MODES:
        raise ValueError(f"Invalid doc_mode ({value!r}) in {CONFIG_PATH}: use require, remind or off.")
    return value


def comment_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("comment_mode") or DEFAULT_CONFIG["comment_mode"]
    if value not in COMMENT_MODES:
        raise ValueError(f"Invalid comment_mode ({value!r}) in {CONFIG_PATH}: use require, remind or off.")
    return value


def duplicate_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("duplicate_mode") or DEFAULT_CONFIG["duplicate_mode"]
    if value not in DUPLICATE_MODES:
        raise ValueError(f"Invalid duplicate_mode ({value!r}) in {CONFIG_PATH}: use warn or off.")
    return value


def dependency_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("dependency_mode") or DEFAULT_CONFIG["dependency_mode"]
    if value not in DEPENDENCY_MODES:
        raise ValueError(f"Invalid dependency_mode ({value!r}) in {CONFIG_PATH}: use warn or off.")
    return value


def layer_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("layer_mode") or DEFAULT_CONFIG["layer_mode"]
    if value not in LAYER_MODES:
        raise ValueError(f"Invalid layer_mode ({value!r}) in {CONFIG_PATH}: use warn or off.")
    return value


def link_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("link_mode") or DEFAULT_CONFIG["link_mode"]
    if value not in LINK_MODES:
        raise ValueError(f"Invalid link_mode ({value!r}) in {CONFIG_PATH}: use warn or off.")
    return value


def pr_comment(cfg=None):
    """The user's consent to post the impact report on a pull request: `off` (default) or `on`; any other value fails loudly."""
    value = (cfg if cfg is not None else load_config()).get("pr_comment") or DEFAULT_CONFIG["pr_comment"]
    if value not in PR_COMMENT_MODES:
        raise ValueError(f"Invalid pr_comment ({value!r}) in {CONFIG_PATH}: use off or on.")
    return value


def decision_mode(cfg=None):
    """The edit hook's decision-notes mode, `warn` or `off`; raises ValueError for any other value in the config file."""
    value = (cfg if cfg is not None else load_config()).get("decision_mode") or DEFAULT_CONFIG["decision_mode"]
    if value not in DECISION_MODES:
        raise ValueError(f"Invalid decision_mode ({value!r}) in {CONFIG_PATH}: use warn or off.")
    return value


def risky_mode(cfg=None):
    """The edit hook's risky-calls mode, `warn` or `off`; raises ValueError for any other value in the config file."""
    value = (cfg if cfg is not None else load_config()).get("risky_mode") or DEFAULT_CONFIG["risky_mode"]
    if value not in RISKY_MODES:
        raise ValueError(f"Invalid risky_mode ({value!r}) in {CONFIG_PATH}: use warn or off.")
    return value


def document_converter(cfg=None):
    """The external converter setting: `{"mode": one of DOCUMENT_CONVERTERS, "python": interpreter path or ""}`. An
    unknown mode or a python that is not text fails loudly."""
    stored = cfg if cfg is not None else load_config()
    mode = stored.get("document_converter") or DEFAULT_CONFIG["document_converter"]
    python = stored.get("document_converter_python") or ""
    if mode not in DOCUMENT_CONVERTERS:
        raise ValueError(f"Invalid document_converter ({mode!r}) in {CONFIG_PATH}: use off, auto, kreuzberg or docling.")
    if not isinstance(python, str):
        raise ValueError(f"Invalid document_converter_python in {CONFIG_PATH}: a path to a Python interpreter.")
    return {"mode": mode, "python": python}


def docs_extras(cfg=None):
    """Which extra documents the docs generator writes: every key of DOCS_EXTRAS, true unless the stored `docs_extras`
    object sets it to false; an unknown key or a value that is not true or false fails loudly."""
    stored = (cfg if cfg is not None else load_config()).get("docs_extras")
    if stored is None:
        stored = {}
    if not isinstance(stored, dict) or set(stored) - set(DOCS_EXTRAS) or any(type(v) is not bool for v in stored.values()):
        raise ValueError(f"Invalid docs_extras in {CONFIG_PATH}: an object with only {', '.join(DOCS_EXTRAS)}, each true or false.")
    return {**{key: True for key in DOCS_EXTRAS}, **stored}


def pattern_proposals(cfg=None):
    """on: the router model reviews searches the redirect rule let run and proposes block patterns for the user to
    accept; off: no review."""
    value = (cfg if cfg is not None else load_config()).get("pattern_proposals") or DEFAULT_CONFIG["pattern_proposals"]
    if value not in PATTERN_PROPOSAL_MODES:
        raise ValueError(f"Invalid pattern_proposals ({value!r}) in {CONFIG_PATH}: use on or off.")
    return value


def structured_output(cfg=None):
    """`{"mode", "output_config_routes", "unsupported_keywords"}`: the stored `structured_output` object over the
    defaults; an unknown mode, key or value type fails loudly."""
    stored = (cfg if cfg is not None else load_config()).get("structured_output")
    if stored is None:
        stored = {}
    defaults = DEFAULT_CONFIG["structured_output"]
    if not isinstance(stored, dict) or set(stored) - set(defaults):
        raise ValueError(f"Invalid structured_output in {CONFIG_PATH}: an object with only {', '.join(defaults)}.")
    value = {**defaults, **stored}
    if value["mode"] not in STRUCTURED_OUTPUT_MODES:
        raise ValueError(f"Invalid structured_output.mode ({value['mode']!r}) in {CONFIG_PATH}: use auto, "
                         "response_format or output_config.")
    for key in ("output_config_routes", "unsupported_keywords"):
        if not isinstance(value[key], list) or not all(isinstance(item, str) and item for item in value[key]):
            raise ValueError(f"Invalid structured_output.{key} in {CONFIG_PATH}: a list of non-empty text.")
    return value


def auto_update(cfg=None):
    """Whether the tray installs a newer release by itself when it starts (once per version)."""
    value = (cfg if cfg is not None else load_config()).get("auto_update", DEFAULT_CONFIG["auto_update"])
    if not isinstance(value, bool):
        raise ValueError(f"Invalid auto_update ({value!r}) in {CONFIG_PATH}: use true or false.")
    return value


LANGUAGES = ("pt-BR", "en")


def language(cfg=None):
    """Interface language the user picked ("pt-BR" or "en"), or "" to follow the Windows display language."""
    value = (cfg if cfg is not None else load_config()).get("language", DEFAULT_CONFIG["language"])
    if value not in ("", *LANGUAGES):
        raise ValueError(f"Invalid language ({value!r}) in {CONFIG_PATH}: use one of {', '.join(LANGUAGES)} or \"\".")
    return value


def hook_mode(cfg=None):
    value = (cfg if cfg is not None else load_config()).get("hook_mode") or DEFAULT_CONFIG["hook_mode"]
    if value not in HOOK_MODES:
        raise ValueError(f"Invalid hook_mode ({value!r}) in {CONFIG_PATH}: use redirect, advise or off.")
    return value


def embedder(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    if "local_models" in cfg:
        raise ValueError(f"{CONFIG_PATH} has the old 'local_models' setting: delete it and choose the embedder (home or "
                         "gateway) in the setup screen.")
    value = cfg.get("embedder") or DEFAULT_CONFIG["embedder"]
    if value not in EMBEDDERS:
        raise ValueError(f"Invalid embedder ({value!r}) in {CONFIG_PATH}: use home or gateway.")
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
