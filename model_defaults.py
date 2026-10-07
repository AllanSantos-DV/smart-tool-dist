"""Preenche modelos recomendados somente após consultar o catálogo do gateway configurado."""

import re

import config
import model_catalog


# Matched as the last path/dot segment of the catalog id, so "gpt-4o-mini" also finds "openai/gpt-4o-mini" (OpenRouter)
# and "openai.gpt-4o-mini" (LiteLLM aliases). Router: small, no reasoning (it runs before tool calls).
RECOMMENDED_MODELS = {
    "router_model": ("chat", "gpt-4o-mini"),
    "scope_model": ("chat", "gpt-5-mini"),
    "research_model": ("chat", "gpt-oss-20b"),
    "embedding_model": ("embedding", "text-embedding-3-small"),
    "rerank_model": ("rerank", "rerank-3-5"),
}


def _matching(catalog, name):
    return next((model_id for model_id in sorted(catalog) if re.split(r"[/.:]", model_id)[-1] == name
                 or model_id.endswith(("/" + name, "." + name)) or model_id == name), None)


def ensure_recommended_models(key_ready):
    """Preenche só campos vazios e só com IDs presentes no catálogo permitido.

    Retorna os campos ainda vazios e uma explicação curta para a tela de setup/tray.
    Um arquivo corrompido ou uma seleção manual nunca é substituído.
    """
    corrupted = config.corrupted_reason()
    if corrupted:
        return {"status": "config_corrupt", "missing": list(RECOMMENDED_MODELS),
                "message": corrupted}

    cfg = config.load_config()
    missing = [field for field in RECOMMENDED_MODELS if not cfg.get(field)]
    if not key_ready:
        return {"status": "needs_key", "missing": missing,
                "message": "Model gateway unavailable; check the base URL and the API key."}
    if not missing:
        return {"status": "ready", "missing": [], "message": "Models configured."}

    try:
        available = {mode: set(model_catalog.list_models(mode)) for mode in
                     {RECOMMENDED_MODELS[field][0] for field in missing}}
    except Exception:
        return {"status": "catalog_unavailable", "missing": missing,
                "message": "Catalog unavailable. Check the base URL, the API key and the connection to the gateway."}

    choices = {field: found for field in missing
               for mode, name in [RECOMMENDED_MODELS[field]]
               for found in [_matching(available[mode], name)] if found}
    if choices:
        def fill_empty(current):
            for field, model_id in choices.items():
                if not current.get(field):
                    current[field] = model_id

        try:
            cfg = config.update_config(fill_empty)
        except (config.ConfigCorruptedError, OSError):
            return {"status": "config_corrupt", "missing": missing,
                    "message": "Could not save the configuration. Open Smart Tool to check the file."}

    missing = [field for field in RECOMMENDED_MODELS if not cfg.get(field)]
    if missing:
        return {"status": "needs_models", "missing": missing,
                "message": "Some recommended models are not in this gateway's catalog. Choose them in the settings."}
    return {"status": "ready", "missing": [], "message": "Recommended models configured."}
