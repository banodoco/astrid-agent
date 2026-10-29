param(
    [Parameter(Mandatory=$true)][string]$DriverApp,
    [Parameter(Mandatory=$true)][string]$DriverAgent,
    [Parameter(Mandatory=$true)][string]$ProductApp,
    [Parameter(Mandatory=$true)][string]$FrozenManifest,
    [Parameter(Mandatory=$true)][string]$RunRoot,
    [Parameter(Mandatory=$true)][string]$EvidenceRoot,
    [Parameter(Mandatory=$true)][string]$ExpectedAgentSha,
    [Parameter(Mandatory=$true)][string]$ExpectedRuntimeSha,
    [Parameter(Mandatory=$true)][string]$Role
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
$local = [IO.Path]::GetFullPath($env:LOCALAPPDATA).TrimEnd('\')
$runLocal = [IO.Path]::GetFullPath((Join-Path $RunRoot 'local-appdata')).TrimEnd('\')
$protectedRoots = @($local, $runLocal) | Select-Object -Unique
$protected = @()
foreach ($root in $protectedRoots) {
    $protected += (Join-Path $root 'Astrid')
    $protected += (Join-Path $root 'Astrid Installer')
    $protected += (Join-Path $root 'Astrid Runtime')
    $protected += (Join-Path $root 'astrid-install.json')
}
$eventsPath = Join-Path $EvidenceRoot 'protected-path-events.jsonl'
$processPath = Join-Path $EvidenceRoot 'process-observations.jsonl'
$tracePath = Join-Path $EvidenceRoot 'kernel-file.etl'
$traceXmlPath = Join-Path $EvidenceRoot 'kernel-file-events.xml'
$watchers = @()
$watcherSources = @()
$traceStarted = $false
$traceName = "TstB3FileIo-$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT-$Role"
$nativeShell = (Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
$shellVersion = (& $nativeShell -NoProfile -NonInteractive -Command '$PSVersionTable.PSEdition + "|" + $PSVersionTable.PSVersion.ToString()').Trim()
if ($shellVersion -notmatch '^Desktop\|5\.1(?:\.|$)') { throw "Expected native Windows PowerShell 5.1; resolved $nativeShell reported $shellVersion" }
$environmentCache = @{}
if (-not ('TstB3ProcessEnvironment' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
public static class TstB3ProcessEnvironment {
    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessBasicInformation {
        public IntPtr Reserved1;
        public IntPtr PebBaseAddress;
        public IntPtr Reserved2A;
        public IntPtr Reserved2B;
        public IntPtr UniqueProcessId;
        public IntPtr InheritedFromUniqueProcessId;
    }
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern IntPtr OpenProcess(uint access, bool inherit, int processId);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool ReadProcessMemory(IntPtr process, IntPtr address, byte[] buffer, int size, out IntPtr read);
    [DllImport("kernel32.dll", SetLastError=true)]
    private static extern bool CloseHandle(IntPtr handle);
    [DllImport("ntdll.dll")]
    private static extern int NtQueryInformationProcess(IntPtr process, int infoClass, out ProcessBasicInformation info, int size, out int returned);
    private static IntPtr ReadPointer(IntPtr process, IntPtr address) {
        byte[] data = new byte[IntPtr.Size]; IntPtr read;
        if (!ReadProcessMemory(process, address, data, data.Length, out read) || read.ToInt64() != data.Length)
            throw new Win32Exception(Marshal.GetLastWin32Error());
        return new IntPtr(BitConverter.ToInt64(data, 0));
    }
    public static string ReadEnvironmentBlock(int processId) {
        if (!Environment.Is64BitProcess) throw new PlatformNotSupportedException("Environment reader requires a 64-bit host.");
        IntPtr process = OpenProcess(0x0410, false, processId);
        if (process == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            ProcessBasicInformation info; int returned;
            int status = NtQueryInformationProcess(process, 0, out info, Marshal.SizeOf(typeof(ProcessBasicInformation)), out returned);
            if (status != 0) throw new InvalidOperationException("NtQueryInformationProcess status 0x" + status.ToString("X8"));
            IntPtr parameters = ReadPointer(process, IntPtr.Add(info.PebBaseAddress, 0x20));
            IntPtr environment = ReadPointer(process, IntPtr.Add(parameters, 0x80));
            byte[] data = new byte[131072]; IntPtr bytesRead;
            if (!ReadProcessMemory(process, environment, data, data.Length, out bytesRead) || bytesRead.ToInt64() < 2)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            string text = Encoding.Unicode.GetString(data, 0, (int)bytesRead.ToInt64());
            int end = text.IndexOf("\0\0", StringComparison.Ordinal);
            if (end >= 0) text = text.Substring(0, end);
            return text;
        } finally { CloseHandle(process); }
    }
}
'@
}

function Write-JsonLine([string]$Path, $Value) {
    $Value | ConvertTo-Json -Compress -Depth 8 | Add-Content -LiteralPath $Path -Encoding UTF8
}

function Get-WhitelistedProcessEnvironment([int]$ProcessId) {
    if ($environmentCache.ContainsKey($ProcessId)) { return $environmentCache[$ProcessId] }
    try {
        $values = [ordered]@{}
        $block = [TstB3ProcessEnvironment]::ReadEnvironmentBlock($ProcessId)
        foreach ($entry in ($block -split "`0")) {
            $separator = $entry.IndexOf('=')
            if ($separator -lt 1) { continue }
            $name = $entry.Substring(0, $separator)
            if ($name -in @('LOCALAPPDATA','APPDATA','TEMP','TMP','UV_CACHE_DIR','ASTRID_RUNTIME_DATA_ROOT','ASTRID_PACKS_PATH')) {
                $values[$name] = $entry.Substring($separator + 1)
            }
        }
        $environmentCache[$ProcessId] = $values
    } catch {
        # A start event may arrive before the process environment is readable.
        # Do not cache failures: the next process-tree sample retries the PEB read.
        return @{ readError=$_.Exception.Message }
    }
    return $environmentCache[$ProcessId]
}

function Get-ProtectedSnapshot([string]$Phase) {
    $rows = @()
    foreach ($path in $protected) {
        $exists = Test-Path -LiteralPath $path
        $row = [ordered]@{ phase=$Phase; path=$path; exists=$exists; entries=@() }
        if ($exists) {
            $item = Get-Item -Force -LiteralPath $path
            $row.attributes = [string]$item.Attributes
            $row.creationTimeUtc = $item.CreationTimeUtc.ToString('o')
            $row.lastWriteTimeUtc = $item.LastWriteTimeUtc.ToString('o')
            if ($item.PSIsContainer) {
                $row.entries = @(Get-ChildItem -Force -Recurse -LiteralPath $path -ErrorAction SilentlyContinue |
                    ForEach-Object {
                        $entry = [ordered]@{
                            path=$_.FullName
                            attributes=[string]$_.Attributes
                            creationTimeUtc=$_.CreationTimeUtc.ToString('o')
                            lastWriteTimeUtc=$_.LastWriteTimeUtc.ToString('o')
                        }
                        if (-not $_.PSIsContainer) { $entry.length=$_.Length }
                        [pscustomobject]$entry
                    })
            } elseif (-not $item.PSIsContainer) {
                $row.length = $item.Length
            }
        }
        $rows += [pscustomobject]$row
    }
    $rows | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $EvidenceRoot "protected-paths-$Phase.json") -Encoding UTF8
}

function Get-ProcessSnapshot([string]$Phase, [int]$RootPid) {
    $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $byPid = @{}
    foreach ($proc in $all) { $byPid[[int]$proc.ProcessId] = $proc }
    $selected = @{}
    foreach ($proc in $all) {
        $pidValue = [int]$proc.ProcessId
        $current = $proc
        $related = ($pidValue -eq $RootPid)
        for ($depth=0; -not $related -and $depth -lt 12 -and $current; $depth++) {
            $parentPid = [int]$current.ParentProcessId
            if ($parentPid -eq $RootPid) { $related = $true; break }
            $current = $byPid[$parentPid]
        }
        $line = [string]$proc.CommandLine
        if ($related -or ($line -and ($line.Contains($RunRoot) -or $line.Contains($ProductApp) -or $line.Contains('astrid_runtime')))) {
            $selected[$pidValue] = [ordered]@{
                phase=$Phase; processId=$pidValue; parentProcessId=[int]$proc.ParentProcessId
                name=[string]$proc.Name; executablePath=[string]$proc.ExecutablePath
                commandLine=$line; creationDate=[string]$proc.CreationDate
                environment=(Get-WhitelistedProcessEnvironment $pidValue)
            }
        }
    }
    foreach ($value in $selected.Values) { Write-JsonLine $processPath $value }
}

function Drain-PathEvents {
    foreach ($event in @(Get-Event -ErrorAction SilentlyContinue)) {
        if ($event.SourceIdentifier -like 'TstB3Path*') {
            $args = $event.SourceEventArgs
            $eventPath = [string]$args.FullPath
            $isProtected = $false
            foreach ($protectedPath in $protected) {
                if ($eventPath.StartsWith($protectedPath, [StringComparison]::OrdinalIgnoreCase)) { $isProtected = $true; break }
            }
            if ($isProtected) {
                $oldPath = $null
                if ($args -is [IO.RenamedEventArgs]) { $oldPath = [string]$args.OldFullPath }
                Write-JsonLine $eventsPath ([ordered]@{
                    timestampUtc=[DateTime]::UtcNow.ToString('o')
                    changeType=[string]$args.ChangeType
                    path=$eventPath
                    oldPath=$oldPath
                })
            }
        }
        Remove-Event -EventIdentifier $event.EventIdentifier -ErrorAction SilentlyContinue
    }
}

function Drain-ProcessStartEvents {
    foreach ($event in @(Get-Event -SourceIdentifier 'TstB3ProcessStart' -ErrorAction SilentlyContinue)) {
        $startEvent = $event.SourceEventArgs.NewEvent
        $id = [int]$startEvent.ProcessID
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
        $line = if ($proc) { [string]$proc.CommandLine } else { '' }
        $isRelevant = ([string]$startEvent.ProcessName -match '^(?i:powershell|pythonw?|uv)\.exe$') -or ($line -and ($line.Contains($RunRoot) -or $line.Contains($ProductApp) -or $line.Contains('astrid_runtime')))
        if (-not $isRelevant) {
            Remove-Event -EventIdentifier $event.EventIdentifier -ErrorAction SilentlyContinue
            continue
        }
        Write-JsonLine $processPath ([ordered]@{
            phase='start-event'; timestampUtc=[DateTime]::UtcNow.ToString('o'); processId=$id
            parentProcessId=[int]$startEvent.ParentProcessID; name=[string]$startEvent.ProcessName
            executablePath=$(if ($proc) { [string]$proc.ExecutablePath } else { $null })
            commandLine=$line
            environment=(Get-WhitelistedProcessEnvironment $id)
        })
        Remove-Event -EventIdentifier $event.EventIdentifier -ErrorAction SilentlyContinue
    }
}

$processWhitelist = @('LOCALAPPDATA','APPDATA','TEMP','TMP','UV_CACHE_DIR','ASTRID_RUNTIME_DATA_ROOT','ASTRID_PACKS_PATH')
$envSnapshot = [ordered]@{}
foreach ($name in $processWhitelist) { $envSnapshot[$name] = [Environment]::GetEnvironmentVariable($name) }
@{
    role=$Role; timestampUtc=[DateTime]::UtcNow.ToString('o'); localAppData=$local
    runRoot=$RunRoot; productApp=$ProductApp; installer=(Join-Path $ProductApp 'public/install.ps1')
    installerShell=$nativeShell; installerShellVersion=$shellVersion
    installerArguments=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',(Join-Path $ProductApp 'public/install.ps1'),'--manifest',$FrozenManifest,'--root',(Join-Path $RunRoot 'program'),'--data-root',(Join-Path $RunRoot 'data'))
    environment=$envSnapshot
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'diagnostic-inputs.json') -Encoding UTF8

Get-ProtectedSnapshot 'before'
# Kernel file ETW records include the event process ID and file path. Keep the
# raw ETL and tracerpt rendering as evidence; the trace is observational only.
try {
    & logman.exe start $traceName -ets -p 'Microsoft-Windows-Kernel-File' 0xFFFFFFFFFFFFFFFF 5 -o $tracePath -f bincirc -max 128 2>&1 |
        Set-Content -LiteralPath (Join-Path $EvidenceRoot 'kernel-file-trace-start.log') -Encoding UTF8
    if ($LASTEXITCODE -eq 0) { $traceStarted = $true }
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'kernel-file-trace-start.log') -Encoding UTF8
}
@{ attempted=$true; started=$traceStarted; provider='Microsoft-Windows-Kernel-File'; etl=$tracePath } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'writer-trace-status.json') -Encoding UTF8
if (-not $traceStarted) { throw 'Kernel file ETW writer tracing could not be started; refusing an incomplete comparison.' }

$watchRoots = @($local, (Split-Path -Parent ([IO.Path]::GetFullPath($RunRoot)))) | Select-Object -Unique
for ($watchIndex=0; $watchIndex -lt $watchRoots.Count; $watchIndex++) {
    $watchRoot = [IO.Path]::GetFullPath($watchRoots[$watchIndex])
    if (-not (Test-Path -LiteralPath $watchRoot)) { throw "Watcher root is missing: $watchRoot" }
    $watcher = New-Object IO.FileSystemWatcher
    $watcher.Path = $watchRoot
    $watcher.IncludeSubdirectories = $true
    $watcher.Filter = '*'
    $watcher.NotifyFilter = [IO.NotifyFilters]::FileName -bor [IO.NotifyFilters]::DirectoryName -bor [IO.NotifyFilters]::LastWrite -bor [IO.NotifyFilters]::CreationTime
    foreach ($name in @('Created','Changed','Deleted','Renamed')) {
        $source = "TstB3Path-$watchIndex-$name"
        $watcherSources += $source
        Register-ObjectEvent -InputObject $watcher -EventName $name -SourceIdentifier $source | Out-Null
    }
    $watcher.EnableRaisingEvents = $true
    $watchers += $watcher
}
Register-WmiEvent -Query 'SELECT * FROM Win32_ProcessStartTrace' -SourceIdentifier 'TstB3ProcessStart' | Out-Null

$originalDriver = Join-Path $DriverApp 'scripts/installer/windows-ps51-lifecycle.ps1'
$driverCopy = Join-Path $EvidenceRoot 'lifecycle-instrumented.ps1'
$driverText = Get-Content -Raw -LiteralPath $originalDriver
$agentPin = '3204bb00135bec8ee2d2d3fb21eed5abfceca649'
$runtimePin = '6d53a19070faf63fc15ae1f81417722edc2c8a5f'
$environmentMarker = '$env:ASTRID_RUNTIME_DATA_ROOT = $DataRoot'
$installMarker = '        & $Shell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $Installer @Extra --manifest $FrozenManifest --root $ProgramRoot --data-root $DataRoot'
$markers = @($environmentMarker,$installMarker,$agentPin,$runtimePin)
foreach ($marker in $markers) {
    if ($driverText.IndexOf($marker, [StringComparison]::Ordinal) -lt 0 -or
        $driverText.IndexOf($marker, [StringComparison]::Ordinal) -ne $driverText.LastIndexOf($marker, [StringComparison]::Ordinal)) {
        throw "The reviewed lifecycle driver changed; expected one insertion point for: $marker"
    }
}
if ($ExpectedAgentSha -notmatch '^[0-9a-f]{40}$' -or $ExpectedRuntimeSha -notmatch '^[0-9a-f]{40}$') {
    throw 'Expected product source refs must be full lowercase commit SHAs.'
}
$envCapture = @'
$DiagnosticEnvironment = [ordered]@{}
foreach ($DiagnosticName in @('LOCALAPPDATA','APPDATA','TEMP','TMP','UV_CACHE_DIR','ASTRID_RUNTIME_DATA_ROOT','ASTRID_PACKS_PATH')) { $DiagnosticEnvironment[$DiagnosticName] = [Environment]::GetEnvironmentVariable($DiagnosticName) }
@{ timestampUtc=[DateTime]::UtcNow.ToString('o'); pid=$PID; stage='lifecycle-environment'; env=$DiagnosticEnvironment } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $TstEvidenceRoot 'lifecycle-environment.json') -Encoding UTF8
'@
$launchCapture = @'
$DiagnosticInstallArgs = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$Installer) + @($Extra) + @('--manifest',$FrozenManifest,'--root',$ProgramRoot,'--data-root',$DataRoot)
$DiagnosticInstallEnv = [ordered]@{}
foreach ($DiagnosticName in @('LOCALAPPDATA','APPDATA','TEMP','TMP','UV_CACHE_DIR','ASTRID_RUNTIME_DATA_ROOT','ASTRID_PACKS_PATH')) { $DiagnosticInstallEnv[$DiagnosticName] = [Environment]::GetEnvironmentVariable($DiagnosticName) }
@{ timestampUtc=[DateTime]::UtcNow.ToString('o'); pid=$PID; stage='installer-launch'; executable=$Shell; arguments=$DiagnosticInstallArgs; env=$DiagnosticInstallEnv } | ConvertTo-Json -Depth 5 | Add-Content -LiteralPath (Join-Path $TstEvidenceRoot 'installer-invocations.jsonl') -Encoding UTF8
'@
$evidenceLiteral = "'" + $EvidenceRoot.Replace("'", "''") + "'"
$evidenceRootCapture = '$TstEvidenceRoot = ' + $evidenceLiteral
$driverText = $driverText.Replace($environmentMarker, $environmentMarker + [Environment]::NewLine + $evidenceRootCapture + [Environment]::NewLine + $envCapture.Trim())
$driverText = $driverText.Replace($installMarker, $launchCapture.Trim() + [Environment]::NewLine + $installMarker)
$driverText = $driverText.Replace($agentPin, $ExpectedAgentSha).Replace($runtimePin, $ExpectedRuntimeSha)
Set-Content -LiteralPath $driverCopy -Value $driverText -Encoding UTF8BOM
@{ original=$originalDriver; originalSha256=(Get-FileHash -Algorithm SHA256 $originalDriver).Hash.ToLowerInvariant(); instrumented=$driverCopy; instrumentedSha256=(Get-FileHash -Algorithm SHA256 $driverCopy).Hash.ToLowerInvariant(); expectedAgentSha=$ExpectedAgentSha; expectedRuntimeSha=$ExpectedRuntimeSha; injections=@('post-RunRoot-environment','pre-installer-process','arm-specific-product-pin-assertions')} |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'instrumentation.json') -Encoding UTF8

