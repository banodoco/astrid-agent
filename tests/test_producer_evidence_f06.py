from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

# The run action intentionally refuses an ordinary direct import.
os.environ["ASTRID_INTERNAL_INVOCATION"] = "1"

from astrid.core.contracts.managed_generation_result import (  # noqa: E402
    ManagedGenerationResultError,
    validate_managed_generation_result,
)
from astrid.packs.vibecomfy.production_engine import ProductionRunResult  # noqa: E402


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
