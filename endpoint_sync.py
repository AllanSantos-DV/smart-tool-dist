"""Aponta os clientes para a porta em que o daemon subiu, só quando ela muda.

O daemon é dono da porta: depois do bind, reescreve apenas as URLs do smart-tool que os clientes guardam (MCP do
Claude Code em ~/.claude.json, hook HTTP em ~/.claude/settings.json, MCP do Codex em ~/.codex/config.toml). Nada roda
por prompt ou por sessão; o Claude Code relê hooks editados pelo file watcher, o MCP já conectado precisa de /mcp.
Cada arquivo é relido logo antes da gravação atômica: o Claude Code reescreve o ~/.claude.json com frequência.
"""
import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse

SERVER = "smart-tool"
HOOK_PATH = "/hooks/pretool"
_CODEX_URL_RE = re.compile(r'(\[mcp_servers\.smart-tool\][^\[]*?\burl\s*=\s*")http://127\.0\.0\.1:\d+(/mcp")')


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def claude_dir():
    """Claude Code keeps settings.json in CLAUDE_CONFIG_DIR when set, else ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or _home(".claude")


def claude_json():
    """User-scope MCP servers: $CLAUDE_CONFIG_DIR/.claude.json when set, else ~/.claude.json."""
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(custom, ".claude.json") if custom else _home(".claude.json")


def _rebase(url, base):
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "127.0.0.1":
        return url
    return urllib.parse.urlunsplit(urllib.parse.urlsplit(base)[:2] + parsed[2:])


def _claude_mcp(text, base):
    data = json.loads(text)
    entry = (data.get("mcpServers") or {}).get(SERVER) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
        return None
    new = _rebase(entry["url"], base)
    if new == entry["url"]:
        return None
    entry["url"] = new
    return json.dumps(data, indent=2, ensure_ascii=False)


def _claude_hook(text, base):
    data = json.loads(text)
    changed = False
    groups = ((data.get("hooks") or {}).get("PreToolUse") or []) if isinstance(data, dict) else []
    for group in groups if isinstance(groups, list) else []:
        for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
            if isinstance(hook, dict) and HOOK_PATH in str(hook.get("url", "")):
                new = _rebase(hook["url"], base)
                changed, hook["url"] = changed or new != hook["url"], new
    return json.dumps(data, indent=2, ensure_ascii=False) if changed else None


def _codex_mcp(text, base):
    port = urllib.parse.urlsplit(base).port
    new = _CODEX_URL_RE.sub(lambda m: f"{m.group(1)}http://127.0.0.1:{port}{m.group(2)}", text, count=1)
    return new if new != text else None


def targets():
    return ((claude_json(), _claude_mcp), (os.path.join(claude_dir(), "settings.json"), _claude_hook),
            (_home(".codex", "config.toml"), _codex_mcp))


_CODEX_ANY_URL_RE = re.compile(r'\[mcp_servers\.smart-tool\][^\[]*?\burl\s*=\s*"([^"]+)"')


def registrations():
    """smart-tool MCP URL kept by each client, or None when it is not registered there."""
    found = {"claude": None, "codex": None}
    for attempt in range(2):
        try:
            with open(claude_json(), encoding="utf-8") as stream:
                entry = (json.load(stream).get("mcpServers") or {}).get(SERVER)
            found["claude"] = entry.get("url") if isinstance(entry, dict) else None
            break
        except ValueError:
            # Claude Code rewrites the file all the time; one more read gets past a half-written copy.
            time.sleep(0.2)
        except (OSError, AttributeError):
            break
    try:
        with open(_home(".codex", "config.toml"), encoding="utf-8") as stream:
            match = _CODEX_ANY_URL_RE.search(stream.read())
        found["codex"] = match.group(1) if match else None
    except OSError:
        pass
    return found


REGISTER_TIMEOUT_S = 60


def register_command(client, base):
    """Official CLI command that registers the smart-tool MCP server (Claude Code: user scope, every project)."""
    url = f"{base}/mcp"
    if client == "claude":
        return ["claude", "mcp", "add", "--transport", "http", SERVER, url, "--scope", "user"]
    if client == "codex":
        return ["codex", "mcp", "add", SERVER, "--url", url]
    raise ValueError(f"Client without MCP registration: {client!r}")


def register(client, base):
    """Runs the client's own CLI (it owns its config file; ~/.claude.json is rewritten by Claude Code all the time)."""
    command = register_command(client, base)
    run_cli(command)
    if registrations().get(client) != f"{base}/mcp":
        raise RuntimeError(f"'{' '.join(command)}' finished, but the registration did not appear in the client's configuration.")
    return " ".join(command)


def run_cli(command):
    """Runs a client CLI command (claude, codex) found on PATH, killing its whole shim tree after REGISTER_TIMEOUT_S.
    Raises RuntimeError with the command and the CLI's own message when it is missing, hangs or exits non-zero."""
    executable = shutil.which(command[0])
    if not executable:
        raise RuntimeError(f"CLI '{command[0]}' not found in Smart Tool's PATH. Register it manually: {' '.join(command)}")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen([executable, *command[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                creationflags=flags)
    except OSError as exc:
        raise RuntimeError(f"'{' '.join(command)}' did not start: {exc}") from None
    try:
        stdout, stderr = proc.communicate(timeout=REGISTER_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        # npm shims are cmd.exe -> node: killing only cmd.exe leaves node holding the pipes forever.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=flags)
        else:
            proc.kill()
        proc.communicate(timeout=10)
        raise RuntimeError(f"'{' '.join(command)}' did not finish in {REGISTER_TIMEOUT_S}s.") from None
    if proc.returncode != 0:
        detail = (stderr or stdout).strip()[-400:]
        raise RuntimeError(f"'{' '.join(command)}' failed (code {proc.returncode}): {detail}")


def known_port():
    """Port the Claude Code MCP entry already points to, so a fallback bind can reuse it without any rewrite."""
    try:
        parsed = urllib.parse.urlsplit(registrations()["claude"] or "")
        return parsed.port if parsed.hostname == "127.0.0.1" else None
    except ValueError:
        return None


def sync(base):
    """Rewrites the smart-tool URLs that point to another local port. Returns the files changed."""
    changed = []
    for path, rewrite in targets():
        for _ in range(2):
            try:
                with open(path, encoding="utf-8") as stream:
                    text = stream.read()
            except FileNotFoundError:
                break
            new = rewrite(text, base)
            if new is None:
                break
            with open(path, encoding="utf-8") as stream:
                if stream.read() != text:
                    continue
            temporary = f"{path}.smart-tool-tmp"
            with open(temporary, "w", encoding="utf-8", newline="") as stream:
                stream.write(new)
            os.replace(temporary, path)
            changed.append(path)
            break
    return changed
