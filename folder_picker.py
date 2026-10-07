"""Seletor nativo de pasta, compartilhado pelo tray e pela tela de projetos."""
import json
import os
import re
import threading
import urllib.request
import webbrowser

_PICKER_LOCK = threading.Lock()


def choose_folder():
    if os.name != "nt":
        raise RuntimeError("Native folder selection is only available on Windows; enter the folder path.")
    if not _PICKER_LOCK.acquire(blocking=False):
        raise ValueError("A folder picker is already open.")
    import ctypes
    from ctypes import wintypes

    class BrowseInfo(ctypes.Structure):
        _fields_ = [("hwndOwner", wintypes.HWND), ("pidlRoot", ctypes.c_void_p),
                    ("pszDisplayName", wintypes.LPWSTR), ("lpszTitle", wintypes.LPCWSTR),
                    ("ulFlags", wintypes.UINT), ("lpfn", ctypes.c_void_p),
                    ("lParam", wintypes.LPARAM), ("iImage", ctypes.c_int)]

    shell, ole = ctypes.windll.shell32, ctypes.windll.ole32
    shell.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BrowseInfo)]
    shell.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell.SHGetPathFromIDListEx.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    shell.SHGetPathFromIDListEx.restype = wintypes.BOOL
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2) in (0, 1)
    selected = None
    try:
        label = ctypes.create_unicode_buffer(32768)
        info = BrowseInfo()
        info.pszDisplayName = ctypes.cast(label, wintypes.LPWSTR)
        info.lpszTitle = "Smart Tool — select the folder to index"
        info.ulFlags = 0x0001 | 0x0040  # Diretórios reais; diálogo redimensionável.
        selected = shell.SHBrowseForFolderW(ctypes.byref(info))
        if not selected:
            return None
        path = ctypes.create_unicode_buffer(32768)
        if not shell.SHGetPathFromIDListEx(selected, path, len(path), 0):
            raise ValueError("The selection is not an accessible folder.")
        return path.value
    finally:
        if selected:
            ole.CoTaskMemFree(selected)
        if initialized:
            ole.CoUninitialize()
        _PICKER_LOCK.release()


def register_from_tray():
    import daemon_launcher
    root = choose_folder()
    if not root:
        return
    url = daemon_launcher.ensure_daemon_running().rstrip("/")
    with urllib.request.urlopen(url + "/projects", timeout=10) as response:
        page = response.read().decode("utf-8")
    match = re.search(r'<meta name="setup-token" content="([a-zA-Z0-9_-]+)">', page)
    if not match:
        raise RuntimeError("Reopen Smart Tool to load the updated projects page.")
    request = urllib.request.Request(url + "/setup/projects", method="POST",
        headers={"Content-Type": "application/json", "X-Setup-Token": match.group(1)},
        data=json.dumps({"action": "register", "project_root": root}).encode("utf-8"))
    with urllib.request.urlopen(request, timeout=10) as response:
        project = json.load(response)["project"]
    webbrowser.open(url + "/projects#" + project["id"])
