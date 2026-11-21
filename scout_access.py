"""Whether `smart_scout` can be called in a Claude Code session without stopping for a permission decision.

The hook payload carries the permission mode but not the session's allow rules, so the rules are read from the same
settings files Claude Code merges: user, project and project-local. Where the answer cannot be shown to be yes, it is no:
a hook that sends an agent to a tool it may not call leaves the work waiting on a prompt nobody answers."""
import json
import os

import endpoint_sync

TOOL = "mcp__smart-tool__smart_scout"
SERVER_RULES = frozenset({TOOL, "mcp__smart-tool__*", "mcp__smart-tool"})
OPEN_MODES = frozenset({"bypassPermissions"})
SETTINGS_FILES = ("settings.json", "settings.local.json")


def _settings(cwd):
    """The parsed permission blocks of the settings files that exist; an unreadable or malformed file is skipped as
    Claude Code skips it, and counts as no allow rule."""
    folders = [endpoint_sync.claude_dir(), os.path.join(cwd or "", ".claude")] if cwd else [endpoint_sync.claude_dir()]
    found = []
    for folder in folders:
        for name in SETTINGS_FILES:
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as stream:
                    data = json.load(stream)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("permissions"), dict):
                found.append(data["permissions"])
    return found


def callable_in(payload):
    """True when the session runs in a mode that allows every tool or an allow rule names `smart_scout` (the tool, its
    server or the server wildcard) and no deny rule does."""
    if not isinstance(payload, dict):
        return False
    permissions = _settings(payload.get("cwd"))

    def rules(key):
        return {rule for block in permissions for rule in (block.get(key) or []) if isinstance(rule, str)}

    if rules("deny") & SERVER_RULES:
        return False
    if payload.get("permission_mode") in OPEN_MODES or any(block.get("defaultMode") in OPEN_MODES for block in permissions):
        return True
    return bool(rules("allow") & SERVER_RULES)
