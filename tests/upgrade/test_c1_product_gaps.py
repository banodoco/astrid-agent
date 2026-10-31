"""UPD escalation reproductions for C1. Failures stay visible in integration.

These exercise declaration policy with temporary files; no installer or service
manager is invoked. The full journey additionally requires live chat verification.
"""
import os
from pathlib import Path

import pytest

from astrid.installer.backend import Backend
from astrid.installer.core import Version, write_json
from astrid.services import service_declaration


pytestmark = pytest.mark.integration


@pytest.mark.parametrize("old_command", ["bare", "pruned"])
def test_upd_c1_old_declaration_rebinds_chat_command_to_candidate(tmp_path, monkeypatch, old_command):
    root, data = tmp_path / "program", tmp_path / "data"
    old_root, candidate = root / "versions/old", root / "versions/candidate"
    old_root.mkdir(parents=True)
    candidate.mkdir()
    python = candidate / ("py/Scripts/python.exe" if os.name == "nt" else "py/bin/python")
    executable = python.with_name("astrid.exe" if os.name == "nt" else "astrid")
    executable.parent.mkdir(parents=True)
    executable.touch()
    executable.chmod(0o755)
    command = "astrid" if old_command == "bare" else str(root / "versions/pruned/py/bin/astrid")
    declaration, profile = old_root / "services.json", old_root / "profile.json"
    write_json(declaration, {"services": [{"name": "chat", "env": {"ASTRID_ACP_COMMAND": command}}]})
    write_json(profile, {"service_declaration": str(declaration)})
    old = Version(old_root, old_root / "py/bin/python", profile, {})
    _cwd, retained = Backend._prior_chat_settings(old)
    for key, value in retained.items():
        monkeypatch.setenv(key, value)
    value = service_declaration(source_checkout=candidate, data_root=data,
                                python=str(python), bun=str(candidate / "bun"))
    chat = next(row for row in value["services"] if row["name"] == "chat")
    selected = chat["env"]["ASTRID_ACP_COMMAND"]
    # Bare POSIX commands must resolve only from the candidate's declared PATH.
    if selected == "astrid" and os.name != "nt":
        import shutil
        selected = shutil.which(selected, path=chat["env"]["PATH"])
    assert selected and Path(selected) == executable, (
        "UPD: stale ASTRID_ACP_COMMAND survived into candidate declaration; "
        f"old={command!r}, candidate={str(executable)!r}, selected={selected!r}. "
        "Reproduce with ci_journey.py --leg update-over-old-declaration --old-declaration "
        + old_command + " and explicit B1 roots."
    )
