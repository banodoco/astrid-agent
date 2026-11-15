"""Skills sync parses only the helper's stdout; warnings on stderr reach the log, not the result."""

import sys
from pathlib import Path
from types import SimpleNamespace

from astrid.installer.backend import Backend


def test_skills_sync_ignores_warnings_on_stderr(tmp_path: Path) -> None:
    script = tmp_path / "helper.py"
    script.write_text(
        "import json, sys\n"
        "print(\"managed copy of built-in pack 'hivemind' is set aside\", file=sys.stderr)\n"
        "print(json.dumps({'actions': [], 'conflicts': []}))\n",
        encoding="utf-8",
    )
    backend = Backend.__new__(Backend)
    backend.service = SimpleNamespace(choices={})
    backend.console_diagnostics = False
    backend.log = tmp_path / "install.log"
    version = SimpleNamespace(python=sys.executable, env={})

    result = backend._run_skills(version, [sys.executable, str(script)])

    assert result["ok"] is True
    assert "set aside" in backend.log.read_text(encoding="utf-8")
