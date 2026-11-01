from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import pytest

from astrid.core.contracts.managed_generation_result import (  # noqa: E402
    ManagedGenerationResultError,
    validate_managed_generation_result,
)
from astrid.packs.vibecomfy.production_engine import ProductionRunResult  # noqa: E402


@pytest.fixture(autouse=True)
def _internal_invocation(monkeypatch):
    monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _evidence_reference(root: Path, relative_path: str, filename: str, data: bytes) -> dict[str, Any]:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {
        "path": relative_path,
        "sha256": _sha256(data),
        "bytes": len(data),
        "filename": filename,
    }


def _managed_payload(root: Path, *, execution_status: str = "succeeded") -> dict[str, Any]:
    media = b"partial-or-complete-media"
    output_path = root / "outputs" / "final.mp4"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(media)
    source = _evidence_reference(root, "evidence/source.bin", "submitted.bin", b"\x00source\xff")
    settings = _evidence_reference(root, "evidence/settings.json", "settings.json", b'{"seed":7}\n')
    return {
        "schema_version": 1,
        "kind": "managed-generation-result.v1",
        "inputs": {
            "prompt": "evidence test",
            "source": source,
            "effective_settings": settings,
            "values": {"seed": 7},
        },
        "outputs": [{
            "producer_output_id": "engine:video:0",
            "output_port": "video",
            "ordinal": 0,
            "path": "outputs/final.mp4",
            "media_type": "video/mp4",
            "bytes": len(media),
            "sha256": _sha256(media),
        }],
        "created": "2026-09-21T12:00:00Z",
        "warnings": [],
        "task_id": "task-1",
        "attempt_id": "attempt-1",
        "producer_run_id": "producer-1",
        "outcomes": {
            "execution": {"status": execution_status},
            "retrieval": {"status": "succeeded"},
            "verification": {"status": "succeeded"},
            "publication": {"status": "not_started"},
        },
        "evidence": {
            "producer": {"engine": {"name": "test-engine"}},
            "transport": {"location": {"opaque": True}},
        },
    }


def _write_production(root: Path, payload: dict[str, Any]) -> ProductionRunResult:
    envelope = root / "managed-generation-result.json"
    envelope.write_text(json.dumps(payload), encoding="utf-8")
    return ProductionRunResult(
        outputs=(root / "outputs" / "final.mp4",),
        managed_generation_result_path=envelope,
        managed_generation_result=payload,
    )


def test_contract_accepts_local_and_portable_source_and_settings_references(tmp_path: Path) -> None:
    payload = _managed_payload(tmp_path)
    validated = validate_managed_generation_result(payload, staging_root=tmp_path)
    assert validated.inputs["source"]["sha256"] == _sha256(b"\x00source\xff")
    assert validated.inputs["source"]["bytes"] == len(b"\x00source\xff")
    assert validated.inputs["source"]["filename"] == "submitted.bin"
    assert validated.inputs["effective_settings"]["filename"] == "settings.json"

    portable = json.loads(json.dumps(payload))
    portable["inputs"]["source"].pop("path")
    portable["inputs"]["effective_settings"].pop("path")
    portable_validated = validate_managed_generation_result(portable, staging_root=tmp_path)
    assert "path" not in portable_validated.inputs["source"]
    assert "path" not in portable_validated.inputs["effective_settings"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda ref: ref.update({"sha256": "A" * 64}),
        lambda ref: ref.update({"bytes": True}),
        lambda ref: ref.update({"filename": "nested/source.bin"}),
    ],
)
def test_contract_rejects_invalid_evidence_reference_fields(tmp_path: Path, mutation) -> None:
    payload = _managed_payload(tmp_path)
    mutation(payload["inputs"]["source"])
    with pytest.raises(ManagedGenerationResultError):
        validate_managed_generation_result(payload, staging_root=tmp_path)


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_contract_allows_verified_partial_outputs_without_publication(tmp_path: Path, status: str) -> None:
    payload = _managed_payload(tmp_path, execution_status=status)
    result = validate_managed_generation_result(payload, staging_root=tmp_path)
    assert len(result.outputs) == 1
    assert result.outcomes["retrieval"].status == "succeeded"
    assert result.outcomes["verification"].status == "succeeded"
    assert result.outcomes["publication"].status == "not_started"


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_contract_rejects_partial_outputs_without_verified_retrieval_or_no_publication(
    tmp_path: Path, status: str
) -> None:
    for phase, invalid_status in (("retrieval", "failed"), ("verification", "failed"), ("publication", "succeeded")):
        payload = _managed_payload(tmp_path, execution_status=status)
        payload["outcomes"][phase]["status"] = invalid_status
        with pytest.raises(ManagedGenerationResultError, match="partial outputs"):
            validate_managed_generation_result(payload, staging_root=tmp_path)


