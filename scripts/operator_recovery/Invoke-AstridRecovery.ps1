# Private operator recovery package: invokes the canonical pinned installer only.
param(
  [Parameter(Mandatory=$true)][ValidateSet('status','prepare','resume')][string]$Action,
  [string]$ProgramRoot = (Join-Path $env:LOCALAPPDATA 'Astrid')
)
$ErrorActionPreference = 'Stop'
$ExpectedAgentSha = '44d965542f80d8b1c7a2b4680eb0c2d419bad665'
$ExpectedRuntimeSha = '9fed6cfc02a1a130011f97af701f5edf9eb374b0'
$ExpectedBootstrapHash = 'bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258'
$ExpectedManifestHash = '903d7703101a0cdbfbce7d4d5dea29f715b615a247499bd309483aa69c31d38c'

function Assert-PackageFile([string]$Path, [string]$ExpectedHash) {
  if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { throw ('Package file missing: ' + $Path) }
  $item = Get-Item -LiteralPath $Path -Force
  if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('Package file must not be a link: ' + $Path) }
  $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -cne $ExpectedHash) { throw ('Package checksum mismatch: ' + $item.Name + '. Nothing was launched.') }
}

try {
  $bootstrap = Join-Path $PSScriptRoot 'bootstrap\install.ps1'
  $manifest = Join-Path $PSScriptRoot 'recovery-manifest.json'
  Assert-PackageFile $bootstrap $ExpectedBootstrapHash
  Assert-PackageFile $manifest $ExpectedManifestHash
  $pins = Get-Content -LiteralPath $manifest -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($pins.agent.sha -cne $ExpectedAgentSha -or $pins.runtime.sha -cne $ExpectedRuntimeSha) {
    throw 'Package source pins differ from this reviewed recovery build. Nothing was launched.'
  }
  if ($ProgramRoot -notmatch '^[A-Za-z]:[\\/]') { throw 'ProgramRoot must be an absolute local Windows path.' }
  $ProgramRoot = [IO.Path]::GetFullPath($ProgramRoot)
  $powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
  if (!(Test-Path -LiteralPath $powershell -PathType Leaf)) { throw 'Windows PowerShell is unavailable.' }

  Write-Host ('Astrid canonical recovery / Agent ' + $ExpectedAgentSha.Substring(0,10))
  Write-Host ('Action: ' + $Action + ' / selected program: ' + $ProgramRoot)
  Write-Host 'The isolated bootstrap may download its pinned packages. Windows is never restarted automatically.'
  # Separate PowerShell process preserves both bootstrap failure exit codes and
  # its file-invocation argument contract. Never dispatch recovery through the selected old launcher.
  & $powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $bootstrap `
      --manifest $manifest --root $ProgramRoot --recover $Action
  $code = $LASTEXITCODE
  if ($code -ne 0) {
    Write-Host ('Recovery stopped with exit code ' + $code + '. Keep all checkpoint and backup files; send this output.')
    exit $code
  }
  if ($Action -eq 'prepare') {
    Write-Host 'PREPARE FINISHED. Save other work, choose Windows Restart, then run 2-Resume.cmd after signing in.'
    Write-Host 'Do not run the ordinary installer or the withdrawn recovery helper between these steps.'
  } elseif ($Action -eq 'resume') {
    Write-Host 'RESUME FINISHED. Run 0-Inspect.cmd and confirm complete/healthy status, then test an existing ComfyUI workflow.'
  }
  exit 0
} catch {
  Write-Host ('STOPPED: ' + $_.Exception.Message)
  Write-Host 'Keep the output, checkpoint and retained backup. Do not change ACLs or delete ownership markers.'
  exit 1
}
