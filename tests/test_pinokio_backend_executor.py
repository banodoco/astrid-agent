"""Disposable subprocess/filesystem tests for the executable Pinokio boundary."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.pinokio_backend_executor import (
    ExecutionError,
    ValidationError,
    admit_hosted_receipt,
    execute,
)


def _git_repo(path: Path, files: dict[str, str]) -> tuple[str, dict[str, str]]:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Executor Test"], check=True)
    hashes = {}
    for relative, content in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        hashes[relative] = hashlib.sha256(content.encode()).hexdigest()
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    ref = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
    return ref, hashes


def _fixture_script(root: Path) -> Path:
    script = root / "boundary.py"
    script.write_text(
        """
import json, os, pathlib, sys

state = pathlib.Path(os.environ["STATE"])
mode = sys.argv[1]
current = state.read_text() if state.exists() else "new"

if mode == "first-refuse":
    if current == "new":
        state.write_text("refused")
        print("Astrid is still running; refused")
        raise SystemExit(42)
    raise SystemExit(0)
if mode == "first-success":
    state.write_text("running")
    print("first action success")
    raise SystemExit(0)
if mode == "retry":
    state.write_text("running")
    print("retry action success")
    raise SystemExit(0)
if mode == "stop":
    state.write_text("stopped")
    print("Astrid stopped.")
    raise SystemExit(0)
if mode == "start":
    state.write_text("running")
    print("Astrid started.")
    raise SystemExit(0)
if mode == "ready":
    running = state.read_text() == "running"
    print(json.dumps({"ready": running, "http_status": 200 if running else 503,
                      "identity": "candidate", "runtime_instance_id": "instance-1"}))
    raise SystemExit(0)
if mode == "status":
    print(json.dumps({"support": {"healthy": state.read_text() == "running"},
                      "runtime_instance_id": "instance-1"}))
    raise SystemExit(0)
if mode == "health":
    print(json.dumps({"status": "ok", "protocol": "workspace.v1",
                      "install": {"managed_by": "pinokio"}, "identity": "candidate",
                      "runtime_instance_id": "instance-1"}))
    raise SystemExit(0)
if mode == "menu":
    print(json.dumps({"open_astrid": state.read_text() == "running"}))
    raise SystemExit(0)
if mode == "processes":
    print(json.dumps({"controller_pid": 101, "runtime_instance_id": "instance-1",
                      "controller_token": os.environ["TOKEN"]}))
    raise SystemExit(0)
if mode in {"busy-refusal", "unknown-refusal"}:
    print(json.dumps({"outcome": "refused", "reason": mode.split("-")[0]}))
    raise SystemExit(7)
if mode == "recovery":
    print(json.dumps({"outcome": "stopped", "same_session": True}))
    raise SystemExit(0)
if mode == "identity-mismatch":
    print(json.dumps({"ready": False, "http_status": 200, "identity": "wrong",
                      "runtime_instance_id": "instance-1"}))
    raise SystemExit(0)
if mode == "instance-mismatch":
    print(json.dumps({"ready": False, "http_status": 200, "identity": "candidate",
                      "runtime_instance_id": "wrong"}))
    raise SystemExit(0)
raise SystemExit(2)
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def _command(script: Path, root: Path, state: Path, mode: str) -> dict:
    return {
        "id": f"fixture.{mode}", "argv": [sys.executable, str(script), mode], "cwd": str(root),
        "env": {"STATE": str(state), "TOKEN": "fixture-secret"}, "redact_env": ["TOKEN"],
        "timeout_seconds": 10,
    }


def _config(tmp_path: Path, *, first_mode: str = "first-refuse", evidence_class: str = "executable-boundary") -> dict:
    root = tmp_path / "fixture"
    root.mkdir(parents=True)
    script = _fixture_script(root)
    state = root / "state.txt"
    guard = root / "guard.txt"
    guard.write_text("unchanged", encoding="utf-8")
    old_path = tmp_path / "old-launcher"
    old_ref, old_hashes = _git_repo(old_path, {"pinokio/update.js": "old launcher\n"})
    candidates = {}
    for name in ("app", "agent", "runtime"):
        path = tmp_path / name
        ref, hashes = _git_repo(path, {"identity.txt": f"{name}\n"})
        candidates[name] = {"path": str(path), "ref": ref, "files": hashes}
    pinokio_path = tmp_path / "candidate-pinokio"
    pinokio_ref, pinokio_hashes = _git_repo(pinokio_path, {"pinokio/update.js": "candidate launcher\n"})
    candidates["pinokio"] = {"path": str(pinokio_path), "ref": pinokio_ref, "files": pinokio_hashes}

    runtime = {name: _command(script, root, state, name) for name in
               ("ready", "status", "health", "menu", "processes", "stop", "start")}
    controls = {
        "busy_stop": {"refusal": _command(script, root, state, "busy-refusal"),
                      "recovery": _command(script, root, state, "recovery"),
                      "expected_reason": "busy", "guard_paths": [str(guard)]},
        "unknown_stop": {"refusal": _command(script, root, state, "unknown-refusal"),
                         "recovery": _command(script, root, state, "recovery"),
                         "expected_reason": "unknown", "guard_paths": [str(guard)]},
        "identity_mismatch": {"probe": _command(script, root, state, "identity-mismatch")},
        "instance_mismatch": {"probe": _command(script, root, state, "instance-mismatch")},
    }
    return {
        "candidate_checkouts": candidates,
        "old_launcher": {"checkout": str(old_path), "ref": old_ref, "file": "pinokio/update.js",
                          "sha256": old_hashes["pinokio/update.js"]},
        "actions": {
            "first": {"kind": "pinokio-action", "command": _command(script, root, state, first_mode),
                       "refusal_exit_codes": [42], "provenance": {"ref": old_ref,
                       "sha256": old_hashes["pinokio/update.js"]},
                       "steps": [{"kind": "git-shell"}, {"kind": "shell", "bootstrap_action": "update-app"}]},
            "retry": {"kind": "pinokio-action", "command": _command(script, root, state, "retry"),
                      "provenance": {"ref": pinokio_ref, "sha256": pinokio_hashes["pinokio/update.js"]},
                      "steps": [{"kind": "shell", "bootstrap_action": "install"}]},
        },
        "runtime": {**runtime, "expected_identity": "candidate", "expected_instance_id": "instance-1"},
        "negative_controls": controls,
        "platform": {"os": "macos", "runner": "fixture-macos", "shell": "sh",
                      "evidence_class": evidence_class, "hosted": evidence_class == "hosted-lifecycle"},
        "source_override_test_mode": False,
    }


