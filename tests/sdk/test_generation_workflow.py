"""Lossless workflow facade and prepared-client delegation."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astrid.packs.runpod.prepared_task import PreparedRunPodTaskClient
from astrid.sdk.generation import GenerationFacade, GenerationInputError
from astrid.sdk.results import InvocationResult


RUNPOD = {
    "target": {"kind": "runpod", "pod_id": "original-pod", "provider_account_ref": "account",
               "storage": {"network_volume_id": "volume"}},
    "remote_activation_required": True,
}


def result(ok=True):
    return InvocationResult("vibecomfy.run", "executor", "command", ok,
                            kernel_run_id="run-1", kernel_task_id="task-1")


@pytest.mark.parametrize("ok,wait,dry_run,resume", [
    (True, True, False, False), (False, True, False, False),
    (True, False, False, False), (True, True, True, False),
    (True, True, False, True),
])
def test_exact_domain_and_controls_use_supplied_client_once(tmp_path, ok, wait, dry_run, resume):
    expected = result(ok)
    client = SimpleNamespace(invoke_result=Mock(return_value=expected))
    domain = {
        "workflow": "", "python": {"object_id": "py", "digest": "sha256:" + "a" * 64,
                                     "filename": "workflow.py", "required": True},
        "companion": {"object_id": "pair", "filename": "workflow.vibe.json"},
        "source": {"object_id": "source", "filename": "source.json"},
        "source_video": "", "source_video_node": "", "source_video_widget": "video",
        "managed_assets": {"object_id": "assets", "filename": "managed-assets.zip"},
        "workflow_inputs": ' { "prompt" : "exact\\ntext", "seed": 0 } ',
        "comfy_url": "", "comfy_output_dir": "",
        "generation_intent": {"version": 1, "metadata": {"exact": [1, False]}},
    }
    controls = dict(
        out=tmp_path / "out", project="project", execution_request=RUNPOD,
        idempotency_context={"operation": "same"}, idempotency_key="same-key",
        recovery_path=tmp_path / "receipt", resume=resume, wait=wait,
        read_managed_outputs=False, timeout_seconds=17.5, poll_seconds=0.25,
        project_root=tmp_path, extra_pack_roots=("pack",), banodoco_config={"a": 1},
        active_theme="theme", include_missing_roots=True, brief=tmp_path / "brief",
        dry_run=dry_run, check_binaries=True, python_exec="python", verbose=True,
        argv=("literal",),
    )
    returned = GenerationFacade().workflow(client=client, **domain, **controls)
    assert returned is expected
    client.invoke_result.assert_called_once_with(
        "vibecomfy.run", kind="executor", client=client, inputs=domain, **controls,
    )
    actual = client.invoke_result.call_args.kwargs
    assert actual["execution_request"] is RUNPOD
    assert actual["inputs"]["python"] is domain["python"]
    assert actual["inputs"]["generation_intent"] is domain["generation_intent"]
    assert "placement" not in actual["inputs"]


def test_omitted_domain_fields_add_no_creative_defaults():
    client = SimpleNamespace(invoke_result=Mock(return_value=result()))
    GenerationFacade().workflow(client=client, project="project", workflow_inputs="")
    args = client.invoke_result.call_args.kwargs
    assert args["inputs"] == {"workflow_inputs": ""}
    assert args["wait"] is True
    assert args["read_managed_outputs"] is True
    assert args["timeout_seconds"] == 3600.0


def test_without_client_uses_public_invoke_result(monkeypatch):
    import astrid.sdk as sdk
    expected = result()
    invoke_result = Mock(return_value=expected)
    monkeypatch.setattr(sdk, "invoke_result", invoke_result)
    monkeypatch.setattr(GenerationFacade, "_invoke", Mock(side_effect=AssertionError("private bypass")))
    assert GenerationFacade().workflow(workflow="ui.json", project="project") is expected
    invoke_result.assert_called_once()
    assert invoke_result.call_args.kwargs["inputs"] == {"workflow": "ui.json"}


def test_real_prepared_wrapper_keeps_two_phase_single_task_semantics(tmp_path):
    final = result()
    underlying = SimpleNamespace(invoke_result=Mock(side_effect=[result(), final]))
    def show(task_id):
        assert task_id == "task-1"
        return {"task_id": task_id, "run_id": "run-1", "capability_id": "vibecomfy.run",
                "state": "queued" if underlying.invoke_result.call_count == 1 else "succeeded",
                "execution_binding": {"effective_target": RUNPOD["target"]}}
    underlying.tasks = SimpleNamespace(show=show)
    runtime = SimpleNamespace(ensure_for_task=Mock(), retire_after_task=Mock())
    prepared = PreparedRunPodTaskClient(underlying, runtime)
    wrapper_invoke = Mock(wraps=prepared.invoke_result)
    prepared.invoke_result = wrapper_invoke
    domain = {"python": {"object_id": "py", "filename": "workflow.py"},
              "workflow_inputs": '{"seed":0}'}
    actual = GenerationFacade().workflow(
        client=prepared, project="project", execution_request=RUNPOD,
        recovery_path=tmp_path / "ordinary-receipt", **domain,
    )
    assert actual is final
    wrapper_invoke.assert_called_once()
    assert underlying.invoke_result.call_count == 2
    first, second = underlying.invoke_result.call_args_list
    assert first.kwargs["wait"] is False
    assert second.kwargs["wait"] is True and second.kwargs["resume"] is True
    assert first.kwargs["inputs"] == second.kwargs["inputs"] == domain
    runtime.ensure_for_task.assert_called_once()
    runtime.retire_after_task.assert_called_once()


@pytest.mark.parametrize("unknown", ["prompt", "model", "rerun", "readiness_profile_path", "endpoint_checkout"])
def test_unknown_or_host_owned_inputs_are_not_accepted(unknown):
    with pytest.raises(TypeError):
        GenerationFacade().workflow(**{unknown: "value"})


def test_target_workflow_does_not_read_preferences_or_forward_job_controls(monkeypatch, tmp_path):
    from contextlib import contextmanager
    import astrid.core.generation.entries as entries
    import astrid.core.generation.availability as availability
    import astrid.sdk.generation as generation

    path = tmp_path / 'workflow.json'
    path.write_text('{}')
    @contextmanager
    def snapshot(path):
        yield path
    monkeypatch.setattr(generation, '_snapshot_generation_workflow', snapshot)
    monkeypatch.setattr(availability, 'require_bundled_workflow_available', lambda *a, **k: None)
    monkeypatch.setattr(entries, 'resolve_workflow_inputs', lambda *a, **k: (
        {'id': 'workflow', 'kind': 'image', 'mode': 't2i'}, {}))
    monkeypatch.setattr(generation, '_load_model_registry', lambda **kwargs: {})
    calls = []
    monkeypatch.setattr(GenerationFacade, '_generate_on_target', lambda self, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(generation, '_runpod_preferences',
                        lambda *a, **k: pytest.fail('generate read saved machine preferences'))
    placement = {'kind': 'runpod'}
    GenerationFacade().workflow(workflow=path, placement=placement, dry_run=True)
    assert calls[0]['placement'] == placement
    assert 'datacenter' not in calls[0] and 'offer_digest' not in calls[0]


@pytest.mark.parametrize('field', [
    'max_usd', 'deadline', 'max_hourly_usd', 'offer_digest', 'preparation_seconds',
    'execution_seconds', 'idle_seconds', 'capacity_timeout_seconds', 'wait_for_capacity',
    'gpu', 'gpu_types', 'datacenter',
])
def test_workflow_rejects_removed_job_selection_controls(field):
    with pytest.raises(TypeError):
        GenerationFacade().workflow(**{field: 'removed'})


@pytest.fixture
def no_generation_preparation(monkeypatch):
    import astrid.sdk as sdk
    monkeypatch.setattr(sdk, '_load_model_registry',
                        lambda **kwargs: pytest.fail('rejected controls reached model discovery'))
    monkeypatch.setattr(GenerationFacade, '_invoke',
                        lambda *a, **k: pytest.fail('rejected controls reached admission'))


@pytest.mark.parametrize('method', ['image', 'video', 'audio'])
@pytest.mark.parametrize('field', [
    'consent', 'max_usd', 'deadline', 'max_hourly_usd', 'offer_digest',
    'preparation_seconds', 'execution_seconds', 'idle_seconds',
    'capacity_timeout_seconds', 'wait_for_capacity', 'gpu', 'gpu_types', 'datacenter',
])
def test_media_generate_rejects_removed_paid_and_job_selection_controls(method, field, no_generation_preparation):
    with pytest.raises(GenerationInputError, match='astrid machines claim'):
        getattr(GenerationFacade(), method)(**{field: 'removed'})


@pytest.mark.parametrize('method', ['workflow', 'image', 'video', 'audio'])
@pytest.mark.parametrize('field', ['datacenter', 'gpu_types', 'max_usd', 'offer_digest'])
def test_generate_rejects_removed_controls_nested_in_inputs(method, field, no_generation_preparation):
    with pytest.raises(GenerationInputError, match='astrid machines claim'):
        getattr(GenerationFacade(), method)(inputs={field: 'removed'})


@pytest.mark.parametrize('method', ['workflow', 'image', 'video', 'audio'])
def test_generate_rejects_paid_consent(method, no_generation_preparation):
    with pytest.raises(GenerationInputError, match='astrid machines claim'):
        getattr(GenerationFacade(), method)(consent=('paid',))


def test_claimed_machine_policy_has_no_paid_controls_or_preferences(monkeypatch):
    import astrid.sdk.generation as generation
    monkeypatch.setattr(generation, '_runpod_preferences',
                        lambda *a, **k: pytest.fail('job read claim preferences'))
    assert generation._runpod_placement_policy(engine='comfyui', machine='pod-a') == {
        'kind': 'runpod', 'engine': 'comfyui', 'machine': 'pod-a',
    }
