"""Human/JSON policy application through the real skills CLI composition."""
import json
import sys
from pathlib import Path

import pytest

from astrid import skills
from astrid.installer import service
from astrid.skills import cli, state


@pytest.mark.parametrize("command", ["sync", "install"])
@pytest.mark.parametrize("json_output", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_off_reports_and_persists_without_readding(tmp_path, capsys, command, json_output, dry_run):
    home = Path.home()
    for harness in ("claude", "codex", "hermes"):
        (home / f".{harness}").mkdir()
    record_path = service.install_selection(home, sys.platform)
    service.write_json(record_path, {"owner": service.LABEL, "program_root": str(tmp_path / "program"),
        "choices": {"skills": True, "skill_copies": True}})
    skills.sync(harness_names=["claude", "codex", "hermes"], mechanism="copy")
    owned = home / ".codex/skills/astrid"
    edited = home / ".claude/skills/astrid/SKILL.md"
    edited.write_bytes(edited.read_bytes() + b"\nmy edit\r\n")
    edit_bytes = edited.read_bytes()
    foreign = home / ".hermes/skills/astrid-unknown"
    foreign.symlink_to(tmp_path / "foreign/astrid/unknown")
    state_bytes = state.state_path().read_bytes()
    policy_bytes = record_path.read_bytes()
    args = [command, "--skills", "off"] + (["--json"] if json_output else []) + (["--dry-run"] if dry_run else [])
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    if json_output:
        report = json.loads(output)
        assert report["conflicts"]
        assert str(owned) in next(row for row in report["actions"] if row["harness"] == "codex")["removed"]
        assert str(edited.parent) in next(row for row in report["actions"] if row["harness"] == "claude")["preserved"]
    else:
        assert "removed:" in output and "preserved:" in output and "conflict" in output
        assert "edited astrid managed copy preserved" in output.lower()
    assert edited.read_bytes() == edit_bytes and foreign.is_symlink()
    if dry_run:
        assert owned.is_dir()
        assert state.state_path().read_bytes() == state_bytes
        assert record_path.read_bytes() == policy_bytes
    else:
        assert not owned.exists()
        assert json.loads(record_path.read_text())["choices"]["skills"] is False
        assert cli.main(["sync"]) == 0
        capsys.readouterr()
        assert not owned.exists()
        assert edited.read_bytes() == edit_bytes and foreign.is_symlink()
