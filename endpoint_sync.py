"""Aponta os clientes para a porta em que o daemon subiu, só quando ela muda.

O daemon é dono da porta: depois do bind, reescreve apenas as URLs do smart-tool que os clientes guardam (MCP do
Claude Code em ~/.claude.json, hook HTTP em ~/.claude/settings.json, MCP do Codex em ~/.codex/config.toml). Nada roda
por prompt ou por sessão; o Claude Code relê hooks editados pelo file watcher, o MCP já conectado precisa de /mcp.
Cada arquivo é relido logo antes da gravação atômica: o Claude Code reescreve o ~/.claude.json com frequência.
"""
import json
import os
import re
import subprocess
import time
import urllib.parse

import node_locator

SERVER = "smart-tool"
HOOK_PATH = "/hooks/pretool"
POST_HOOK_PATH = "/hooks/posttool"
_CODEX_URL_RE = re.compile(r'(\[mcp_servers\.smart-tool\][^\[]*?\burl\s*=\s*")http://127\.0\.0\.1:\d+(/mcp")')


def _home(*parts):
    return os.path.join(os.path.expanduser("~"), *parts)


def claude_dir():
    """Claude Code keeps settings.json in CLAUDE_CONFIG_DIR when set, else ~/.claude."""
    return os.environ.get("CLAUDE_CONFIG_DIR") or _home(".claude")


def codex_dir():
    """Codex keeps config.toml and hooks.json in CODEX_HOME when set, else ~/.codex."""
    return os.environ.get("CODEX_HOME") or _home(".codex")


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
    """settings.json text with our PreToolUse and PostToolUse hook URLs moved to base, or None when none changed."""
    data = json.loads(text)
    changed = False
    hooks = (data.get("hooks") or {}) if isinstance(data, dict) else {}
    for event, path in (("PreToolUse", HOOK_PATH), ("PostToolUse", POST_HOOK_PATH)):
        groups = (hooks.get(event) or []) if isinstance(hooks, dict) else []
        for group in groups if isinstance(groups, list) else []:
            for hook in (group.get("hooks") or []) if isinstance(group, dict) else []:
                if isinstance(hook, dict) and path in str(hook.get("url", "")):
                    new = _rebase(hook["url"], base)
                    changed, hook["url"] = changed or new != hook["url"], new
    return json.dumps(data, indent=2, ensure_ascii=False) if changed else None


def _codex_mcp(text, base):
    port = urllib.parse.urlsplit(base).port
    new = _CODEX_URL_RE.sub(lambda m: f"{m.group(1)}http://127.0.0.1:{port}{m.group(2)}", text, count=1)
    return new if new != text else None


def targets():
    return ((claude_json(), _claude_mcp), (os.path.join(claude_dir(), "settings.json"), _claude_hook),
            (os.path.join(codex_dir(), "config.toml"), _codex_mcp))


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
        with open(os.path.join(codex_dir(), "config.toml"), encoding="utf-8") as stream:
            match = _CODEX_ANY_URL_RE.search(stream.read())
        found["codex"] = match.group(1) if match else None
    except OSError:
        pass
    return found


REGISTER_TIMEOUT_S = 120
TIMEOUT_ENV = "SMART_TOOL_CLI_TIMEOUT_S"
CLI_EXTENSIONS = (".exe", ".cmd", ".bat")
NPM_CLAUDE_FIX = "npm install -g @anthropic-ai/claude-code@latest --include=optional --allow-scripts=@anthropic-ai/claude-code"
_NPM_CLAUDE_BINARY = os.path.join("node_modules", "@anthropic-ai", "claude-code", "bin", "claude.exe")
_CMD_METACHARS = re.compile(r'[&|<>^%!"\r\n]')


def register_command(client, base):
    """Official CLI command that registers the smart-tool MCP server (Claude Code: user scope, every project)."""
    url = f"{base}/mcp"
    if client == "claude":
        return ["claude", "mcp", "add", "--transport", "http", SERVER, url, "--scope", "user"]
    if client == "codex":
        return ["codex", "mcp", "add", SERVER, "--url", url]
    raise ValueError(f"Client without MCP registration: {client!r}")


def remove_command(client):
    """Official CLI command that removes the smart-tool MCP server a client holds."""
    if client == "claude":
        return ["claude", "mcp", "remove", SERVER, "--scope", "user"]
    if client == "codex":
        return ["codex", "mcp", "remove", SERVER]
    raise ValueError(f"Client without MCP registration: {client!r}")


