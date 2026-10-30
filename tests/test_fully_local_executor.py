"""Runnable disposable execution tests for the M4-4b local boundary."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.fully_local_executor import (
    DISPOSABLE_MARKER,
    MODE_BYTES,
    ExecutionError,
    _ensure_runtime_layout,
    run_execution,
    validate_execution_receipt,
)
from scripts.journey_fully_local import (
    DeterministicFakeAdapter,
    ReceiptValidationError,
    run_journey,
)

ROOT = Path(__file__).resolve().parents[1]


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(path), *args], check=check,
                          capture_output=True, text=True)


def _disposable_fixture(tmp_path: Path) -> tuple[Path, Path, str, bytes]:
    checkout = tmp_path / "editor-checkout"
    remote = tmp_path / "editor-origin.git"
    state = tmp_path / "runtime-state"
    checkout.mkdir()
    state.mkdir()
    (checkout / DISPOSABLE_MARKER).write_text("m4-4b-disposable\n", encoding="utf-8")
    (checkout / "editor.txt").write_text("prior editor\n", encoding="utf-8")
    _git(checkout, "init", "-q", "--initial-branch=main")
    _git(checkout, "config", "user.name", "Executor Test")
    _git(checkout, "config", "user.email", "executor@example.invalid")
    _git(checkout, "add", "--all")
    _git(checkout, "commit", "-qm", "baseline")
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    _git(checkout, "remote", "add", "origin", str(remote))
    (state / DISPOSABLE_MARKER).write_text("m4-4b-disposable\n", encoding="utf-8")
    install, mode, data = _ensure_runtime_layout(state)
    return checkout, state, _git(checkout, "rev-parse", "HEAD").stdout.strip(), mode.read_bytes()


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


LOCAL_PORTS_AVAILABLE = _port_free(17340) and _port_free(5180)


def test_end_to_end_spawns_build_and_readiness_processes_and_restores(tmp_path: Path) -> None:
    checkout, state, baseline_head, mode_before = _disposable_fixture(tmp_path)
    install_before = (state / "runtime" / "install.json").read_bytes()
    output = tmp_path / "receipt.json"

    if not LOCAL_PORTS_AVAILABLE:
        with pytest.raises(ExecutionError, match="occupied"):
            run_execution(checkout=checkout, state_root=state, output=output)
        assert not output.exists()
        return

    receipt = run_execution(checkout=checkout, state_root=state, output=output)

    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8")) == receipt
    assert receipt["evidence_class"] == "local_process"
    assert receipt["hosted_linux_receipt_required"] is True
    assert receipt["process_evidence"]["real_subprocesses"] is True
    assert receipt["process_evidence"]["build_pid"] > 0
    assert receipt["process_evidence"]["temporary_runtime_pid"] > 0
    assert receipt["process_evidence"]["readiness_pid"] > 0
    assert receipt["failure"]["exit_status"] == 23
    assert "editor build spawned" in receipt["failure"]["stdout"]
    assert "ASTRID_PINOKIO_FAILURE" in receipt["failure"]["stderr"]
    assert receipt["checkout"]["before"]["head"] == baseline_head
    assert receipt["checkout"]["after"] == receipt["checkout"]["before"]
    assert (state / "runtime" / "install.json").read_bytes() == install_before
    assert (state / "runtime" / "install-mode.json").read_bytes() == mode_before == MODE_BYTES.encode()
    assert receipt["cleanup"]["lock"]["removed"] is False
    assert receipt["readiness"]["ready"]["hosted_door"] is False
    assert receipt["readiness"]["controlled_stop"]["status"] == "stopped"
    assert _git(checkout, "status", "--porcelain=v1", "--untracked-files=all").stdout == ""
    assert _git(checkout, "rev-parse", "HEAD").stdout.strip() == baseline_head
    assert _port_free(17340)
    assert _port_free(5180)


def test_cli_is_explicit_and_emits_machine_receipt(tmp_path: Path) -> None:
    checkout, state, _head, _mode = _disposable_fixture(tmp_path)
    output = tmp_path / "cli-receipt.json"
    result = subprocess.run(
        [sys.executable, "scripts/fully_local_executor.py", "--checkout", str(checkout),
         "--state-root", str(state), "--output", str(output)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    if LOCAL_PORTS_AVAILABLE:
        assert result.returncode == 0, result.stderr
        validate_execution_receipt(json.loads(output.read_text(encoding="utf-8")))
    else:
        assert result.returncode == 1
        assert "occupied" in result.stderr
        assert not output.exists()


def test_dirty_or_unmarked_inputs_fail_closed(tmp_path: Path) -> None:
    checkout, state, _head, _mode = _disposable_fixture(tmp_path)
    (checkout / "dirty.txt").write_text("must refuse\n", encoding="utf-8")
    with pytest.raises(ExecutionError, match="clean"):
        run_execution(checkout=checkout, state_root=state)

    (checkout / "dirty.txt").unlink()
    (state / DISPOSABLE_MARKER).unlink()
    with pytest.raises(ExecutionError, match="explicitly disposable"):
        run_execution(checkout=checkout, state_root=state)


def test_source_fake_only_evidence_is_rejected() -> None:
    with pytest.raises(ReceiptValidationError, match="local_process"):
        validate_execution_receipt(run_journey(DeterministicFakeAdapter()))


@pytest.mark.skipif(
    not LOCAL_PORTS_AVAILABLE,
    reason="the strict local route ports are occupied by an existing process",
)
def _validator_fixture() -> dict:
    receipt = run_journey(DeterministicFakeAdapter())
    receipt["evidence_class"] = "local_process"
    receipt["platform"] = {"kind": "darwin", "linux_receipt": False, "hosted_linux_receipt": False}
    receipt["hosted_linux_receipt_required"] = True
    receipt["failure"]["stdout"] = "spawned build\n"
    receipt["failure"]["stderr"] = "ASTRID_PINOKIO_FAILURE=fixture\n"
    receipt["process_evidence"] = {
        "real_subprocesses": True, "build_pid": 1, "temporary_runtime_pid": 2,
        "readiness_pid": 3,
    }
    return receipt


def test_route_fallback_is_rejected_after_real_execution(tmp_path: Path) -> None:
    if LOCAL_PORTS_AVAILABLE:
        checkout, state, _head, _mode = _disposable_fixture(tmp_path)
        receipt = run_execution(checkout=checkout, state_root=state)
    else:
        receipt = _validator_fixture()
    receipt["route"]["backend"] = True
    with pytest.raises(ReceiptValidationError, match="backend"):
        validate_execution_receipt(receipt)


def test_local_receipt_cannot_be_smuggled_as_hosted_linux(tmp_path: Path) -> None:
    if LOCAL_PORTS_AVAILABLE:
        checkout, state, _head, _mode = _disposable_fixture(tmp_path)
        receipt = run_execution(checkout=checkout, state_root=state)
    else:
        receipt = _validator_fixture()
    receipt["platform"]["linux_receipt"] = True
    with pytest.raises(ReceiptValidationError, match="outside hosted Linux"):
        validate_execution_receipt(receipt)
