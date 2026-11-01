"""Narrow, fail-closed selection of the existing RC-0 direct update rows."""

RC0_CANDIDATE = {
    "app": "a55bec9ff384b5bbfee5535962880084ab2adb1b",
    "agent": "af5ae8afdb9829beb2bba4f811dbe97765bd72b2",
    "runtime": "47e5949465cb6f3afdfd1cb8a0e90009fd9d4020",
    "pinokio": "89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4",
    "manifest_sha256": "ced136903a7319e78a0be7db0cc1709c0663eaf7d567c040fe1812a916693874",
    "artifacts": {
        "public/install": "100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d",
        "public/install.sh": "591c4c51b148fccef2a7d1c45782d9ff949d5fd03d46cd82b5591e3a23d1b285",
        "public/install.ps1": "bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258",
    },
}

DIRECT_ROWS = (
    {"job_id": "direct-macos", "mode": "direct", "leg": "release0-to-release1", "runner": "macos-14", "shell": "sh", "os": "macos"},
    {"job_id": "direct-linux", "mode": "direct", "leg": "release0-to-release1", "runner": "ubuntu-24.04", "shell": "sh", "os": "linux"},
    {"job_id": "direct-windows", "mode": "direct", "leg": "release0-to-release1", "runner": "windows-2022", "shell": "powershell.exe", "os": "windows"},
)


def select_matrix(candidate: dict, matrix: list[dict]) -> list[dict]:
    """Select existing rows only after exact candidate and row-shape checks."""
    if candidate != RC0_CANDIDATE:
        raise ValueError("RC-0 direct route requires the exact frozen candidate and bootstrap hashes")
    selected = [row for row in matrix if row.get("job_id") in {item["job_id"] for item in DIRECT_ROWS}]
    if len(selected) != len(DIRECT_ROWS) or any(selected.count(row) != 1 for row in DIRECT_ROWS):
        raise ValueError("RC-0 direct route requires the three unchanged direct update rows")
    return selected
