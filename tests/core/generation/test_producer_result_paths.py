"""Producer evidence must cross the strict harvesting boundary after staging."""
import importlib

import pytest

from astrid.core.generation import producer_result
from tests.test_producer_evidence_f06 import _evidence_reference, _managed_payload, _write_production


@pytest.mark.parametrize("workflow_name", ["submitted.json", "workflow.py"])
@pytest.mark.parametrize("run_directory", [".", ".vibecomfy-runtime/out/runs/native-1"])
def test_harvest_retained_source_from_outside_producer_root(tmp_path, monkeypatch, workflow_name, run_directory):
    submitted = tmp_path / "entry"
    submitted.mkdir()
    workflow = submitted / workflow_name
    source = workflow.with_name("source.json") if workflow.suffix == ".py" else workflow
    original = b'{ "prompt": "original source" }\r\n'
    source.write_bytes(original)
    if workflow.suffix == ".py":
        workflow.write_text("# converted workflow; original bytes live in source.json\n")
    destination = tmp_path / "producer"
    producer_root = destination / run_directory
    payload = _managed_payload(producer_root)
    payload["inputs"].pop("source")
    payload["inputs"].pop("effective_settings")
    payload["evidence"]["producer"]["runtime"] = {"approved_projection": {}}
    production = _write_production(producer_root, payload)
    retained = producer_result.source_input_ref(workflow, destination)
    source.unlink()
    # Compilation is outside this boundary; supply its exact bytes at the
    # existing approval seam without requiring VibeComfy on the CPU host.
    monkeypatch.setattr(producer_result, "_projection", lambda runtime: object())
    def stage_settings(record, root):
        return _evidence_reference(root, "effective-settings.json", "effective-settings.json", b'{"seed":7}\n')
    monkeypatch.setattr(producer_result, "stage_executed_settings", stage_settings)
    production = producer_result.complete_producer_result(
        production, destination, task_id="task-1", attempt_id="attempt-1", source=retained,
    )

    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    host_root = tmp_path / "host"
    manifest = run._materialize_managed_generation_result(
        host_root, production, task_identity="task-1", attempt_identity="attempt-1",
        workflow_path=workflow,
    )

    reference = manifest["managed_generation_result"]["inputs"]["source"]
    assert "path" not in reference
    assert reference["sha256"] == retained["sha256"]
    assert reference["bytes"] == len(original)
    assert reference["filename"] == source.name
    evidence = host_root / "outputs" / f"evidence-source-{source.name}"
    assert evidence.read_bytes() == original
    assert any(row["content_hash"] == reference["object_id"] for row in manifest["outputs"])


@pytest.mark.parametrize("run_directory", [".", ".vibecomfy-runtime/out/runs/native-1"])
def test_bind_settings_inside_native_receipt_root(tmp_path, run_directory):
    destination = tmp_path / "producer"
    producer_root = destination / run_directory
    payload = _managed_payload(producer_root)
    payload["inputs"].pop("effective_settings")
    production = _write_production(producer_root, payload)
    data = b'{"seed":7}\n'
    settings = _evidence_reference(destination, "effective-settings.json", "effective-settings.json", data)

    producer_result.bind_executed_settings(
        production, settings, task_id="task-1", attempt_id="attempt-1", staging_root=destination,
    )
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    host_root = tmp_path / "host"
    manifest = run._materialize_managed_generation_result(
        host_root, production, task_identity="task-1", attempt_identity="attempt-1",
        workflow_path=tmp_path / "absent.py",
    )
    reference = manifest["managed_generation_result"]["inputs"]["effective_settings"]
    assert "path" not in reference
    assert reference["sha256"] == settings["sha256"]
    assert (host_root / "outputs" / "evidence-effective_settings-effective-settings.json").read_bytes() == data


def test_after_outputs_replaces_the_output_before_its_digest_is_recorded(tmp_path):
    """The seam h3's composed candidate uses: after the outputs are copied, before they are sealed."""
    import hashlib
    destination = tmp_path / "producer"
    payload = _managed_payload(destination)
    production = _write_production(destination, payload)
    settings = _evidence_reference(destination, "effective-settings.json", "effective-settings.json", b'{"seed":7}\n')
    producer_result.bind_executed_settings(
        production, settings, task_id="task-1", attempt_id="attempt-1", staging_root=destination)
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    candidate = tmp_path / "candidate.bin"
    candidate.write_bytes(b"composed candidate")
    seen = []

    def after_outputs(paths):
        seen.append([path.read_bytes() for path in paths])
        return [candidate, *paths[1:]]

    host_root = tmp_path / "host"
    manifest = run._materialize_managed_generation_result(
        host_root, production, task_identity="task-1", attempt_identity="attempt-1",
        workflow_path=tmp_path / "absent.py", after_outputs=after_outputs)
    first = manifest["managed_generation_result"]["outputs"][0]
    assert seen and seen[0][0] != b"composed candidate"  # the hook saw the producer's file first
    assert (host_root / first["path"]).read_bytes() == b"composed candidate"
    assert first["sha256"] == hashlib.sha256(b"composed candidate").hexdigest()
    assert first["bytes"] == len(b"composed candidate")


def test_after_outputs_refuses_an_inventory_only_result(tmp_path):
    """No managed result to seal: a requested after_outputs hook fails closed instead of being skipped."""
    import json
    import pytest
    from astrid.core.contracts.errors import AstridValueError
    run = importlib.import_module("astrid.packs.vibecomfy.actions.run.run")
    spec = json.dumps({"entry": "h3", "job_dir": str(tmp_path), "inputs": {}, "files": {}})
    with pytest.raises(AstridValueError, match="after_outputs requires a managed generation result"):
        run._refuse_unsealed_hook(spec)
    run._refuse_unsealed_hook("{}")  # no hook requested: nothing to refuse
