"""In-place edits of the `[mcp_servers.<name>]` tables of Codex's config.toml.

`codex mcp add` and `codex mcp remove` serialize the whole file again and drop the comments, among them the markers
other tools put around the blocks they manage. Here only the table of the named server is added, replaced or removed;
every other byte of the file stays as it was.
"""
import os
import re
import tomllib

import atomic_io

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _header_path(line):
    """Key path of a table header line (`[a.b]` -> ["a", "b"]); None when the line is not a header."""
    if not line.lstrip().startswith("["):
        return None
    try:
        node = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path = []
    while isinstance(node, dict) and len(node) == 1:
        key, node = next(iter(node.items()))
        path.append(key)
        if isinstance(node, list):
            break
    return path


def _span(lines, name):
    """(start, end) line indexes of the server's table and its subtables, without the comments and blank lines that
    trail it (they belong to what comes next); None when the file has no such table."""
    start = end = None
    for index, line in enumerate(lines):
        path = _header_path(line)
        if path is None:
            continue
        if path[:2] == ["mcp_servers", name]:
            start = index if start is None else start
        elif start is not None and end is None:
            end = index
    if start is None:
        return None
    end = len(lines) if end is None else end
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return start, end


def _value(value):
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") \
            .replace("\t", "\\t") + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    raise TypeError(f"Unsupported value for config.toml: {value!r}")


def _table(name, fields, eol):
    key = name if _BARE_KEY.match(name) else _value(name)
    return eol.join([f"[mcp_servers.{key}]", *(f"{field} = {_value(value)}" for field, value in fields.items())]) + eol


def _read(path):
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            return stream.read()
    except FileNotFoundError:
        return ""


def _others(text, name):
    """Everything the file defines except the server `name`, as parsed TOML."""
    data = tomllib.loads(text)
    servers = data.get("mcp_servers") or {}
    servers.pop(name, None)
    if not servers:
        data.pop("mcp_servers", None)
    return data


def _write(path, before, after, name, fields):
    """Writes after (the edited text) when it parses, holds exactly `fields` for the server and changed nothing else.
    Raises ValueError otherwise, leaving the file alone."""
    try:
        parsed = tomllib.loads(after)
        unchanged = _others(after, name) == _others(before, name)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} would not be valid TOML after editing [mcp_servers.{name}]: {exc}") from None
    held = (parsed.get("mcp_servers") or {}).get(name)
    if not unchanged or held != (fields or None):
        raise ValueError(f"Editing [mcp_servers.{name}] in {path} would change more than that table "
                         f"(a layout this editor does not handle); edit the file by hand.")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_io.write_text(path, after)


def set_server(path, name, fields):
    """Makes `[mcp_servers.<name>]` in the config.toml at path hold exactly `fields` (string or list of strings per
    key): the existing table is replaced where it is, a missing one is appended. Raises ValueError, writing nothing,
    when the file is not valid TOML or the edit would touch anything else."""
    before = _read(path)
    eol = "\r\n" if "\r\n" in before else "\n"
    lines = before.splitlines(keepends=True)
    span = _span(lines, name)
    table = _table(name, fields, eol)
    if span:
        after = "".join(lines[:span[0]]) + table + "".join(lines[span[1]:])
    else:
        gap = "" if not before or before.endswith(eol + eol) else (eol if before.endswith(eol) else eol + eol)
        after = before + gap + table
    _write(path, before, after, name, fields)


def remove_server(path, name):
    """Removes `[mcp_servers.<name>]` and its subtables from the config.toml at path; nothing happens when it is not
    there. Raises ValueError, writing nothing, when the edit would touch anything else."""
    before = _read(path)
    lines = before.splitlines(keepends=True)
    span = _span(lines, name)
    if not span:
        return
    _write(path, before, "".join(lines[:span[0]] + lines[span[1]:]), name, {})
