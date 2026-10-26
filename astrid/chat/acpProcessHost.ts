import type {
  JsonRpcProcessLike,
  JsonRpcWritableStreamLike,
} from './process-types.ts';
import { ASTRID_ACP_REQUEST_TIMEOUT_MS } from './astridBridgeWire.ts';

export type AcpRpcId = number | string;

export type AcpRpcNotification = {
  readonly jsonrpc: '2.0';
  readonly method: string;
  readonly params?: unknown;
};

export type AcpProcessHostCallbacks = {
  readonly requestPermission?: (params: unknown) => unknown | Promise<unknown>;
  readonly readTextFile?: (params: unknown) => unknown | Promise<unknown>;
  readonly writeTextFile?: (params: unknown) => unknown | Promise<unknown>;
  readonly createTerminal?: (params: unknown) => unknown | Promise<unknown>;
  readonly terminalOutput?: (params: unknown) => unknown | Promise<unknown>;
  readonly waitForTerminalExit?: (params: unknown) => unknown | Promise<unknown>;
  readonly killTerminal?: (params: unknown) => unknown | Promise<unknown>;
  readonly releaseTerminal?: (params: unknown) => unknown | Promise<unknown>;
  readonly onNotification?: (notification: AcpRpcNotification) => void;
  readonly onDisconnect?: (error: AcpProcessHostError) => void;
};

export type AcpProcessSpawnOptions = {
  readonly command: string;
  readonly args: readonly string[];
  readonly cwd?: string;
  readonly env?: Record<string, string | undefined>;
};

export type AcpProcessSpawner = (options: AcpProcessSpawnOptions) => JsonRpcProcessLike;