$arguments = @('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',$driverCopy,
    '-RunRoot',$RunRoot,'-Verifier',(Join-Path $DriverAgent 'scripts/verify_installed.py'),'-ProductApp',$ProductApp,'-FrozenManifest',$FrozenManifest)
$shell = $nativeShell
$quoted = foreach ($value in $arguments) { '"' + ([string]$value).Replace('"','\"') + '"' }
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = $shell
$start.Arguments = $quoted -join ' '
$start.UseShellExecute = $false
$start.CreateNoWindow = $false
$child = New-Object Diagnostics.Process
$child.StartInfo = $start
$child.Start() | Out-Null
$driverPid = $child.Id
@{ pid=$driverPid; executable=$shell; commandLine=$start.Arguments; timestampUtc=[DateTime]::UtcNow.ToString('o') } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'driver-process.json') -Encoding UTF8

try {
    while (-not $child.HasExited) {
        Get-ProcessSnapshot 'during' $driverPid
        Drain-PathEvents
        Drain-ProcessStartEvents
        Start-Sleep -Milliseconds 500
        $child.Refresh()
    }
    $child.WaitForExit()
    $exitCode = $child.ExitCode
} finally {
    Drain-PathEvents
    Drain-ProcessStartEvents
    Get-ProcessSnapshot 'after' $driverPid
    foreach ($watcher in $watchers) { $watcher.EnableRaisingEvents = $false; $watcher.Dispose() }
    foreach ($source in $watcherSources) { Unregister-Event -SourceIdentifier $source -ErrorAction SilentlyContinue }
    Unregister-Event -SourceIdentifier 'TstB3ProcessStart' -ErrorAction SilentlyContinue
    Get-ProtectedSnapshot 'after'
    if ($traceStarted) {
        & logman.exe stop $traceName -ets 2>&1 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'kernel-file-trace-stop.log') -Encoding UTF8
        & tracerpt.exe $tracePath -y -of XML -o $traceXmlPath 2>&1 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'kernel-file-trace-convert.log') -Encoding UTF8
        if ($LASTEXITCODE -ne 0) { @{converted=$false; note='tracerpt conversion failed; raw ETL retained.'} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'writer-trace-conversion.json') -Encoding UTF8 }
        else {
            @{converted=(Test-Path -LiteralPath $traceXmlPath); xml=$traceXmlPath} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'writer-trace-conversion.json') -Encoding UTF8
            if (Test-Path -LiteralPath $traceXmlPath) {
                [xml]$traceXml = Get-Content -Raw -LiteralPath $traceXmlPath
                foreach ($eventNode in $traceXml.SelectNodes("//*[local-name()='Event']")) {
                    $systemNode = $eventNode.SelectSingleNode("./*[local-name()='System']")
                    $executionNode = $eventNode.SelectSingleNode("./*[local-name()='System']/*[local-name()='Execution']")
                    $eventDataNode = $eventNode.SelectSingleNode("./*[local-name()='EventData']")
                    $data = [ordered]@{}
                    if ($eventDataNode) {
                        foreach ($dataNode in $eventDataNode.SelectNodes("./*[local-name()='Data']")) { $data[[string]$dataNode.GetAttribute('Name')] = [string]$dataNode.InnerText }
                    }
                    $joined = ($data.Values -join ' ')
                    if ($joined -match '(?i)Astrid|AppData\\Local') {
                        $eventId = $eventNode.SelectSingleNode("./*[local-name()='System']/*[local-name()='EventID']")
                        $timeNode = $eventNode.SelectSingleNode("./*[local-name()='System']/*[local-name()='TimeCreated']")
                        $providerNode = $null
                        if ($systemNode) { $providerNode = $systemNode.SelectSingleNode("./*[local-name()='Provider']") }
                        $timestampUtc = $null
                        if ($timeNode) { $timestampUtc = [string]$timeNode.GetAttribute('SystemTime') }
                        $eventIdValue = $null
                        if ($eventId) { $eventIdValue = [string]$eventId.InnerText }
                        $providerValue = $null
                        if ($providerNode) { $providerValue = [string]$providerNode.GetAttribute('Name') }
                        $processIdValue = $null
                        if ($executionNode) { $processIdValue = [int]$executionNode.GetAttribute('ProcessID') }
                        Write-JsonLine (Join-Path $EvidenceRoot 'protected-path-writer-events.jsonl') ([ordered]@{
                            timestampUtc=$timestampUtc
                            eventId=$eventIdValue
                            provider=$providerValue
                            processId=$processIdValue
                            data=$data
                        })
                    }
                }
            }
        }
    }
}
@{ exitCode=$exitCode; driverPid=$driverPid; timestampUtc=[DateTime]::UtcNow.ToString('o') } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $EvidenceRoot 'driver-result.json') -Encoding UTF8
if ($exitCode -ne 0) { exit $exitCode }
