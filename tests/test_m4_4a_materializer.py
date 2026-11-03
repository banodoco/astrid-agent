"""Focused checks for the unavailable native seam and runner-local bindings."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import materialize_m4_4a_backend as materializer


@pytest.fixture
def checkouts(tmp_path):
    roots = {}
    candidate = {}
    for name in ("app", "agent", "runtime", "pinokio"):
        root = tmp_path / name
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        if name == "pinokio":
            (root / "pinokio").mkdir()
            for file in ("update.js", "stop.js"):
                (root / "pinokio" / file).write_text(file, encoding="utf-8")
        else:
            (root / "tracked").write_text(name, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
        candidate[name] = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        roots[name] = root
    return candidate, roots


def test_prepare_binds_checked_out_refs_and_builder_actions(checkouts, monkeypatch):
    candidate, roots = checkouts
    old = "a" * 40
    calls = []
    monkeypatch.setattr(materializer.ci_journey, "validate_old_launcher_provenance",
                        lambda path, ref: {"ref": ref, "path": "pinokio/update.js"})
    def build(path, ref, *, app_directory):
        calls.append((path, ref, app_directory))
        return {"run": [{"ref": ref}]}
    monkeypatch.setattr(materializer.ci_journey, "build_pinokio_action", build)
    os_label = materializer.ci_journey.journey_os_label()
    shell = "powershell.exe" if os_label == "windows" else "sh"
    result = materializer.prepare_bindings(candidate, roots, old, os_label, "local", shell)
    assert result["candidate_checkouts"]["app"] == {"path": str(roots["app"].resolve()), "ref": candidate["app"]}
    assert calls == [(roots["pinokio"].resolve(), old, roots["pinokio"].resolve()),
                     (roots["pinokio"].resolve(), candidate["pinokio"], roots["pinokio"].resolve())]
    assert result["actions"] == {"first": {"steps": [{"ref": old}]},
                                  "retry": {"steps": [{"ref": candidate["pinokio"]}]}}
    assert set(result["candidate_checkouts"]["pinokio"]["files"]) == {"pinokio/update.js", "pinokio/stop.js"}

    altered = {**candidate, "agent": "f" * 40}
    with pytest.raises(ValueError, match="agent checkout differs"):
        materializer.prepare_bindings(altered, roots, old, os_label, "local", shell)
    (roots["runtime"] / "untracked").write_text("not frozen")
    with pytest.raises(ValueError, match="runtime checkout is not clean"):
        materializer.prepare_bindings(candidate, roots, old, os_label, "local", shell)


def test_missing_native_seam_refuses_without_emitting_config(tmp_path, monkeypatch, capsys):
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({}))
    output = tmp_path / "config.json"
    monkeypatch.setattr(materializer, "prepare_bindings", lambda *args: {"prepared": True})
    monkeypatch.setattr(sys, "argv", ["materializer", "--candidate", str(candidate),
        "--app-checkout", "app", "--agent-checkout", "agent", "--runtime-checkout", "runtime",
        "--pinokio-checkout", "pinokio", "--old-launcher-ref", "a" * 40,
        "--os", "linux", "--runner", "local", "--shell", "sh", "--output", str(output)])
    assert materializer.main() == 1
    assert "NEEDS M-PIN" in capsys.readouterr().err
    assert not output.exists()
    output.write_text("stale")
    assert materializer.main() == 1
    assert "stale config" in capsys.readouterr().err
    assert output.read_text() == "stale"


def test_workflow_calls_hook_only_for_backend_after_checkout_and_stops_on_error():
    workflow = (Path(__file__).parents[1] / ".github/workflows/journey.yml").read_text()
    job = workflow.split("  journey:\n", 1)[1].split("  m4_5:", 1)[0]
    assert job.index("path: pinokio") < job.index("agent/scripts/materialize_m4_4a_backend.py")
    run = job.split("- name: Run the matrix-owned lifecycle driver", 1)[1]
    hook = run.index("agent/scripts/materialize_m4_4a_backend.py")
    journey = run.index("agent/scripts/ci_journey.py")
    assert "if ('${{ matrix.mode }}' -eq 'pinokio-backend')" in run[:hook]
    assert "if (@($selectedConfig.PSObject.Properties).Count -eq 0)" in run[:hook]
    for arg in ("--candidate", "--app-checkout", "--agent-checkout", "--runtime-checkout",
                "--pinokio-checkout", "--old-launcher-ref", "--os", "--runner", "--shell", "--output"):
        assert arg in run[hook:journey]
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in run[hook:journey]
    assert "--pinokio-backend-config" in run[hook:journey]
