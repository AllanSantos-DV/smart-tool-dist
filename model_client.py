"""Smart Tool model client: talks to the configured model gateway through its adapter."""
import contextvars
import time
import usage_meter

import gateway

OFFLINE = contextvars.ContextVar("model_offline", default=None)


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
    # Callers name routes as /v1/...; the adapter knows where its API starts.
    if not path.startswith("/v1/"):
        raise ValueError("Only /v1/ routes are accepted by this client.")
    return gateway.request(path[len("/v1/"):], body=body, timeout=timeout)
