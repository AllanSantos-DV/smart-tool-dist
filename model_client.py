"""Smart Tool model client: talks to the configured model gateway through its adapter."""
import contextvars
import io
import json
import threading
import time
import urllib.error
import usage_meter

import config
import gateway
import model_breaker
import model_health

OFFLINE = contextvars.ContextVar("model_offline", default=None)
_NO_TEMPERATURE = set()
_no_temperature_lock = threading.Lock()


def get_token():
    """Kept for callers that pass a token along: credentials now live in the model adapter. Fails when no adapter."""
    if OFFLINE.get():
        raise RuntimeError(OFFLINE.get())
    if gateway.selected()[0] is None:
        raise RuntimeError("No model gateway configured: choose an adapter in the setup screen.")
    return ""


def fetch(path, token, method="GET", body=None, timeout=15):
    """One gateway request; returns the parsed JSON. A call that names a model first passes that model's circuit and
    request cap (model_breaker, CircuitOpen when paused), and every attempt is metered. HTTP errors propagate."""
    if OFFLINE.get():
        raise RuntimeError(OFFLINE.get())
    model=body.get("model") if isinstance(body,dict) else None
    model_breaker.guard(model)
    with model_breaker.slot(model):
        started=time.monotonic();result=None;headers=None;failure=None
        try:
            result,headers=_fetch(path,token,method,body,timeout)
            return result
        except Exception as exc:
            failure=exc
            raise
        finally:
            model_breaker.note(model,failure)
            usage_meter.record(path,body,result,headers,failure,time.monotonic()-started)
            model_health.record(path,body,failure)


def _fetch(path, _token, method="GET", body=None, timeout=15):
    """Routes are named /v1/...; the adapter knows where its API starts. Some models (a Claude on Bedrock, the GPT-5
    family) reject `temperature` with HTTP 400; the request is repeated once without it and the model is remembered, so
    any model of the gateway works with the same call. A second refusal propagates."""
    if not path.startswith("/v1/"):
        raise ValueError("Only /v1/ routes are accepted by this client.")
    route = path[len("/v1/"):]
    model = body.get("model") if isinstance(body, dict) else None
    if isinstance(body, dict) and "temperature" in body and model in _NO_TEMPERATURE:
        body = {key: value for key, value in body.items() if key != "temperature"}
    try:
        return gateway.request(route, body=body, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 400 or not isinstance(body, dict) or "temperature" not in body:
            raise
        detail = exc.read()
        if b"temperature" not in detail.lower():
            exc.read = io.BytesIO(detail).read
            raise
    with _no_temperature_lock:
        _NO_TEMPERATURE.add(model)
    return gateway.request(route, body={key: value for key, value in body.items() if key != "temperature"},
                           timeout=timeout)


def _strict_schema(schema, unsupported):
    """Copy of a JSON Schema for the model's constrained decoding: every object closes with additionalProperties false
    and the keywords the gateway does not apply (`unsupported`) are dropped; the caller's code keeps enforcing them."""
    if isinstance(schema, list):
        return [_strict_schema(item, unsupported) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key in unsupported:
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: _strict_schema(sub, unsupported) for name, sub in value.items()}
        else:
            result[key] = _strict_schema(value, unsupported)
    if result.get("type") == "object" and "additionalProperties" not in result:
        result["additionalProperties"] = False
    return result


_JSON_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _check_shape(value, schema, where):
    """Raises ValueError naming `where` when `value` breaks the type, enum, required or additionalProperties of `schema`:
    the answer of a model is checked even when the gateway claims to apply the schema."""
    kind = schema.get("type")
    if kind == "integer":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif kind in _JSON_TYPES:
        ok = isinstance(value, _JSON_TYPES[kind]) and not (kind != "boolean" and isinstance(value, bool))
    else:
        ok = True
    if not ok:
        raise ValueError(f"structured output: {where} is not {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"structured output: {where} is {value!r}, outside {schema['enum']}")
    if kind == "object":
        properties = schema.get("properties") or {}
        missing = [name for name in schema.get("required") or [] if name not in value]
        if missing:
            raise ValueError(f"structured output: {where} lacks {', '.join(missing)}")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"structured output: {where} has unknown fields {', '.join(sorted(set(value) - set(properties)))}")
        for name, sub in properties.items():
            if name in value:
                _check_shape(value[name], sub, f"{where}.{name}")
    elif kind == "array" and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _check_shape(item, schema["items"], f"{where}[{index}]")


def _route(model):
    """The gateway route of a model id: the text before its first dot, slash or colon (`vertex_ai.anthropic.claude-...`)."""
    return str(model or "").replace("/", ".").replace(":", ".").split(".")[0].lower()


def structured(model, system, user, tool, timeout=30):
    """One chat call whose answer is a JSON object validated against the parameters schema of `tool` (the tool
    definition is only the carrier of name and schema; `system` may be empty). The request format comes from the `structured_output`
    configuration: `output_config` for the routes listed there, `response_format` otherwise; never `temperature`.
    Raises RuntimeError on a stop other than a normal one, a refusal or an empty answer, and ValueError when the
    object breaks the schema."""
    settings = config.structured_output()
    schema = _strict_schema(tool["function"]["parameters"], set(settings["unsupported_keywords"]))
    route = _route(model)
    routes = {item.lower() for item in settings["output_config_routes"]}
    if settings["mode"] == "output_config" and route not in routes:
        raise ValueError(f"structured_output.mode is output_config but the route {route!r} of {model} is not in "
                         f"output_config_routes {sorted(routes)}.")
    messages = [{"role": "system", "content": system}] if system else []
    body = {"model": model, "messages": messages + [{"role": "user", "content": user}]}
    if settings["mode"] == "output_config" or (settings["mode"] == "auto" and route in routes):
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    else:
        body["response_format"] = {"type": "json_schema", "json_schema": {
            "name": tool["function"]["name"], "strict": True, "schema": schema}}
    response = fetch("/v1/chat/completions", "", method="POST", body=body, timeout=timeout)
    choice = response["choices"][0]
    message = choice.get("message") or {}
    name = tool["function"]["name"]
    if message.get("refusal"):
        raise RuntimeError(f"{name}: the model refused ({str(message['refusal'])[:200]})")
    if choice.get("finish_reason") != "stop":
        raise RuntimeError(f"{name}: the model stopped with {choice.get('finish_reason')!r} instead of finishing the answer")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"{name}: the model returned no content")
    value = json.loads(content)
    _check_shape(value, schema, name)
    return value
