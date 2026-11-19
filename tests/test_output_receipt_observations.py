"""Local receipt observations cannot authorize changed or foreign output bytes."""
import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.foundation.output_receipts import (
    OBSERVATIONS_NAME, bind_output_receipts, content_receipt, make_output_receipt,
    output_digest, write_output_observations,
)
from tests._local_speed_hashes import OutputHashObserver


def receipt_fixture(tmp_path):
    path = tmp_path / "output.bin"
    data = b"attributed output bytes"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    receipt = make_output_receipt(path, digest, len(data), prompt_id="prompt-1",
                                  descriptor={"filename": path.name})
    payload = {"task_id": "task-1", "attempt_id": "attempt-1", "producer_run_id": "run-1",
               "evidence": {"producer": {"output_receipts": {
                   path.name: content_receipt(receipt, path.name)}}}}
    write_output_observations(tmp_path, payload, {path.name: receipt})
    return path, data, digest, receipt, payload


def test_stable_content_receipt_reuses_only_the_current_local_observation(tmp_path, monkeypatch):
    path, data, digest, receipt, payload = receipt_fixture(tmp_path)
    observer = OutputHashObserver({digest: len(data)}).install(monkeypatch)
    bound = bind_output_receipts(payload, tmp_path)
    assert bound == {path.name: receipt}
    assert output_digest(path, bound[path.name]) == digest
    assert observer.snapshot().completed_streams == 0
    assert payload["evidence"]["producer"]["output_receipts"][path.name]["file_identity"] == {
        "sha256": digest, "bytes": len(data), "path": path.name,
    }


@pytest.mark.parametrize("change", ["missing", "malformed", "task", "prompt", "logical-path", "symlink"])
def test_unusable_sidecar_keeps_independent_output_hash(tmp_path, monkeypatch, change):
    path, data, digest, receipt, payload = receipt_fixture(tmp_path)
    sidecar = tmp_path / OBSERVATIONS_NAME
    record = json.loads(sidecar.read_bytes())
    if change == "missing":
        sidecar.unlink()
    elif change == "malformed":
        sidecar.write_text("not-json")
    elif change == "task":
        record["task_id"] = "foreign-task"
        sidecar.write_text(json.dumps(record))
    elif change == "prompt":
        record["output_receipts"][path.name]["prompt_id"] = "foreign-prompt"
        sidecar.write_text(json.dumps(record))
    elif change == "logical-path":
        payload["evidence"]["producer"]["output_receipts"][path.name]["file_identity"]["path"] = "other.bin"
    else:
        target = tmp_path / "other-observations.json"
        sidecar.rename(target)
        sidecar.symlink_to(target)
    observer = OutputHashObserver({digest: len(data)}).install(monkeypatch)
    bound = bind_output_receipts(payload, tmp_path)
    assert bound == {}
    assert output_digest(path, bound.get(path.name)) == digest
    assert observer.snapshot().completed_streams == 1
    assert observer.snapshot().bytes_hashed == len(data)
    assert not observer.snapshot().coverage_gaps


def test_same_length_change_cannot_rebind_an_old_observation(tmp_path):
    path, data, digest, receipt, payload = receipt_fixture(tmp_path)
    path.write_bytes(b"x" * len(data))
    bound = bind_output_receipts(payload, tmp_path)
    assert bound == {}
    assert output_digest(path, bound.get(path.name)) != digest
