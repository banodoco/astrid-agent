$ErrorActionPreference = 'Stop'
$evidence = Join-Path $env:RUNNER_TEMP 'p19-evidence'
$root = "C:\p19-$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT"
$user = 'p19standard'
$task = "p19-$env:GITHUB_RUN_ID-$env:GITHUB_RUN_ATTEMPT"
$created = $false
$registered = $false
$exitCode = 1
Start-Transcript -Path (Join-Path $evidence 'host-transcript.txt')
try {
    Write-Output "workflow_sha=$env:GITHUB_WORKFLOW_SHA run_id=$env:GITHUB_RUN_ID attempt=$env:GITHUB_RUN_ATTEMPT"
    whoami /all
    if (Get-LocalUser -Name $user -ErrorAction SilentlyContinue) { throw "Refusing existing account $user" }
    New-Item -ItemType Directory -Path $root | Out-Null
    New-LocalUser -Name $user -NoPassword -Description 'Temporary P19 census account' | Out-Null
    $created = $true
    $account = "$env:COMPUTERNAME\$user"
    $userSid = (Get-LocalUser -Name $user).SID.Value
    $adminGroup = Get-LocalGroup -SID 'S-1-5-32-544'
    $usersGroup = Get-LocalGroup -SID 'S-1-5-32-545'
    if (-not (Get-LocalGroupMember -Group $usersGroup | Where-Object { $_.SID.Value -eq $userSid })) {
        Add-LocalGroupMember -Group $usersGroup -Member $account
    }
    if (Get-LocalGroupMember -Group $adminGroup | Where-Object { $_.SID.Value -eq $userSid }) { throw 'Temporary account belongs to Administrators' }
    Write-Output "standard_account=$account sid=$userSid"
    & icacls.exe $root /grant "${user}:(OI)(CI)M" /T
    if ($LASTEXITCODE -ne 0) { throw "icacls exit $LASTEXITCODE" }
    $source = Join-Path $root 'source'
    New-Item -ItemType Directory -Path $source | Out-Null
    Get-ChildItem -Force "$env:GITHUB_WORKSPACE/source" | Copy-Item -Destination $source -Recurse -Force
    Copy-Item "$env:GITHUB_WORKSPACE/venue/scripts/p19-native-venue/windows-census-p19.py" $root
    Copy-Item "$env:GITHUB_WORKSPACE/venue/scripts/p19-native-venue/child.ps1" $root
    $probeHash = (Get-FileHash (Join-Path $root 'windows-census-p19.py') -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($probeHash -ne 'edbc4bae5e9a182c0cafc86985c27c8265be7edcaf1a7ac92db65f0f916b1a1d') { throw "Wrong probe hash $probeHash" }
    $python = (Get-Command python.exe -ErrorAction Stop).Source
    $git = (Get-Command git.exe -ErrorAction Stop).Source
    @{python=$python; git=$git; account_sid=$userSid; source=$source; root=$root; probe_sha256=$probeHash} |
        ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $root 'config.json')
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$root\child.ps1`" -Root `"$root`""
    $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $task -Action $action -Principal $principal -Force | Out-Null
    $registered = $true
    Start-ScheduledTask -TaskName $task
    $deadline = (Get-Date).AddSeconds(180)
    while (-not (Test-Path (Join-Path $root 'finished.txt')) -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    Get-ScheduledTaskInfo -TaskName $task | Format-List *
    if (-not (Test-Path (Join-Path $root 'finished.txt'))) { throw 'Standard-user task did not produce finished.txt within 180 seconds; inspect task result and partial evidence' }
    $exitCode = [int](Get-Content (Join-Path $root 'finished.txt') -Raw).Trim()
    Write-Output "inner_exit=$exitCode"
} catch {
    Write-Output "VENUE_FAILURE: $($_ | Out-String)"
    $exitCode = 1
} finally {
    if ($registered) {
        Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $task -Confirm:$false -ErrorAction SilentlyContinue
    }
    if (Test-Path $root) {
        Get-ChildItem -Force $root | Where-Object { $_.Name -ne 'source' } | Copy-Item -Destination $evidence -Recurse -Force
    }
    if ($created) { Remove-LocalUser -Name $user -ErrorAction Continue }
    Stop-Transcript
}
exit $exitCode
