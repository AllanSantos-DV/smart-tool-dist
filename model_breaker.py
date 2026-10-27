"""Protection of the model gateway, per model id: a circuit that opens after consecutive 429/503 answers and a cap on
the requests in flight.

Pattern: a circuit breaker (closed, open for a pause, then one trial call decides). Retrying a gateway that answers
busy only adds load (AWS Bedrock throughput guide: honor Retry-After, back off with jitter); once it keeps answering
busy the caller stops asking for a while and serves what it can without the model.
"""
import contextlib
import threading
import time
import urllib.error

FAILURES_TO_OPEN = 3
PAUSE_S = 30
MAX_IN_FLIGHT = 6
_BUSY_CODES = (429, 503)
_lock = threading.Lock()
_failures = {}
_open_until = {}
_slots = {}


class CircuitOpen(RuntimeError):
    """The model is paused after repeated busy answers; no request was sent."""


def guard(model):
    """Raises CircuitOpen while `model` is paused. Nothing happens for a call without a model id."""
    if not model:
        return
    with _lock:
        left = _open_until.get(model, 0) - time.monotonic()
    if left > 0:
        raise CircuitOpen(f"Model {model} is paused for {int(left) + 1} s after {FAILURES_TO_OPEN} consecutive "
                          "HTTP 429/503 answers from the gateway; no request was sent.")


def note(model, failure=None):
    """Records the outcome of a request: a success closes the circuit, a 429/503 counts toward opening it, any other
    failure changes nothing. After the pause the first failure opens it again."""
    if not model:
        return
    busy = isinstance(failure, urllib.error.HTTPError) and failure.code in _BUSY_CODES
    with _lock:
        if failure is None:
            _failures[model] = 0
        elif busy:
            _failures[model] = _failures.get(model, 0) + 1
            if _failures[model] >= FAILURES_TO_OPEN:
                _open_until[model] = time.monotonic() + PAUSE_S


@contextlib.contextmanager
def slot(model):
    """Holds one of the `MAX_IN_FLIGHT` places of `model` for the duration of a request."""
    if not model:
        yield
        return
    with _lock:
        semaphore = _slots.setdefault(model, threading.BoundedSemaphore(MAX_IN_FLIGHT))
    with semaphore:
        yield


def reset():
    """Forgets every circuit and cap (tests)."""
    with _lock:
        _failures.clear()
        _open_until.clear()
        _slots.clear()
