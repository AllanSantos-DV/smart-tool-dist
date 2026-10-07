#!/usr/bin/env python3
import argparse
import filecmp
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import venv
from pathlib import Path

import paths

SOURCE = Path(__file__).resolve().parent
DEFAULT_TARGET = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "Programs" / "SmartTool"
TASK_NAME = "SmartTool-Tray"
ENTRY_SCRIPTS = ("tray.py", "smart_tool_daemon.py", "pretool_router.py")
STARTUP_SCRIPT = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "Microsoft" / "Windows" / \
    "Start Menu" / "Programs" / "Startup" / "SmartTool-Tray.vbs"
MANIFEST = ".install-manifest.json"
REGISTRY = Path(paths.DATA_DIR) / "daemon.json"
BLOCKED = {"__pycache__", ".token-guard", ".git", ".pytest_cache", ".data", ".artifacts", ".claude", ".vscode",
           ".impeccable", "tests", "sessions", "results", "dist", "node_modules", ".node-runtime", ".venv",
           ".scratch", ".memory", "docs", "reports", "site", "design", "release", "npm", ".github", "AGENTS.md", "CLAUDE.md", "DESIGN.md", "PRODUCT.md", "install.py", ".gitignore", ".gitattributes", MANIFEST}
PACKAGE_ONLY = {"bin", "package.json", ".npmignore"}
REQUIRED_TOOLS = {"smart_search", "smart_search_result", "web_search", "project_manage"}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def ships(rel):
    parts = Path(rel).parts
    *folders, name = parts
    return (parts[0] not in PACKAGE_ONLY
            and not any(part in BLOCKED or part.startswith("_cdp_") for part in folders)
            and name not in BLOCKED and not name.endswith((".log", ".pyc", ".png")))


def source_files():
    files = []
    for current, dirs, names in os.walk(SOURCE):
        dirs[:] = sorted(d for d in dirs if d not in BLOCKED and not d.startswith("_cdp_"))
        files += [rel for rel in ((Path(current) / name).relative_to(SOURCE).as_posix() for name in sorted(names))
                  if ships(rel)]
    return files


def python_paths(target):
    scripts = target / ".venv" / "Scripts"
    return scripts / "python.exe", scripts / "pythonw.exe"


def powershell(script, env=None, timeout=30):
    return subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], env={**os.environ, **(env or {})},
                          capture_output=True, text=True, timeout=timeout)


def _processes():
    """(pid, command line) of every python/pythonw process."""
    listed = powershell("Get-CimInstance Win32_Process -Filter \"Name = 'pythonw.exe' OR Name = 'python.exe'\" | "
                        "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }").stdout
    rows = (row.partition("\t") for row in listed.splitlines())
    return [(int(pid), line) for pid, _tab, line in rows if pid.strip().isdigit()]


def _script_folder(script):
    path = Path(script)
    return path.parent.parent if path.name == "pretool_router.py" else path.parent


def _same_folder(path, folder):
    """Compares through resolve(), which turns 8.3 short and mixed spellings into the long name."""
    try:
        return path.resolve() == folder.resolve()
    except OSError:
        return False


def stop_processes(target):
    def running():
        return [pid for pid, line in _processes() for script in _script_paths(line)
                if Path(script).name != "pretool_router.py" and _same_folder(_script_folder(script), target)]

    for pid in running():
        powershell(f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")
    deadline = time.monotonic() + 10
    while True:
        left = running()
        if not left:
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"Could not close the running Smart Tool ({len(left)} process(es) still active).")
        time.sleep(0.5)


def wait_port_free():
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and _daemon_up():
        time.sleep(0.5)
    if _daemon_up():
        raise RuntimeError("Another Smart Tool daemon is still active on the registered port; close it before updating.")


def _daemon_up():
    try:
        url = json.loads(REGISTRY.read_text(encoding="utf-8"))["url"]
        with _OPENER.open(url + "/health", timeout=1) as response:
            return response.status == 200
    except (OSError, ValueError, KeyError):
        return False


def hook_files():
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return claude / "settings.json", Path.home() / ".codex" / "hooks.json"


def _script_paths(text):
    """Absolute paths ending in an entry script, as written in text, tried from every drive letter (paths may hold
    spaces)."""
    script = re.compile(r'[^"\r\n]*?(?:' + "|".join(map(re.escape, ENTRY_SCRIPTS)) + ")")
    for drive in re.finditer(r"[A-Za-z]:[\\/]", text):
        found = script.match(text, drive.start())
        if found:
            yield found.group(0)


def _install_folder(script):
    folder = _script_folder(script)
    try:
        return folder.resolve() if (folder / MANIFEST).is_file() and (folder / "smart_tool_daemon.py").is_file() else None
    except OSError:
        return None


