#!/usr/bin/env python3
"""Instala as dependências opcionais de busca dentro da pasta da Smart Tool.

Uso: python install_runtime.py [--check]
Não altera ~/.claude nem instala pacotes npm globalmente.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
NODE_RUNTIME = NODE_DIR / ".node-runtime"


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
        digest = hashlib.sha256()
        with urllib.request.urlopen(base + archive, timeout=60) as response, zip_path.open("wb") as out:
            while block := response.read(1024 * 1024):
                digest.update(block)
                out.write(block)
        if digest.hexdigest().lower() != expected.lower():
            raise RuntimeError("Node SHA-256 checksum does not match the official index.")
        unpacked = temp_dir / "runtime"
        unpacked.mkdir()
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.infolist():
                parts = Path(member.filename.replace("\\", "/")).parts
                if parts == (f"node-{version}-win-x64",) and member.is_dir():
                    continue
                if len(parts) < 2 or parts[0] != f"node-{version}-win-x64" or ".." in parts:
                    raise RuntimeError("Node archive contains an unexpected path.")
                destination = unpacked.joinpath(*parts[1:])
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target)
        if not (unpacked / "node.exe").is_file() or not (unpacked / "npm.cmd").is_file():
            raise RuntimeError("Node package does not contain the expected executables.")
        previous = temp_dir / "previous-node-runtime"
        if NODE_RUNTIME.exists():
            os.replace(NODE_RUNTIME, previous)
        try:
            os.replace(unpacked, NODE_RUNTIME)
        except OSError:
            if previous.exists() and not NODE_RUNTIME.exists():
                os.replace(previous, NODE_RUNTIME)
            raise
    print(f"Node {version} installed inside Smart Tool.")


def _run(args, *, cwd=None, label="Web dependency", timeout_s=600):
    env = os.environ.copy()
    if NODE_RUNTIME.is_dir():
        env["PATH"] = str(NODE_RUNTIME) + os.pathsep + env.get("PATH", "")
    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0
    print(f"{label}: starting...", flush=True)
    started = time.monotonic()
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               errors="replace", creationflags=flags)
    while True:
        remaining = timeout_s - (time.monotonic() - started)
        if remaining <= 0:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                process.kill()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=5)
            raise RuntimeError(f"{label} exceeded {timeout_s}s; check network, proxy and access to the package registry.")
        try:
            stdout, stderr = process.communicate(timeout=min(30, remaining))
            break
        except subprocess.TimeoutExpired:
            print(f"{label}: waiting for {int(time.monotonic() - started)}s...", flush=True)
    if process.returncode:
        detail = (stderr or stdout).strip()[-1500:]
        raise RuntimeError(f"{' '.join(map(str, args[:3]))} failed: {detail}")
    print(f"{label}: done.", flush=True)


def _check():
    node, _npm = _node_commands()
    status = {
        "node": bool(node),
        "open_websearch": bool(node and OPEN_WEBSEARCH_CLI.is_file()),
        "typescript_parser": bool(node and TYPESCRIPT_PARSER.is_file()),
        "java_parser":bool(node and JAVA_PARSER.is_file()),
        "angular_parser":bool(node and ANGULAR_PARSER.is_file()),
        "browser_python": BROWSER_PYTHON.is_file(),
        "browser_packages": False,
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
    return status


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
    if not all(path.is_file() for path in (OPEN_WEBSEARCH_CLI,TYPESCRIPT_PARSER,JAVA_PARSER,ANGULAR_PARSER)):
        _run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=NODE_DIR,
             label="Web adapters and code parser via npm", timeout_s=300)
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
             label="Camoufox and Crawl4AI via pip", timeout_s=600)
    # Camoufox usa Firefox modificado; Crawl4AI usa Chromium via Playwright.
    if not reuse_browser_runtime:
        _run([str(BROWSER_PYTHON), "-m", "camoufox", "fetch"],
             label="Camoufox browser", timeout_s=600)
        _run([str(BROWSER_PYTHON), "-m", "playwright", "install", "chromium"],
             label="Chromium browser", timeout_s=600)
    status = _check()
    if not all(status.values()):
        raise RuntimeError(f"Incomplete web runtime: {status}")
    print("Smart Tool runtime installed: Node, open-websearch, TypeScript, Java, Angular, Camoufox and Crawl4AI.")


def main():
    parser = argparse.ArgumentParser(description="Install/check the Smart Tool web runtime")
    parser.add_argument("--check", action="store_true", help="Show the status without installing")
    parser.add_argument("--reuse-browser-runtime", metavar="FOLDER",
                        help="Reuse an existing venv without duplicating files (useful when disk is low)")
    args = parser.parse_args()
    if args.check:
        for name, ready in _check().items():
            print(f"{name}: {'ok' if ready else 'missing'}")
        return 0 if all(_check().values()) else 1
    try:
        install(args.reuse_browser_runtime)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Web runtime installation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
