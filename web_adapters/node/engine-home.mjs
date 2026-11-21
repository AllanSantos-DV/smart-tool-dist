/**
 * Brings a shared engine of the engine registry up: installs it or updates it with the kit's SHA-256 and Ed25519 checks,
 * starts it hidden and waits for its announce file. A home that already answers at the registry's version or newer is
 * left alone, whichever way it was installed: the kit only knows the copy it installed itself and would replace a
 * working one. Prints one JSON object on stdout and exits 0 also for a refusal, so the caller reads `ok` and `reason`.
 *
 * Usage: node engine-home.mjs <casa-de-embeddings|motor-de-grafo>
 */
import { ensureHome, discover, CASA, GRAFO } from "engine-kit/kit/client.mjs";
import { resolve } from "engine-kit/kit/resolve.mjs";

const ENGINES = { [CASA]: true, [GRAFO]: true };

const parts = (version) => String(version ?? "").split(".").map((piece) => Number.parseInt(piece, 10) || 0);

/** True when `version` is the same as `wanted` or newer. */
const atLeast = (version, wanted) => {
  const [have, need] = [parts(version), parts(wanted)];
  for (let index = 0; index < Math.max(have.length, need.length); index += 1) {
    if ((have[index] ?? 0) !== (need[index] ?? 0)) { return (have[index] ?? 0) > (need[index] ?? 0); }
  }
  return true;
};

const show = ({ shutdownToken, engine, ...visible }) => console.log(JSON.stringify(visible));

const name = process.argv[2];
if (!ENGINES[name]) {
  console.log(JSON.stringify({ ok: false, reason: `unknown engine ${name}` }));
} else {
  try {
    const running = await discover(name);
    const registry = running.ok ? await resolve(name) : null;
    if (running.ok && registry?.ok && atLeast(running.version, registry.engine.version)) {
      show({ ...running, installed: running.version });
    } else {
      show(await ensureHome(name));
    }
  } catch (error) {
    console.log(JSON.stringify({ ok: false, reason: `${name}: ${error?.message ?? error}` }));
  }
}
