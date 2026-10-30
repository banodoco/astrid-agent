"""Contract tests for the provisional M4-4b fully-local journey."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.journey_fully_local import (
    DeterministicFakeAdapter,
    ReceiptValidationError,
    run_journey,
    validate_receipt,
)


def _receipt() -> dict:
    return run_journey()


def _mutated(**changes) -> dict:
    receipt = _receipt()
    for path, value in changes.items():
        cursor = receipt
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor[int(part)] if part.isdigit() else cursor[part]
        last = parts[-1]
        cursor[int(last) if last.isdigit() else last] = value
    return receipt


def test_fake_success_proves_route_failure_restore_and_local_readiness() -> None:
    receipt = _receipt()

    assert [event["name"] for event in receipt["route"]["events"]][7:12] == [
        "ref_build_boundary", "temporary_runtime_healthy", "editor_build",
        "temporary_runtime_down", "restore_editor_checkout",
    ]
    assert receipt["failure"]["exit_status"] != 0
    assert receipt["checkout"]["restoration_source"] == "fake_updater_restore"
    assert receipt["mode"]["before_bytes"] == receipt["mode"]["after_bytes"]
    assert receipt["readiness"]["ready"]["hosted_door"] is False


def test_adapter_evidence_must_match_receipt() -> None:
    class Mislabelled(DeterministicFakeAdapter):
        evidence_class = "linux_receipt"

    with pytest.raises(ReceiptValidationError, match="evidence class"):
        run_journey(Mislabelled())


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        ("route.backend", True, "backend"),
        ("route.foreground_service", True, "foreground"),
        ("failure.exit_status", 0, "nonzero"),
        ("mode.after_bytes", '{"schema_version":1,"mode":"backend"}', "mode"),
        ("cleanup.lock.removed", True, "deletion"),
        ("readiness.ready.hosted_door", True, "local"),
    ],
)
def test_unsafe_routes_and_changed_invariants_fail_closed(path: str, value, message: str) -> None:
    with pytest.raises(ReceiptValidationError, match=message):
        validate_receipt(_mutated(**{path: value}))


def test_premature_ready_and_missing_checkout_field_fail_closed() -> None:
    receipt = _receipt()
    receipt["route"]["events"][1]["ready"] = True
    with pytest.raises(ReceiptValidationError, match="ready"):
        validate_receipt(receipt)

    receipt = _receipt()
    del receipt["checkout"]["after"]["origin"]
    with pytest.raises(ReceiptValidationError, match="checkout.after.origin"):
        validate_receipt(receipt)


def test_source_fake_evidence_can_never_pass_linux_gate() -> None:
    with pytest.raises(ReceiptValidationError, match="Linux receipt gate"):
        run_journey(require_linux=True)


def test_fake_adapter_does_not_mutate_agent_checkout() -> None:
    checkout = Path(__file__).resolve().parents[1]
    before = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True, capture_output=True,
    ).stdout
    receipt = run_journey(DeterministicFakeAdapter())
    after = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        check=True, capture_output=True,
    ).stdout

    assert receipt["evidence_class"] == "source_fake"
    assert before == after


def test_cli_is_non_destructive_by_default(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"
    result = subprocess.run(
        [sys.executable, "scripts/journey_fully_local.py", "--output", str(output)],
        check=True, capture_output=True, text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.stdout == ""
    assert '"evidence_class": "source_fake"' in output.read_text(encoding="utf-8")
