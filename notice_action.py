#!/usr/bin/env python3
"""Runs a toast button: `pythonw notice_action.py "smart-tool-action:<action>?<query>"`.

Started by Windows through the protocol registered in `toast_notice.register`, with no window. Every outcome (done or
refused) is appended to `notice-actions.log` in the data folder, because nothing else can show an error here.
"""
import os
import sys
import time
import urllib.parse
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import model_upgrade
import paths
import toast_notice

LOG_PATH = os.path.join(paths.DATA_DIR, "notice-actions.log")


def run(uri):
    """Returns a short result line. Raises ValueError for a link that is not ours or an action that does not exist."""
    prefix = toast_notice.PROTOCOL + ":"
    if not uri.startswith(prefix):
        raise ValueError(f"Not a Smart Tool link: {uri[:80]!r}")
    action, _, query = uri[len(prefix):].partition("?")
    params = dict(urllib.parse.parse_qsl(query))
    if action == "models-apply":
        return model_upgrade.apply(params.get("n", ""))
    if action == "models-decline":
        return model_upgrade.decline(params.get("n", ""))
    if action == "open-setup":
        import daemon_launcher
        webbrowser.open(daemon_launcher.ensure_daemon_running().rstrip("/") + "/setup#" + params.get("section", "models"))
        return "setup opened"
    if action == "mute-model":
        import model_health
        return model_health.mute(params.get("model", ""))
    raise ValueError(f"Unknown action {action!r}")


def main(argv):
    """Runs the link in `argv[1]` and logs the outcome; returns the process exit code (0 done, 1 refused)."""
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        line = f"{stamp} ok {argv[1] if len(argv) > 1 else ''} -> {run(argv[1])}"
        code = 0
    except Exception as exc:
        line = f"{stamp} FAIL {argv[1] if len(argv) > 1 else ''} -> {type(exc).__name__}: {exc}"
        code = 1
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        log.write(line + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
