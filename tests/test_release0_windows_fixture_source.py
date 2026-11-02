"""Source-level regression coverage for the Release-0 Windows fixture."""

from pathlib import Path


def test_private_directory_imports_security_before_get_acl():
    source = (Path(__file__).parent / "fixtures/release0/served/install.ps1").read_text(encoding="utf-8")
    function = source.split("    function Initialize-PrivateDirectory", 1)[1].split("\n    try {", 1)[0]

    assert function.index("            return\n") < function.index("        Import-Module Microsoft.PowerShell.Security\n")
    assert function.index("        Import-Module Microsoft.PowerShell.Security\n") < function.index(
        "        $Acl = Get-Acl -LiteralPath $Path\n"
    )
