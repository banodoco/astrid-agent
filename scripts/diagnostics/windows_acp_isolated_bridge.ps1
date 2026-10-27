param()

# Launch the selected chat bundle in an isolated temporary Runtime Job, then
# exercise only /models and initialize-only /connect with a disposable token.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

function Quote-WindowsArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $quoted = '"'; $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') { $quoted += ('\' * (2 * $slashes + 1)) + '"'; $slashes = 0; continue }
        $quoted += ('\' * $slashes) + $character; $slashes = 0
    }
    return $quoted + ('\' * (2 * $slashes)) + '"'
}

function Invoke-LocalRequest([System.Net.Http.HttpClient]$Client, [string]$Base,
                             [string]$Path, [string]$Method, [string]$Token,
                             [string]$Body = $null) {
    $uri = [System.Uri]::new("$Base$Path")
    if ($uri.Scheme -ne 'http' -or
        ($uri.Host -ne 'localhost' -and -not [System.Net.IPAddress]::IsLoopback($uri.Host -as [System.Net.IPAddress]))) {
        throw 'Diagnostic endpoint is not loopback HTTP.'
    }
    $request = [System.Net.Http.HttpRequestMessage]::new([System.Net.Http.HttpMethod]::new($Method), $uri)
    $request.Headers.Add('Origin', 'https://astrid.haus')
    $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Token)
    $request.Headers.Add('X-Astrid-Bridge-Version', 'v1')
    if ($Method -eq 'POST' -and $PSBoundParameters.ContainsKey('Body') -and $null -ne $Body) {
        $request.Content = [System.Net.Http.StringContent]::new($Body, [System.Text.Encoding]::UTF8, 'application/json')
    }
    try {
        $response = $Client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            $parsed = $null
            if ($content) { try { $parsed = $content | ConvertFrom-Json } catch { } }
            return [pscustomobject]@{ status = [int]$response.StatusCode; body = $parsed }
        } finally { $response.Dispose() }
    } finally { $request.Dispose() }
}

function Get-SafeExceptionSummary([System.Exception]$Exception) {
    $chain = @()
    $current = $Exception
    while ($null -ne $current -and $chain.Count -lt 6) {
        $chain += [pscustomobject]@{ class = $current.GetType().Name; hresult = ('0x{0:X8}' -f $current.HResult) }
        $current = $current.InnerException
    }
    $root = $Exception
    while ($null -ne $root.InnerException) { $root = $root.InnerException }
    $classification = 'other'
    if ($root -is [System.Threading.Tasks.TaskCanceledException] -or
        $root -is [System.TimeoutException]) { $classification = 'timeout' }
    elseif ($root -is [System.Net.Sockets.SocketException] -and
        $root.SocketErrorCode -eq [System.Net.Sockets.SocketError]::ConnectionRefused) { $classification = 'connect-refused' }
    elseif ($root -is [System.Net.ProtocolViolationException]) { $classification = 'http-protocol-violation' }
    elseif ($root -is [System.FormatException]) { $classification = 'format-error' }
    elseif ($root -is [System.InvalidOperationException]) { $classification = 'invalid-operation' }
    return [pscustomobject]@{ class = $Exception.GetType().Name; classification = $classification; chain = $chain }
}

function Invoke-SafeProbe([string]$Name, [scriptblock]$Request, [System.Collections.IDictionary]$Result) {
    $script:phase = $Name
    try { return (& $Request) }
    catch {
        $summary = Get-SafeExceptionSummary $_.Exception
        $classification = $summary.classification
        $classes = @($summary.chain | ForEach-Object { $_.class })
        if ($Name -in @('health', 'models') -and $classes -contains 'ProtocolViolationException') {
            $classification = 'get-content-unsupported'
        } elseif ($classes -contains 'FormatException') { $classification = 'header-format' }
        $Result['request_errors'] += [pscustomobject]@{ phase = $Name; class = $summary.class; classification = $classification; chain = $summary.chain }
        return $null
    }
}

