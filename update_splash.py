"""Update screen: a small window with no buttons that runs the update with no console window and shows each step in
the interface language, then the result. Started by the tray (update_check.run_update) as `pythonw update_splash.py
<version>`; it outlives the tray, which the installer stops and starts again.

The install command runs with CREATE_NO_WINDOW, so it and every console program it starts (node, the installer,
pip, PowerShell, schtasks) share one hidden console. Its output goes to update_check.UPDATE_LOG; a failure shows the last line,
the log path and a Windows notification, and the installer has already restored the previous version. The outcome names
the version read from the installed version.py after the run, not the one this process loaded before it."""
import os
import queue
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import i18n
import node_locator
import toast_notice
import update_check
import version

TIMEOUT_S = 20 * 60
SUCCESS_CLOSE_MS = 4000
FAILURE_CLOSE_MS = 15000
MUTEX_NAME = "Local\\SmartToolUpdate"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STEPS = (
    ("Downloading uv", i18n.N_("Downloading components…")),
    ("Preparing Python", i18n.N_("Preparing Python…")),
    ("TLS inspection", i18n.N_("Preparing Python…")),
    ("Camoufox browser", i18n.N_("Checking the browsers…")),
    ("Chromium browser", i18n.N_("Checking the browsers…")),
    ("Older Smart Tool installation found", i18n.N_("Removing an older installation…")),
    ("Updating Smart Tool at", i18n.N_("Copying the new files…")),
    ("Installing Smart Tool at", i18n.N_("Copying the new files…")),
    ("Files changed:", i18n.N_("Installing dependencies…")),
    ("Browser control", i18n.N_("Checking browser control…")),
    ("Failed:", i18n.N_("Restoring the previous version…")),
)


def step_for(line):
    """English catalog key of the step an installer output line starts, or None."""
    for marker, step in STEPS:
        if marker in line:
            return step
    return None


def install_command(target):
    """argv of the update: the npx command a user types, through cmd.exe because npx is a .cmd script, for the version
    the tray announced. `@latest` would let npx reuse an older package from its cache and install that instead, and
    --expect-version makes the installer refuse a package that is not that version."""
    npx = node_locator.which("npx.cmd")
    if not npx:
        raise RuntimeError(i18n.t("Node.js (npx) was not found; install Node.js and try again."))
    return ["cmd.exe", "/d", "/c", npx, "--yes", f"{update_check.PACKAGE}@{target}", "install",
            "--expect-version", target], npx


def installed_version(folder=os.path.dirname(os.path.abspath(__file__))):
    """VERSION as written in folder's version.py now: the installer replaces that file, so after an update it tells which
    version is really installed. Raises when the file has no VERSION line."""
    path = os.path.join(folder, "version.py")
    with open(path, encoding="utf-8") as stream:
        found = re.search(r'^VERSION = "([^"]+)"', stream.read(), re.M)
    if not found:
        raise RuntimeError(f"{path} has no VERSION line")
    return found.group(1)


def run_install(lines, target):
    """Runs the install command hidden, puts each output line on `lines`, writes them to update_check.UPDATE_LOG and returns
    (exit code, last non-empty line). Kills the whole process tree after TIMEOUT_S."""
    command, npx = install_command(target)
    os.makedirs(os.path.dirname(update_check.UPDATE_LOG), exist_ok=True)
    last = ""
    with open(update_check.UPDATE_LOG, "w", encoding="utf-8") as log:
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(command)}\n")
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                              cwd=os.path.expanduser("~"), env=node_locator.environment(prefer=npx)) as proc:
            timer = threading.Timer(TIMEOUT_S, lambda: subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW))
            timer.start()
            try:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    if line.strip():
                        last = line.strip()
                        lines.put(last)
                code = proc.wait()
            finally:
                timer.cancel()
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} exit {code}\n")
    return code, last


def _single_instance():
    """True when no other update screen is running (a named mutex held for the life of this process)."""
    if sys.platform != "win32":
        return True
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return bool(handle) and ctypes.get_last_error() != 183


