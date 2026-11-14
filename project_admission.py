"""Which folder a call may turn into a project, and which registered projects were never one.

A project is the folder where the agent session started (or the repository that holds it), never a folder an agent
merely navigated to: personal folders, drive roots, application data, system folders, build output and a Java home
are refused, a subfolder resolves to the registered project or the repository that holds it, and a session whose
origin is known only indexes its own project. Registered folders that break these rules are junk (`junk_reason`)."""
import os
import tempfile

import project_identity
import project_store

PERSONAL_NAMES = ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music")
TOOL_FOLDERS = frozenset(("bin", "obj", "node_modules", ".git", "site-packages", "__pycache__"))
POSIX_SYSTEM = ("/usr", "/bin", "/sbin", "/etc", "/var", "/opt", "/lib", "/proc", "/sys", "/dev", "/boot")


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


def _inside(path, base):
    return path == base or path.startswith(base.rstrip(os.sep) + os.sep)


def _env_folders(*names):
    return [_norm(os.environ[name]) for name in names if os.environ.get(name)]


def _personal_folders():
    """Home, its OneDrive roots and the standard user folders in each, compared with normcase."""
    home = _norm(os.path.expanduser("~"))
    bases = [home]
    try:
        bases += [os.path.join(home, name) for name in os.listdir(home) if name.lower().startswith("onedrive")]
    except OSError:
        pass
    return {_norm(path) for base in bases for path in (base, *(os.path.join(base, name) for name in PERSONAL_NAMES))}


def _system_roots():
    roots = _env_folders("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData")
    return roots if os.name == "nt" else [_norm(path) for path in POSIX_SYSTEM]


def _scratch_roots():
    """Where tests, scratch runs and the system temporary folder live, even though they sit under application data."""
    roots = [_norm(tempfile.gettempdir()), *_env_folders("SMART_TOOL_SCRATCH")]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(_norm(os.path.join(os.environ["LOCALAPPDATA"], "tmp")))
    return roots


def _is_java_home(folder):
    for launcher in ("java.exe", "java"):
        if os.path.isfile(os.path.join(folder, "bin", launcher)) and (
                os.path.isfile(os.path.join(folder, "release")) or os.path.isfile(os.path.join(folder, "lib", "modules"))):
            return True
    return False


def _ancestors(real):
    """`real` and every folder above it, nearest first."""
    current = real
    while True:
        yield current
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def refusal(root):
    """Why root cannot be a project, or None: a drive or filesystem root, a personal folder, application data, a
    system folder, a hidden tool folder in the home folder (`~/.claude`), a build or dependency folder, the temporary
    folder itself, or a Java home (or inside one)."""
    real = project_identity.display_root(root)
    norm = _norm(real)
    scratch = _scratch_roots()
    if os.path.dirname(real) == real:
        return "a drive or filesystem root"
    if norm in _personal_folders():
        return "a personal folder, not a project"
    if norm == scratch[0]:
        return "the temporary folder"
    home = _norm(os.path.expanduser("~"))
    if _inside(norm, home) and norm != home and os.path.relpath(norm, home).split(os.sep)[0].startswith("."):
        return "inside a tool's hidden folder in the home folder"
    if any(_inside(norm, base) for base in _system_roots()):
        return "a system or application folder"
    if (any(_inside(norm, base) for base in _env_folders("APPDATA", "LOCALAPPDATA", "LOCALAPPDATA_LOW"))
            and not any(_inside(norm, base) for base in scratch)):
        return "application data"
    folder = next((part for part in real.replace("\\", "/").split("/") if part.lower() in TOOL_FOLDERS), None)
    if folder:
        return f"a build or dependency folder ({folder})"
    java = _env_folders("JAVA_HOME")
    if any(_inside(norm, base) for base in java) or any(_is_java_home(path) for path in _ancestors(real)):
        return "a Java home"
    return None


def repository_root(path):
    """The nearest folder at or above path that holds `.git` and could be a project, or None. A repository the rules
    refuse (a dotfiles repository in the home folder) is not a project to resolve to."""
    for folder in _ancestors(project_identity.display_root(path)):
        if os.path.exists(os.path.join(folder, ".git")):
            return folder if refusal(folder) is None else None
    return None


def _registered_holding(real):
    """The deepest registered project that is real or holds it, ignoring registered junk."""
    base = project_identity.canonical_root(real)
    holding = [p for p in project_store.all_projects()
               if (base == (root := project_identity.canonical_root(p["root"])) or base.startswith(root + os.sep))
               and junk_reason(p) is None]
    return max(holding, key=lambda p: len(p["root"])) if holding else None


def resolve(path):
    """(root, registered): the project path belongs to, from the registry first, then the repository that holds it,
    else the folder itself."""
    held = _registered_holding(path)
    if held:
        return project_identity.display_root(held["root"]), True
    return repository_root(path) or project_identity.display_root(path), False


def admit(path, origin=None):
    """The project root a call on `path` may use. A registered project (or a folder inside one) always passes; a new
    project must not be refused by `refusal`, and when the session's `origin` folder is known it must be that
    session's own project. Raises ValueError naming the reason and what to do."""
    root, registered = resolve(path)
    if registered:
        return root
    reason = refusal(root)
    if reason:
        raise ValueError(f"Not indexing {root}: it is {reason}. A project is the folder where the session started; "
                         "pass that folder as project_root.")
    if origin:
        home, _held = resolve(origin)
        home_reason = refusal(home)
        if home_reason:
            raise ValueError(f"Not indexing {root}: the session started in {home}, which is {home_reason}, not a "
                             "project. Start the session inside the project folder.")
        if _inside(_norm(root), _norm(home)):
            return home
        raise ValueError(f"Not indexing {root}: the session started in the project {home}, and a new project is "
                         "indexed only from the session that started in it.")
    return root


def junk_reason(project):
    """Why a registered project was never a project, or None: its folder is refused by `refusal`, it holds two or
    more projects, or it is a subfolder of a repository or of another registered project."""
    root = project["root"]
    if not os.path.isdir(root):
        return None
    reason = refusal(root)
    if reason:
        return reason
    if os.path.exists(os.path.join(root, ".git")):
        return None
    if project_store.container_error(root):
        return "a folder of projects"
    outer = repository_root(os.path.dirname(root))
    if outer:
        return f"a subfolder of the repository {outer}"
    base = project_identity.canonical_root(root)
    for other in project_store.all_projects():
        folder = project_identity.canonical_root(other["root"])
        if base.startswith(folder + os.sep) and junk_reason_shallow(other) is None:
            return f"a subfolder of the project {other['root']}"
    return None


def junk_reason_shallow(project):
    """`junk_reason` without the subfolder rules: whether the registered folder could be a project on its own."""
    root = project["root"]
    return None if not os.path.isdir(root) else refusal(root)
