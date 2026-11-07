"""Declared layers of a project: an ordered list from the highest layer (entry points) to the lowest (data access), each
defined by path globs and annotations, saved in the project at .smart-tool/layers.json once the agent confirms it.

The model follows the layer rules of validated tools: import-linter's layers contract (an ordered list, higher layers
may depend on lower ones), ArchUnit's layeredArchitecture (a layer is defined by a package pattern) and
dependency-cruiser (paths relative to the project root, forward slashes). A file belongs to the first layer, in the
declared order, whose path glob matches it or whose annotation starts one of its lines. The suggestion reads folder
names and annotations only; nothing is decided without the agent confirming it."""
import fnmatch
import json
import os
import posixpath
import re
from collections import Counter

import atomic_io
import code_graph
import index_profile
import index_scope

RULE_PATH = os.path.join(".smart-tool", "layers.json")
MAX_LAYERS = 12
MAX_PATTERNS = 200
MAX_UNASSIGNED = 30
_NAME = re.compile(r"[A-Za-z][\w-]{0,39}")
CONVENTIONS = (
    ("entry", {"api", "apis", "cmd", "cli", "commands", "controller", "controllers", "endpoint", "endpoints",
               "graphql", "handler", "handlers", "http", "resolvers", "rest", "route", "router", "routers", "routes",
               "views", "web"},
     ("@RestController", "@Controller", "@RequestMapping", "@GetMapping", "@PostMapping", "@PutMapping",
      "@DeleteMapping", "@PatchMapping", "@Path", "@app.route", "@bp.route", "@blueprint.route", "@app.get",
      "@app.post", "@app.put", "@app.delete", "@app.patch", "@router.get", "@router.post", "@router.put",
      "@router.delete", "@router.patch")),
    ("service", {"application", "business", "interactors", "service", "services", "usecase", "usecases",
                 "use_cases"},
     ("@Service", "@Injectable")),
    ("domain", {"core", "domain", "entities", "entity", "model", "models"},
     ("@Entity", "@Embeddable")),
    ("data", {"dao", "daos", "database", "db", "migrations", "persistence", "repo", "repos", "repositories",
              "repository", "sql", "storage", "store"},
     ("@Repository", "@Mapper", "@Dao")),
)


class Rule:
    """A validated layer rule: assign() gives the layer of a file from its path and text."""

    def __init__(self, layers):
        self.layers = layers
        self._paths = [[re.compile(fnmatch.translate(p)) for p in layer["paths"]] for layer in layers]
        self._annotations = [re.compile(r"^[ \t]*(?:" + "|".join(re.escape(a) for a in layer["annotations"])
                                        + r")(?![\w.])", re.M) if layer["annotations"] else None
                             for layer in layers]

    def assign(self, path, text=None):
        """Name of the first layer whose glob matches path or whose annotation starts a line of text; None if none.

        Parameters: path (str) project-relative with forward slashes; text (str or None) the file's source.
        Returns: str or None.
        """
        for layer, globs, annotation in zip(self.layers, self._paths, self._annotations):
            if any(g.match(path) for g in globs) or (text and annotation and annotation.search(text)):
                return layer["name"]
        return None


