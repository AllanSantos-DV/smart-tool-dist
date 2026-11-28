"""smart_search `projects`: one call over several registered projects, one section per project.

Each project is searched by the ordinary smart_search path with the arguments the call shares, so its section is the
answer the same search sent alone would give, ranked inside that project's own index. Sections are concatenated, never
merged: scores of indexes built with different embedding models cannot be compared, so a project whose index was built
with a model other than the current embedder is not searched and its section says so.
"""
import config
import indexer
import local_embedder
import project_admission
import project_identity
import project_store

ALL = "all"


def validate(projects):
    """The `projects` argument when it is well formed.

    Parameters: projects ("all" or a non-empty list of non-empty folder paths). Returns: projects unchanged. Raises:
    ValueError for anything else.

    Technical doc: docs/scope/technical/_root.md#cross_project_search.py::validate"""
    if projects == ALL or (isinstance(projects, list) and projects
                           and all(isinstance(item, str) and item.strip() for item in projects)):
        return projects
    raise ValueError('projects must be "all" (every registered project) or a non-empty list of project folders.')


def _targets(projects):
    """`[(registered project or None, path asked)]` for `projects`, each registered project once, in order. A folder
    inside a registered project stands for that project."""
    registry = project_store.all_projects()
    if projects == ALL:
        return [(project, project["root"]) for project in registry]
    by_root = {project_identity.canonical_root(project["root"]): project for project in registry}
    targets, seen = [], set()
    for path in projects:
        root, registered = project_admission.resolve(path)
        project = by_root.get(project_identity.canonical_root(root)) if registered else None
        key = project_identity.canonical_root(root) if project else path
        if key not in seen:
            seen.add(key)
            targets.append((project, path))
    return targets


def skip_reason(project, cfg):
    """Why `project` is not searched by a cross-project call, or None when it is.

    Parameters: project (a registry record); cfg (the loaded config). Returns: the reason as a sentence, or None. A
    registered folder that is junk or gone, a project without a readable index and an index built with another model than
    the current embedder are skipped, never indexed or re-embedded by a search over many projects.

    Technical doc: docs/scope/technical/_root.md#cross_project_search.py::skip_reason"""
    junk = project_admission.junk_reason(project)
    if junk:
        return f"this registered folder is not a usable project: {junk}."
    current = indexer.info(project["root"])
    if current.get("error"):
        return f"its index cannot be read ({current['error']}); search it alone to see the cause."
    if not current.get("chunks"):
        return "it has no index yet, so there is nothing to search; search it alone (its project_root) to build one."
    model = local_embedder.resolve(cfg, current.get("model_id"))
    built = current.get("model_id")
    if model == built:
        return None
    now = f"the embedder is now {model}" if model else "the embeddings home is not running"
    return (f"its index was built with {built or 'an unknown model'} and {now}; results of two models are never mixed. "
            "Search it alone to read that index while it is re-embedded in the background.")


def _label(project, path):
    """`project: name (root)` for a registered project, `project: path` for a folder that is not one."""
    if project is None:
        return f"project: {path}"
    return f"project: {project.get('name') or project['root']} ({project_identity.display_root(project['root'])})"


def expand(arguments, cfg=None):
    """The searches `arguments["projects"]` stands for, as `[{"label", "arguments", "skipped"}]`, in order.

    Parameters: arguments (the smart_search arguments, `projects` present); cfg (the loaded config, read when omitted).
    Returns: one entry per project; `arguments` is the smart_search call of that project (project_root set, `projects`
    removed) and `skipped` is None, or the reason the project is not searched and `arguments` is None. Raises:
    ValueError for a malformed `projects`.

    Technical doc: docs/scope/technical/_root.md#cross_project_search.py::expand"""
    projects = validate(arguments["projects"])
    cfg = cfg if cfg is not None else config.load_config()
    shared = {key: value for key, value in arguments.items() if key != "projects"}
    plan = []
    for project, path in _targets(projects):
        if project is None:
            plan.append({"label": _label(None, path), "arguments": None,
                         "skipped": "not a registered project; search it alone (its project_root) to register it."})
            continue
        reason = skip_reason(project, cfg)
        plan.append({"label": _label(project, path), "skipped": reason,
                     "arguments": None if reason else {**shared, "project_root": project["root"]}})
    return plan


def render(results):
    """The text the agent reads: a count line, then one numbered section per project with its answer, `SKIPPED: reason`
    or `ERROR: reason`.

    Parameters: results ([{"label", "status", "text" | "reason" | "error"}], one per project in order; status is ok,
    skipped or failed).

    Technical doc: docs/scope/technical/_root.md#cross_project_search.py::render"""
    count = {status: sum(result["status"] == status for result in results) for status in ("ok", "skipped", "failed")}
    blocks = [f"smart_search: {len(results)} projects, {count['ok']} answered, {count['skipped']} skipped, "
              f"{count['failed']} failed."]
    for number, result in enumerate(results, 1):
        body = {"ok": lambda: result["text"], "skipped": lambda: f"SKIPPED: {result['reason']}",
                "failed": lambda: f"ERROR: {result['error']}"}[result["status"]]()
        blocks.append(f"[{number}/{len(results)}] {result['label']}\n{str(body).rstrip()}")
    return "\n\n".join(blocks)
