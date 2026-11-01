# Task-local capture helper. Invoke at the installer's preparation boundary,
# once immediately before and once immediately after preparation, under the
# installer's inherited token. ProductRoot must be that install's purelib.
param(
    [Parameter(Mandatory=$true)][string]$ProductRoot,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][ValidateSet('before','after')][string]$Phase
)
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$root = [IO.Path]::GetFullPath($ProductRoot).TrimEnd('\')
$relative = @(
    'astrid\core\timeline\schema\timeline-config.schema.json',
    'runtime.lock.json', 'astrid\__init__.py', 'astrid\core\__init__.py',
    'astrid\core\timeline\banodoco_schema.py', 'astrid\core\rendering\_local_schema.py',
    'astrid\core\timeline\__init__.py', 'astrid\core\rendering\__init__.py'
)
$paths = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
foreach ($leaf in $relative) {
    $path = Join-Path $root $leaf
    while ($true) {
        [void]$paths.Add($path)
        if ($path -ieq $root) { break }
        $parent = Split-Path -Parent $path
        if (-not $parent -or $parent -eq $path) { throw "Path escaped product root: $path" }
        $path = $parent
    }
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$raw = Join-Path $OutputDirectory "identity-$Phase.txt"
@("PID=$PID", "Name=$($identity.Name)", "UserSID=$($identity.User.Value)",
  "AuthenticationType=$($identity.AuthenticationType)", "ImpersonationLevel=$($identity.ImpersonationLevel)",
  "ProductRoot=$root", '--- whoami /all ---') | Out-File -LiteralPath $raw -Encoding utf8
& whoami.exe /all 2>&1 | Out-File -LiteralPath $raw -Encoding utf8 -Append
if ($LASTEXITCODE -ne 0) { throw "whoami exited $LASTEXITCODE" }
$rows = @()
foreach ($path in ($paths | Sort-Object)) {
    $acl = Get-Acl -LiteralPath $path
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    $rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
        [ordered]@{ sid=$_.IdentityReference.Value; type=[string]$_.AccessControlType;
          rights=[string]$_.FileSystemRights; rights_numeric=[int64]$_.FileSystemRights;
          inherited=$_.IsInherited; inheritance=[string]$_.InheritanceFlags;
          propagation=[string]$_.PropagationFlags }
    })
    $rows += [ordered]@{ path=$path; owner_sid=$owner; owner=$acl.Owner;
      access_rules_protected=$acl.AreAccessRulesProtected;
      sddl=$acl.GetSecurityDescriptorSddlForm([Security.AccessControl.AccessControlSections]::All);
      access=$rules }
}
$aclPath = Join-Path $OutputDirectory "acl-$Phase.json"
[ordered]@{ phase=$Phase; product_root=$root; identity_name=$identity.Name;
    identity_sid=$identity.User.Value; paths=$rows } | ConvertTo-Json -Depth 12 |
    Out-File -LiteralPath $aclPath -Encoding utf8
Get-FileHash -Algorithm SHA256 -LiteralPath $raw,$aclPath |
    Format-List Algorithm,Hash,Path | Out-File -LiteralPath (Join-Path $OutputDirectory "sha256-$Phase.txt") -Encoding utf8
# Compare the helper's process token with the actual calling installer process.
Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Principal;
public static class OwnerCallerToken {
  [StructLayout(LayoutKind.Sequential)] struct Luid { public UInt32 Low; public Int32 High; }
  [StructLayout(LayoutKind.Sequential)] struct Stats {
    public Luid TokenId, AuthenticationId; public Int64 ExpirationTime;
    public Int32 TokenType, ImpersonationLevel; public UInt32 DynamicCharged, DynamicAvailable, GroupCount, PrivilegeCount;
    public Luid ModifiedId;
  }
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool OpenProcessToken(IntPtr p, UInt32 access, out IntPtr token);
  [DllImport("advapi32.dll", SetLastError=true)] static extern bool GetTokenInformation(IntPtr t, int c, out Stats s, int n, out int length);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  public static string Describe(int pid) {
    IntPtr h;
    if (!OpenProcessToken(Process.GetProcessById(pid).Handle, 8, out h)) throw new System.ComponentModel.Win32Exception();
    try {
      Stats s; int n;
      if (!GetTokenInformation(h, 10, out s, Marshal.SizeOf(typeof(Stats)), out n)) throw new System.ComponentModel.Win32Exception();
      using (WindowsIdentity i = new WindowsIdentity(h)) return String.Format("pid={0} name={1} sid={2} token_id=[{3},{4}] authentication_id=[{5},{6}] type={7} impersonation={8} groups={9} privileges={10}", pid,i.Name,i.User.Value,s.TokenId.High,s.TokenId.Low,s.AuthenticationId.High,s.AuthenticationId.Low,s.TokenType,s.ImpersonationLevel,s.GroupCount,s.PrivilegeCount);
    } finally { CloseHandle(h); }
  }
}
'@
$parentPid = [int](Get-CimInstance Win32_Process -Filter "ProcessId = $PID").ParentProcessId
@([OwnerCallerToken]::Describe($parentPid), [OwnerCallerToken]::Describe($PID)) |
    Out-File -LiteralPath (Join-Path $OutputDirectory "token-$Phase.txt") -Encoding utf8
