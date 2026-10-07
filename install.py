#!/usr/bin/env python3
import argparse
import filecmp
import json
import os
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
MANIFEST = ".install-manifest.json"
REGISTRY = Path(paths.DATA_DIR) / "daemon.json"
BLOCKED = {"__pycache__", ".token-guard", ".git", ".pytest_cache", ".data", ".artifacts", ".claude", ".vscode",
           ".impeccable", "tests", "sessions", "results", "dist", "node_modules", ".node-runtime", ".venv",
           ".scratch", ".memory", "docs", "reports", "site", "design", "release", "npm", ".github", "AGENTS.md", "CLAUDE.md", "DESIGN.md", "PRODUCT.md", "install.py", ".gitignore", ".gitattributes", MANIFEST}
PACKAGE_ONLY = {"bin", "package.json", ".npmignore"}
REQUIRED_TOOLS = {"smart_search", "smart_search_result", "web_search", "project_manage"}
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_FIND = ("$tray = $env:SMART_TRAY.ToLowerInvariant(); $daemon = $env:SMART_DAEMON.ToLowerInvariant(); "
         "Get-CimInstance Win32_Process -Filter \"Name = 'pythonw.exe' OR Name = 'python.exe'\" | "
         "Where-Object { $_.CommandLine -and ($_.CommandLine.ToLowerInvariant().Contains($tray) -or "
         "$_.CommandLine.ToLowerInvariant().Contains($daemon)) }")


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


def stop_processes(target):
    env = {"SMART_TRAY": str(target / "tray.py"), "SMART_DAEMON": str(target / "smart_tool_daemon.py")}
    powershell(_FIND + " | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }", env=env)
    deadline = time.monotonic() + 10
    while True:
        left = powershell("@(" + _FIND + ").Count", env=env).stdout.strip()
        if left == "0":
            return
        if time.monotonic() > deadline:
            raise RuntimeError(f"Could not close the running Smart Tool ({left or '?'} process(es) still active).")
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
