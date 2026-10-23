#!/usr/bin/env node
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { Failure, certificateAdvice, download, isCertificateText, nativeTlsEnvironment } from "./network.mjs";

const PACKAGE_DIR = join(dirname(fileURLToPath(import.meta.url)), "..");
const RUNTIME_DIR = join(homedir(), ".smart-tool", "runtime");
const UV_VERSION = "0.12.23";
const UV_ASSET = "uv-x86_64-pc-windows-msvc.zip";
const UV_SHA256 = "75d05de6762778c31ee183398de7dd15093fad0ed90b1f236d8205ea5ec00c90";
const UV_DIR = join(RUNTIME_DIR, `uv-${UV_VERSION}`);
const UV_EXE = join(UV_DIR, "uv.exe");
const PYTHON_VERSION = "3.12";
const PYTHON_DIR = join(RUNTIME_DIR, "python");
const USAGE = `Usage: npx @allansantos-dev/smart-tool install [--target DIR] [--no-start] [--skip-deps]

Installs or updates Smart Tool for this Windows user. Run it again with @latest to update.`;

function fail(message) {
  throw new Failure(message);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { encoding: "utf8", windowsHide: true, ...options });
  if (result.error) fail(`${command} did not start: ${result.error.message}`);
  if (result.status !== 0) fail(`${command} ${args.join(" ")} exited with ${result.status}: ${(result.stderr || result.stdout || "").trim().slice(-2000)}`);
  return (result.stdout || "").trim();
}

function runAgain(env) {
  const again = spawnSync(process.execPath, [fileURLToPath(import.meta.url), ...process.argv.slice(2)], { stdio: "inherit", env });
  if (again.error) fail(`could not run again: ${again.error.message}`);
  return again.status ?? 1;
}

async function ensureUv() {
  if (existsSync(UV_EXE)) return;
  const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${UV_ASSET}`;
  console.log(`Downloading uv ${UV_VERSION} (Python manager)...`);
  const { body: archive, rerun } = await download(url, { rerun: runAgain });
  if (rerun !== undefined) return { rerun };
  const digest = createHash("sha256").update(archive).digest("hex");
  if (digest !== UV_SHA256) fail(`${UV_ASSET} checksum mismatch (expected ${UV_SHA256}, got ${digest}).`);
  const scratch = mkdtempSync(join(tmpdir(), "smart-tool-uv-"));
  try {
    const zip = join(scratch, UV_ASSET);
    writeFileSync(zip, archive);
    mkdirSync(UV_DIR, { recursive: true });
    run(join(process.env.SystemRoot || "C:\\Windows", "System32", "tar.exe"), ["-xf", zip, "-C", UV_DIR]);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
  if (!existsSync(UV_EXE)) fail(`uv.exe not found in ${UV_DIR} after extracting ${UV_ASSET}.`);
}

function ensurePython() {
  const env = { ...process.env, UV_PYTHON_INSTALL_DIR: PYTHON_DIR };
  console.log(`Preparing Python ${PYTHON_VERSION} in ${PYTHON_DIR}...`);
  const install = ["python", "install", PYTHON_VERSION, "--no-bin", "--no-registry"];
  try {
    run(UV_EXE, install, { env });
  } catch (error) {
    const native = nativeTlsEnvironment(env);
    if (!(error instanceof Failure) || !isCertificateText(error.message)) throw error;
    if (!native) throw new Failure(`${error.message}\n${certificateAdvice(env)}`);
    console.log("TLS inspection detected by uv; running it again with the Windows certificate store...");
    env.UV_NATIVE_TLS = native.UV_NATIVE_TLS;
    run(UV_EXE, install, { env });
  }
  const python = run(UV_EXE, ["python", "find", "--managed-python", "--no-python-downloads", PYTHON_VERSION], { env });
  if (!python.toLowerCase().startsWith(PYTHON_DIR.toLowerCase())) fail(`uv returned a Python outside ${PYTHON_DIR}: ${python}`);
  return python;
}

async function main() {
  const [command, ...rest] = process.argv.slice(2);
  if (command === "--version" || command === "-v") {
    console.log(JSON.parse(readFileSync(join(PACKAGE_DIR, "package.json"), "utf8")).version);
    return;
  }
  if (command !== "install") {
    console.log(USAGE);
    process.exitCode = command === undefined || command === "--help" || command === "-h" ? 0 : 1;
    return;
  }
  if (process.platform !== "win32" || process.arch !== "x64") fail(`Smart Tool runs on Windows x64 only (this is ${process.platform}-${process.arch}).`);
  const again = await ensureUv();
  if (again) {
    process.exitCode = again.rerun;
    return;
  }
  const python = ensurePython();
  const installer = spawnSync(python, [join(PACKAGE_DIR, "install.py"), "--skip-tests", ...rest], {
    stdio: "inherit",
    cwd: PACKAGE_DIR,
    env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
  });
  if (installer.error) fail(`the installer did not start: ${installer.error.message}`);
  process.exitCode = installer.status ?? 1;
}

main().catch((error) => {
  console.error(error instanceof Failure ? `smart-tool: ${error.message}` : `smart-tool: ${error.stack || error}`);
  process.exitCode = 1;
});