def register(client, base):
    """Makes the client's smart-tool MCP entry point to base, through the client's own CLI (it owns its config file;
    ~/.claude.json is rewritten by Claude Code all the time). Idempotent: the same URL runs nothing, another URL (the
    port changed) is removed first because `mcp add` refuses an existing name, no entry only adds. Returns what was run."""
    url = f"{base}/mcp"
    current = registrations().get(client)
    if current == url:
        return f"nothing to run: {client} already points to {url}"
    command = register_command(client, base)
    if current:
        run_cli(remove_command(client))
    run_cli(command)
    if registrations().get(client) != url:
        raise RuntimeError(f"'{' '.join(command)}' finished, but the registration did not appear in the client's configuration.")
    return " ".join(command)


def cli_folders():
    """Where the native installers of the clients put their CLI, for a PATH that does not list them."""
    home, local, roaming = os.path.expanduser("~"), os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    folders = [os.path.join(home, ".local", "bin")]
    if local:
        folders.append(os.path.join(local, "Programs", "OpenAI", "Codex", "bin"))
    if roaming:
        folders.append(os.path.join(roaming, "npm"))
    return folders


def resolve_cli(name):
    """Full path of the client CLI `name` as a .exe, .cmd or .bat (never a .ps1 or the extensionless script npm leaves
    beside its shims, which Windows cannot run), looked up in the PATH as Windows stores it now and then in cli_folders;
    None when absent."""
    for folder in node_locator.search_path() + cli_folders():
        for extension in CLI_EXTENSIONS:
            path = os.path.join(folder, name + extension)
            if os.path.isfile(path):
                return path
    return None


def _is_native(path):
    with open(path, "rb") as stream:
        return stream.read(2) == b"MZ"


def check_runnable(executable):
    """Raises RuntimeError, without running anything, when executable is a .exe that is not a Windows program or an npm
    shim whose package holds such a file: Windows answers those with a modal "not compatible" box and the command hangs.
    That is how Claude Code installed by npm 11 ends up when its postinstall is blocked (a ~500-byte text claude.exe)."""
    if executable.lower().endswith(".exe"):
        broken = executable if not _is_native(executable) else None
    else:
        package_binary = os.path.join(os.path.dirname(executable), _NPM_CLAUDE_BINARY)
        broken = package_binary if os.path.isfile(package_binary) and not _is_native(package_binary) else None
    if not broken:
        return
    if os.path.join("@anthropic-ai", "claude-code") in broken:
        raise RuntimeError(f"Claude Code was installed by npm without its native binary ({broken} is a text file). "
                           f"Run: {NPM_CLAUDE_FIX}")
    raise RuntimeError(f"{broken} is not a Windows executable (it does not start with MZ); reinstall that CLI.")


def cli_timeout_s():
    """Seconds a client CLI may run: SMART_TOOL_CLI_TIMEOUT_S when set (a positive number), else REGISTER_TIMEOUT_S."""
    raw = os.environ.get(TIMEOUT_ENV)
    if raw is None:
        return REGISTER_TIMEOUT_S
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0
    if seconds <= 0:
        raise RuntimeError(f"{TIMEOUT_ENV} must be a positive number of seconds, not {raw!r}.")
    return seconds


def run_cli(command):
    """Runs a client CLI command (claude, codex) found by resolve_cli, killing its whole shim tree after cli_timeout_s.
    Raises RuntimeError with the command and the CLI's own message when it is missing, cannot run, hangs or exits non-zero."""
    executable = resolve_cli(command[0])
    if not executable:
        raise RuntimeError(f"CLI '{command[0]}' not found in Smart Tool's PATH. Register it manually: {' '.join(command)}")
    check_runnable(executable)
    timeout = cli_timeout_s()
    unsafe = [arg for arg in command[1:] if _CMD_METACHARS.search(arg)]
    if executable.lower().endswith((".cmd", ".bat")) and unsafe:
        # cmd.exe parses a .cmd shim's arguments again: & | < > ^ % ! would run as commands or expand as variables.
        raise RuntimeError(f"'{command[0]}' is a cmd.exe script and this argument has characters cmd.exe would "
                           f"interpret: {unsafe[0]}. Register it manually: {' '.join(command)}")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen([executable, *command[1:]], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                creationflags=flags)
    except OSError as exc:
        raise RuntimeError(f"'{' '.join(command)}' did not start: {exc}") from None
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # npm shims are cmd.exe -> node: killing only cmd.exe leaves node holding the pipes forever.
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=flags)
        else:
            proc.kill()
        proc.communicate(timeout=10)
        raise RuntimeError(f"'{' '.join(command)}' did not finish in {timeout:g}s.") from None
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