def _events(receipt_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (receipt_dir / "pinokio-backend/events.jsonl").read_text().splitlines()]


def test_refusal_stop_retry_start_and_real_boundaries(tmp_path: Path):
    config = _config(tmp_path)
    output = tmp_path / "receipt"
    receipt = execute(config, output)

    assert receipt["status"] == "pass"
    assert receipt["pinokio_backend"]["first_click"]["outcome"] == "refused"
    assert receipt["pinokio_backend"]["stop_after_refusal"]["result"] == "stopped"
    assert receipt["pinokio_backend"]["retry_after_stop"]["result"] == "succeeded"
    assert receipt["pinokio_backend"]["stop_start"]["start"]["result"] == "ready"
    assert (tmp_path / "fixture/state.txt").read_text() == "running"
    assert "fixture-secret" not in (output / "pinokio-backend/commands.jsonl").read_text()
    assert "fixture-secret" not in (output / "pinokio-backend/processes-before.json").read_text()
    names = [row["event"] for row in _events(output)]
    assert names.index("action_built") < names.index("launcher_ref_advanced") < names.index("first_click_begin")
    assert names.index("ready_sentinel") < names.index("installer_end") < names.index("action_end")
    assert names.index("action_end") < names.index("health_probe_begin") < names.index("health_probe_pass")
    assert [row["seq"] for row in _events(output)] == list(range(1, len(names) + 1))
    assert receipt["pinokio_backend"]["negative_controls"]["busy_stop"]["mutation"] is False


def test_success_branch_has_no_retry_or_manual_start(tmp_path: Path):
    config = _config(tmp_path, first_mode="first-success")
    receipt = execute(config, tmp_path / "receipt")
    backend = receipt["pinokio_backend"]
    assert backend["first_click"]["outcome"] == "succeeded"
    assert backend["stop_after_refusal"] is None
    assert backend["retry_after_stop"] is None
    assert backend["not_applicable"] == ["A3"]
    assert backend["readiness_after_update"]["manual_start"] is False


def test_provenance_is_checked_before_action_process(tmp_path: Path):
    config = _config(tmp_path)
    marker = tmp_path / "action-would-have-run"
    action = config["actions"]["first"]["command"]
    action["argv"] = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('ran')"]
    config["candidate_checkouts"]["agent"]["ref"] = "0" * 40
    with pytest.raises(ValidationError, match="does not match immutable ref"):
        execute(config, tmp_path / "receipt")
    assert not marker.exists()


def test_mutable_checkout_and_forbidden_fallback_fail_closed(tmp_path: Path):
    config = _config(tmp_path)
    (tmp_path / "agent" / "untracked.txt").write_text("mutable", encoding="utf-8")
    with pytest.raises(ValidationError, match="mutable"):
        execute(config, tmp_path / "receipt")
    config = _config(tmp_path / "second")
    config["actions"]["first"]["command"]["direct_installer"] = True
    with pytest.raises(ValidationError, match="forbidden"):
        execute(config, tmp_path / "second-receipt")


def test_source_fake_is_explicit_and_rejected_by_hosted_gate(tmp_path: Path):
    config = _config(tmp_path, evidence_class="source_fake")
    receipt = execute(config, tmp_path / "receipt")
    assert receipt["evidence_class"] == "source_fake"
    with pytest.raises(ValidationError, match="cannot satisfy hosted"):
        admit_hosted_receipt(receipt)


def test_cli_requires_explicit_output_and_help_is_available(tmp_path: Path):
    module = Path(__file__).parents[1] / "scripts/pinokio_backend_executor.py"
    help_result = subprocess.run([sys.executable, str(module), "--help"], capture_output=True, text=True, check=False)
    assert help_result.returncode == 0
    assert "--output" in help_result.stdout
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(_config(tmp_path / "cli")), encoding="utf-8")
    run_result = subprocess.run(
        [sys.executable, str(module), "--config", str(config_path), "--output", str(tmp_path / "cli-receipt")],
        capture_output=True, text=True, check=False,
    )
    assert run_result.returncode == 0, run_result.stderr
    assert (tmp_path / "cli-receipt/journey-result.json").is_file()