export function commandForPlatform(command: string, args: readonly string[], platform: NodeJS.Platform): { command: string; args: string[]; windowsVerbatimArguments?: boolean } {
  if (platform !== 'win32' || !/\.(?:cmd|bat)$/i.test(command)) return { command, args: [...args] };
  const quote = (value: string) => {
    if (/[\r\n"%]/.test(value)) throw new Error('Windows command paths and arguments cannot contain quotes, %, or newlines.');
    return `"${value}"`;
  };
  return {
    command: 'cmd.exe',
    args: ['/d', '/s', '/c', `"${quote(command)}${args.length ? ` ${args.map(quote).join(' ')}` : ''}"`],
    windowsVerbatimArguments: true,
  };
}

export type AcpProcessHostOptions = {
  /** Injected only by host fixtures; production uses the explicit Node spawner. */
  readonly process?: JsonRpcProcessLike;
  readonly spawnProcess?: AcpProcessSpawner;
  readonly command?: string;
  /**
   * Complete argv after the command. When supplied, this replaces the
   * generic `omp acp` argv construction below. It is used by named-agent
   * launchers such as Astrid, which own their own ACP entrypoint and prompt
   * wiring.
   */
  readonly args?: readonly string[];
  readonly cwd?: string;
  readonly profile?: string;
  readonly sessionDir?: string;
  /** Explicit named-agent prompt file; never inline or discover ambiently. */
  readonly systemPrompt?: string;
  readonly env?: Record<string, string | undefined>;
  /** Deadline for control RPCs; session/prompt waits for actual completion or disconnect. */
  readonly requestTimeoutMs?: number;
  readonly callbacks?: AcpProcessHostCallbacks;
};

export class AcpProcessHostError extends Error {
  readonly code:
    | 'agent_not_installed'
    | 'agent_sign_in_required'
    | 'agent_runtime_missing'
    | 'acp_unavailable'
    | 'acp_transport'
    | 'acp_timeout'
    | 'acp_process_exited'
    | 'acp_callback_unavailable';
  readonly next_action?: string;
  readonly method?: string;
  readonly requestId?: AcpRpcId;

  constructor(
    message: string,
    options: {
      readonly code: AcpProcessHostError['code'];
      readonly next_action?: string;
      readonly method?: string;
      readonly requestId?: AcpRpcId;
      readonly cause?: unknown;
    },
  ) {
    super(message);
    this.name = 'AcpProcessHostError';
    this.code = options.code;
    this.next_action = options.next_action;
    this.method = options.method;
    this.requestId = options.requestId;
    if (options.cause !== undefined) {
      Object.defineProperty(this, 'cause', {
        configurable: true,
        enumerable: false,
        value: options.cause,
        writable: false,
      });
    }
  }
}

type PendingRequest = {
  readonly method: string;
  readonly resolve: (value: unknown) => void;
  readonly reject: (error: unknown) => void;
  readonly timeout: ReturnType<typeof setTimeout> | undefined;
};

type IncomingResponse = {
  readonly jsonrpc: '2.0';
  readonly id: AcpRpcId;
  readonly result?: unknown;
  readonly error?: { readonly code: number; readonly message: string; readonly data?: unknown };
};

type IncomingRequest = {
  readonly jsonrpc: '2.0';
  readonly id?: AcpRpcId;
  readonly method: string;
  readonly params?: unknown;
};

type ChildProcessLike = JsonRpcProcessLike & {
  readonly kill?: (signal?: NodeJS.Signals | number) => boolean;
};

type NodeProcessRuntime = {
  readonly env?: Record<string, string | undefined>;
  readonly getBuiltinModule?: (id: string) => unknown;
};

function loadNodeChildProcess(): typeof import('node:child_process') | null {
  const runtime = (globalThis as { readonly process?: NodeProcessRuntime }).process;
  if (typeof runtime?.getBuiltinModule !== 'function') return null;
  const builtin = runtime.getBuiltinModule('node:child_process');
  return (builtin as typeof import('node:child_process') | undefined) ?? null;
}

function defaultSpawnProcess(options: AcpProcessSpawnOptions): JsonRpcProcessLike {
  const childProcess = loadNodeChildProcess();
  if (!childProcess) {
    throw new AcpProcessHostError(
      'The ACP host is only available in the user-machine Node host; browser execution is disabled.',
      { code: 'acp_unavailable' },
    );
  }

  const runtime = (globalThis as { readonly process?: NodeProcessRuntime }).process;
  const invocation = commandForPlatform(options.command, options.args, process.platform);
  const child = childProcess.spawn(invocation.command, invocation.args, {
    cwd: options.cwd,
    env: { ...(runtime?.env ?? {}), ...(options.env ?? {}) },
    shell: false,
    windowsVerbatimArguments: invocation.windowsVerbatimArguments,
    stdio: 'pipe',
  });
  return child;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function isRpcId(value: unknown): value is AcpRpcId {
  return (typeof value === 'number' && Number.isSafeInteger(value)) || typeof value === 'string';
}

function isResponse(value: unknown): value is IncomingResponse {
  const record = asRecord(value);
  return record?.jsonrpc === '2.0' && isRpcId(record.id)
    && ('result' in record || 'error' in record);
}

function isRequest(value: unknown): value is IncomingRequest {
  const record = asRecord(value);
  return record?.jsonrpc === '2.0'
    && typeof record.method === 'string'
    && (!('id' in record) || isRpcId(record.id));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function callbackName(method: string): keyof AcpProcessHostCallbacks | null {
  switch (method) {
    case 'session/request_permission': return 'requestPermission';
    case 'fs/read_text_file': return 'readTextFile';
    case 'fs/write_text_file': return 'writeTextFile';
    case 'terminal/create': return 'createTerminal';
    case 'terminal/output': return 'terminalOutput';
    case 'terminal/wait_for_exit': return 'waitForTerminalExit';
    case 'terminal/kill': return 'killTerminal';
    case 'terminal/release': return 'releaseTerminal';
    default: return null;
  }
}

/**
 * Host-only ephemeral ACP connection. OMP owns profiles, session files,
 * session identity, and lifecycle; this class owns one stdio process and no
 * session registry. Do not import or instantiate it from browser/UI code.
 */
export class AcpProcessHost {
  private readonly options: AcpProcessHostOptions;
  private readonly callbacks: AcpProcessHostCallbacks;
  private readonly pending = new Map<AcpRpcId, PendingRequest>();
  private readonly requestTimeoutMs: number;
  private process: ChildProcessLike | null;
  private ownsProcess: boolean;
  private nextRequestId = 1;
  private buffer = '';
  private disposed = false;
  private disconnected = false;
  private attached = false;

  constructor(options: AcpProcessHostOptions = {}) {
    this.options = options;
    this.callbacks = options.callbacks ?? {};
    this.requestTimeoutMs = options.requestTimeoutMs ?? ASTRID_ACP_REQUEST_TIMEOUT_MS;
    this.process = (options.process as ChildProcessLike | undefined) ?? null;
    this.ownsProcess = options.process === undefined;
    if (this.requestTimeoutMs < 1) {
      throw new RangeError('requestTimeoutMs must be positive');
    }
  }

  get childProcess(): JsonRpcProcessLike | null {
    return this.process;
  }

  async connect(): Promise<this> {
    if (this.disposed) throw this.disposedError();
    if (this.process) {
      if (!this.attached) {
        this.attachProcess(this.process);
        this.attached = true;
      }
      return this;
    }

    const command = this.options.command ?? 'omp';
    const args = this.options.args ? [...this.options.args] : [
      'acp',
      ...(this.options.profile ? ['--profile', this.options.profile] : []),
      ...(this.options.sessionDir ? ['--session-dir', this.options.sessionDir] : []),
      ...(this.options.systemPrompt ? ['--system-prompt', this.options.systemPrompt] : []),
    ];
    const spawnProcess = this.options.spawnProcess ?? defaultSpawnProcess;
    try {
      this.process = spawnProcess({
        command,
        args,
        cwd: this.options.cwd,
        env: this.options.env,
      }) as ChildProcessLike;
    } catch (cause) {
      if (cause instanceof AcpProcessHostError) throw cause;
      throw new AcpProcessHostError(
        `Unable to start ACP process "${command}".`,
        { code: 'acp_unavailable', cause },
      );
    }
    this.ownsProcess = true;
    this.attachProcess(this.process);
    this.attached = true;
    return this;
  }

  initialize<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('initialize', params);
  }

  authenticate<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('authenticate', params);
  }

  newSession<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('session/new', params);
  }

  loadSession<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('session/load', params);
  }

  listSessions<T = unknown>(params: unknown = {}): Promise<T> {
    return this.request<T>('session/list', params);
  }

  resumeSession<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('session/resume', params);
  }

  forkSession<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('session/fork', params);
  }

  closeSession<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('session/close', params);
  }

  prompt<T = unknown>(params: unknown): Promise<T> {
    return this.request<T>('session/prompt', params);
  }

  async cancel(params: unknown): Promise<void> {
    await this.notify('session/cancel', params);
  }

  async request<T = unknown>(method: string, params?: unknown): Promise<T> {
    await this.connect();
    const process = this.requireProcess();
    const id = this.nextRequestId;
    this.nextRequestId += 1;
    const response = new Promise<unknown>((resolve, reject) => {
      // A prompt response ends the entire agent turn, which can legitimately run
      // for hours. Expiring only our waiter neither stops the agent nor proves
      // failure, and would incorrectly release the shared turn reservation.
      const timeout = method === 'session/prompt' ? undefined : setTimeout(() => {
        this.pending.delete(id);
        reject(new AcpProcessHostError(
          `ACP request "${method}" timed out.`,
          { code: 'acp_timeout', method, requestId: id },
        ));
      }, this.requestTimeoutMs);
      this.pending.set(id, { method, resolve, reject, timeout });
    });
    try {
      this.write(process.stdin, { jsonrpc: '2.0', id, method, ...(params === undefined ? {} : { params }) });
    } catch (cause) {
      const pending = this.pending.get(id);
      if (pending) {
        clearTimeout(pending.timeout);
        this.pending.delete(id);
        pending.reject(new AcpProcessHostError(
          `Unable to write ACP request "${method}".`,
          { code: 'acp_transport', method, requestId: id, cause },
        ));
      }
    }
    return await response as T;
  }

  async notify(method: string, params?: unknown): Promise<void> {
    await this.connect();
    this.write(this.requireProcess().stdin, {
      jsonrpc: '2.0',
      method,
      ...(params === undefined ? {} : { params }),
    });
  }

  async dispose(signal: NodeJS.Signals = 'SIGTERM'): Promise<void> {
    if (this.disposed) return;
    this.disposed = true;
    const error = new AcpProcessHostError('ACP host disposed.', { code: 'acp_transport' });
    this.rejectPending(error);
    const process = this.process;
    this.process = null;
    if (!process) return;
    try {
      process.stdin?.end?.();
    } catch {
      // Process teardown remains best-effort after pending requests are closed.
    }
    if (this.ownsProcess) process.kill?.(signal);
  }

  private requireProcess(): ChildProcessLike {
    if (!this.process || this.disconnected) {
      throw new AcpProcessHostError('ACP process is not connected.', { code: 'acp_process_exited' });
    }
    return this.process;
  }

  private disposedError(): AcpProcessHostError {
    return new AcpProcessHostError('ACP host is disposed.', { code: 'acp_transport' });
  }

  private attachProcess(process: ChildProcessLike): void {
    const stdout = process.stdout;
    if (!stdout || !process.stdin) {
      this.handleDisconnect(new AcpProcessHostError(
        'ACP process stdio streams are unavailable.',
        { code: 'acp_transport' },
      ));
      return;
    }
    stdout.on('data', (chunk) => this.onData(chunk));
    stdout.on('end', () => this.handleDisconnect(this.processExitedError()));
    stdout.on('close', () => this.handleDisconnect(this.processExitedError()));
    process.on('exit', (code, signal) => {
      this.handleDisconnect(new AcpProcessHostError(
        signal ? `ACP process exited with ${signal}.` : `ACP process exited with code ${code ?? 'unknown'}.`,
        { code: 'acp_process_exited' },
      ));
    });
    process.on('error', (cause) => {
      this.handleDisconnect(new AcpProcessHostError(
        `ACP process failed: ${errorMessage(cause)}`,
        { code: 'acp_process_exited', cause },
      ));
    });
  }

  private processExitedError(): AcpProcessHostError {
    return new AcpProcessHostError('ACP process stdio disconnected.', { code: 'acp_process_exited' });
  }

  private onData(chunk: string | Uint8Array): void {
    if (this.disposed || this.disconnected) return;
    this.buffer += typeof chunk === 'string'
      ? chunk
      : new TextDecoder().decode(chunk, { stream: true });
    let newlineIndex = this.buffer.indexOf('\n');
    while (newlineIndex >= 0) {
      const line = this.buffer.slice(0, newlineIndex).replace(/\r$/, '');
      this.buffer = this.buffer.slice(newlineIndex + 1);
      if (line.trim()) this.onMessage(line);
      newlineIndex = this.buffer.indexOf('\n');
    }
  }

  private onMessage(line: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch (cause) {
      this.handleDisconnect(new AcpProcessHostError(
        'ACP returned malformed JSON-RPC.',
        { code: 'acp_transport', cause },
      ));
      return;
    }
    if (isResponse(parsed)) {
      this.resolveResponse(parsed);
      return;
    }
    if (!isRequest(parsed)) {
      this.handleDisconnect(new AcpProcessHostError(
        'ACP returned an invalid JSON-RPC message.',
        { code: 'acp_transport' },
      ));
      return;
    }
    if (parsed.id === undefined) {
      this.callbacks.onNotification?.({
        jsonrpc: '2.0',
        method: parsed.method,
        ...(parsed.params === undefined ? {} : { params: parsed.params }),
      });
      return;
    }
    void this.handleRequest(parsed);
  }

  private resolveResponse(response: IncomingResponse): void {
    const pending = this.pending.get(response.id);
    if (!pending) return;
    clearTimeout(pending.timeout);
    this.pending.delete(response.id);
    if (response.error) {
      const details = asRecord(response.error.data)?.details;
      const reason = typeof details === 'string' ? details : response.error.message;
      const recovery = (pending.method === 'session/load' || pending.method === 'session/resume')
        && reason.startsWith('Could not restore model ')
        ? ' This chat\'s saved model is unavailable. Start a new chat.' : '';
      pending.reject(new AcpProcessHostError(
        `ACP request "${pending.method}" failed: ${reason}${recovery}`,
        { code: 'acp_transport', method: pending.method, requestId: response.id },
      ));
      return;
    }
    pending.resolve(response.result);
  }

  private async handleRequest(request: IncomingRequest): Promise<void> {
    const process = this.process;
    if (!process) return;
    const callback = callbackName(request.method);
    const handler = callback
      ? this.callbacks[callback] as ((params: unknown) => unknown | Promise<unknown>) | undefined
      : undefined;
    if (!handler) {
      this.write(process.stdin, {
        jsonrpc: '2.0',
        id: request.id,
        error: {
          code: -32001,
          message: `ACP callback "${request.method}" is unavailable; refusing the request.`,
        },
      });
      return;
    }
    try {
      const result = await handler(request.params);
      this.write(process.stdin, { jsonrpc: '2.0', id: request.id, result });
    } catch {
      this.write(process.stdin, {
        jsonrpc: '2.0',
        id: request.id,
        error: { code: -32000, message: `ACP callback "${request.method}" failed.` },
      });
    }
  }

  private write(stream: JsonRpcWritableStreamLike | null, message: Record<string, unknown>): void {
    if (!stream) {
      throw new AcpProcessHostError('ACP stdin is unavailable.', { code: 'acp_transport' });
    }
    stream.write(`${JSON.stringify(message)}\n`);
  }

  private rejectPending(error: AcpProcessHostError): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timeout);
      pending.reject(error);
      this.pending.delete(id);
    }
  }

  private handleDisconnect(error: AcpProcessHostError): void {
    if (this.disconnected) return;
    this.disconnected = true;
    this.rejectPending(error);
    if (!this.disposed) this.callbacks.onDisconnect?.(error);
  }
}