@pytest.mark.parametrize("created", ["2026-09-21T12:00:00", "2026-09-21T12:00:00+01:00", "not-a-time"])
def test_contract_requires_created_timestamp_in_utc(tmp_path: Path, created: str) -> None:
    payload = _managed_payload(tmp_path)
    payload["created"] = created
    with pytest.raises(ManagedGenerationResultError, match="created must be a UTC timestamp"):
        validate_managed_generation_result(payload, staging_root=tmp_path)


def test_materializer_copies_exact_evidence_bytes_and_keeps_portable_identity(tmp_path: Path) -> None:
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    producer_root = tmp_path / "producer"
    producer_root.mkdir()
    payload = _managed_payload(producer_root)
    production = _write_production(producer_root, payload)

    manifest = run._materialize_managed_generation_result(
        tmp_path / "host-spool",
        production,
        task_identity="task-1",
        attempt_identity="attempt-1",
        workflow_path=tmp_path / "workflow.json",
        producer_context={"values": {"context_value": "kept"}, "target": "test-target"},
    )

    rebased = manifest["managed_generation_result"]
    for key, expected in (("source", b"\x00source\xff"), ("effective_settings", b'{"seed":7}\n')):
        reference = rebased["inputs"][key]
        assert "path" not in reference
        assert reference == {
            "object_id": f"sha256:{_sha256(expected)}",
            "sha256": _sha256(expected),
            "bytes": len(expected),
            "filename": "submitted.bin" if key == "source" else "settings.json",
        }
        copied = tmp_path / "host-spool" / "outputs" / f"evidence-{key}-{reference['filename']}"
        assert copied.read_bytes() == expected
    # Existing producer values are retained as the authoritative values map.
    assert rebased["inputs"]["values"] == {"seed": 7}
    assert rebased["evidence"]["producer"]["engine"] == {"name": "test-engine"}
    assert rebased["evidence"]["transport"]["target"] == "test-target"
    output = next(item for item in manifest["outputs"] if item.get("role") == "result")
    assert output["producer"]["producer_output_id"] == "engine:video:0"
    assert output["producer"]["producer_run_id"] == "producer-1"


@pytest.mark.parametrize("status", ["failed", "cancelled", "interrupted"])
def test_materializer_retains_verified_partial_media_on_terminal_failure(tmp_path: Path, status: str) -> None:
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    producer_root = tmp_path / status
    producer_root.mkdir()
    payload = _managed_payload(producer_root, execution_status=status)
    production = _write_production(producer_root, payload)

    manifest = run._materialize_managed_generation_result(
        tmp_path / f"host-spool-{status}",
        production,
        task_identity="task-1",
        attempt_identity="attempt-1",
        workflow_path=tmp_path / "workflow.json",
    )

    output = next(item for item in manifest["outputs"] if item.get("role") == "result")
    assert (tmp_path / f"host-spool-{status}" / output["path"]).read_bytes() == b"partial-or-complete-media"
    assert output["is_primary"] is False
    assert manifest["managed_generation_result"]["outcomes"]["publication"]["status"] == "not_started"


