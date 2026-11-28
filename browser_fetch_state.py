"""What happened to the Camoufox browser download: a log of its lifecycle and the last state, both in the data folder.
The download runs in a separate process (the installer's browser step), so the log and the state file are how the
daemon, the setup audit and the user learn what it did. Pure stdlib so the browser environment can import it."""
import json
import os
import time

import paths

LOG_PATH = os.path.join(paths.DATA_DIR, "browser-fetch.log")
STATE_PATH = os.path.join(paths.DATA_DIR, "browser-fetch.json")
LOG_MAX_BYTES = 256 * 1024


def log(event, **fields):
    """Appends `<time> event=<event> key="value" ...` as one line; a line break inside a value is joined."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
        os.replace(LOG_PATH, LOG_PATH + ".1")
    parts = [time.strftime("%Y-%m-%d %H:%M:%S"), f"event={event}"]
    parts += [f'{key}="{" ".join(str(value).split())}"' for key, value in fields.items()]
    with open(LOG_PATH, "a", encoding="utf-8") as stream:
        stream.write(" ".join(parts) + "\n")


def write_state(state, reason="", next_action="", build=""):
    """Records `state` ("ok", "degraded" or "failed") with what broke, what to do and the build in use."""
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    record = {"state": state, "reason": reason, "next_action": next_action, "build": build,
              "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(STATE_PATH, "w", encoding="utf-8") as stream:
        json.dump(record, stream)


def read_state():
    """The last recorded state, or None when no download ever ran or the file cannot be read."""
    try:
        with open(STATE_PATH, encoding="utf-8") as stream:
            record = json.load(stream)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None
