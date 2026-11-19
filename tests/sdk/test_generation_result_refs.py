"""The synchronous projection keeps Runtime identities after spool cleanup."""
from types import SimpleNamespace

import pytest

from astrid.core.generation.backends.base import GenerationResult
from astrid.sdk.results import InvocationResult, _reconstruct_generation_result


def terminal(*, ok=True):
    source = {'digest': 'sha256:' + 'a' * 64, 'sha256': 'a' * 64,
              'bytes': 17, 'filename': 'original.py'}
    settings = {'object_id': 'sha256:' + 'b' * 64, 'sha256': 'b' * 64,
                'bytes': 23, 'filename': 'executed-api.json'}
    association = {'association_id': 'assoc-manifest', 'output_port': 'result_manifest',
                   'object_id': 'sha256:' + 'c' * 64, 'filename': 'managed-generation-result.json'}
    log_output = {'role': 'log', 'variant_key': 'engine', 'filename': 'engine.log',
                  'media_type': 'text/plain', 'size_bytes': 10, 'digest': 'sha256:' + 'd' * 64}
    artifact_output = {'role': 'artifact', 'variant_key': 'preview', 'filename': 'preview.png',
                       'media_type': 'image/png', 'size_bytes': 20, 'digest': 'sha256:' + 'e' * 64}
    settled = {'manifest_ref': association['object_id'], 'inputs': {'effective_settings': settings},
               'manifest_path': '/deleted/spool/result.json',
               'outputs': [log_output, artifact_output]}
    task = {'task_id': 'task-1', 'state': 'succeeded' if ok else 'failed',
            'spec': {'inputs': {'source': source}}, 'result': settled}
    result = InvocationResult('vibecomfy.run', 'executor', 'command', ok,
                              kernel_task_id='task-1', error=None if ok else {'message': 'producer failed'},
                              raw_result={'task': task, 'managed_outputs': [association]})
    return SimpleNamespace(result=result, source=source, settings=settings, association=association,
                           log_output=log_output)


def test_terminal_projection_uses_task_manifest_and_input_refs():
    fixture = terminal()
    result = _reconstruct_generation_result(fixture.result)
    assert result.task_id == 'task-1'
    assert result.manifest_ref == fixture.association['object_id']
    assert result.result_manifest == fixture.association
    assert result.source_ref == fixture.source
    assert result.effective_settings_ref == fixture.settings
    assert '/deleted/spool' not in str(result.to_dict())
    assert GenerationResult.from_dict(result.to_dict()) == result


def test_success_json_projects_only_task_log_outputs_and_roundtrips():
    fixture = terminal()
    result = _reconstruct_generation_result(fixture.result)
    serialized = result.to_dict()
    assert serialized['logs'] == [fixture.log_output]
    assert GenerationResult.from_dict(serialized) == result


def test_manifest_association_alone_is_a_durable_projection():
    fixture = terminal()
    del fixture.result.raw_result['task']['result']['manifest_ref']
    result = _reconstruct_generation_result(fixture.result)
    assert result.manifest_ref == fixture.association['object_id']
    assert result.result_manifest['association_id'] == 'assoc-manifest'


def test_failed_readback_keeps_manifest_and_input_refs_on_exception():
    fixture = terminal(ok=False)
    with pytest.raises(Exception, match='producer failed') as caught:
        _reconstruct_generation_result(fixture.result)
    assert caught.value.task_id == 'task-1'
    assert caught.value.manifest_ref == fixture.association['object_id']
    assert caught.value.source_ref == fixture.source
    assert caught.value.effective_settings_ref == fixture.settings


def test_unassociated_staged_settings_are_not_public_object_refs():
    fixture = terminal()
    fixture.result.raw_result['task']['result']['inputs']['effective_settings'] = {
        'sha256': 'b' * 64, 'bytes': 23, 'filename': 'api.json', 'path': '/deleted/api.json'}
    result = _reconstruct_generation_result(fixture.result)
    assert result.effective_settings_ref is None


def test_runtime_nested_spec_and_associated_refs_drop_spool_locators():
    fixture = terminal()
    raw = fixture.result.raw_result
    raw['task']['spec'] = {'input_object_ids': [fixture.source['digest']],
                           'spec': raw['task']['spec']}
    raw['managed_outputs'][0]['path'] = '/deleted/spool/result.json'
    raw['task']['result']['inputs']['effective_settings'] = {
        **fixture.settings, 'path': '/deleted/spool/settings.json'}
    result = _reconstruct_generation_result(fixture.result)
    assert result.source_ref == fixture.source
    assert result.effective_settings_ref == fixture.settings
    assert '/deleted/spool' not in str(result.to_dict())