class Splash:
    """The window: product name, the version change, the current step, a moving bar; no buttons and no close box."""

    def __init__(self, target):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.root = root = tk.Tk()
        root.withdraw()
        root.title("Smart Tool")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        width, height = 500, 236
        root.geometry(f"{width}x{height}+{(root.winfo_screenwidth() - width) // 2}+{(root.winfo_screenheight() - height) // 3}")
        frame = tk.Frame(root, bg="#ffffff", highlightthickness=1, highlightbackground="#c9d2df")
        frame.pack(fill="both", expand=True)
        tk.Frame(frame, bg="#2f6fed", height=6).pack(fill="x")
        body = tk.Frame(frame, bg="#ffffff", padx=28, pady=18)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="Smart Tool", bg="#ffffff", fg="#2f6fed", font=("Segoe UI Semibold", 11)).pack(anchor="w")
        self.title = tk.Label(body, text=i18n.t("Updating Smart Tool"), bg="#ffffff", fg="#1b2430",
                              font=("Segoe UI Semibold", 17))
        self.title.pack(anchor="w", pady=(2, 0))
        tk.Label(body, text=i18n.t("Version {old} → {new}", old=version.VERSION, new=target), bg="#ffffff",
                 fg="#5b6472", font=("Segoe UI", 10)).pack(anchor="w")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("Smart.Horizontal.TProgressbar", troughcolor="#e8edf5", background="#2f6fed",
                        bordercolor="#e8edf5", lightcolor="#2f6fed", darkcolor="#2f6fed")
        self.bar = ttk.Progressbar(body, mode="indeterminate", style="Smart.Horizontal.TProgressbar", length=444)
        self.bar.pack(anchor="w", pady=(16, 8))
        self.bar.start(12)
        self.step = tk.Label(body, text=i18n.t("Downloading version {version}…", version=target), bg="#ffffff",
                             fg="#1b2430", font=("Segoe UI", 10), anchor="w", justify="left", wraplength=444)
        self.step.pack(anchor="w", fill="x")
        self.note = tk.Label(body, text=i18n.t("This can take a few minutes. You do not need to do anything."),
                             bg="#ffffff", fg="#5b6472", font=("Segoe UI", 9), anchor="w", justify="left",
                             wraplength=444)
        self.note.pack(anchor="w", fill="x", pady=(6, 0))
        root.deiconify()

    def show_step(self, key):
        self.step.config(text=i18n.t(key))

    def finish(self, ok, title, detail, note):
        self.bar.stop()
        self.bar.config(mode="determinate", maximum=100, value=100 if ok else 0)
        self.title.config(text=title, fg="#18723c" if ok else "#b52c2c")
        self.step.config(text=detail)
        self.note.config(text=note)
        self.root.after(SUCCESS_CLOSE_MS if ok else FAILURE_CLOSE_MS, self.root.destroy)


def main(argv):
    """Shows the screen, runs the update and reports the outcome; returns the installer's exit code."""
    if len(argv) < 2:
        raise SystemExit("usage: update_splash.py <version>: the version to install, as announced by the tray")
    target = argv[1]
    if not _single_instance():
        return 0
    splash = Splash(target)
    lines, outcome = queue.Queue(), {}

    def work():
        try:
            outcome["code"], outcome["last"] = run_install(lines, target)
        except Exception as exc:
            outcome["code"], outcome["last"] = 1, f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=work, daemon=True)
    worker.start()

    def pump():
        while not lines.empty():
            step = step_for(lines.get_nowait())
            if step:
                splash.show_step(step)
        if worker.is_alive():
            splash.root.after(200, pump)
            return
        try:
            installed = installed_version()
        except (OSError, RuntimeError) as exc:
            installed = None
            outcome["code"], outcome["last"] = outcome["code"] or 1, f"{type(exc).__name__}: {exc}"
        if outcome["code"] == 0 and installed == target:
            splash.finish(True, i18n.t("Smart Tool updated"),
                          i18n.t("Version {version} is installed and running.", version=target),
                          i18n.t("This window closes by itself."))
            return
        if outcome["code"] == 0:
            outcome["last"] = f"installed version.py says {installed}, not {target}"
        detail = i18n.t("The update did not finish; version {version} is still installed.",
                        version=installed or version.VERSION)
        where = i18n.t("Details in {path}", path=update_check.UPDATE_LOG)
        splash.finish(False, i18n.t("Update not finished"), f"{detail}\n{outcome['last'][:180]}", where)
        try:
            toast_notice.show(i18n.t("Smart Tool update not finished"), [detail, where],
                              [(i18n.t("View log"), toast_notice.action_uri("open-update-log"))], tag="update-failed")
        except Exception as exc:
            splash.note.config(text=f"{splash.note.cget('text')}\n{exc}")

    splash.root.after(200, pump)
    splash.root.mainloop()
    worker.join(timeout=1)
    return outcome.get("code", 1)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
