param()

# Compare the harmless ACP profile lookup under ordinary and
# hidden/no-console Bun launches, using the selected version's chat command,
# environment, and working directory. Raw child stdout/stderr never leave the
# temporary Bun runner.
$ErrorActionPreference = 'Stop'

function Quote-WindowsArgument([string]$Value) {
    if ($Value.Length -gt 0 -and $Value -notmatch '[\s"]') { return $Value }
    $quoted = '"'
    $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $slashes++; continue }
        if ($character -eq '"') {
            $quoted += ('\' * (2 * $slashes + 1)) + '"'
            $slashes = 0
            continue
        }
        $quoted += ('\' * $slashes) + $character
        $slashes = 0
    }
    $quoted += ('\' * (2 * $slashes)) + '"'
    return $quoted
}

function Start-ProbeRunner([string]$Bun, [string]$Runner, [string]$Config,
                           [hashtable]$Environment, [string]$WorkingDirectory,
                           [bool]$Hidden) {
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $Bun
    $contextName = if ($Hidden) { 'hidden' } else { 'console' }
    $start.Arguments = ((@($Runner, $Config, $contextName) | ForEach-Object { Quote-WindowsArgument $_ }) -join ' ')
    $start.WorkingDirectory = $WorkingDirectory
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $Hidden
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables.Clear()
    foreach ($key in $Environment.Keys) { $start.EnvironmentVariables[$key] = [string]$Environment[$key] }

    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        if (-not $process.Start()) { throw 'The Bun diagnostic runner did not start.' }
        $process.StandardInput.Close()
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(60000)) {
            try { $process.Kill() } catch { }
            $process.WaitForExit()
            return [pscustomobject]@{ context = $contextName; bun_version = $null; runner_status = $null;
                timed_out = $true; elapsed_ms = [long]$timer.ElapsedMilliseconds; probes = @() }
        }
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        # Read stderr solely to avoid a blocked redirected pipe. Never return it.
        [void]$stderrTask.GetAwaiter().GetResult()
        try {
            $payload = $stdout | ConvertFrom-Json -ErrorAction Stop
            return [pscustomobject]@{ context = $contextName; bun_version = [string]$payload.bun_version;
                runner_status = [int]$process.ExitCode;
                timed_out = $false; elapsed_ms = [long]$timer.ElapsedMilliseconds; probes = @($payload.probes) }
        } catch {
            return [pscustomobject]@{ context = $contextName; bun_version = $null; runner_status = [int]$process.ExitCode;
                timed_out = $false; elapsed_ms = [long]$timer.ElapsedMilliseconds; probes = @();
                runner_result = 'invalid-json' }
        }
    } finally {
        $process.Dispose()
    }
}

$result = [ordered]@{
    selected_version = $null
    bun_version = $null
    command_kind = $null
    command_matches_selected_version = $false
    service_env_keys = @()
    service_path_present = $false
    virtual_env_present = $false
    working_directory_exists = $false
    contexts = @()
    error = $null
}

