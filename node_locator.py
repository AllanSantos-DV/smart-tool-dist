"""Where Node.js (and npx/npm) is on this machine, looked up when it is needed and never remembered.

A process keeps the PATH it was born with: the tray started at logon never sees a Node installed, moved or removed
afterwards, and everything it launches (the updater, the installer, the daemon) inherits that stale PATH. The lookup is
therefore: the process PATH, then the PATH Windows stores for the machine and the user (what a new process would get),
then the usual install folders. The Node that Smart Tool downloads into web_adapters/node/.node-runtime is the last
resort of `node()`, used only when the machine has none.
"""
import os
import shutil

LOCAL_RUNTIME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web_adapters", "node", ".node-runtime")
_MACHINE_ENVIRONMENT = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"


def _registry_path():
    """Entries of the machine and user PATH as stored in the registry, variables expanded."""
    if os.name != "nt":
        return []
    import winreg
    entries = []
    for hive, subkey in ((winreg.HKEY_LOCAL_MACHINE, _MACHINE_ENVIRONMENT), (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(hive, subkey) as key:
                entries += os.path.expandvars(winreg.QueryValueEx(key, "Path")[0]).split(os.pathsep)
        except FileNotFoundError:
            continue
    return entries


def _unique(entries):
    seen, kept = set(), []
    for entry in entries:
        entry = entry.strip().strip('"')
        if entry and os.path.normcase(entry) not in seen:
            seen.add(os.path.normcase(entry))
            kept.append(entry)
    return kept


def search_path():
    """The process PATH followed by the registry PATH entries it does not have."""
    return _unique(os.environ.get("PATH", "").split(os.pathsep) + _registry_path())


def known_folders():
    """Where Node installers and version managers put node.exe, for a machine whose PATH does not list it."""
    program_files = [os.environ.get(name) for name in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    local = os.environ.get("LOCALAPPDATA")
    home = os.path.expanduser("~")
    candidates = [os.path.join(base, "nodejs") for base in program_files if base]
    candidates += [os.environ.get("NVM_SYMLINK"), os.environ.get("FNM_MULTISHELL_PATH"),
                   os.path.join(os.environ["VOLTA_HOME"], "bin") if os.environ.get("VOLTA_HOME") else None,
                   os.path.join(local, "Programs", "nodejs") if local else None,
                   os.path.join(home, "scoop", "apps", "nodejs", "current"),
                   os.path.join(home, "scoop", "apps", "nodejs-lts", "current"),
                   os.path.join(os.environ["ChocolateyInstall"], "bin") if os.environ.get("ChocolateyInstall") else None]
    return _unique(path for path in candidates if path)


_FOUND = {}


def which(name):
    """Full path of an executable (`node`, `npx.cmd`...): the PATH first, the known install folders only when the PATH
    does not have it; None when neither does. A path found is remembered per PATH value and used again only while the
    file is still there, so a lookup costs a stat, not a scan of every folder, and a moved Node is searched again."""
    key = (name, os.environ.get("PATH", ""))
    found = _FOUND.get(key)
    if found and os.path.isfile(found):
        return found
    found = (shutil.which(name, path=os.pathsep.join(search_path()))
             or shutil.which(name, path=os.pathsep.join(known_folders())))
    if found:
        _FOUND[key] = found
    return found


def local_node():
    """The Node downloaded into Smart Tool, or None."""
    path = os.path.join(LOCAL_RUNTIME, "node.exe")
    return path if os.path.isfile(path) else None


def node():
    """node.exe of the machine (PATH first), else the Node inside Smart Tool, else None."""
    return which("node") or local_node()


def environment(prefer=None):
    """A copy of the environment with the current PATH (see search_path), the folder of `prefer` (an executable)
    first: what a process launched now should inherit."""
    env = {key: value for key, value in os.environ.items() if key.upper() != "PATH"}
    entries = search_path()
    if prefer:
        entries.insert(0, os.path.dirname(prefer))
    env["PATH"] = os.pathsep.join(_unique(entries))
    return env


def exists(command):
    """Whether a registered executable (a full path, or a bare name resolved through the PATH) is still there."""
    return os.path.isfile(command) or bool(which(command))
