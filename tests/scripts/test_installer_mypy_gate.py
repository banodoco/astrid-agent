"""The #370 ZIP/TAR preflight keeps the type of each archive member."""

import subprocess
import sys
from pathlib import Path


def test_installer_archive_members_have_no_attribute_type_errors():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "-m", "mypy", "astrid/installer/tools.py", "--hide-error-context",
         "--no-pretty", "--show-error-codes"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    assert "checked 1 source file" in result.stdout, result.stdout + result.stderr
    errors = [line for line in result.stdout.splitlines() if "[attr-defined]" in line]
    assert errors == [], "\n".join(errors)