def _task_definition():
    result = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], capture_output=True, text=True,
                            timeout=20)
    return result.stdout if result.returncode == 0 else ""


def other_installs(target):
    """Installer-made Smart Tool folders other than target, found through what still runs or points at them: tray and
    daemon processes, the logon task or Startup script, and agent hook commands."""
    texts = [line for _pid, line in _processes()] + [_task_definition()]
    texts += [path.read_text(encoding="utf-8", errors="replace") for path in (STARTUP_SCRIPT, *hook_files())
              if path.is_file()]
    folders = {_install_folder(script) for text in texts for script in _script_paths(text)}
    return sorted(folder for folder in folders
                  if folder and not _same_folder(folder, target) and not _same_folder(folder, SOURCE))


def _repointed(text, folder, target):
    """text with each spelling of folder found in front of an entry script (long, 8.3 or mixed) replaced by target."""
    for script in set(_script_paths(text)):
        cut = len(Path(script).name) + 1 + (len("hooks") + 1 if Path(script).name == "pretool_router.py" else 0)
        spelled = script[:-cut]
        if _same_folder(Path(spelled), folder):
            text = text.replace(spelled, target.as_posix() if "/" in spelled else str(target))
    return text


def remove_other_installs(target):
    """An older installation in another folder keeps the port and the hooks: stop it, point the hooks at target and
    delete its program folder. Its state folders are left alone."""
    removed = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for folder in other_installs(target):
        print(f"Older Smart Tool installation found at {folder}; removing it.")
        stop_processes(folder)
        for path in hook_files():
            original = path.read_text(encoding="utf-8") if path.is_file() else ""
            text = _repointed(original, folder, target)
            if text != original:
                shutil.copy2(path, path.with_name(f"{path.name}.smart-tool-backup-{stamp}"))
                path.write_text(text, encoding="utf-8")
                print(f"Hooks in {path} now point to {target}.")
        startup = STARTUP_SCRIPT.read_text(encoding="utf-8", errors="replace") if STARTUP_SCRIPT.is_file() else ""
        if any(_same_folder(_script_folder(script), folder) for script in _script_paths(startup)):
            STARTUP_SCRIPT.unlink()
        shutil.rmtree(folder)
        removed.append(folder)
    return removed


def copy_files(target, files, backup, changed, removed):
    previous = set(json.loads((target / MANIFEST).read_text(encoding="utf-8"))["files"]) if (target / MANIFEST).is_file() else set()
    if (target / MANIFEST).is_file():
        shutil.copy2(target / MANIFEST, backup / MANIFEST)
    for rel in files:
        source, dest = SOURCE / rel, target / rel
        if dest.is_file() and filecmp.cmp(source, dest, shallow=False):
            continue
        if dest.is_file():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dest, backup / rel)
        changed.append(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    for rel in sorted(previous - set(files)):
        if (target / rel).is_file():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(target / rel, backup / rel)
            removed.append(rel)
            folder = (target / rel).parent
            while folder != target and not any(folder.iterdir()):
                folder.rmdir()
                folder = folder.parent
    (target / MANIFEST).write_text(json.dumps({"installed_at": time.time(), "source": str(SOURCE), "files": files},
                                              indent=1), encoding="utf-8")


def restore(target, backup, changed, removed):
    for rel in changed:
        if (backup / rel).is_file():
            shutil.copy2(backup / rel, target / rel)
        else:
            (target / rel).unlink(missing_ok=True)
    for rel in removed:
        if (backup / rel).is_file():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(backup / rel, target / rel)
    if (backup / MANIFEST).is_file():
        shutil.copy2(backup / MANIFEST, target / MANIFEST)
    else:
        (target / MANIFEST).unlink(missing_ok=True)


def freeze(python):
    if not python.exists():
        return None
    return subprocess.run([str(python), "-m", "pip", "freeze", "--disable-pip-version-check"],
                          check=True, capture_output=True, text=True).stdout


def restore_packages(python, frozen, backup):
    if frozen is None:
        return
    (backup / "requirements.frozen.txt").write_text(frozen, encoding="utf-8")
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                    str(backup / "requirements.frozen.txt")], check=True)


def install_dependencies(target, python):
    if not python.exists():
        venv.create(python.parent.parent, with_pip=True)
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r",
                    str(target / "requirements.txt")], check=True)
    subprocess.run([str(python), str(target / "install_runtime.py")], check=True)


def start(target, python, pythonw):
    subprocess.run([str(python), "-c", "import autostart, json; print(json.dumps(autostart.install()))"],
                   cwd=target, check=True, capture_output=True, text=True, timeout=60)
    subprocess.Popen([str(pythonw if pythonw.exists() else python), str(target / "tray.py")], cwd=target,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0))


