param(
    [string]$DataRoot,
    [int]$RequestTimeoutSeconds = 45
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

function Get-SafeDetail([object]$Value) {
    if ($null -eq $Value) { return $null }
    $text = [string]$Value
    $text = $text -replace '(?i)Bearer\s+[^\s,;]+', 'Bearer [redacted]'
    $text = $text -replace '(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+', '$1=[redacted]'
    $text = $text -replace '(?i)([?&](?:token|key|secret|signature)=)[^&\s]+', '$1[redacted]'
    $text = $text -replace '(?i)https?://[^/@\s]+:[^/@\s]+@', 'https://[redacted]@'
    $text = $text -replace '(?i)[A-Z]:\\Users\\[^\\\s]+', '[user-path]'
    $text = $text -replace '[A-Z]:\\[^\s"'';,]+', '[path]'
    if ($text.Length -gt 500) { $text = $text.Substring(0, 500) }
    return $text
}

function Invoke-BridgeRequest(
    [System.Net.Http.HttpClient]$Client,
    [string]$BaseUri,
    [string]$Path,
    [string]$Method,
    [string]$Token,
    [string]$Body = $null
) {
    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::new($Method), "$BaseUri$Path")
    $request.Headers.Add('Origin', 'https://astrid.haus')
    if (-not [string]::IsNullOrEmpty($Token)) {
        $request.Headers.Authorization = [System.Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $Token)
        $request.Headers.Add('X-Astrid-Bridge-Version', 'v1')
    }
    if ($null -ne $Body) {
        $request.Content = [System.Net.Http.StringContent]::new(
            $Body, [System.Text.Encoding]::UTF8, 'application/json')
    }
    try {
        $response = $Client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
            $parsed = $null
            if ($content) {
                try { $parsed = $content | ConvertFrom-Json } catch { $parsed = $null }
            }
            return [pscustomobject]@{
                status = [int]$response.StatusCode
                body = $parsed
                raw = $content
                error = $null
            }
        } finally {
            $response.Dispose()
        }
    } catch {
        return [pscustomobject]@{
            status = $null
            body = $null
            raw = $null
            error = (Get-SafeDetail $_.Exception.Message)
        }
    } finally {
        $request.Dispose()
    }
}

$result = [ordered]@{
    data_root_kind = 'default'
    task = $null
    runtime = $null
    bridge = [ordered]@{}
    error = $null
}

try {
    $task = Get-ScheduledTask -TaskName 'Astrid Runtime' -ErrorAction Stop
    $taskInfo = Get-ScheduledTaskInfo -TaskName 'Astrid Runtime' -ErrorAction Stop
} catch {
    $result.error = 'Astrid Runtime Scheduled Task is unavailable; no process or bridge files were inspected.'
    [pscustomobject]$result | ConvertTo-Json -Depth 8
    return
}

$defaultDataRoot = Join-Path $env:LOCALAPPDATA 'Astrid Runtime'
$dataRootWasProvided = -not [string]::IsNullOrWhiteSpace($DataRoot)
if (-not $dataRootWasProvided) {
    $selectionPath = Join-Path $env:LOCALAPPDATA 'astrid-install.json'
    if (Test-Path -LiteralPath $selectionPath) {
        try {
            $selection = Get-Content -LiteralPath $selectionPath -Raw | ConvertFrom-Json
            if ($selection.data_root) { $DataRoot = [string]$selection.data_root }
        } catch { $DataRoot = $null }
    }
    if (-not $DataRoot) { $DataRoot = $defaultDataRoot }
}
if ($DataRoot -ne $defaultDataRoot) {
    $result.data_root_kind = 'custom'
}

