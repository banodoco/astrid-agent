"""The installed Pinokio mode is source-, slot-, candidate-, and session-bound."""
from __future__ import annotations

import json

import pytest

from scripts import check_journey_evidence as evidence_gate
from scripts import pinokio_install_mode_receipt as producer


CANDIDATE = {
    "app": "a" * 40,
    "agent": "b" * 40,
    "runtime": "c" * 40,
    "pinokio": "d" * 40,
    "manifest_sha256": "e" * 64,
}
SESSION = "f" * 32


@pytest.mark.parametrize(
    ("slot_mode", "slot_id", "installed"),
    [
        ("pinokio-backend", "pinokio-backend-linux", "backend"),
        ("pinokio-fully-local", "pinokio-fully-local-linux", "local"),
    ],
)
def test_producer_projection_is_accepted_by_typed_consumer(
    tmp_path, slot_mode, slot_id, installed
):
    path = (tmp_path / "install-mode.json").resolve()
    path.write_text(json.dumps({"schema_version": 1, "mode": installed}))
    binding = producer.build_binding(
        path, slot_id=slot_id, slot_mode=slot_mode,
        candidate=CANDIDATE, session=SESSION,
    )
    row = producer.project_journey_result(
        {
            "job_id": slot_id,
            "mode": slot_mode,
            "provider_session": SESSION,
            "candidate_checkouts": {
                name: {"sha": CANDIDATE[name]}
                for name in ("app", "agent", "runtime", "pinokio")
            },
        },
        binding,
    )
    assert evidence_gate.pinokio_install_mode_errors(
        {"job_id": slot_id, "mode": slot_mode}, row
    ) == []
    assert row["pinokio_install_mode_receipt"] == {
        "schema_version": 1, "mode": installed,
    }


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"schema_version":true,"mode":"backend"}',
        '{"schema_version":2,"mode":"backend"}',
        '{"schema_version":1,"mode":"other"}',
        '{"schema_version":1,"mode":"backend","extra":true}',
        '{"schema_version":1,"schema_version":1,"mode":"backend"}',
    ],
)
def test_installed_mode_parser_fails_closed(tmp_path, raw):
    path = (tmp_path / "install-mode.json").resolve()
    path.write_text(raw)
    with pytest.raises(producer.InstallModeReceiptError):
        producer.read_receipt(path, slot_mode="pinokio-backend")


def test_missing_symlink_and_cross_mode_are_rejected(tmp_path):
    missing = (tmp_path / "missing.json").resolve()
    with pytest.raises(producer.InstallModeReceiptError):
        producer.read_receipt(missing, slot_mode="pinokio-backend")
    target = (tmp_path / "target.json").resolve()
    target.write_text('{"schema_version":1,"mode":"backend"}')
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    with pytest.raises(producer.InstallModeReceiptError):
        producer.read_receipt(alias.absolute(), slot_mode="pinokio-backend")
    with pytest.raises(producer.InstallModeReceiptError, match="conflicts"):
        producer.read_receipt(target, slot_mode="pinokio-fully-local")


@pytest.mark.parametrize("mutation", ["session", "candidate", "existing"])
def test_projection_refuses_unbound_journey_rows(tmp_path, mutation):
    path = (tmp_path / "install-mode.json").resolve()
    path.write_text('{"schema_version":1,"mode":"backend"}')
    binding = producer.build_binding(
        path, slot_id="pinokio-backend-linux", slot_mode="pinokio-backend",
        candidate=CANDIDATE, session=SESSION,
    )
    row = {
        "job_id": "pinokio-backend-linux",
        "mode": "pinokio-backend",
        "provider_session": SESSION,
        "candidate_checkouts": {
            name: {"sha": CANDIDATE[name]}
            for name in ("app", "agent", "runtime", "pinokio")
        },
    }
    if mutation == "session":
        row["provider_session"] = "0" * 32
    elif mutation == "candidate":
        row["candidate_checkouts"]["agent"]["sha"] = "0" * 40
    else:
        row["pinokio_install_mode_receipt"] = {"schema_version": 1, "mode": "backend"}
    with pytest.raises(producer.InstallModeReceiptError):
        producer.project_journey_result(row, binding)
