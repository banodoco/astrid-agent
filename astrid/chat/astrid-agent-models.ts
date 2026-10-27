import { discoveryProbe as execFileAsync } from './discoveryProbe.ts';
import { modelCatalogSchema, type AcpModelCatalog } from './modelCatalog.ts';
import { ASTRID_ACP_COMMAND } from './astridAcpLauncher.ts';

type UsageLimit = NonNullable<AcpModelCatalog['providers'][number]['usage']>['limits'][number];

/** The documented `astrid agent models --json` v1 response. */
type AstridModelSnapshot = {
  schemaVersion: 1;
  currentModel: string | null;
  providers: Array<{
    id: string;
    name: string;
    signedIn: boolean;
    usable: boolean;
    reason?: 'needs_sign_in' | 'no_available_models' | null;
    login?: 'oauth' | 'api-key' | 'local';
    recommended?: boolean;
    models: Array<{ id: string; name: string; selector: string }>;
    usage?: Array<{ limits: Array<{
      amount: Omit<UsageLimit, 'durationMs' | 'resetsAt' | 'status'>;
      window?: Pick<UsageLimit, 'durationMs' | 'resetsAt'>;
      status?: UsageLimit['status'];
    }> }>;
  }>;
};

type AstridModelOptions = { command?: string; cwd: string; profile?: string; timeoutMs?: number };

const MODEL_CATALOG_TIMEOUT_MS = 30_000;

function safeProbeFailureSummary(error: unknown): string {
  if (!error || typeof error !== 'object') return '';
  const read = (key: string) => Reflect.get(error, key);
  const code = read('code');
  const status = read('status');
  const signal = read('signal');
  const flags = [
    typeof status === 'number' ? `exit=${status}` : undefined,
    typeof signal === 'string' ? `signal=${signal}` : undefined,
    typeof code === 'string' && code !== signal ? `error=${code}` : undefined,
    read('timedOut') === true ? 'timeout=true' : undefined,
    read('outputLimit') === true ? 'output_limit=true' : undefined,
  ].filter(Boolean);
  return flags.join(', ');
}

async function runAstrid(options: AstridModelOptions, args: string[]): Promise<string> {
  try {
    const result = await execFileAsync(options.command ?? ASTRID_ACP_COMMAND, ['agent', ...args, '--json'], {
      cwd: options.cwd,
      env: { ...process.env, ...(options.profile ? { OMP_PROFILE: options.profile } : {}) },
      encoding: 'utf8',
      // The Python wrapper starts OMP sequentially for model metadata and login
      // providers, then may spend up to three seconds on optional usage data.
      // Keep the HTTP request bounded while allowing the installed OMP startup
      // path to complete on slower Windows service hosts.
      timeout: options.timeoutMs ?? MODEL_CATALOG_TIMEOUT_MS,
      killSignal: 'SIGKILL',
      maxBuffer: 8 * 1024 * 1024,
    });
    return result.stdout;
  } catch (error) {
    // Child diagnostics can contain private context. Astrid owns discovery,
    // authentication and persistence; logs only process state, never output.
    const summary = safeProbeFailureSummary(error);
    console.warn(`Astrid model command failed${summary ? ` (${summary})` : ''}.`);
    throw new Error('Astrid model operation failed. Try again.');
  }
}

function modelCatalog(output: string): AcpModelCatalog {
  try {
    const snapshot = JSON.parse(output) as AstridModelSnapshot;
    if (snapshot.schemaVersion !== 1) throw new Error('Unsupported schema');
    return modelCatalogSchema.parse({
      currentModel: snapshot.currentModel,
      providers: snapshot.providers.map(provider => ({
        id: provider.id,
        name: provider.name,
        signedIn: provider.signedIn,
        usable: provider.usable,
        ...(provider.reason !== undefined ? { reason: provider.reason } : {}),
        ...(provider.login !== undefined ? { login: provider.login } : {}),
        ...(provider.recommended !== undefined ? { recommended: provider.recommended } : {}),
        models: provider.models.map(({ id, name }) => ({ id, name })),
        ...(provider.usage ? { usage: { limits: provider.usage.flatMap(report => report.limits.map(limit => {
          const amount = limit.amount;
          // Adapt the public CLI contract to the existing App usage shape.
          // Only fields already accepted by the browser contract cross over.
          return Object.fromEntries(Object.entries({
            unit: amount.unit, used: amount.used, limit: amount.limit, remaining: amount.remaining,
            usedFraction: amount.usedFraction, remainingFraction: amount.remainingFraction,
            durationMs: limit.window?.durationMs, resetsAt: limit.window?.resetsAt, status: limit.status,
          }).filter(([, value]) => value !== undefined));
        })) } } : {}),
      })),
    });
  } catch {
    throw new Error('Astrid returned an invalid model catalog; expected JSON v1.');
  }
}

export async function getAstridAgentModelCatalog(options: AstridModelOptions): Promise<AcpModelCatalog> {
  return modelCatalog(await runAstrid(options, ['models']));
}
