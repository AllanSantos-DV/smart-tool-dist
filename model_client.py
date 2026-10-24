"""Smart Tool model client: talks to the configured model gateway through its adapter."""
import contextvars
import io
import threading
import time
import urllib.error
import usage_meter

import gateway

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
    if OFFLINE.get():
        raise RuntimeError(OFFLINE.get())
    started=time.monotonic();result=None;headers=None;failure=None
    try:
        result,headers=_fetch(path,token,method,body,timeout)
        return result
    except Exception as exc:
        failure=exc
        raise
    finally:
        usage_meter.record(path,body,result,headers,failure,time.monotonic()-started)


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