def test_runtime_unprefixed_cas_refs_keep_their_original_identity():
    fixture = terminal()
    fixture.source['digest'] = 'a' * 64
    fixture.settings['object_id'] = 'b' * 64
    fixture.association['object_id'] = 'c' * 64
    fixture.result.raw_result['task']['result']['manifest_ref'] = 'c' * 64
    result = _reconstruct_generation_result(fixture.result)
    assert result.manifest_ref == 'c' * 64
    assert result.result_manifest['object_id'] == 'c' * 64
    assert result.source_ref == fixture.source
    assert result.effective_settings_ref == fixture.settings


def test_settled_reference_view_is_reconstructed_without_spool_paths():
    fixture = terminal()
    reference_view = {"references": [{"occurrence": "performance", "modality": "video",
        "trim": [1, 3], "tag": "<Video 1>",
        "input_binding": {"binding": "video_reference_0", "port": "ref_videos.ref_video_0"},
        "retained_picture_frames_estimate": {"value": 47, "kind": "metadata_upper_bound", "observed": False}}]}
    fixture.result.raw_result['task']['result']['payload'] = {"workflow_hook_evidence": {
        "reference_view": reference_view,
    }}
    fixture.result.raw_result['task']['result']['manifest_path'] = '/deleted/spool/result.json'

    result = _reconstruct_generation_result(fixture.result)

    assert result.manifest["reference_view"] == reference_view
    assert "/deleted/spool" not in str(result.to_dict())


def test_untrusted_manifest_reference_view_is_not_projected():
    fixture = terminal()
    fixture.result.raw_result['task']['result']['payload'] = {"manifest": {
        "reference_view": {"references": [{"occurrence": "/tmp/private.mp4"}]},
    }}
    result = _reconstruct_generation_result(fixture.result)
    assert "reference_view" not in result.manifest


def test_non_h3_capability_cannot_mint_workflow_reference_view():
    fixture = terminal()
    fixture.result.raw_result["task"]["result"]["payload"] = {
        "workflow_hook_evidence": {"reference_view": {"references": []}},
    }
    result = InvocationResult("speech.generate", "executor", "command", True,
        kernel_task_id="task-1", raw_result=fixture.result.raw_result)
    result = _reconstruct_generation_result(result)
    assert "reference_view" not in result.manifest


def test_direct_h3_manifest_cas_readback_authenticates_and_survives_scratch_cleanup(tmp_path):
    import hashlib
    import json
    from types import SimpleNamespace

    from astrid.sdk.invocation import _observe_h3_workflow_hook_evidence
    from astrid.sdk.exceptions import CapabilityInvocationError

    view = {"references": [{"occurrence": "performance", "modality": "video",
        "retained_picture_frames_estimate": {"value": 47, "kind": "metadata_upper_bound", "observed": False}}]}
    data = json.dumps({"kind": "managed-generation-result.v1", "evidence": {
        "producer": {"workflow_hook": {"reference_view": view}}}}).encode()
    scratch = tmp_path / "managed-generation-result.json"
    scratch.write_bytes(data)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    association = {"association_id": "result-manifest-assoc", "task_id": "task-1",
        "run_id": "run-1", "attempt_id": "attempt-1",
        "output_port": "result_manifest", "role": "manifest", "media_type": "application/json",
        "durability": "durable", "lifecycle": {"state": "available"},
        "object_id": digest, "digest": digest, "size": len(data)}
    client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda object_id: data))
    scratch.unlink()

    raw = {"task": {"task_id": "task-1", "run_id": "run-1", "attempt_id": "attempt-1",
        "result": {"payload": {"workflow_hook_evidence": {"reference_view": {"references": []}}}}},
        "managed_outputs": [association]}
    projected = _observe_h3_workflow_hook_evidence(raw, client, task_id="task-1",
                                                    run_id="run-1", attempt_id="attempt-1")
    assert projected == {"reference_view": view}
    legacy_association = {**association, "object_id": digest.removeprefix("sha256:"),
                          "digest": digest.removeprefix("sha256:")}
    legacy_raw = {"task": {"task_id": "task-1", "run_id": "run-1", "attempt_id": "attempt-1",
        "result": {}}, "managed_outputs": [legacy_association]}
    assert _observe_h3_workflow_hook_evidence(legacy_raw, client, task_id="task-1",
        run_id="run-1", attempt_id="attempt-1") == projected
    result = InvocationResult("vibecomfy.run", "executor", "command", True,
        kernel_task_id="task-1", raw_result=raw)
    assert _reconstruct_generation_result(result).manifest["reference_view"] == view

    with pytest.raises(CapabilityInvocationError, match="digest verification"):
        bad_client = SimpleNamespace(media=SimpleNamespace(read_bytes=lambda object_id: data + b"!"))
        _observe_h3_workflow_hook_evidence({"managed_outputs": [association]}, bad_client,
            task_id="task-1", run_id="run-1", attempt_id="attempt-1")
