import json
import os

import pytest
from scripts import ci_self_hosted_cleanup as diagnostic


def loaded(root):
    data = root / "data"
    return (f"gui/503/haus.astrid.runtime = {{\n"
            f" path = {root}/home/Library/LaunchAgents/haus.astrid.runtime.plist\n"
            f" program = {root}/program/versions/old/py/bin/python\n"
            f" ASTRID_RUNTIME_DATA_ROOT => {data}\n"
            f" from astrid_runtime.cli import main\n main(['run', '--data-root', {str(data)!r}])\n}}\n")


def test_service_proof_rejects_other_program_and_data(tmp_path):
    root = tmp_path / "astrid-r1-direct-macos"
    state = loaded(root)
    diagnostic.prove_service(state, root)
    for unsafe in (state.replace("versions/old/py/bin/python", "versions/old/py/bin/other"),
                   state.replace(str(root / "data"), str(tmp_path / "other")),
                   state.replace("LaunchAgents/haus", "LaunchAgents/other")):
        with pytest.raises(RuntimeError):
            diagnostic.prove_service(unsafe, root)


@pytest.mark.parametrize("kind", ["absent", "matching", "unrecognized"])
def test_diagnostic_reports_without_mutation(tmp_path, monkeypatch, capsys, kind):
    root = tmp_path / "astrid-r1-direct-macos"
    root.mkdir()
    monkeypatch.setattr(diagnostic.os, "getuid", lambda: 503)
    monkeypatch.setenv("RUNNER_NAME", "hannah-mac")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(diagnostic, "prove_root", lambda _: None)
    state = None if kind == "absent" else loaded(root)
    if kind == "unrecognized":
        state = state.replace("versions/old", "versions/other/bad")
    monkeypatch.setattr(diagnostic, "launch_state", lambda: state)
    monkeypatch.setattr(diagnostic, "active_smoke_process", lambda _: False)
    monkeypatch.setattr(diagnostic.subprocess, "run", lambda *a, **k: pytest.fail("unexpected command"))
    diagnostic.main()
    result = json.loads(capsys.readouterr().out)
    assert result["service"] == {"absent": "absent", "matching": "loaded, matching smoke identity",
                                 "unrecognized": "loaded, identity does not match smoke"}[kind]
    assert str(tmp_path) not in json.dumps(result)
    assert root.exists()


def test_root_marker_rejects_symlinks(tmp_path, monkeypatch):
    monkeypatch.setattr(diagnostic, "UID", os.getuid())
    root = tmp_path / "astrid-r1-direct-macos"
    root.mkdir()
    marker = root / "tst-b1-owner.json"
    marker.write_text(json.dumps({"schema_version": 1, "run_id": "a" * 32,
                                  "root": str(root / "program"), "data_root": str(root / "data")}))
    diagnostic.prove_root(root)
    marker.unlink()
    marker.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError):
        diagnostic.prove_root(root)


def test_process_scan_includes_multiline_runner_command(tmp_path, monkeypatch):
    root = tmp_path / "astrid-r1-direct-macos"
    monkeypatch.setattr(diagnostic.subprocess, "check_output", lambda *args, **kwargs:
                        f" 501 10 /bin/safe {root}\n 503 11 python -c import os\npath={root}/data\n")
    assert diagnostic.active_smoke_process(root)


def test_manual_workflow_embeds_only_read_only_diagnostic():
    from pathlib import Path
    import textwrap
    base = Path(__file__).parents[1]
    workflow = (base / ".github/workflows/journey.yml").read_text()
    embedded = workflow.split("          /opt/homebrew/opt/python@3.11/bin/python3.11 - <<'PY'\n", 1)[1].rsplit("\n          PY\n", 1)[0]
    assert textwrap.dedent(embedded).strip() == (base / "scripts/ci_self_hosted_cleanup.py").read_text().strip()
    assert "actions/checkout" not in workflow
    assert all(token not in workflow for token in ("bootout", "shutil.rmtree", ".unlink("))


@pytest.mark.parametrize("prefix", ["", "Bad request.\n", "Bad request.\r\n"])
def test_launch_state_accepts_only_exact_missing_service(prefix, monkeypatch):
    from types import SimpleNamespace
    missing = f'Could not find service "{diagnostic.LABEL}" in domain for user gui: {diagnostic.UID}'
    monkeypatch.setattr(diagnostic.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=113, stderr=prefix + missing + "\n", stdout=""))
    assert diagnostic.launch_state() is None
    monkeypatch.setattr(diagnostic.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=113, stderr=prefix + missing + " other failure", stdout=""))
    with pytest.raises(RuntimeError, match="Cannot establish"):
        diagnostic.launch_state()
