"""Where Smart Tool keeps its state: ~/.smart-tool (config.json and model adapters) and ~/.smart-tool/data (indexes,
daemon registry, caches, metrics). Pure stdlib so the installer can import it too."""
import os

HOME_DIR = os.path.join(os.path.expanduser("~"), ".smart-tool")
DATA_DIR = os.path.join(HOME_DIR, "data")

_KNOWN_FOLDERS = {"USERPROFILE": "{5E6C858F-0E22-4760-9AFE-EA3317B67173}",
                  "LOCALAPPDATA": "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}"}


def _windows_folder(folder_id):
    """The folder Windows itself reports for a known-folder id; it does not follow environment variables."""
    import ctypes
    import uuid
    guid = uuid.UUID(folder_id)
    raw = (ctypes.c_byte * 16).from_buffer_copy(guid.bytes_le)
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(raw), 0, None, ctypes.byref(out)) != 0:
        raise OSError(f"Windows did not report the known folder {folder_id}.")
    try:
        return out.value
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def overridden_folders():
    """USERPROFILE and LOCALAPPDATA values that differ from the folders Windows reports for the user, as sentences.
    The scheduler, the process table and the registry are machine-wide and ignore these variables, so a run under
    redirected folders would still reach the real installation."""
    found = []
    for name, folder_id in _KNOWN_FOLDERS.items():
        real, given = _windows_folder(folder_id), os.environ.get(name, "")
        if os.path.normcase(os.path.normpath(given or real)) != os.path.normcase(os.path.normpath(real)):
            found.append(f"{name}={given} (Windows reports {real})")
    return found


def refuse_overridden_folders(action):
    """Stops `action` (an installer or uninstaller run) before it changes anything when the user folders are redirected."""
    found = overridden_folders()
    if found:
        raise SystemExit(f"Refusing to {action}: " + "; ".join(found) + ". The logon task, the running processes and the "
                         "registry are shared with the real installation, so the run would reach it. Run it with the "
                         "real user folders.")
