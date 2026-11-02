import copy
import json

import pytest

from scripts import check_journey_evidence as gate
from scripts.journey_matrix import GPU_MATRIX, emit_gpu_receipt


def sample(contract):
    candidate = {key: ch * 40 for key, ch in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    candidate["manifest_sha256"] = "e" * 64
    manifest = {"agent": {"sha": candidate["agent"]},
                "runtime": {"sha": candidate["runtime"], "schema_digest": "sha256:schema"},
                "tools": {"uv": {"version": "1"}}}
    checks = {key: "pass" for key in (*contract["checks"], "task_settled", "workspace_content", "one_runtime", "unrelated_sentinel")}
    row = {"schema_version": 2, "receipt_kind": "gpu-installed-journey", "scenario": contract["scenario_id"],
           "mode": contract["mode"], "status": "pass", "runner_image": "runpod", "shell": "sh",
           "runner_provenance": {"RUNNER_OS": "Linux", "ImageOS": "runpod", "ImageVersion": "image@sha256:observed"},
           "os_build": "Linux-6.8", "source_override_test_mode": False, "final_cleanup": "pass",
           "manifest_sha256": candidate["manifest_sha256"],
           "candidate_checkouts": {k: {"sha": candidate[k]} for k in ("app", "agent", "runtime", "pinokio")},
           "sources": {"app": {"sha": candidate["app"]}, "agent": manifest["agent"], "runtime": manifest["runtime"]},
           "installed_tuple": {**{k: candidate[k] for k in ("app", "agent", "runtime")},
                               "schema_digest": manifest["runtime"]["schema_digest"], "tools": {"uv": "1"}},
           "import_origins": {"runtime": "/installed/runtime"}, "checks": checks,
           "inner_exits": {name: 0 for name in contract["inner_exits"]}}
    return candidate, manifest, row


def test_complete_matrix_and_each_missing_field(tmp_path):
    candidate, manifest, _ = sample(GPU_MATRIX[0])
    for contract in GPU_MATRIX:
        _, _, row = sample(contract)
        path = tmp_path / contract["scenario_id"] / "journey-result.json"
        path.parent.mkdir()
        path.write_text(json.dumps(row))
    assert gate.evaluate_gpu_matrix(candidate, manifest, tmp_path) == []
    for contract in GPU_MATRIX:
        path = tmp_path / contract["scenario_id"] / "journey-result.json"
        original = json.loads(path.read_text())
        def leaves(obj, prefix=()):
            for key, value in obj.items():
                if isinstance(value, dict) and value:
                    yield from leaves(value, prefix + (key,))
                else:
                    yield prefix + (key,)
        for keys in leaves(original):
            bad = copy.deepcopy(original)
            parent = bad
            for key in keys[:-1]:
                parent = parent[key]
            del parent[keys[-1]]
            path.write_text(json.dumps(bad))
            assert gate.evaluate_gpu_matrix(candidate, manifest, tmp_path), (contract["scenario_id"], keys)
        path.write_text(json.dumps(original))
    path.unlink()
    assert gate.evaluate_gpu_matrix(candidate, manifest, tmp_path)


def test_duplicates_and_bad_exits_rejected(tmp_path):
    contract = GPU_MATRIX[0]
    candidate, manifest, row = sample(contract)
    path = tmp_path / "journey-result.json"
    path.write_text(json.dumps(row))
    assert not gate.evaluate_gpu_matrix(candidate, manifest, tmp_path, ["R1"])
    other = tmp_path / "dup" / "journey-result.json"
    other.parent.mkdir()
    other.write_text(json.dumps(row))
    assert any("duplicate" in e for e in gate.evaluate_gpu_matrix(candidate, manifest, tmp_path, ["R1"]))
    other.unlink()
    path.write_text(json.dumps(row)[:-1] + ', "status": "pass"}')
    assert any("duplicate JSON field" in e for e in gate.evaluate_gpu_matrix(candidate, manifest, tmp_path, ["R1"]))
    row["inner_exits"]["install"] = False
    path.write_text(json.dumps(row))
    assert any("inner exit" in e for e in gate.evaluate_gpu_matrix(candidate, manifest, tmp_path, ["R1"]))


def test_emitter_copies_capture_and_keeps_unrecorded_fields_missing(tmp_path):
    contract = GPU_MATRIX[0]
    candidate, manifest, row = sample(contract)
    (tmp_path / "journey-capture.json").write_text(json.dumps(row))
    emitted = emit_gpu_receipt(tmp_path, "R1")
    (tmp_path / "journey-result.json").write_text(json.dumps(emitted))
    assert not gate.evaluate_gpu_matrix(candidate, manifest, tmp_path, ["R1"])
    with pytest.raises(ValueError):
        emit_gpu_receipt(tmp_path, "R2")
    (tmp_path / "journey-capture.json").unlink()
    empty = emit_gpu_receipt(tmp_path, "R1")
    assert empty["manifest_sha256"] is None and empty["installed_tuple"] is None
    (tmp_path / "journey-result.json").write_text(json.dumps(empty))
    assert gate.evaluate_gpu_matrix(candidate, manifest, tmp_path, ["R1"])


def test_historical_replay_digest_and_row_selection(tmp_path):
    import hashlib
    out = tmp_path / "out"
    receipt = out / "fixed" / "R1-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"row": "R1", "VERDICT": "PASS", "exit_code": 0,
                                   "manifest_sha256": "a" * 64}))
    index = {"fixed": [{"row": "R1", "receipt": "out/fixed/R1-receipt.json",
                        "receipt_sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}]}
    (out / "rows-final.json").write_text(json.dumps(index))
    row = emit_gpu_receipt(out, "R1", variant="fixed")
    assert row["status"] == "pass" and row["manifest_sha256"] == "a" * 64
    assert row["installed_tuple"] is None
    with pytest.raises(ValueError):
        emit_gpu_receipt(out, "R1")
    receipt.write_text('{}')
    with pytest.raises(ValueError, match="digest"):
        emit_gpu_receipt(out, "R1", variant="fixed")


def test_historical_shakedown_selection_and_duplicates(tmp_path):
    for suffix in ("a", "b"):
        (tmp_path / f"R4-{suffix}-receipt.json").write_text(
            json.dumps({"row": "R4-512", "VERDICT": "FAIL", "exit_code": 2}))
    with pytest.raises(ValueError, match="multiple"):
        emit_gpu_receipt(tmp_path, "R4")
    row = emit_gpu_receipt(tmp_path, "R4", variant="R4-a-receipt.json")
    assert row["status"] == "fail" and row["inner_exits"] == {"row": 2}
    (tmp_path / "journey-capture.json").write_text('{"scenario":"R4","status":"pass","status":"fail"}')
    with pytest.raises(ValueError, match="duplicate JSON field"):
        emit_gpu_receipt(tmp_path, "R4")