def validate(layers):
    """The layer list as saved, or ValueError naming the first problem.

    Parameters: layers (list of {'name', 'paths', 'annotations'}) ordered from the highest layer to the lowest.
    Returns: list of {'name': str, 'paths': [str], 'annotations': [str]}.
    """
    if not isinstance(layers, list) or not 1 <= len(layers) <= MAX_LAYERS:
        raise ValueError(f"layers must be a list of 1 to {MAX_LAYERS} layers, highest first.")
    clean, names = [], set()
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or set(layer) - {"name", "paths", "annotations"}:
            raise ValueError(f"layers[{index}] must be an object with name, paths and annotations only.")
        name = layer.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name) or name.casefold() in names:
            raise ValueError(f"layers[{index}].name must be a unique word (letters, digits, - or _, up to 40).")
        names.add(name.casefold())
        paths, annotations = layer.get("paths", []), layer.get("annotations", [])
        if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths) \
                or len(paths) > MAX_PATTERNS:
            raise ValueError(f"layers[{index}].paths must be a list of up to {MAX_PATTERNS} path globs.")
        for pattern in paths:
            parts = pattern.replace("\\", "/").split("/")
            if pattern.startswith(("/", "\\")) or re.match(r"[A-Za-z]:", pattern) or ".." in parts:
                raise ValueError(f"layers[{index}].paths: '{pattern}' must be relative to the project root.")
        if not isinstance(annotations, list) or not all(isinstance(a, str) and re.fullmatch(r"@[\w.]+", a)
                                                        for a in annotations):
            raise ValueError(f"layers[{index}].annotations must be a list like ['@RestController', '@app.route'].")
        if not paths and not annotations:
            raise ValueError(f"layers[{index}] ('{name}') needs at least one path glob or annotation.")
        clean.append({"name": name, "paths": [p.replace("\\", "/") for p in paths], "annotations": annotations})
    return clean


def load(root):
    """The confirmed rule of the project, or None when it has none; a file that does not validate raises ValueError.

    Parameters: root (str) project folder.
    Returns: Rule or None.
    """
    path = os.path.join(root, RULE_PATH)
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} could not be read: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"layers"}:
        raise ValueError(f"{path} must be an object with a single 'layers' list.")
    try:
        return Rule(validate(data["layers"]))
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from None