@pytest.mark.parametrize("error_type,status", [(RuntimeError, "failed"), (KeyboardInterrupt, "cancelled")])
def test_run_failure_retains_original_source_before_conversion_and_preserves_error(tmp_path, monkeypatch, error_type, status):
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    from astrid.core.generation.backends import registry

    workflow = tmp_path / "submitted.json"
    original = b'{ "exact": true }\r\n'
    workflow.write_bytes(original)
    error = error_type("producer stopped")

    def execute(*args, **kwargs):
        workflow.write_bytes(b"converted bytes")
        raise error

    adapter = SimpleNamespace(workflow=execute)
    descriptor = SimpleNamespace(create=lambda **kwargs: adapter,
                                 placement=SimpleNamespace(resolve=lambda placement: "local"))
    monkeypatch.setattr(registry, "resolve_workflow_descriptor", lambda *args, **kwargs: descriptor)
    spool = tmp_path / "spool"
    with pytest.raises(error_type) as raised:
        run._run_and_settle(workflow, spool, task_identity="task-1", attempt_identity="attempt-1", registry=object())
    assert raised.value is error
    manifest = json.loads((spool / "manifest.json").read_text())
    managed = manifest["managed_generation_result"]
    assert managed["outcomes"]["execution"]["status"] == status
    assert managed["inputs"]["source"]["sha256"] == _sha256(original)
    source_output = next(row for row in manifest["outputs"] if row["content_hash"] == "sha256:" + _sha256(original))
    assert (spool / source_output["path"]).read_bytes() == original
    if status == "cancelled":
        assert managed["evidence"]["transport"]["cancel"]["stop_observed"] is None


def test_model_identity_never_upgrades_an_observed_or_mismatched_digest(tmp_path):
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    producer = tmp_path / "producer"
    producer.mkdir()
    payload = _managed_payload(producer)
    payload["evidence"]["producer"]["models"] = [
        {"name": "lora", "recorded_sha256": None, "observed_sha256": "1" * 64, "identity_status": "verified"},
        {"name": "vae", "recorded_sha256": "2" * 64, "observed_sha256": "3" * 64, "identity_status": "verified"},
        {"name": "encoder", "recorded_sha256": "4" * 64, "observed_sha256": "4" * 64},
    ]
    manifest = run._materialize_managed_generation_result(
        tmp_path / "spool", _write_production(producer, payload),
        task_identity="task-1", attempt_identity="attempt-1", workflow_path=tmp_path / "unused.json",
    )
    assert [row["identity_status"] for row in manifest["managed_generation_result"]["evidence"]["producer"]["models"]] == [
        "observed", "observed", "verified",
    ]
    assert any("vae" in warning for warning in manifest["warnings"])


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_returned_terminal_producer_result_never_becomes_successful_settlement(tmp_path, monkeypatch, status):
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.backends import registry

    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    producer = tmp_path / "producer"
    producer.mkdir()
    payload = _managed_payload(producer, execution_status=status)
    production = _write_production(producer, payload)
    adapter = SimpleNamespace(workflow=lambda *args, **kwargs: production)
    descriptor = SimpleNamespace(create=lambda **kwargs: adapter,
                                 placement=SimpleNamespace(resolve=lambda placement: "local"))
    monkeypatch.setattr(registry, "resolve_workflow_descriptor", lambda *args, **kwargs: descriptor)
    spool = tmp_path / "spool"
    with pytest.raises(AstridError) as raised:
        run._run_and_settle(tmp_path / "absent.json", spool, task_identity="task-1",
                            attempt_identity="attempt-1", registry=object())
    assert raised.value.code == f"producer_{status}"
    manifest = json.loads((spool / "manifest.json").read_text())
    assert manifest["managed_generation_result"]["outcomes"]["execution"]["status"] == status


@pytest.mark.parametrize("field", ["source", "effective_settings"])
def test_changed_local_evidence_bytes_are_rejected(tmp_path, field):
    payload = _managed_payload(tmp_path)
    (tmp_path / payload["inputs"][field]["path"]).write_bytes(b"changed")
    with pytest.raises(ManagedGenerationResultError, match="hashes to"):
        validate_managed_generation_result(payload, staging_root=tmp_path)


def test_source_and_effective_settings_with_identical_bytes_share_one_artifact(tmp_path):
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    producer = tmp_path / "producer"
    producer.mkdir()
    payload = _managed_payload(producer)
    payload["inputs"]["effective_settings"] = dict(payload["inputs"]["source"])
    manifest = run._materialize_managed_generation_result(
        tmp_path / "spool", _write_production(producer, payload), task_identity="task-1",
        attempt_identity="attempt-1", workflow_path=tmp_path / "unused.json",
    )
    assert len(manifest["outputs"]) == 3
    assert len({row["content_hash"] for row in manifest["outputs"]}) == 3
    inputs = manifest["managed_generation_result"]["inputs"]
    assert inputs["source"]["object_id"] == inputs["effective_settings"]["object_id"]
