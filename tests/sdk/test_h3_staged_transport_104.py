"""Bounded staged child transport carries ordered references without authority."""
from types import SimpleNamespace

import pytest

from astrid.core.execution._child_bridge import ChildBridgeError, _staged_child_task
from astrid.sdk._child_bridge import _child_request, _validate_output_page
from astrid.sdk.exceptions import CapabilityValidationError

OID = "sha256:" + "a" * 64
PIN = "sha256:" + "b" * 64


def test_sdk_carries_stage_and_ordered_refs_and_binds_child_key():
    cap = SimpleNamespace(id="pack.child", inputs=[SimpleNamespace(name="source", type="file")])
    inputs = {"source": {"object_id": OID, "filename": "source.mp4"}}
    ref = {"name": "source", "producer_task_id": "task", "association_id": "assoc", "output_port": "video"}
    kwargs = dict(digest=PIN, child_key=None, wait=True, timeout_seconds=10, poll_seconds=1)
    first = _child_request(cap, inputs, stage="compose", input_refs=[ref], **kwargs)
    second = _child_request(cap, inputs, stage="compose", input_refs=[{**ref, "association_id": "other"}], **kwargs)
    assert first["stage"] == "compose" and first["input_refs"] == [ref]
    assert first["child_key"] != second["child_key"]
    with pytest.raises(CapabilityValidationError):
        _child_request(cap, inputs, stage="compose", **kwargs)


def test_finalizer_keeps_required_publication_intent_in_host_custody():
    from pathlib import Path

    import astrid
    from astrid.core.contracts.binding import assert_provided_inputs_bound, expand_command
    from astrid.core.execution.executor.actions import action_executor_definition
    from astrid.core.pack.loader import load_pack_manifest
    from astrid.packs.h3_av.actions.publication_finalizer.run import build_parser

    pack = load_pack_manifest(Path(astrid.__file__).parent / "packs/h3_av/pack.yaml")
    definition = action_executor_definition(pack, "publication_finalizer", pack.actions["publication_finalizer"])
    intent = {"version": 1, "modality": "video", "metadata": {"sealed": True}}
    values = {"python_exec": "python", "out": "/worker/out", "candidate": "/worker/candidate.media",
              "verification": "/worker/verification.json", "expected_request_digest": "a" * 64,
              "generation_intent": intent}
    binding = expand_command(definition.command, definition.inputs, values, definition.metadata)
    assert_provided_inputs_bound(binding, definition.inputs, values, definition.metadata)
    port = next(port for port in definition.inputs if port.name == "generation_intent")
    assert port.required and port.type == "json"
    assert binding.host_consumed_inputs == {"generation_intent"}
    assert "--generation-intent" not in binding.argv
    assert values["generation_intent"] == intent
    parsed = build_parser().parse_args(binding.argv[3:])
    assert parsed.candidate == Path(values["candidate"])
    assert parsed.verification == Path(values["verification"])
    assert parsed.expected_request_digest == "a" * 64


def test_staged_runtime_transport_preserves_order_and_ref_identity():
    child = {"child_id": "key", "capability_id": "pack.child", "capability_digest": PIN}
    refs = [{"name": "source", "producer_task_id": "task", "association_id": "assoc", "output_port": "video"}]
    ordered = [{"name": "source", "object_id": OID, "filename": "source.mp4", "required": True}]
    task = _staged_child_task(child=child, inputs={"source": {"object_id": OID}}, ordered_inputs=ordered, stage="compose", input_refs=refs)
    assert task["input_refs"] == refs
    assert task["spec"]["inputs"]["source"] == {"object_id": OID, "filename": "source.mp4", "required": True}
    with pytest.raises(ChildBridgeError):
        _staged_child_task(child=child, inputs={}, ordered_inputs=ordered, stage="compose", input_refs=[{**refs[0], "association_id": ""}])
    with pytest.raises(ChildBridgeError):
        _staged_child_task(child=child, inputs={}, ordered_inputs=ordered, stage="compose", input_refs=[{**refs[0], "name": "other"}])


def test_verified_output_projection_keeps_selector_fields():
    row = {"association_id": "assoc", "run_id": "run", "task_id": "task", "attempt_id": "attempt",
           "object_id": OID, "digest": OID, "size": 1, "filename": "output.mp4",
           "media_type": "video/mp4", "output_port": "output", "ordinal": 0,
           "group_key": "main", "variant_key": "original"}
    assert _validate_output_page([row], task_id="task", run_id="run", attempt_id="attempt") == [row]
    with pytest.raises(ValueError):
        _validate_output_page([{**row, "attempt_id": "stale"}], task_id="task", run_id="run", attempt_id="attempt")
    with pytest.raises(ValueError):
        _validate_output_page([row, row], task_id="task")


def test_compose_evidence_must_equal_verified_associations():
    import copy
    import json
    from astrid.core.execution._child_bridge import _validate_compose_evidence
    outputs = {}
    refs = []
    rows = {}
    for role, name in (("video", "generated"), ("audio", "generated_audio")):
        row = {"association_id": role, "task_id": "task", "run_id": "run", "attempt_id": "attempt",
               "object_id": OID, "digest": OID, "size": 1, "filename": role, "media_type": role + "/test",
               "output_port": "shared", "ordinal": 0, "producer": {"output_port": role, "producer_output_id": role}}
        outputs[("task", role)] = row
        refs.append({"name": name, "producer_task_id": "task", "association_id": role, "output_port": "shared"})
        rows[role] = row
    evidence = {"run_id": "run", "task_id": "task", "attempt_id": "attempt", "output_contract": "separate_av_full_timeline", "generated_outputs": rows}
    _validate_compose_evidence({"generation_provenance": json.dumps(evidence)}, refs, outputs)
    for field, value in (("association_id", "forged"), ("attempt_id", "stale"), ("producer", {"output_port": "audio", "producer_output_id": "forged"})):
        altered = copy.deepcopy(evidence)
        altered["generated_outputs"]["audio"][field] = value
        with pytest.raises(ChildBridgeError, match="host-verified"):
            _validate_compose_evidence({"generation_provenance": json.dumps(altered)}, refs, outputs)
