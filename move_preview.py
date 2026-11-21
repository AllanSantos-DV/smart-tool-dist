"""What moving a file or folder breaks, asked before the move: the modules that import it, the Markdown and HTML links
(with their anchors) and the README and `docs` lines that point at it, the anchors of the generated documents that name its
functions and the tests that reach it. Nothing is moved or written; the lists are the places an agent edits after the move.
"""
import os
import posixpath
import re
from collections import defaultdict
from urllib.parse import urlsplit

import affected_tests
import code_graph
import doc_scope
import index_profile
import index_scope
import link_check
import project_scan
import rename_preview
import web_document_graph

DEFAULT_LIMIT = 30
NOTE = ("Static analysis of the indexed files plus the git-tracked Markdown and HTML text: imports built at run time, "
        "paths assembled in strings and files git does not track are not seen, so search the old path as text too.")


def _relative(value, name):
    """A project-relative path with forward slashes and no `.` segments.

    Raises: ValueError when it is not a string, is absolute, climbs out with `..`, is the project folder or is inside `.git`.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Pass {name}: a file or folder relative to the project folder.")
    text = value.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"[A-Za-z]:", text):
        raise ValueError(f"{name} must be relative to the project folder, not {value!r}.")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or ".." in parts or parts[0].lower() == ".git":
        raise ValueError(f"{name} {value!r} must name a file or folder inside the project, not the project folder, "
                         "a `..` path or `.git`.")
    return "/".join(parts)


def _within(candidate, path):
    """Whether `candidate` is `path` or sits under it."""
    return candidate == path or candidate.startswith(path + "/")


def _inside(root, rel):
    """Whether the real location of `rel` (links followed) stays under the project folder."""
    base = os.path.realpath(root)
    try:
        return os.path.commonpath([base, os.path.realpath(os.path.join(root, rel))]) == base
    except ValueError:
        return False


def _validated(root, path, to):
    """The two paths normalized, after the checks that make the preview meaningful.

    Raises: ValueError when `path` does not exist or resolves outside the project, `to` already exists, or `to` is inside
    `path`.
    """
    path, to = _relative(path, "path"), _relative(to, "to")
    if not os.path.lexists(os.path.join(root, path)):
        raise ValueError(f"path {path!r} does not exist in the project; nothing to move.")
    if not _inside(root, path):
        raise ValueError(f"path {path!r} resolves outside the project folder.")
    if os.path.lexists(os.path.join(root, to)):
        raise ValueError(f"to {to!r} already exists; a move would overwrite it. Choose a free destination.")
    if _within(to, path):
        raise ValueError(f"to {to!r} is inside path {path!r}; a folder cannot move into itself.")
    return path, to


def _mention_test(path):
    """A test for a line that writes the path as text. A path with a folder or an extension counts as written; a bare
    name (`docs`, `Makefile`) counts only in backticks or followed by a slash, so a plain word is not a mention."""
    escaped = re.escape(path)
    if "/" in path or os.path.splitext(path)[1]:
        pattern = re.compile(rf"(?<![\w./-]){escaped}(?![\w-])")
    else:
        pattern = re.compile(rf"`{escaped}/?`|(?<![\w./-]){escaped}/")
    return pattern.search


def _scan(root, path):
    """Links that lead to the path and the doc lines that write it, read from the tracked Markdown and HTML files.

    A link counts when its target is the path or under it and its file stays where it is; a link between two files that
    move together keeps working. A line that holds such a link is not repeated as a mention.

    Returns: tuple (list of {"path", "line", "link", "anchor", "root_relative"}, list of {"path", "line", "text"}).
    Raises: ValueError when git cannot list the folder.
    """
    links, mentions = [], []
    mentioned = _mention_test(path)
    extensions = tuple(sorted(set(link_check.EXTENSIONS) | set(rename_preview.DOC_EXTENSIONS)))
    for rel, text in project_scan.tracked_texts(root, extensions):
        if text is None:
            continue
        linked = set()
        if rel.lower().endswith(link_check.EXTENSIONS) and not _within(rel, path):
            for item in web_document_graph.analyze({rel: text}, ())["dependencies"]:
                if item["resolution"] == "external":
                    continue
                parts = urlsplit(item["specifier"])
                if parts.path and _within(web_document_graph.local_target(rel, item["specifier"]), path):
                    linked.add(item["line"])
                    links.append({"path": rel, "line": item["line"], "link": item["specifier"],
                                  "anchor": parts.fragment or None, "root_relative": parts.path.startswith("/")})
        if rename_preview._is_doc(rel):
            mentions += [{"path": rel, "line": number, "text": line.strip()[:rename_preview.MAX_LINE_CHARS]}
                         for number, line in enumerate(text.splitlines(), 1)
                         if number not in linked and mentioned(line)]
    return links, mentions


def _graph(root, path, view_id, limit):
    """The importers and tests of the files under `path`, from the shared code graph.

    Returns: tuple (importers, tests, dict with the totals, the graph's note or "").
    """
    data = code_graph.build_current(root, view_id, shared=True)
    files = {item["path"] for item in data.get("files") or []}
    moved = {name for name in files if _within(name, path)}
    reverse, importers = defaultdict(set), []
    for dep in data.get("dependencies") or []:
        if not dep.get("target"):
            continue
        reverse[dep["target"]].add(dep["source"])
        if dep["kind"] == "import" and dep["target"] in moved and dep["source"] not in moved:
            importers.append({"path": dep["source"], "line": dep["line"], "import": dep["specifier"]})
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    test_files = {name for name in files if index_profile.kind(name, profile) == "test"}
    for conftest, users in affected_tests._fixture_users(root, test_files).items():
        reverse[conftest] |= users
    named = set()
    for found in (affected_tests._path_literal_users(root, test_files, files),
                  affected_tests._import_string_users(root, test_files, files)):
        for target, users in found.items():
            reverse[target] |= users
            if target in moved:
                named |= users
    depth = affected_tests._depths(sorted(moved), reverse)
    reaching = sorted((name for name in depth if name in test_files and name not in moved
                       and not affected_tests._support(root, name)), key=lambda name: (depth[name], name))
    tests = [{"path": name, "why": "names the moved file in a string" if name in named
              else f"imports a moved file ({depth[name]} step{'s' if depth[name] > 1 else ''})"} for name in reaching]
    importers.sort(key=lambda row: (row["path"], row["line"]))
    note = "" if moved else f"{path} holds no file of the import graph, so importers and tests could not be listed."
    if data.get("truncated"):
        note = f"{note} {code_graph.left_out_sentence(data)} Importers and tests beyond the cut are not listed.".strip()
    return importers, tests, {"moved_files": len(moved)}, note


def _doc_anchors(root, path):
    """The anchors of the generated documents that name a function of `path`, and why none was listed when the documents
    do not exist.

    Returns: tuple (list of {"file", "line", "id", "level"}, str).
    Raises: ValueError when a page of docs/scope is in an older format.
    """
    if not os.path.isdir(os.path.join(root, doc_scope.DOCS_DIR)):
        return [], f"No {doc_scope.DOCS_DIR} yet: generate it with project_manage action=docs generate=true."
    markers = doc_scope._scan(root)[0]
    return [{"file": f"{doc_scope.DOCS_DIR}/{item['file']}", "line": item["line"], "id": item["id"],
             "level": "technical" if item["file"].startswith("technical/") else item["file"][:-3]}
            for item in markers if _within(item["id"].rpartition("::")[0], path)], ""


def preview(root, path, to, view_id=None, limit=DEFAULT_LIMIT):
    """Everything moving `path` to `to` leaves pointing at the old place.

    Parameters: root (str) project folder; path (str) file or folder to move; to (str) destination, which must not exist;
    view_id (str or None) index view; limit (int) rows in each list.
    Returns: dict {"path", "to", "kind": "file" or "folder", "imported_by": [{"path", "line", "import"}], "links":
    [{"path", "line", "link", "anchor", "root_relative"}], "doc_mentions": [{"path", "line", "text"}], "doc_anchors":
    [{"file", "line", "id", "level"}], "tests": [{"path", "why"}], "counts": {list: total, "moved_files": int},
    "doc_scope": str ("" when docs/scope exists, otherwise why no anchor could be listed), "note": str}.
    Raises: ValueError when `path` does not exist or leaves the project, `to` exists or is inside `path`, the folder is not
    a git repository or a page of docs/scope is in an older format; RuntimeError when the code graph cannot be built.
    """
    path, to = _validated(root, path, to)
    links, mentions = _scan(root, path)
    importers, tests, totals, graph_note = _graph(root, path, view_id, limit)
    anchors, guidance = _doc_anchors(root, path)
    counts = {"imported_by": len(importers), "links": len(links), "doc_mentions": len(mentions),
              "doc_anchors": len(anchors), "tests": len(tests), **totals}
    kind = "folder" if os.path.isdir(os.path.join(root, path)) else "file"
    return {"path": path, "to": to, "kind": kind, "imported_by": importers[:limit], "links": links[:limit],
            "doc_mentions": mentions[:limit], "doc_anchors": anchors[:limit], "tests": tests[:limit], "counts": counts,
            "doc_scope": guidance, "note": f"{graph_note} {NOTE}".strip()}
