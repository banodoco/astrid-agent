import { readFileSync } from 'node:fs';

function readJson(path: string, label: string) {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch (error) {
    throw new Error(`cannot read ${label}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function readToken(path: string): string {
  const raw = readFileSync(path, 'utf8').trim();
  if (!raw) throw new Error('runtime credential is empty');
  if (!raw.startsWith('{')) return raw;
  const parsed = JSON.parse(raw);
  if (typeof parsed?.token !== 'string' || !parsed.token.trim()) {
    throw new Error('runtime credential has no token');
  }
  return parsed.token.trim();
}

/**
 * The Runtime to forward to right now: its loopback endpoint from the discovery file and its bearer token, read afresh on every
 * call so a restart on a new port or a rotated token is followed. Throws on a missing, malformed or non-loopback file.
 * `credentialFile` names a different token file than the discovery's own (the loopback gateway holds a product-scoped one).
 */
export function currentRuntime(discoveryPath: string, { credentialFile: credentialFileOverride }: { credentialFile?: string } = {}) {
  if (!discoveryPath) throw new Error('ASTRID_WORKSPACE_DISCOVERY is not configured');
  const discovery = readJson(discoveryPath, 'runtime discovery');
  const endpoint = new URL(String(discovery.endpoint ?? ''));
  if (endpoint.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(endpoint.hostname) || !endpoint.port) {
    throw new Error('runtime discovery endpoint must be an explicit HTTP loopback URL');
  }
  const credentialFile = String(credentialFileOverride ?? discovery.credential_file ?? '').trim();
  if (!credentialFile) throw new Error('runtime discovery has no credential file');
  return { endpoint, token: readToken(credentialFile) };
}

