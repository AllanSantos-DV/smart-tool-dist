"""Built-in model adapter for any OpenAI-compatible API: OpenAI, OpenRouter, a LiteLLM proxy, Ollama, vLLM...

The base URL ends where the OpenAI SDK's `base_url` ends (usually `/v1`); the API key, when given, goes as a Bearer
token. A model adapter is a module with NAME, LABEL, FIELDS, request() and verify(); see gateway.py for the contract.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

NAME = "openai_compatible"
LABEL = "OpenAI-compatible"
FIELDS = (
    {"name": "base_url", "label": "Base URL", "required": True, "placeholder": "https://api.openai.com/v1",
     "hint": "Where the API starts, like the OpenAI SDK base_url: https://openrouter.ai/api/v1, "
             "http://localhost:11434/v1."},
    {"name": "api_key", "label": "API key", "secret": True,
     "hint": "Optional (Ollama and local proxies do not need one)."},
)
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _base(options):
    url = str(options.get("base_url") or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.query or parsed.fragment:
        raise ValueError("Enter the OpenAI-compatible base URL, for example https://api.openai.com/v1.")
    return url


def request(options, route, body=None, timeout=15):
    """POST when body is given, GET otherwise. Returns (json, response headers); HTTP errors propagate."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if options.get("api_key"):
        headers["Authorization"] = "Bearer " + options["api_key"]
    req = urllib.request.Request(f"{_base(options)}/{route}", data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    with _OPENER.open(req, timeout=timeout) as response:
        return json.load(response), response.headers


def verify(options):
    url = _base(options)
    try:
        data, _headers = request(options, "models", timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(f"{url} rejected the API key (HTTP {exc.code}).") from None
        if exc.code == 404:
            raise RuntimeError(f"{url}/models does not exist (HTTP 404): the base URL must end where the API "
                               "starts, usually at /v1.") from None
        raise RuntimeError(f"{url} answered HTTP {exc.code} on /models.") from None
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{url} did not respond on /models: {getattr(exc, 'reason', exc)}") from None
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise RuntimeError(f"{url}/models did not return the list in OpenAI format ({{\"data\": [...]}}).")
