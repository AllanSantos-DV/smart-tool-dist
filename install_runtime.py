#!/usr/bin/env python3
"""Instala as dependências opcionais de busca dentro da pasta da Smart Tool.

Uso: python install_runtime.py [--check]
Não altera ~/.claude nem instala pacotes npm globalmente.
"""
import argparse
import collections
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import venv
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
NODE_DIR = ROOT / "web_adapters" / "node"
BROWSER_DIR = ROOT / "web_adapters" / "browser"
BROWSER_PYTHON = BROWSER_DIR / ".venv" / "Scripts" / "python.exe"
OPEN_WEBSEARCH_CLI = NODE_DIR / "node_modules" / "open-websearch" / "build" / "index.js"
TYPESCRIPT_PARSER = NODE_DIR / "node_modules" / "typescript" / "lib" / "typescript.js"
JAVA_PARSER = NODE_DIR / 'node_modules' / 'java-parser' / 'src' / 'index.js'
ANGULAR_PARSER = NODE_DIR / 'node_modules' / '@angular' / 'compiler' / 'fesm2022' / 'compiler.mjs'
# Browser control for agents (browser_control.py): Microsoft's Playwright MCP over the Chromium installed below.
PLAYWRIGHT_MCP_CLI = NODE_DIR / "node_modules" / "@playwright" / "mcp" / "cli.js"
NODE_RUNTIME = NODE_DIR / ".node-runtime"
# Moli renders the JavaScript pages web_fetch cannot read over HTTP; Chromium (Crawl4AI) stays as its fallback.
# Measured on 2026-10-10 over 21 such pages: 1.6 s and 90 MB peak (median) against 4.2 s and 581 MB for Chromium.
MOLI_VERSION = "1.1.15"
MOLI_FOLDER = f"moli-v{MOLI_VERSION}-x86_64-pc-windows-msvc"
MOLI_URL = f"https://github.com/lexmount/moli/releases/download/v{MOLI_VERSION}/moli-x86_64-pc-windows-msvc.zip"
MOLI_SHA256 = "64c00ce02d6db8e1c22c55e88fbcaaf3e7517e930f24c25ff46a457b421040c8"
MOLI_DIR = BROWSER_DIR / "moli"
MOLI_EXE = MOLI_DIR / "moli.exe"