def verify(target, python):
    script = ("import json,urllib.request,daemon_launcher; url=daemon_launcher.ensure_daemon_running(wait_seconds=30); "
              "body=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}}).encode(); "
              "req=urllib.request.Request(url+'/mcp',data=body,method='POST',headers={'Content-Type':'application/json'}); "
              "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
              "print(json.dumps(sorted(r['name'] for r in json.load(opener.open(req,timeout=10))['result']['tools'])))")
    result = subprocess.run([str(python), "-c", script], cwd=target, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError("Daemon did not respond: " + ((result.stderr or result.stdout).strip().splitlines() or ["no output"])[-1])
    missing = REQUIRED_TOOLS - set(json.loads(result.stdout.strip().splitlines()[-1]))
    if missing:
        raise RuntimeError("Missing MCP tools: " + ", ".join(sorted(missing)))
    pid = json.loads(REGISTRY.read_text(encoding="utf-8"))["pid"]
    command = powershell(f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {int(pid)}').CommandLine").stdout
    if str(target / "smart_tool_daemon.py").lower() not in command.lower():
        raise RuntimeError(f"The active daemon (pid {pid}) does not belong to this installation: {command.strip()[:200]}")


MIN_PYTHON = (3, 11)


def main():
    if sys.version_info < MIN_PYTHON:
        raise SystemExit(f"Smart Tool needs Python {'.'.join(map(str, MIN_PYTHON))} or newer; this is "
                         f"{sys.version.split()[0]} ({sys.executable}).")
    parser = argparse.ArgumentParser(description="Install or update Smart Tool over the existing installation.")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--skip-deps", action="store_true", help="do not run pip or install_runtime.py")
    parser.add_argument("--no-start", action="store_true", help="do not restart or check the daemon")
    parser.add_argument("--skip-tests", action="store_true",
                        help="install without running tests/run.py (npm package, whose tests ran in CI; emergency only otherwise)")
    args = parser.parse_args()
    target = args.target.resolve()
    if target == SOURCE:
        raise SystemExit("The target cannot be the source folder itself.")
    if args.skip_tests:
        print("Tests skipped by --skip-tests.")
    elif not (SOURCE / "tests" / "run.py").is_file():
        raise SystemExit(f"No test suite in {SOURCE}; a packaged install runs with --skip-tests (npx @allansantos-dev/smart-tool install).")
    else:
        interpreter = python_paths(target)[0] if python_paths(target)[0].is_file() else Path(sys.executable)
        tests = subprocess.run([str(interpreter), str(SOURCE / "tests" / "run.py")], capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
        print(tests.stdout.rstrip())
        if tests.stderr.strip():
            print(tests.stderr.rstrip()[-3000:], file=sys.stderr)
        if tests.returncode:
            raise SystemExit("Tests failed; installation aborted without touching the installed version.")
    python, pythonw = python_paths(target)
    files = source_files()
    existed = (target / "smart_tool_daemon.py").is_file()
    print(("Updating" if existed else "Installing") + f" Smart Tool at {target} ({len(files)} files from {SOURCE})")
    target.mkdir(parents=True, exist_ok=True)
    remove_other_installs(target)
    stop_processes(target)
    if not args.no_start:
        wait_port_free()
    backup = target.parent / f".smart-tool-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    backup.mkdir()
    changed, removed, frozen, deps_touched = [], [], None, False
    try:
        copy_files(target, files, backup, changed, removed)
        print(f"Files changed: {len(changed)}; removed as obsolete: {len(removed)}")
        if not args.skip_deps:
            frozen = freeze(python)
            deps_touched = True
            install_dependencies(target, python)
        if not args.no_start:
            start(target, python, pythonw)
            verify(target, python)
    except BaseException as exc:
        print(f"Failed: {exc}. Restoring the previous version.", file=sys.stderr)
        problems = []
        steps = [lambda: stop_processes(target), lambda: restore(target, backup, changed, removed)]
        if deps_touched:
            steps.append(lambda: restore_packages(python, frozen, backup))
        if existed and not args.no_start:
            steps.append(lambda: start(target, python, pythonw))
        for step in steps:
            try:
                step()
            except Exception as error:
                problems.append(str(error))
        if problems:
            print(f"Incomplete rollback; backup kept at {backup}: " + " | ".join(problems), file=sys.stderr)
            raise SystemExit(1)
        shutil.rmtree(backup, ignore_errors=True)
        raise SystemExit(1)
    shutil.rmtree(backup)
    print("Smart Tool ready." if not args.no_start else "Files installed; daemon not restarted (--no-start).")


if __name__ == "__main__":
    main()
