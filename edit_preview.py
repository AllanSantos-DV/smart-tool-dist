"""The files an agent edit would produce, computed before the edit runs: Claude Code Edit, MultiEdit and Write, and
Codex apply_patch (*** Begin Patch / Update File / Add File). Returns (absolute path, text before or None, text after)
for each file it can rebuild; an edit it cannot rebuild yields nothing, so a hook built on it never blocks wrongly."""
import os


def _read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except (OSError, UnicodeDecodeError):
        return None


def _replace(text, old, new, every):
    if not old or old not in text:
        return None
    return text.replace(old, new) if every else text.replace(old, new, 1)


def _claude(tool, data):
    path = data.get("file_path")
    if not isinstance(path, str) or not os.path.isabs(path):
        return []
    before = _read(path)
    if tool == "Write":
        content = data.get("content")
        return [(path, before, content)] if isinstance(content, str) else []
    if before is None:
        return []
    edits = data.get("edits") if tool == "MultiEdit" else [data]
    after = before
    for edit in edits or []:
        after = _replace(after, edit.get("old_string"), edit.get("new_string", ""), edit.get("replace_all") is True)
        if after is None:
            return []
    return [(path, before, after)]


def _apply_hunks(text, hunks):
    lines = text.splitlines()
    for old, new in hunks:
        if not old:
            lines.extend(new)
            continue
        at = next((i for i in range(len(lines) - len(old) + 1) if lines[i:i + len(old)] == old), None)
        if at is None:
            return None
        lines[at:at + len(old)] = new
    return "\n".join(lines) + ("\n" if text.endswith("\n") or not text else "")


def _codex(patch, cwd):
    if not isinstance(patch, str) or "*** Begin Patch" not in patch:
        return []
    files, current = [], None
    for raw in patch.splitlines():
        if raw.startswith(("*** Update File: ", "*** Add File: ")):
            current = {"action": raw.split(":", 1)[0][4:], "path": raw.split(":", 1)[1].strip(), "hunks": [[[], []]]}
            files.append(current)
        elif raw.startswith(("*** Delete File: ", "*** End Patch", "*** Move to: ")) or current is None:
            current = None if raw.startswith(("*** Delete File: ", "*** End Patch")) else current
        elif raw.startswith("@@"):
            current["hunks"].append([[], []])
        elif raw.startswith("+"):
            current["hunks"][-1][1].append(raw[1:])
        elif raw.startswith("-"):
            current["hunks"][-1][0].append(raw[1:])
        elif raw.startswith(" ") or raw == "":
            current["hunks"][-1][0].append(raw[1:])
            current["hunks"][-1][1].append(raw[1:])
    results = []
    for entry in files:
        path = entry["path"] if os.path.isabs(entry["path"]) else os.path.join(cwd or os.getcwd(), entry["path"])
        hunks = [hunk for hunk in entry["hunks"] if hunk[0] or hunk[1]]
        if entry["action"] == "Add File":
            results.append((path, None, "\n".join(line for hunk in hunks for line in hunk[1]) + "\n"))
            continue
        before = _read(path)
        after = _apply_hunks(before, hunks) if before is not None else None
        if after is not None:
            results.append((path, before, after))
    return results


def preview(tool_name, tool_input, cwd=None):
    data = tool_input if isinstance(tool_input, dict) else {}
    if tool_name in ("Edit", "MultiEdit", "Write") and "file_path" in data:
        return _claude(tool_name, data)
    if tool_name in ("apply_patch", "Edit", "Write") and "command" in data:
        return _codex(data.get("command"), cwd)
    return []