$client = $null
$connectionId = $null
$registration = $null
try {
    $taskVersion = $null
    $taskProgramRoot = $null
    if ($task.Actions.Count -gt 0 -and $task.Actions[0].Execute -match '^(?<root>.+)\\versions\\(?<version>[^\\]+)\\py\\Scripts\\pythonw?\.exe$') {
        $taskProgramRoot = $Matches.root
        $taskVersion = $Matches.version
    }
    $taskResult = [long]$taskInfo.LastTaskResult
    $taskResultUnsigned = [uint32]($taskResult -band 4294967295)
    $result.task = [ordered]@{
        state = [string]$task.State
        last_task_result = $taskResult
        last_task_result_hex = ('0x{0:X8}' -f $taskResultUnsigned)
        selected_version = $taskVersion
        multiple_instances = [string]$task.Settings.MultipleInstances
    }

    $discovery = $null
    $discoveryPath = Join-Path $DataRoot 'runtime\discovery.json'
    try { $discovery = Get-Content $discoveryPath -Raw | ConvertFrom-Json }
    catch { $result.runtime = [ordered]@{ discovery_error = (Get-SafeDetail $_.Exception.Message) } }
    $client = [System.Net.Http.HttpClient]::new()
    $client.Timeout = [TimeSpan]::FromSeconds([Math]::Max(2, [Math]::Min(60, $RequestTimeoutSeconds)))
    if ($discovery) {
        $reportedRuntimePid = $null
        $runtimeProcessName = $null
        if ($discovery.pid -is [int] -or $discovery.pid -is [long]) {
            $runtimePid = [int]$discovery.pid
            $reportedRuntimePid = $runtimePid
            try { $runtimeProcess = Get-Process -Id $runtimePid -ErrorAction Stop } catch { $runtimeProcess = $null }
        }
        $runtimeStartTime = $null
        if ($runtimeProcess) {
            $runtimeProcessName = $runtimeProcess.ProcessName
            try { $runtimeStartTime = $runtimeProcess.StartTime.ToUniversalTime().ToString('o') } catch { $runtimeStartTime = 'unavailable' }
        }
        $result.runtime = [ordered]@{
            discovery_pid = $reportedRuntimePid
            discovery_birth_id = (Get-SafeDetail $discovery.process_birth_id)
            process_exists = ($null -ne $runtimeProcess)
            process_name = $runtimeProcessName
            process_start_time = $runtimeStartTime
            runtime_instance_id_present = [bool]$discovery.runtime_instance_id
        }
        $runtimeHealth = Invoke-BridgeRequest $client $discovery.door_endpoint '/api/runtime/v1/health' 'GET' ''
        $result.runtime.health_http = $runtimeHealth.status
        if ($runtimeHealth.error) { $result.runtime.health_error = $runtimeHealth.error }
        if ($runtimeHealth.body) {
            $result.runtime.health_status = [string]$runtimeHealth.body.status
            $result.runtime.services = @($runtimeHealth.body.services | ForEach-Object {
                [ordered]@{
                    name = [string]$_.name
                    state = [string]$_.state
                    restart_count = $_.restart_count
                    last_exit = $_.last_exit
                    reason = (Get-SafeDetail $_.reason)
                }
            })
        }
    } else {
        $result.runtime = [ordered]@{ discovery_error = 'Runtime discovery metadata is unavailable.' }
    }

    $registrationPath = Join-Path $DataRoot 'runtime\chat.json'
    try { $registration = Get-Content $registrationPath -Raw | ConvertFrom-Json }
    catch {
        $result.bridge.registration_error = (Get-SafeDetail $_.Exception.Message)
        $registration = $null
    }
    if (-not $registration) {
        if (-not $result.bridge.registration_error) {
            $result.bridge.registration_error = 'Protected chat registration is unavailable.'
        }
    } else {
        if (-not $registration.endpoint -or -not $registration.token) {
            $result.bridge.registration_error = 'Protected chat registration is missing its endpoint or credential.'
        } else {
            $base = [string]$registration.endpoint
            $acpToken = [string]$registration.token
            try {
                $bridgePort = ([Uri]$base).Port
                $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $bridgePort -ErrorAction Stop |
                    Where-Object { $_.LocalAddress -eq '127.0.0.1' })
                $result.bridge.listener = @($listeners | ForEach-Object {
                    $listenerProcess = $null
                    $listenerStart = $null
                    $listenerName = $null
                    try { $listenerProcess = Get-Process -Id $_.OwningProcess -ErrorAction Stop } catch { $listenerProcess = $null }
                    if ($listenerProcess) {
                        $listenerName = $listenerProcess.ProcessName
                        try { $listenerStart = $listenerProcess.StartTime.ToUniversalTime().ToString('o') }
                        catch { $listenerStart = 'unavailable' }
                    }
                    [ordered]@{
                        pid = [int]$_.OwningProcess
                        process_name = $listenerName
                        process_start_time = $listenerStart
                    }
                })
            } catch {
                $result.bridge.listener_error = (Get-SafeDetail $_.Exception.Message)
            }

            $configuredCommand = $null
            $versionRoot = $null
            if ($taskProgramRoot -and $taskVersion) {
                $versionRoot = Join-Path (Join-Path $taskProgramRoot 'versions') $taskVersion
                $servicesPath = Join-Path $versionRoot 'services.json'
                try {
                    $servicesFile = Get-Item -LiteralPath $servicesPath -ErrorAction Stop
                    $result.bridge.services_json_mtime_utc = $servicesFile.LastWriteTimeUtc.ToString('o')
                    $servicesDeclaration = Get-Content -LiteralPath $servicesPath -Raw | ConvertFrom-Json
                    $chatService = @($servicesDeclaration.services | Where-Object { $_.name -eq 'chat' }) | Select-Object -First 1
                    if ($chatService) { $configuredCommand = [string]$chatService.env.ASTRID_ACP_COMMAND }
                } catch {
                    $result.bridge.services_json_error = (Get-SafeDetail $_.Exception.Message)
                }
            }
            if ($configuredCommand) {
                $commandExtension = [System.IO.Path]::GetExtension($configuredCommand)
                if ($commandExtension -match '(?i)^\.(cmd|bat)$') {
                    $commandKind = 'cmd-shim'
                } elseif ([System.IO.Path]::IsPathRooted($configuredCommand)) {
                    $commandKind = 'absolute-path'
                    if ($commandExtension -match '(?i)^\.exe$') { $commandKind = 'absolute-exe' }
                } else {
                    $commandKind = 'bare-command'
                }
                $selectedMatch = $false
                if ([System.IO.Path]::IsPathRooted($configuredCommand) -and $versionRoot) {
                    try {
                        $scriptsRoot = [System.IO.Path]::GetFullPath((Join-Path $versionRoot 'py\Scripts')).TrimEnd('\') + '\'
                        $configuredFullPath = [System.IO.Path]::GetFullPath($configuredCommand)
                        $selectedMatch = $configuredFullPath.StartsWith($scriptsRoot, [System.StringComparison]::OrdinalIgnoreCase)
                    } catch { $selectedMatch = $false }
                }
                $result.bridge.configured_command_kind = $commandKind
                $result.bridge.configured_command_matches_selected_version = $selectedMatch
            } else {
                $result.bridge.configured_command_kind = 'unavailable'
                $result.bridge.configured_command_matches_selected_version = $null
            }

            $bridgeHealth = Invoke-BridgeRequest $client $base '/health' 'GET' $acpToken
            $result.bridge.health_http = $bridgeHealth.status
            if ($bridgeHealth.error) { $result.bridge.health_error = $bridgeHealth.error }
            if ($bridgeHealth.body) {
                $result.bridge.health_status = [string]$bridgeHealth.body.status
                $result.bridge.protocol = [string]$bridgeHealth.body.protocol
            }

            $models = Invoke-BridgeRequest $client $base '/models' 'GET' $acpToken
            $result.bridge.models_http = $models.status
            if ($models.error) { $result.bridge.models_error = $models.error }
            if ($models.body.providers) {
                $result.bridge.providers = @($models.body.providers | ForEach-Object {
                    [ordered]@{
                        provider = [string]$_.id
                        signed_in = $_.signedIn
                        model_count = @($_.models).Count
                    }
                })
            } elseif ($models.body.error) {
                $result.bridge.models_error = Get-SafeDetail $models.body.error
                if ($models.body.detail) { $result.bridge.models_detail = Get-SafeDetail $models.body.detail }
            }

            $agents = Invoke-BridgeRequest $client $base '/agents' 'GET' $acpToken
            $result.bridge.agents_http = $agents.status
            if ($agents.error) { $result.bridge.agents_error = $agents.error }
            if ($agents.body.agents) {
                $result.bridge.agents = @($agents.body.agents | ForEach-Object {
                    [ordered]@{
                        id = [string]$_.id
                        state = [string]$_.state
                        sign_in = [string]$_.signIn
                        model_count = @($_.models).Count
                    }
                })
            } elseif ($agents.body.error) {
                $result.bridge.agents_error = Get-SafeDetail $agents.body.error
                if ($agents.body.detail) { $result.bridge.agents_detail = Get-SafeDetail $agents.body.detail }
            }

            # initialize only: this creates no ACP session and sends no model prompt.
            $connect = Invoke-BridgeRequest $client $base '/connect' 'POST' $acpToken '{"agent":"astrid"}'
            $result.bridge.connect_http = $connect.status
            if ($connect.error) { $result.bridge.connect_error = $connect.error }
            if ($connect.status -ge 200 -and $connect.status -lt 300 -and $connect.body.connection_id) {
                $connectionId = [string]$connect.body.connection_id
                $result.bridge.initialize = 'succeeded'
            } else {
                $result.bridge.initialize = 'failed'
                if ($connect.body.error) { $result.bridge.connect_error = Get-SafeDetail $connect.body.error }
                if ($connect.body.detail) { $result.bridge.connect_detail = Get-SafeDetail $connect.body.detail }
                if (-not $connect.body -and $connect.raw) { $result.bridge.connect_error = Get-SafeDetail $connect.raw }
            }
        }
    }
} catch {
    $result.error = Get-SafeDetail $_.Exception.Message
} finally {
    if ($connectionId -and $client -and $registration) {
        try {
            $closed = Invoke-BridgeRequest $client $base "/$connectionId" 'DELETE' $acpToken
            $result.bridge.close_http = $closed.status
            if ($closed.error) { $result.bridge.close_error = $closed.error }
            if ($closed.body) { $result.bridge.closed = [bool]$closed.body.closed }
        } catch {
            $result.bridge.close_error = Get-SafeDetail $_.Exception.Message
        }
    }
    if ($client) { $client.Dispose() }
}

[pscustomobject]$result | ConvertTo-Json -Depth 8