def _node_supported(node):
    """Aceita apenas versoes suportadas pelo @angular/compiler 20.3.0."""
    try:
        result = subprocess.run([str(node), "--version"], capture_output=True, text=True,
                                timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    match = re.fullmatch(r"v(\d+)\.(\d+)\.\d+", result.stdout.strip())
    if result.returncode or not match:
        return False
    major, minor = map(int, match.groups())
    return (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major >= 24


def _node_commands():
    local_node, local_npm = NODE_RUNTIME / "node.exe", NODE_RUNTIME / "npm.cmd"
    if NODE_RUNTIME.exists():
        if local_node.is_file() and local_npm.is_file() and _node_supported(local_node):
            return str(local_node), str(local_npm)
        return None, None
    node = shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    return (node, npm) if node and npm and _node_supported(node) else (None, None)


def _download_verified(url, expected_sha256, path):
    """Downloads url to path and fails when its SHA-256 differs from the published one."""
    digest = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as response, path.open("wb") as out:
        while block := response.read(1024 * 1024):
            digest.update(block)
            out.write(block)
    if digest.hexdigest().lower() != expected_sha256.lower():
        raise RuntimeError(f"SHA-256 of {url} does not match the published checksum.")


def _extract_folder(zip_path, top, destination):
    """Extracts the files under the archive's single top folder into destination, refusing any other path."""
    destination.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            parts = Path(member.filename.replace("\\", "/")).parts
            if parts == (top,) and member.is_dir():
                continue
            if len(parts) < 2 or parts[0] != top or ".." in parts:
                raise RuntimeError(f"{zip_path.name} contains an unexpected path: {member.filename}")
            target = destination.joinpath(*parts[1:])
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as source, target.open("wb") as out:
                    shutil.copyfileobj(source, out)


def _replace_folder(unpacked, final, temp_dir):
    """Swaps final for unpacked, putting the previous folder back if the swap fails."""
    previous = temp_dir / "previous"
    if final.exists():
        os.replace(final, previous)
    try:
        os.replace(unpacked, final)
    except OSError:
        if previous.exists() and not final.exists():
            os.replace(previous, final)
        raise


def _install_node():
    """Baixa o Node LTS oficial e confere SHA-256 antes de extrair."""
    print("Downloading Node LTS for the Smart Tool web runtime...", flush=True)
    with urllib.request.urlopen("https://nodejs.org/dist/index.json", timeout=30) as response:
        releases = json.load(response)
    release = next((row for row in releases if row.get("lts") and
                    "win-x64-zip" in row.get("files", [])), None)
    if not release:
        raise RuntimeError("Node LTS for Windows x64 not found in the official index.")
    version = release["version"]
    archive = f"node-{version}-win-x64.zip"
    base = f"https://nodejs.org/dist/{version}/"
    with urllib.request.urlopen(base + "SHASUMS256.txt", timeout=30) as response:
        checksums = response.read().decode("ascii")
    expected = next((line.split()[0] for line in checksums.splitlines()
                     if line.split()[-1:] == [archive]), None)
    if not expected:
        raise RuntimeError("Official Node checksum not found.")
    with tempfile.TemporaryDirectory(prefix="node-setup-", dir=NODE_DIR) as temp:
        temp_dir = Path(temp)
        zip_path = temp_dir / archive
        _download_verified(base + archive, expected, zip_path)
        unpacked = temp_dir / "runtime"
        _extract_folder(zip_path, f"node-{version}-win-x64", unpacked)
        if not (unpacked / "node.exe").is_file() or not (unpacked / "npm.cmd").is_file():
            raise RuntimeError("Node package does not contain the expected executables.")
        _replace_folder(unpacked, NODE_RUNTIME, temp_dir)
    print(f"Node {version} installed inside Smart Tool.")


def _moli_ready():
    try:
        return MOLI_EXE.is_file() and (MOLI_DIR / "VERSION").read_text(encoding="utf-8").strip() == MOLI_VERSION
    except OSError:
        return False


def _install_moli():
    """Downloads the pinned Moli release from GitHub, checks its SHA-256 and unpacks it into web_adapters/browser."""
    print(f"Downloading Moli {MOLI_VERSION} (structured-first headless browser, 43 MB)...", flush=True)
    BROWSER_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="moli-setup-", dir=BROWSER_DIR) as temp:
        temp_dir = Path(temp)
        zip_path = temp_dir / "moli.zip"
        _download_verified(MOLI_URL, MOLI_SHA256, zip_path)
        unpacked = temp_dir / "moli"
        _extract_folder(zip_path, MOLI_FOLDER, unpacked)
        if not (unpacked / "moli.exe").is_file():
            raise RuntimeError("The Moli package does not contain moli.exe.")
        _replace_folder(unpacked, MOLI_DIR, temp_dir)
    print(f"Moli {MOLI_VERSION} installed inside Smart Tool.")


def _run(args, *, cwd=None, label="Web dependency", idle_s=600, stream=False, attempts=1):
    """Runs a step with UTF-8 output and closed stdin (a prompt fails at once instead of hanging); it is stopped only
    after idle_s without any output, and stream shows the output live."""
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    if NODE_RUNTIME.is_dir():
        env["PATH"] = str(NODE_RUNTIME) + os.pathsep + env.get("PATH", "")
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    for attempt in range(1, attempts + 1):
        print(f"{label}: starting{f' (attempt {attempt} of {attempts})' if attempt > 1 else ''}...", flush=True)
        try:
            _run_once(args, cwd, env, flags, label, idle_s, stream)
            print(f"{label}: done.", flush=True)
            return
        except RuntimeError as exc:
            if attempt == attempts:
                raise
            print(f"{exc}; trying again.", flush=True)


def _run_once(args, cwd, env, flags, label, idle_s, stream):
    with subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", creationflags=flags) as process:
        tail = collections.deque(maxlen=40)
        last_output = [time.monotonic()]
        shown = {"stage": None, "tens": -1}

        def pump():
            for line in process.stdout:
                line = line.rstrip()
                if not line:
                    continue
                tail.append(line)
                last_output[0] = time.monotonic()
                percent = re.match(r"(.*?)(\d{1,3})%$", line)
                if percent:
                    stage, tens = percent.group(1), int(percent.group(2)) // 10
                    if stage == shown["stage"] and tens <= shown["tens"]:
                        continue
                    shown.update(stage=stage, tens=tens)
                if stream:
                    print(f"{label}: {line}", flush=True)

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        started = noted = time.monotonic()
        while process.poll() is None:
            now = time.monotonic()
            if now - last_output[0] > idle_s:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    process.kill()
                process.wait(timeout=10)
                raise RuntimeError(f"{label} stopped: no output for {idle_s}s; last output: {tail[-1] if tail else 'none'}")
            if not stream and now - noted >= 30:
                noted = now
                print(f"{label}: waiting for {int(now - started)}s...", flush=True)
            time.sleep(0.5)
        reader.join(timeout=10)
        if process.returncode:
            if not stream:
                for line in list(tail)[-15:]:
                    print(f"{label}: {line}", flush=True)
            raise RuntimeError(f"{label} failed (exit {process.returncode}): {tail[-1] if tail else 'no output'}")


def _check():
    node, _npm = _node_commands()
    status = {
        "node": bool(node),
        "open_websearch": bool(node and OPEN_WEBSEARCH_CLI.is_file()),
        "typescript_parser": bool(node and TYPESCRIPT_PARSER.is_file()),
        "java_parser":bool(node and JAVA_PARSER.is_file()),
        "angular_parser":bool(node and ANGULAR_PARSER.is_file()),
        "playwright_mcp": bool(node and PLAYWRIGHT_MCP_CLI.is_file()),
        "browser_python": BROWSER_PYTHON.is_file(),
        "browser_packages": False,
        "camoufox_browser": False,
        "chromium_browser": False,
        "moli_browser": _moli_ready(),
    }
    if status["browser_python"]:
        try:
            result = subprocess.run(
                [str(BROWSER_PYTHON), "-c", "import camoufox, crawl4ai, playwright"],
                capture_output=True, check=False, timeout=30,
            )
            status["browser_packages"] = result.returncode == 0
        except subprocess.TimeoutExpired:
            status["browser_packages"] = False
    if status["browser_packages"]:
        status.update(_browser_executables())
    return status


_BROWSERS_PROBE = """
import json, os
from camoufox.pkgman import LAUNCH_FILE, OS_NAME, camoufox_path
from playwright.sync_api import sync_playwright
found = {}
try:
    found["camoufox_browser"] = os.path.isfile(os.path.join(camoufox_path(download_if_missing=False), LAUNCH_FILE[OS_NAME]))
except Exception:
    found["camoufox_browser"] = False
with sync_playwright() as playwright:
    found["chromium_browser"] = os.path.isfile(playwright.chromium.executable_path)
print(json.dumps(found))
"""


def _browser_executables():
    """Both browsers must exist on disk: a download tool reporting success is not enough."""
    try:
        result = subprocess.run([str(BROWSER_PYTHON), "-c", _BROWSERS_PROBE], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", check=False, timeout=60)
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        return {"camoufox_browser": False, "chromium_browser": False}


def _link_existing_browser(existing, link):
    try:
        os.symlink(existing, link, target_is_directory=True)
    except OSError:
        # Junction não exige Developer Mode nem privilégio de symlink no Windows.
        env = os.environ.copy()
        env["SMART_TOOL_BROWSER_LINK_PATH"] = str(link)
        env["SMART_TOOL_BROWSER_LINK_TARGET"] = str(existing)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             "New-Item -ItemType Junction -Path $env:SMART_TOOL_BROWSER_LINK_PATH "
             "-Target $env:SMART_TOOL_BROWSER_LINK_TARGET | Out-Null"],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode or not link.is_dir():
            raise RuntimeError(f"Could not link the existing runtime: {result.stderr.strip()[-600:]}")


def install(reuse_browser_runtime=None):
    if os.name != "nt":
        raise RuntimeError("This browser installer is built for Windows.")
    node, npm = _node_commands()
    if not node or not npm:
        _install_node()
        node, npm = _node_commands()
    if not all(path.is_file() for path in (OPEN_WEBSEARCH_CLI, TYPESCRIPT_PARSER, JAVA_PARSER, ANGULAR_PARSER,
                                               PLAYWRIGHT_MCP_CLI)):
        _run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=NODE_DIR,
             label="Web adapters and code parser via npm", idle_s=300)
    if reuse_browser_runtime:
        existing = Path(reuse_browser_runtime).expanduser().resolve()
        if not (existing / "Scripts" / "python.exe").is_file():
            raise RuntimeError(f"Invalid browser environment: {existing}")
        link = BROWSER_DIR / ".venv"
        if link.exists() and link.resolve() != existing:
            raise RuntimeError(f"Another browser environment already exists at {link}")
        if not link.exists():
            _link_existing_browser(existing, link)
    if not BROWSER_PYTHON.is_file():
        venv.create(BROWSER_DIR / ".venv", with_pip=True)
    if not _check()["browser_packages"]:
        _run([str(BROWSER_PYTHON), "-m", "pip", "install", "--disable-pip-version-check",
              "--no-input", "-r", str(BROWSER_DIR / "requirements.txt")],
             label="Camoufox and Crawl4AI via pip", idle_s=600)
    # Camoufox usa Firefox modificado; Crawl4AI usa Chromium via Playwright.
    if not reuse_browser_runtime:
        _run([str(BROWSER_PYTHON), str(BROWSER_DIR / "fetch_camoufox.py")], label="Camoufox browser (1.3 GB)",
             idle_s=180, stream=True, attempts=3)
        _run([str(BROWSER_PYTHON), "-m", "playwright", "install", "chromium"],
             label="Chromium browser", idle_s=180, stream=True, attempts=3)
    if not _moli_ready():
        _install_moli()
    status = _check()
    if not all(status.values()):
        raise RuntimeError(f"Incomplete web runtime: {status}")
    print("Smart Tool runtime installed: Node, open-websearch, TypeScript, Java, Angular, Playwright MCP, Camoufox, Crawl4AI and Moli.")


def main():
    parser = argparse.ArgumentParser(description="Install/check the Smart Tool web runtime")
    parser.add_argument("--check", action="store_true", help="Show the status without installing")
    parser.add_argument("--reuse-browser-runtime", metavar="FOLDER",
                        help="Reuse an existing venv without duplicating files (useful when disk is low)")
    args = parser.parse_args()
    if args.check:
        status = _check()
        for name, ready in status.items():
            print(f"{name}: {'ok' if ready else 'missing'}")
        return 0 if all(status.values()) else 1
    try:
        install(args.reuse_browser_runtime)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Web runtime installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