def save(root, layers):
    """Validate and write the rule to .smart-tool/layers.json in the project.

    Parameters: root (str) project folder; layers (list) as validate() takes it.
    Returns: Rule.
    """
    clean = validate(layers)
    path = os.path.join(root, RULE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_io.write_text(path, json.dumps({"layers": clean}, indent=2, ensure_ascii=False) + "\n")
    return Rule(clean)


def suggest(paths, read):
    """Layer rule proposed from folder names and annotations, in the CONVENTIONS order.

    The deepest folder named after a convention decides a file's layer (internal/service/model/x.go is domain); an
    annotation of a higher layer wins over the folder. Folders become globs (`internal/handler/**`); annotations that
    matched a file are kept on their layer.

    Parameters: paths (iterable of str) code files, project-relative; read (callable path -> str or None) their text.
    Returns: list of layers as validate() takes them (only layers that matched something).
    """
    globs, annotations = {name: set() for name, _f, _a in CONVENTIONS}, {name: set() for name, _f, _a in CONVENTIONS}
    for path in paths:
        text = read(path) or ""
        hit = next(((name, a) for name, _folders, names in CONVENTIONS for a in names
                    if re.search(r"^[ \t]*" + re.escape(a) + r"(?![\w.])", text, re.M)), None)
        if hit:
            annotations[hit[0]].add(hit[1])
            continue
        parts = path.split("/")[:-1]
        for depth in range(len(parts), 0, -1):
            layer = next((name for name, folders, _a in CONVENTIONS if parts[depth - 1].lower() in folders), None)
            if layer:
                globs[layer].add("/".join(parts[:depth]) + "/**")
                break
    return [{"name": name, "paths": sorted(globs[name]), "annotations": sorted(annotations[name])}
            for name, _f, _a in CONVENTIONS if globs[name] or annotations[name]]


def summary(rule, graph, read):
    """How a rule splits the code graph: files and symbols per layer, calls between layers (direction 'down' follows
    the declared order, 'up' goes against it) and the folders no layer covers.

    Parameters: rule (Rule); graph (dict) code graph with 'symbols' and 'calls'; read (callable path -> str or None).
    Returns: dict with 'layers', 'calls_between', 'unassigned' and 'truncated'.
    """
    order = {layer["name"]: index for index, layer in enumerate(rule.layers)}
    paths = sorted({s["path"] for s in graph.get("symbols") or []})
    layer_of = {path: rule.assign(path, read(path) if any(l["annotations"] for l in rule.layers) else None)
                for path in paths}
    symbol_layer = {s["id"]: layer_of[s["path"]] for s in graph.get("symbols") or []}
    files, symbols = Counter(layer_of.values()), Counter(symbol_layer.values())
    between = Counter()
    for call in graph.get("calls") or []:
        source, target = symbol_layer.get(call["source"]), symbol_layer.get(call["target"])
        if source and target and source != target:
            between[(source, target)] += call.get("count", 1)
    unassigned = Counter(posixpath.dirname(p) or "." for p, layer in layer_of.items() if layer is None)
    return {"layers": [{**layer, "files": files[layer["name"]], "symbols": symbols[layer["name"]]}
                       for layer in rule.layers],
            "calls_between": [{"from": a, "to": b, "calls": n, "direction": "down" if order[a] < order[b] else "up"}
                              for (a, b), n in sorted(between.items(), key=lambda item: (order[item[0][0]],
                                                                                        order[item[0][1]]))],
            "unassigned": [{"folder": folder, "files": n} for folder, n in unassigned.most_common(MAX_UNASSIGNED)],
            "unassigned_files": files[None], "truncated": bool(graph.get("truncated"))}


def report(root, layers=None, confirm=False, view_id=None):
    """The layers action: the saved rule, a suggestion, a preview of a proposed rule, or the confirmed save, each with
    the summary of how it splits the project's code (tests left out).

    Parameters: root (str) project folder; layers (list or None) proposed rule; confirm (bool) save the proposed rule;
    view_id (str or None) index view to read.
    Returns: dict with 'saved', 'rule_path', 'layers' (the rule with per-layer counts), 'calls_between', 'unassigned'
    and 'guidance'.
    """
    if type(confirm) is not bool:
        raise ValueError("confirm must be true or false.")
    if confirm and layers is None:
        raise ValueError("confirm=true saves the rule passed in layers; pass layers.")
    graph = code_graph.build_current(root, view_id, shared=True)
    profile = index_profile.current((index_scope.load_scope(root) or {}).get("profile"))
    symbols = [s for s in graph.get("symbols") or [] if index_profile.kind(s["path"], profile) == "code"]
    code = {**graph, "symbols": symbols}
    texts = {}

    def read(path):
        if path not in texts:
            try:
                with open(os.path.join(root, path), encoding="utf-8") as stream:
                    texts[path] = stream.read()
            except (OSError, UnicodeDecodeError):
                texts[path] = None
        return texts[path]

    path = os.path.join(root, RULE_PATH)
    if layers is not None:
        rule = save(root, layers) if confirm else Rule(validate(layers))
        guidance = ("Saved; documentation and the graph use this rule from now on." if confirm else
                    "Preview only: call again with the same layers and confirm=true to save the rule in the project.")
        return {"saved": confirm, "rule_path": path, **summary(rule, code, read), "guidance": guidance}
    rule = load(root)
    if rule:
        return {"saved": True, "rule_path": path, **summary(rule, code, read),
                "guidance": "To change it, pass the new layers (preview), then the same layers with confirm=true."}
    proposed = suggest(sorted({s["path"] for s in symbols}), read)
    if not proposed:
        return {"saved": False, "rule_path": path, "layers": [], "calls_between": [], "unassigned": [],
                "truncated": bool(graph.get("truncated")),
                "guidance": "No folder or annotation follows a known layer convention; declare the layers yourself "
                            "(name, paths, annotations, highest first) and confirm them."}
    return {"saved": False, "rule_path": path, **summary(Rule(proposed), code, read),
            "guidance": "Suggested from folder names and annotations, not saved: review the layers (rename, merge, move "
                        "folders from 'unassigned'), then pass them with confirm=true."}