$tempDirectory = $null
try {
    $task = Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop
    if (-not $task.Actions -or $task.Actions.Count -eq 0 -or
        $task.Actions[0].Execute -notmatch '^(?<root>.+)\\versions\\(?<version>[^\\]+)\\py\\Scripts\\pythonw?\.exe$') {
        throw 'Could not identify the selected Runtime version from its Scheduled Task.'
    }
    $programRoot = $Matches.root
    $versionId = $Matches.version
    $versionRoot = Join-Path (Join-Path $programRoot 'versions') $versionId
    $result.selected_version = $versionId

    $profilePath = Join-Path $versionRoot 'profile.json'
    $profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
    $declarationPath = [string]$profile.service_declaration
    $sourceCheckout = [string]$profile.source_checkout
    if (-not [System.IO.Path]::IsPathRooted($declarationPath) -or
        -not [System.IO.Path]::IsPathRooted($sourceCheckout)) {
        throw 'The selected profile does not contain absolute service declaration and source paths.'
    }
    $declaration = Get-Content -LiteralPath $declarationPath -Raw | ConvertFrom-Json
    $chat = @($declaration.services | Where-Object { $_.name -eq 'chat' }) | Select-Object -First 1
    if (-not $chat -or -not $chat.argv -or -not $chat.env) {
        throw 'The selected service declaration has no usable chat entry.'
    }
    $bun = [string]$chat.argv[0]
    if (-not (Test-Path -LiteralPath $bun -PathType Leaf)) { throw 'The selected chat Bun executable is missing.' }
    $command = [string]$chat.env.ASTRID_ACP_COMMAND
    if (-not $command) { throw 'The selected chat service has no ACP command.' }

    $extension = [System.IO.Path]::GetExtension($command)
    if ($extension -match '(?i)^\.(cmd|bat)$') { $result.command_kind = 'cmd-shim' }
    elseif ([System.IO.Path]::IsPathRooted($command)) {
        $result.command_kind = if ($extension -match '(?i)^\.exe$') { 'absolute-exe' } else { 'absolute-path' }
        try {
            $scriptsRoot = [System.IO.Path]::GetFullPath((Join-Path $versionRoot 'py\Scripts')).TrimEnd('\') + '\'
            $result.command_matches_selected_version = [System.IO.Path]::GetFullPath($command).StartsWith(
                $scriptsRoot, [System.StringComparison]::OrdinalIgnoreCase)
        } catch { $result.command_matches_selected_version = $false }
    } else { $result.command_kind = 'bare-command' }

    $environment = @{}
    foreach ($entry in [System.Environment]::GetEnvironmentVariables().GetEnumerator()) {
        $environment[([string]$entry.Key).ToUpperInvariant()] = [string]$entry.Value
    }
    # Match task configuration in launch order: selected version, allowlisted
    # user service settings, Runtime staging, then the chat declaration.
    $versionDocument = Get-Content -LiteralPath (Join-Path $versionRoot 'version.json') -Raw | ConvertFrom-Json
    foreach ($property in $versionDocument.env.PSObject.Properties) {
        $environment[$property.Name.ToUpperInvariant()] = [string]$property.Value
    }
    $settingsPath = Join-Path $programRoot 'service-env.json'
    if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
        $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
        foreach ($property in $settings.PSObject.Properties) {
            if ($property.Name -notin @('ASTRID_WARM_IDLE_SECONDS', 'ASTRID_ALLOW_UNQUALIFIED_GPU')) {
                throw 'The selected service environment contains an unsupported setting.'
            }
            $environment[$property.Name.ToUpperInvariant()] = [string]$property.Value
        }
    }
    foreach ($reserved in @('PYTHONPATH', 'PYTHONHOME', 'ASTRID_RUNTIME_JOB_HANDLE',
                              'ASTRID_RUNTIME_SERVICE_NAME', 'ASTRID_RUNTIME_SERVICE_LAUNCH_ID')) {
        $environment.Remove($reserved)
    }
    foreach ($key in @($environment.Keys)) {
        if ($key.StartsWith('ASTRID_RUNTIME_CUSTODY_', [System.StringComparison]::OrdinalIgnoreCase)) {
            $environment.Remove($key)
        }
    }
    $scriptsDirectory = Join-Path $versionRoot 'py\Scripts'
    $environment['PATH'] = $scriptsDirectory + [System.IO.Path]::PathSeparator + [string]$environment['PATH']
    $environment['VIRTUAL_ENV'] = Join-Path $versionRoot 'py'
    foreach ($property in $chat.env.PSObject.Properties) {
        $environment[$property.Name.ToUpperInvariant()] = [string]$property.Value
    }
    $environment['ASTRID_RUNTIME_SERVICE_NAME'] = 'chat'
    $environment['ASTRID_RUNTIME_SERVICE_LAUNCH_ID'] = [guid]::NewGuid().ToString('N')
    if (-not $environment['HOME']) { $environment['HOME'] = $environment['USERPROFILE'] }
    $result.service_env_keys = @($chat.env.PSObject.Properties.Name | Sort-Object)
    $result.service_path_present = [bool]$environment['PATH']
    $result.virtual_env_present = [bool]$environment['VIRTUAL_ENV']
    $result.working_directory_exists = Test-Path -LiteralPath $sourceCheckout -PathType Container

    $tempDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ('astrid-acp-launch-probe-' + [guid]::NewGuid().ToString('N'))
    [System.IO.Directory]::CreateDirectory($tempDirectory) | Out-Null
    $runnerPath = Join-Path $tempDirectory 'probe.mjs'
    $configPath = Join-Path $tempDirectory 'config.json'
    $runner = @'
import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
const cfg = JSON.parse(readFileSync(process.argv[2], 'utf8'));
const context = process.argv[3];
const command = cfg.command;
const env = { ...process.env };
if (env.ASTRID_ACP_PROFILE && !env.OMP_PROFILE) env.OMP_PROFILE = env.ASTRID_ACP_PROFILE;
const probes = [];
for (const probe of [
  { name: 'profile', args: ['--agent-profile-file'], cwd: cfg.source_cwd, timeout: 5000, maxBuffer: 65536, hidden: false },
]) {
  for (const inputMode of ['ignore', 'empty-pipe']) {
    const started = Date.now();
    let result;
    try {
      result = spawnSync(command, probe.args, {
        cwd: probe.cwd, env, encoding: 'utf8', timeout: probe.timeout, maxBuffer: probe.maxBuffer,
        windowsHide: probe.hidden,
        stdio: inputMode === 'ignore' ? ['ignore', 'pipe', 'pipe'] : ['pipe', 'pipe', 'pipe'],
        ...(inputMode === 'empty-pipe' ? { input: '' } : {}),
      });
    } catch (error) {
      result = { status: null, signal: null, error };
    }
    const error = result.error;
    const safeIdentifier = value => {
      if (typeof value === 'number') return value;
      return typeof value === 'string' && /^[A-Za-z0-9_.+-]{1,40}$/.test(value)
        ? value : (value == null ? null : 'redacted');
    };
    const row = {
      probe: probe.name,
      stdin: inputMode,
      elapsed_ms: Date.now() - started,
      status: Number.isInteger(result.status) ? result.status : null,
      signal: typeof result.signal === 'string' ? result.signal : null,
      error_code: safeIdentifier(error?.code),
      error_errno: safeIdentifier(error?.errno),
      error_syscall: safeIdentifier(error?.syscall),
      timed_out: error?.code === 'ETIMEDOUT' || error?.killed === true,
      stderr_present: typeof result.stderr === 'string' && result.stderr.length > 0,
    };
    if (typeof result.stderr === 'string') {
      const exceptionClass = result.stderr.match(/\b(ModuleNotFoundError|ImportError|PermissionError|FileNotFoundError|NotADirectoryError|OSError|RuntimeError|ValueError)\b/);
      const winError = result.stderr.match(/\[WinError\s+(\d+)\]/i);
      row.stderr_exception_class = exceptionClass?.[1] ?? null;
      row.stderr_winerror = winError ? Number(winError[1]) : null;
    }
    if (probe.name === 'profile' && row.status === 0 && typeof result.stdout === 'string') {
      const resolved = result.stdout.trim();
      row.profile_exists = resolved.length > 0 && existsSync(resolved);
    }
    probes.push(row);
  }
}
process.stdout.write(JSON.stringify({ bun_version: Bun.version, probes }));
'@
    [System.IO.File]::WriteAllText($runnerPath, $runner, [System.Text.UTF8Encoding]::new($false))
    $config = [ordered]@{ command = $command; source_cwd = $sourceCheckout }
    [System.IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 8 -Compress),
        [System.Text.UTF8Encoding]::new($false))

    $visible = Start-ProbeRunner $bun $runnerPath $configPath $environment $sourceCheckout $false
    $hidden = Start-ProbeRunner $bun $runnerPath $configPath $environment $sourceCheckout $true
    $result.contexts = @($visible, $hidden)
    $result.bun_version = if ($visible.bun_version) { $visible.bun_version } else { $hidden.bun_version }
} catch {
    $result.error = 'Diagnostic setup failed: ' + $_.Exception.GetType().Name
} finally {
    if ($tempDirectory -and (Test-Path -LiteralPath $tempDirectory)) {
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force -ErrorAction SilentlyContinue
    }
}

[pscustomobject]$result | ConvertTo-Json -Depth 8
