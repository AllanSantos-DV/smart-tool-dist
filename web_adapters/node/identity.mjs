// Identidades de navegador (User-Agent + cabeçalhos coerentes) e a repetição da requisição bloqueada com outra
// identidade. O pacote vive em ../identities.json, o mesmo que web_identity.py lê.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const IDENTITIES_PATH = fileURLToPath(new URL('../identities.json', import.meta.url));
const MAX_ATTEMPTS = 3;
const BLOCK_STATUSES = new Set([202, 403, 429]);
const LOG_MARKER = '[smart-tool] web-identity ';

/** The identity pool from identities.json; a missing or malformed file throws, never a single fixed identity. */
export function loadIdentities() {
  const { identities } = JSON.parse(readFileSync(IDENTITIES_PATH, 'utf-8'));
  if (!identities?.length || identities.some((item) => !item.name || !item.user_agent)) {
    throw new Error(`${IDENTITIES_PATH} must list identities with a name and a user_agent`);
  }
  return identities;
}

/** Up to MAX_ATTEMPTS distinct identities in random order. */
export function attemptOrder() {
  const pool = loadIdentities();
  for (let i = pool.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  return pool.slice(0, MAX_ATTEMPTS);
}

/** Headers of one identity (User-Agent plus the family's own headers); `extra` overrides or adds. */
export function requestHeaders(identity, extra = {}) {
  return { 'User-Agent': identity.user_agent, ...(identity.headers ?? {}), ...extra };
}

/** Writes one identity log line to stderr, the line the daemon forwards to web-identity.log. */
export function logLine(provider, identity, reason, outcome) {
  process.stderr.write(`${LOG_MARKER}provider=${provider} identity=${identity} reason=${JSON.stringify(reason)} outcome=${outcome}\n`);
}

/**
 * Runs `attempt(identity)` with up to MAX_ATTEMPTS identities until one is not blocked.
 *
 * `attempt(identity, remainingMs)` returns `{ blocked: reason }` when the site refused that identity (a block status or a challenge page) and
 * any other object when the site answered; that object is returned. A thrown error is not a block and propagates.
 * One log line is written per block and one when a later identity recovers; when all are blocked or `budgetMs` is used
 * up it throws, so the whole call ends before the daemon's own timeout kills it.
 */
export async function withIdentities(provider, attempt, { delayMs = Number(process.env.SMART_TOOL_IDENTITY_DELAY_MS ?? 400), budgetMs = 12000 } = {}) {
  const tried = [];
  const started = Date.now();
  let lastReason = '';
  for (const identity of attemptOrder()) {
    if (tried.length > 0 && delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
    const remainingMs = budgetMs - (Date.now() - started);
    if (tried.length > 0 && remainingMs < 1500) {
      lastReason = `${lastReason}; time budget used up`;
      break;
    }
    const outcome = await attempt(identity, remainingMs);
    if (!outcome.blocked) {
      if (tried.length > 0) logLine(provider, identity.name, `after ${tried.join(', ')} blocked`, 'recovered');
      return outcome;
    }
    tried.push(identity.name);
    lastReason = outcome.blocked;
    logLine(provider, identity.name, lastReason, 'blocked');
  }
  logLine(provider, tried[tried.length - 1], `all ${tried.length} identities blocked`, 'exhausted');
  throw new Error(`${provider} blocked all ${tried.length} browser identities tried (${tried.join(', ')}); last reason: ${lastReason}`);
}

export { BLOCK_STATUSES };
