#!/usr/bin/env python3
"""Hook PreToolUse de comando (Codex): cliente fino que repassa o payload ao daemon do Smart Tool (POST
/hooks/pretool), onde a decisão roda já aquecida (`hook_decision.py`). O Claude Code chama a mesma rota direto, como
hook `type: "http"`. Falha aberto: daemon fora do ar ou erro = saída neutra `{}`, nunca `allow`.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import daemon_launcher

TIMEOUT_S = 14


def _client():
    args = sys.argv[1:]
    return args[args.index("--client") + 1] if "--client" in args and args.index("--client") + 1 < len(args) else ""


def main():
    raw = sys.stdin.buffer.read()
    base = daemon_launcher.daemon_url_if_up()
    if not base:
        daemon_launcher.start_daemon_if_down()
        print(json.dumps({"systemMessage": "Smart Tool: daemon subindo, roteamento pulado nesta chamada."},
                         ensure_ascii=False))
        return
    request = urllib.request.Request(f"{base}/hooks/pretool?{urllib.parse.urlencode({'client': _client()})}",
                                     data=raw, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
        output = json.loads(response.read() or b"{}")
    print(json.dumps(output if isinstance(output, dict) else {}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        print("{}")
