// Network failures of the bootstrap on machines whose firewall inspects TLS (a corporate root CA in the Windows store,
// unknown to Node's bundled certificates). Nothing here overwrites a TLS setting the user already made.

export class Failure extends Error {}

const CERTIFICATE_CODES = new Set([
  "SELF_SIGNED_CERT_IN_CHAIN",
  "DEPTH_ZERO_SELF_SIGNED_CERT",
  "UNABLE_TO_GET_ISSUER_CERT",
  "UNABLE_TO_GET_ISSUER_CERT_LOCALLY",
  "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
  "CERT_UNTRUSTED",
  "CERT_HAS_EXPIRED",
]);
const CERTIFICATE_TEXT = /certificate|UnknownIssuer|invalid peer|self[- ]signed/i;

/** The real reason of a failed fetch: Node reports "fetch failed" and keeps the code in `error.cause`. */
export function errorCode(error) {
  return error?.cause?.code ?? error?.code ?? "unknown";
}

export function isCertificateError(error) {
  return CERTIFICATE_CODES.has(errorCode(error));
}

/** Whether this Node can read the system certificate store through NODE_USE_SYSTEM_CA (22.15+, 23.8+). */
export function supportsSystemCa(version = process.versions.node) {
  const [major, minor] = version.split(".").map(Number);
  return major >= 24 || (major === 23 && minor >= 8) || (major === 22 && minor >= 15);
}

/** The environment of a second run that trusts the system certificate store, or null when the user already chose
 * (NODE_USE_SYSTEM_CA set to anything), this is already that second run, or this Node cannot do it. */
export function systemCaEnvironment(env = process.env, version = process.versions.node) {
  if (env.NODE_USE_SYSTEM_CA !== undefined || env.SMART_TOOL_SYSTEM_CA_RETRY || !supportsSystemCa(version)) return null;
  return { ...env, NODE_USE_SYSTEM_CA: "1", SMART_TOOL_SYSTEM_CA_RETRY: "1" };
}

/** The environment for retrying uv with the Windows certificate store, or null when UV_NATIVE_TLS is already set. */
export function nativeTlsEnvironment(env = process.env) {
  return env.UV_NATIVE_TLS !== undefined ? null : { ...env, UV_NATIVE_TLS: "1" };
}

export function isCertificateText(text) {
  return CERTIFICATE_TEXT.test(text);
}

export function certificateAdvice(env = process.env) {
  const configured = ["NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "UV_NATIVE_TLS"].filter((name) => env[name]);
  return "The network seems to inspect TLS with a certificate Node does not trust. Point NODE_EXTRA_CA_CERTS at the " +
    "corporate root certificate (a .pem file), or use Node 22.15+ with NODE_USE_SYSTEM_CA=1 to trust the Windows " +
    "certificate store." + (configured.length ? ` Already set here and left untouched: ${configured.join(", ")}.` : "");
}

export function describeFetchError(url, error, env = process.env) {
  const code = errorCode(error);
  const reason = error?.cause?.message ? `${error.message}: ${error.cause.message}` : String(error?.message ?? error);
  return `download of ${url} failed (${code}): ${reason}` + (isCertificateError(error) ? `\n${certificateAdvice(env)}` : "");
}

/** Downloads url as a Buffer. A certificate failure first runs `rerun(environment)` once, a second run of the whole
 * bootstrap that trusts the system store, and returns { rerun: result } when it did; otherwise it throws Failure with
 * the error code and what to configure. */
export async function download(url, { fetchFn = fetch, env = process.env, version = process.versions.node, rerun } = {}) {
  let response;
  try {
    response = await fetchFn(url);
  } catch (error) {
    const retry = isCertificateError(error) && rerun ? systemCaEnvironment(env, version) : null;
    if (retry) {
      console.log(`TLS inspection detected (${errorCode(error)}); running again with the Windows certificate store...`);
      return { rerun: rerun(retry) };
    }
    throw new Failure(describeFetchError(url, error, env));
  }
  if (!response.ok) throw new Failure(`download of ${url} failed: HTTP ${response.status}`);
  return { body: Buffer.from(await response.arrayBuffer()) };
}