function Safe-ErrorCode([object]$Value) {
    if ($Value -is [string] -and $Value -match '^[A-Za-z0-9_.-]{1,64}$') { return $Value }
    return 'redacted'
}

$result = [ordered]@{
    diagnostic_revision = 'isolated-bridge-4'
    phase = 'startup'
    owner_ready = $false
    registration_found = $false
    endpoint_loopback = $null
    endpoint_port = $null
    request_errors = @()
    exception = $null
    selected_version = $null
    bun_version = $null
    command_kind = $null
    health_http = $null
    models_http = $null
    provider_count = $null
    model_count = $null
    signed_in_count = $null
    connect_http = $null
    connect_error_code = $null
    initialize_succeeded = $false
    connection_deleted = $null
    child_calls = @()
    job_drained = $false
    error = $null
}
$tempDirectory = $null
$owner = $null
$client = $null
$connectionId = $null
$registration = $null
$script:phase = 'startup'

try {
    $task = Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop
    if (-not $task.Actions -or $task.Actions.Count -eq 0 -or
        $task.Actions[0].Execute -notmatch '^(?<root>.+)\\versions\\(?<version>[^\\]+)\\py\\Scripts\\pythonw?\.exe$') {
        throw 'Could not identify the selected Runtime version from its Scheduled Task.'
    }
    $programRoot = $Matches.root
    $versionRoot = Join-Path (Join-Path $programRoot 'versions') $Matches.version
    $result.selected_version = $Matches.version
    $profile = Get-Content -LiteralPath (Join-Path $versionRoot 'profile.json') -Raw | ConvertFrom-Json
    $version = Get-Content -LiteralPath (Join-Path $versionRoot 'version.json') -Raw | ConvertFrom-Json
    $sourceCheckout = [string]$profile.source_checkout
    $python = [string]$version.python
    $declaration = Get-Content -LiteralPath ([string]$profile.service_declaration) -Raw | ConvertFrom-Json
    $chat = @($declaration.services | Where-Object { $_.name -eq 'chat' }) | Select-Object -First 1
    if (-not $chat -or -not $chat.argv -or -not $chat.env) { throw 'Selected chat service declaration is incomplete.' }
    $bun = [string]$chat.argv[0]
    $bundle = [string]$chat.argv[1]
    $command = [string]$chat.env.ASTRID_ACP_COMMAND
    if ([System.IO.Path]::GetExtension($command) -notmatch '(?i)^\.exe$') {
        throw 'The selected ACP command is not an absolute executable; this probe requires the configured executable path.'
    }
    if (-not (Test-Path -LiteralPath $bun -PathType Leaf) -or
        -not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $bundle -PathType Leaf) -or
        -not (Test-Path -LiteralPath $command -PathType Leaf)) { throw 'A selected Runtime executable is missing.' }
    $result.command_kind = 'absolute-exe'
    $result.command_matches_selected_version = [System.IO.Path]::GetFullPath($command).StartsWith(
        [System.IO.Path]::GetFullPath((Join-Path $versionRoot 'py\Scripts')).TrimEnd('\') + '\',
        [System.StringComparison]::OrdinalIgnoreCase)

    # Reconstruct the Windows Runtime and service environment. Windows Python
    # canonicalizes os.environ names to uppercase; use that same key spelling.
    $environment = @{}
    foreach ($entry in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $environment[([string]$entry.Key).ToUpperInvariant()] = [string]$entry.Value
    }
    foreach ($property in $version.env.PSObject.Properties) {
        $environment[$property.Name.ToUpperInvariant()] = [string]$property.Value
    }
    $settingsPath = Join-Path $programRoot 'service-env.json'
    if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
        $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
        foreach ($property in $settings.PSObject.Properties) {
            if ($property.Name -notin @('ASTRID_WARM_IDLE_SECONDS', 'ASTRID_ALLOW_UNQUALIFIED_GPU')) {
                throw 'The selected service environment has an unsupported setting.'
            }
            $environment[$property.Name.ToUpperInvariant()] = [string]$property.Value
        }
    }
    foreach ($key in @($environment.Keys)) {
        if ($key -in @('PYTHONPATH', 'PYTHONHOME', 'ASTRID_RUNTIME_JOB_HANDLE',
                       'ASTRID_RUNTIME_SERVICE_NAME', 'ASTRID_RUNTIME_SERVICE_LAUNCH_ID') -or
            $key.StartsWith('ASTRID_RUNTIME_CUSTODY_', [System.StringComparison]::OrdinalIgnoreCase)) {
            $environment.Remove($key)
        }
    }
    $scripts = Join-Path $versionRoot 'py\Scripts'
    $environment['PATH'] = $scripts + [System.IO.Path]::PathSeparator + [string]$environment['PATH']
    $environment['VIRTUAL_ENV'] = Join-Path $versionRoot 'py'
    foreach ($property in $chat.env.PSObject.Properties) {
        $environment[$property.Name.ToUpperInvariant()] = [string]$property.Value
    }
    $environment['ASTRID_RUNTIME_SERVICE_NAME'] = 'chat'
    $environment['ASTRID_RUNTIME_SERVICE_LAUNCH_ID'] = [guid]::NewGuid().ToString('N')
    if (-not $environment['HOME']) { $environment['HOME'] = $environment['USERPROFILE'] }

    $tempDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ('astrid-acp-isolated-' + [guid]::NewGuid().ToString('N'))
    $runtimeTemp = Join-Path $tempDirectory 'runtime'
    [System.IO.Directory]::CreateDirectory($runtimeTemp) | Out-Null
    $runnerPath = Join-Path $tempDirectory 'chat-probe.mjs'
    $ownerPath = Join-Path $tempDirectory 'job-owner.py'
    $reportPath = Join-Path $tempDirectory 'child-calls.json'
    $discoveryPath = Join-Path $runtimeTemp 'discovery.json'
    $registrationPath = Join-Path $runtimeTemp 'chat.json'

    $runner = @'
import { renameSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
const originalGetBuiltinModule = process.getBuiltinModule?.bind(process);
const cp = originalGetBuiltinModule?.('node:child_process');
if (!cp) throw new Error('child_process instrumentation unavailable');
const calls = [];
const reportPath = process.env.ASTRID_ACP_PROBE_REPORT;
let fatal = null;
const persist = () => {
  try {
    const temporary = reportPath + '.tmp';
    writeFileSync(temporary, JSON.stringify({ bun_version: Bun.version, fatal, calls }));
    renameSync(temporary, reportPath);
  } catch { }
};
const safe = value => typeof value === 'number' ? value :
  (typeof value === 'string' && /^[A-Za-z0-9_.+-]{1,40}$/.test(value) ? value : (value == null ? null : 'redacted'));
const classify = (command, argv) => {
  if (argv.includes('--agent-profile-file')) return 'profile_file';
  if (argv.includes('--agent-context')) return 'agent_context';
  if (argv.includes('agent') && argv.includes('models')) return 'model_catalog';
  if (argv.some(value => typeof value === 'string' && (value === '--mode=acp' || value === 'acp'))) return 'acp_initialize';
  return command === process.env.ASTRID_ACP_COMMAND ? 'astrid_cli_other' : 'agent_discovery';
};
const stdioSummary = value => {
  if (typeof value === 'string') return { stdin: value, stdout: value, stderr: value };
  const items = Array.isArray(value) ? value : [];
  return { stdin: String(items[0] ?? 'default'), stdout: String(items[1] ?? 'default'), stderr: String(items[2] ?? 'default') };
};
const stderrSummary = value => {
  const text = Buffer.isBuffer(value) ? value.toString('utf8') : (typeof value === 'string' ? value : '');
  const type = text.match(/\b(ModuleNotFoundError|ImportError|PermissionError|FileNotFoundError|NotADirectoryError|OSError|RuntimeError|ValueError)\b/);
  const win = text.match(/\[WinError\s+(\d+)\]/i);
  return { stderr_present: text.length > 0, stderr_exception_class: type?.[1] ?? null, stderr_winerror: win ? Number(win[1]) : null };
};
const argvFrom = (args, index) => Array.isArray(args[index]) ? args[index].map(String) : [];
const optionsFrom = (args, index) => (args[index] && !Array.isArray(args[index]) ? args[index] : args[index + 1]) ?? {};
const patchSync = original => function(...args) {
  const command = String(args[0] ?? ''), argv = argvFrom(args, 1), options = optionsFrom(args, 2), start = Date.now();
  let result;
  try { result = original.apply(this, args); }
  catch (error) {
    calls.push({ operation: 'spawnSync', stage: classify(command, argv), command_kind: /\.exe$/i.test(command) ? 'exe' : (/\.(cmd|bat)$/i.test(command) ? 'cmd' : 'other'),
      option_keys: Object.keys(options).sort(), stdio: stdioSummary(options.stdio), elapsed_ms: Date.now() - start,
      status: null, signal: null, error_code: safe(error?.code), error_errno: safe(error?.errno),
      error_syscall: safe(error?.syscall), timed_out: false, ...stderrSummary('') });
    persist(); throw error;
  }
  const row = { operation: 'spawnSync', stage: classify(command, argv), command_kind: /\.exe$/i.test(command) ? 'exe' : (/\.(cmd|bat)$/i.test(command) ? 'cmd' : 'other'),
    option_keys: Object.keys(options).sort(), stdio: stdioSummary(options.stdio), elapsed_ms: Date.now() - start,
    status: Number.isInteger(result.status) ? result.status : null, signal: safe(result.signal),
    error_code: safe(result.error?.code), error_errno: safe(result.error?.errno), error_syscall: safe(result.error?.syscall),
    timed_out: result.error?.code === 'ETIMEDOUT' || result.error?.killed === true, ...stderrSummary(result.stderr) };
  calls.push(row); persist();
  return result;
};
const patchSpawn = original => function(...args) {
  const command = String(args[0] ?? ''), argv = argvFrom(args, 1), options = optionsFrom(args, 2), start = Date.now();
  const row = { operation: 'spawn', stage: classify(command, argv), command_kind: /\.exe$/i.test(command) ? 'exe' : (/\.(cmd|bat)$/i.test(command) ? 'cmd' : 'other'),
    option_keys: Object.keys(options).sort(), stdio: stdioSummary(options.stdio), elapsed_ms: 0,
    status: null, signal: null, error_code: null, error_errno: null, error_syscall: null,
    timed_out: false, stderr_present: false, stderr_exception_class: null, stderr_winerror: null, completed: false };
  calls.push(row); persist();
  let tail = '';
  let finished = false;
  const finish = (code, signal, error) => {
    if (finished) return; finished = true;
    Object.assign(row, { elapsed_ms: Date.now() - start, status: Number.isInteger(code) ? code : null,
      signal: safe(signal), error_code: safe(error?.code), error_errno: safe(error?.errno),
      error_syscall: safe(error?.syscall), completed: true, ...stderrSummary(tail) });
    persist();
  };
  let child;
  try { child = original.apply(this, args); }
  catch (error) { finish(null, null, error); throw error; }
  child.stderr?.on('data', chunk => { if (tail.length < 8192) tail += Buffer.from(chunk).toString('utf8').slice(0, 8192 - tail.length); });
  child.once('error', error => finish(null, null, error));
  child.once('close', (code, signal) => finish(code, signal, null));
  return child;
};
const originals = { spawnSync: cp.spawnSync, spawn: cp.spawn };
cp.spawnSync = patchSync(originals.spawnSync);
cp.spawn = patchSpawn(originals.spawn);
try { Object.defineProperty(process, 'getBuiltinModule', { configurable: true, value: id => id === 'node:child_process' ? cp : originalGetBuiltinModule(id) }); } catch {}
try { originalGetBuiltinModule('node:module').syncBuiltinESMExports?.(); } catch {}
try {
  const bundlePath = process.env.ASTRID_ACP_PROBE_BUNDLE;
  const serviceEnv = { ...process.env };
  delete serviceEnv.ASTRID_ACP_PROBE_BUNDLE;
  delete serviceEnv.ASTRID_ACP_PROBE_REPORT;
  delete process.env.ASTRID_ACP_PROBE_BUNDLE;
  delete process.env.ASTRID_ACP_PROBE_REPORT;
  delete process.env.ASTRID_ACP_BRIDGE_TOKEN;
  const { runChatService } = await import(pathToFileURL(bundlePath).href);
  await runChatService(serviceEnv, undefined, process.stdin);
} catch (error) {
  fatal = { class: safe(error?.name ?? 'Error'), code: safe(error?.code) };
  persist();
} finally {
  const deadline = Date.now() + 2000;
  while (calls.some(row => row.operation === 'spawn' && !row.completed) && Date.now() < deadline)
    await new Promise(resolve => setTimeout(resolve, 25));
  try {
    const temporary = reportPath + '.tmp';
    writeFileSync(temporary, JSON.stringify({ bun_version: Bun.version, fatal, calls }));
    renameSync(temporary, reportPath);
  } catch { }
}
'@
    [System.IO.File]::WriteAllText($runnerPath, $runner, [System.Text.UTF8Encoding]::new($false))

    $ownerCode = @'
import json, os, subprocess, sys, threading, time, secrets
from runtime_protocol import platform
prefix = "ASTRID_DIAG:"
def emit(value): print(prefix + json.dumps(value, separators=(",", ":")), flush=True)
child = None
drain_error = False
try:
    bun, runner, temp_root, source = sys.argv[1:]
    os.environ["ASTRID_RUNTIME_DATA_ROOT"] = temp_root
    os.environ["ASTRID_SUPPORT_ROOT"] = os.path.join(temp_root, "runtime")
    os.environ["ASTRID_WORKSPACE_DISCOVERY"] = os.path.join(temp_root, "runtime", "discovery.json")
    os.environ["ASTRID_ACP_BRIDGE_PORT"] = "0"
    os.environ["ASTRID_ACP_LOCAL_MEDIA_ONLY"] = "1"
    os.environ["ASTRID_ACP_BRIDGE_TOKEN"] = secrets.token_urlsafe(32)
    os.environ.pop("ASTRID_BRIDGE_TOKEN", None)
    os.environ["ASTRID_ACP_PROBE_REPORT"] = os.path.join(temp_root, "child-calls.json")
    child = platform.spawn_tree([bun, runner], hidden=True, cwd=source, env=dict(os.environ),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
    def discard_output():
        try:
            while child.stdout.read(65536): pass
        except OSError: pass
    reader = threading.Thread(target=discard_output, daemon=True)
    reader.start()
    emit({"started": True})
    sys.stdin.readline()
    try: child.stdin.close()
    except OSError: pass
    try: child.wait(timeout=6.0)
    except subprocess.TimeoutExpired: pass
    job = getattr(child, "_runtime_job", None)
    drained = False
    try:
        try: platform.signal_tree(child, platform.KILL)
        except Exception: pass
        deadline = time.monotonic() + 10.0
        while (job is not None and getattr(job, "handle", None)
               and hasattr(job, "active_processes") and time.monotonic() < deadline):
            try:
                if job.active_processes() == 0:
                    drained = True
                    break
            except Exception: break
            time.sleep(0.05)
    finally:
        if job is not None and getattr(job, "handle", None):
            if drained and hasattr(platform, "tree_drained"):
                try: drained = bool(platform.tree_drained(child))
                except Exception: drained = False
            if getattr(job, "handle", None):
                try: job.close()
                except Exception: drained = False
    try: child.wait(timeout=2.0)
    except Exception: pass
    reader.join(2.0)
    emit({"stopped": True, "job_drained": drained, "root_exit": child.returncode})
except BaseException as exc:
    emit({"started": child is not None, "supervisor_error_class": type(exc).__name__,
          "supervisor_error_code": getattr(exc, "winerror", None) or getattr(exc, "errno", None)})
finally:
    if child is not None:
        job = getattr(child, "_runtime_job", None)
        if job is not None and getattr(job, "handle", None):
            try: job.close()
            except Exception: pass
'@
    [System.IO.File]::WriteAllText($ownerPath, $ownerCode, [System.Text.UTF8Encoding]::new($false))
    $environment['ASTRID_ACP_PROBE_BUNDLE'] = $bundle
    $environment['ASTRID_ACP_PROBE_REPORT'] = $reportPath
    $environment['ASTRID_RUNTIME_DATA_ROOT'] = $tempDirectory
    $environment['ASTRID_SUPPORT_ROOT'] = $runtimeTemp
    $environment['ASTRID_WORKSPACE_DISCOVERY'] = $discoveryPath
    $environment['ASTRID_ACP_BRIDGE_PORT'] = '0'
    $environment['ASTRID_ACP_LOCAL_MEDIA_ONLY'] = '1'
    $environment.Remove('ASTRID_BRIDGE_TOKEN')

    $ownerStart = [System.Diagnostics.ProcessStartInfo]::new()
    $ownerStart.FileName = $python
    $ownerStart.Arguments = (@('-X', 'utf8', '-I', $ownerPath, $bun, $runnerPath, $tempDirectory, $sourceCheckout) |
        ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' '
    $ownerStart.WorkingDirectory = $sourceCheckout
    $ownerStart.UseShellExecute = $false
    $ownerStart.CreateNoWindow = $true
    $ownerStart.RedirectStandardInput = $true
    $ownerStart.RedirectStandardOutput = $true
    $ownerStart.RedirectStandardError = $true
    $ownerStart.EnvironmentVariables.Clear()
    foreach ($key in $environment.Keys) { $ownerStart.EnvironmentVariables[$key] = [string]$environment[$key] }
    $owner = [System.Diagnostics.Process]::new()
    $owner.StartInfo = $ownerStart
    if (-not $owner.Start()) { throw 'Runtime Job owner did not start.' }
    $script:phase = 'owner-handshake'
    $owner.StandardError.ReadToEndAsync() | Out-Null
    $helloTask = $owner.StandardOutput.ReadLineAsync()
    if (-not $helloTask.Wait(20000)) { throw 'Runtime Job owner start timed out.' }
    $hello = $helloTask.GetAwaiter().GetResult()
    if (-not $hello.StartsWith('ASTRID_DIAG:')) { throw 'Runtime Job owner did not report readiness.' }
    $helloValue = $hello.Substring('ASTRID_DIAG:'.Length) | ConvertFrom-Json
    if (-not $helloValue.started) { throw 'Runtime Job owner could not start the selected Bun service.' }
    $result.owner_ready = $true

    $script:phase = 'registration-wait'
    $deadline = [DateTime]::UtcNow.AddSeconds(25)
    while ([DateTime]::UtcNow -lt $deadline -and -not (Test-Path -LiteralPath $registrationPath)) {
        if ($owner.HasExited) { break }
        Start-Sleep -Milliseconds 100
    }
    if (-not (Test-Path -LiteralPath $registrationPath)) { throw 'Isolated bridge did not publish its temporary registration.' }
    $result.registration_found = $true
    $script:phase = 'registration-read'
    $registration = Get-Content -LiteralPath $registrationPath -Raw | ConvertFrom-Json
    if (-not $registration.endpoint -or -not $registration.token) { throw 'Temporary bridge registration is incomplete.' }
    $script:phase = 'endpoint-validation'
    $endpoint = [System.Uri]::new([string]$registration.endpoint)
    $result.endpoint_loopback = ($endpoint.Scheme -eq 'http' -and
        ($endpoint.Host -eq 'localhost' -or [System.Net.IPAddress]::IsLoopback($endpoint.Host -as [System.Net.IPAddress])))
    if (-not $result.endpoint_loopback) { throw 'Temporary bridge endpoint is not loopback HTTP.' }
    $result.endpoint_port = $endpoint.Port
    $script:phase = 'client-create'
    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromSeconds(35)

    $health = Invoke-SafeProbe 'health' { Invoke-LocalRequest $client ([string]$registration.endpoint) '/health' 'GET' ([string]$registration.token) } $result
    if ($null -ne $health) { $result.health_http = $health.status }

    $models = Invoke-SafeProbe 'models' { Invoke-LocalRequest $client ([string]$registration.endpoint) '/models' 'GET' ([string]$registration.token) } $result
    if ($null -ne $models) { $result.models_http = $models.status }
    if ($null -ne $models -and $models.body.providers -is [array]) {
        $providers = @($models.body.providers)
        $result.provider_count = $providers.Count
        $result.model_count = ($providers | ForEach-Object { @($_.models).Count } | Measure-Object -Sum).Sum
        $result.signed_in_count = @($providers | Where-Object { $_.signedIn -eq $true }).Count
    } elseif ($null -ne $models -and $models.body.error) { $result.models_error_code = Safe-ErrorCode $models.body.error }

    $connect = Invoke-SafeProbe 'connect' { Invoke-LocalRequest $client ([string]$registration.endpoint) '/connect' 'POST' `
        ([string]$registration.token) '{"agent":"astrid"}' } $result
    if ($null -ne $connect) { $result.connect_http = $connect.status }
    if ($null -ne $connect -and $connect.body.error) { $result.connect_error_code = Safe-ErrorCode $connect.body.error }
    if ($null -ne $connect -and $connect.status -ge 200 -and $connect.status -lt 300 -and $connect.body.connection_id) {
        $connectionId = [string]$connect.body.connection_id
        $result.initialize_succeeded = $true
    }
} catch {
    $result.phase = $script:phase
    $result.exception = Get-SafeExceptionSummary $_.Exception
    $result.error = 'Diagnostic failed during ' + $script:phase + ': ' + $result.exception.class + ' (' + $result.exception.classification + ')'
} finally {
    if ($connectionId -and $client -and $registration) {
        try {
            $deleted = Invoke-LocalRequest $client ([string]$registration.endpoint) "/$connectionId" 'DELETE' ([string]$registration.token)
            $result.connection_deleted = ($deleted.status -ge 200 -and $deleted.status -lt 300)
        } catch { $result.connection_deleted = $false }
    }
    if ($owner -and -not $owner.HasExited) {
        try {
            $owner.StandardInput.WriteLine('stop')
            $cleanupTask = $owner.StandardOutput.ReadLineAsync()
            if ($cleanupTask.Wait(20000)) {
                $cleanupLine = $cleanupTask.GetAwaiter().GetResult()
                if ($cleanupLine -and $cleanupLine.StartsWith('ASTRID_DIAG:')) {
                    $cleanup = $cleanupLine.Substring('ASTRID_DIAG:'.Length) | ConvertFrom-Json
                    $result.job_drained = [bool]$cleanup.job_drained
                }
            }
            if (-not $owner.WaitForExit(5000)) { try { $owner.Kill() } catch { }; $owner.WaitForExit() }
        } catch { try { $owner.Kill() } catch { } }
    }
    if ($client) { $client.Dispose() }
    if ($owner) { $owner.Dispose() }
    if ($reportPath -and (Test-Path -LiteralPath $reportPath)) {
        try {
            $childReport = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
            $result.bun_version = [string]$childReport.bun_version
            $result.child_fatal_class = $childReport.fatal.class
            $result.child_calls = @($childReport.calls)
        } catch { }
    }
    if ($tempDirectory -and (Test-Path -LiteralPath $tempDirectory)) {
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$result.phase = $script:phase
[pscustomobject]$result | ConvertTo-Json -Depth 8
