# Hosted direct Windows row only. This must run in Windows PowerShell 5.1,
# like the driver's scheduled-task preflight.
$ErrorActionPreference = 'Stop'
$diag = Join-Path $env:RUNNER_TEMP 'journey-manager-direct-windows'
New-Item -ItemType Directory -Force -Path $diag | Out-Null
Start-Transcript -Path (Join-Path $diag 'prerequisite.log') | Out-Null
$taskName = 'Astrid-Journey-Manager-Probe-' + [guid]::NewGuid().ToString('N')
$registered = $false
$result = 1
try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    Write-Output "Identity: $identity"
    $service = Get-Service -Name Schedule -ErrorAction Stop
    Write-Output "Task Scheduler initial status: $($service.Status)"
    if ($service.Status -ne 'Running') { Start-Service -Name Schedule -ErrorAction Stop }
    $service.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Running, [TimeSpan]::FromSeconds(30))
    Write-Output "Task Scheduler status: $((Get-Service -Name Schedule).Status)"

    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\cmd.exe" -Argument '/c exit 0'
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive
    Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -ErrorAction Stop | Out-Null
    $registered = $true
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
    if ($task.TaskName -ne $taskName) { throw 'Scheduled-task query returned an unexpected task.' }
    Write-Output 'Temporary task registration and query succeeded.'

    $result = 0
} catch {
    Write-Output ("Prerequisite failure: " + ($_ | Out-String))
} finally {
    if ($registered) {
        try {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
            Write-Output 'Temporary task removed.'
        } catch {
            Write-Output ("Temporary task cleanup failed: " + ($_ | Out-String))
            $result = 1
        }
    }
    Write-Output "setup_exit=$result"
    Stop-Transcript | Out-Null
}
exit $result
