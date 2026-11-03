param([Parameter(Mandatory=$true)][string]$Root)
$ErrorActionPreference = 'Stop'
$exitCode = 1
Start-Transcript -Path (Join-Path $Root 'child-transcript.txt')
try {
    $config = Get-Content (Join-Path $Root 'config.json') -Raw | ConvertFrom-Json
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    Write-Output "identity=$($identity.Name) sid=$($identity.User.Value) admin=$($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
    # Read the mandatory integrity label directly; do not depend on localized
    # whoami output or infer integrity from administrator membership.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class P19Integrity {
    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool GetTokenInformation(IntPtr token, int type, IntPtr info, int length, out int needed);
    public static string Sid(IntPtr token) {
        int size;
        GetTokenInformation(token, 25, IntPtr.Zero, 0, out size);
        if (size <= 0) throw new Win32Exception(Marshal.GetLastWin32Error());
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try {
            if (!GetTokenInformation(token, 25, buffer, size, out size))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return new SecurityIdentifier(Marshal.ReadIntPtr(buffer)).Value;
        } finally { Marshal.FreeHGlobal(buffer); }
    }
}
'@
    $integrity = [P19Integrity]::Sid($identity.Token)
    Write-Output "integrity_sid=$integrity"
    whoami /all | Tee-Object -FilePath (Join-Path $Root 'child-whoami.txt')
    if ($LASTEXITCODE -ne 0) { throw "whoami exit $LASTEXITCODE" }
    [Environment]::OSVersion.VersionString
    if ($identity.User.Value -ne $config.account_sid) { throw 'Unexpected child identity' }
    if ($integrity -ne 'S-1-16-8192') { throw "Unexpected integrity SID $integrity; medium required" }
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Child token is elevated' }
    $env:GIT_CONFIG_COUNT = '1'
    $env:GIT_CONFIG_KEY_0 = 'safe.directory'
    $env:GIT_CONFIG_VALUE_0 = $config.source.Replace('\','/')
    $env:PATH = (Split-Path $config.git) + ';' + $env:PATH
    $env:TEMP = $Root
    $env:TMP = $Root
    Set-Location $config.source
    & $config.git rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "git rev-parse exit $LASTEXITCODE" }
    & $config.git status --short --untracked-files=all
    if ($LASTEXITCODE -ne 0) { throw "git status exit $LASTEXITCODE" }
    $probe = Join-Path $Root 'windows-census-p19.py'
    $output = Join-Path $Root 'proof'
    $args = @('-u', ('"' + $probe + '"'), ('"' + $output + '"'))
    $process = Start-Process -FilePath $config.python -ArgumentList $args -WorkingDirectory $config.source -NoNewWindow -Wait -PassThru -RedirectStandardOutput (Join-Path $Root 'probe-stdout.txt') -RedirectStandardError (Join-Path $Root 'probe-stderr.txt')
    $exitCode = $process.ExitCode
    Write-Output "probe_inner_exit=$exitCode"
} catch {
    Write-Output "CHILD_FAILURE: $($_ | Out-String)"
    $exitCode = 1
} finally {
    Stop-Transcript
    Set-Content -Encoding ASCII -Path (Join-Path $Root 'finished.txt') -Value $exitCode
}
exit $exitCode
