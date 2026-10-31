from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.actions.compile.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.generation import generation_timing
from astrid.packs.h3_av.actions.transform.run import _generation_intent


def _locked_model_workflow(**kwargs):
    from astrid.packs.h3_av.shared.generation import build_generation_workflow

    return build_generation_workflow(
        references=["reference.png"], prompt="A scene.", frames=24,
        model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=1,
        **kwargs,
    )


def test_generation_assets_use_recipe_lock_revisions():
    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    from astrid.packs.h3_av.shared import generation

    lock = load_recipe_model_lock("vibecomfy-h3", "linux-x86_64-cu130")
    workflow = _locked_model_workflow(adapter_strength=0.7)
    assets = {f'{row["subdir"]}/{row["name"]}': row for row in workflow.metadata["model_assets"]}
    assert len(lock.models) == 5
    assert set(assets) == {model.destination for model in lock.models} | {
        f"loras/{generation.CHARACTER_SWAP_FILENAME}"
    }
    for model in lock.models:
        assert assets[model.destination]["url"] == (
            f"https://huggingface.co/{model.repository}/resolve/{model.revision}/{model.path}"
        )
    character = assets[f"loras/{generation.CHARACTER_SWAP_FILENAME}"]
    assert character["url"] == (
        f"https://huggingface.co/{generation.CHARACTER_SWAP_REPOSITORY}/resolve/"
        f"{generation.CHARACTER_SWAP_REVISION}/{generation.CHARACTER_SWAP_FILENAME}"
    )
    assert character["hf_revision"] == generation.CHARACTER_SWAP_REVISION
    assert f"loras/{generation.TURBO_FILENAME}" not in {
        f'{row["subdir"]}/{row["name"]}'
        for row in _locked_model_workflow(turbo_enabled=False).metadata["model_assets"]
    }


@pytest.mark.parametrize("invalid", [
    {"revision": None}, {"revision": "main"}, {"repository": None},
    {"path": None}, {"url": "https://huggingface.co/owner/repo/resolve/main/model"},
])
def test_generation_rejects_incomplete_or_unpinned_lock_entry(monkeypatch, invalid):
    from astrid.packs.h3_av.shared import generation

    lock = generation.load_recipe_model_lock("vibecomfy-h3", "linux-x86_64-cu130")
    broken = replace(lock.models[0], **invalid)
    monkeypatch.setattr(generation, "load_recipe_model_lock", lambda *_: replace(
        lock, models=(broken, *lock.models[1:]),
    ))
    with pytest.raises(ValueError, match="pinned Hugging Face source"):
        _locked_model_workflow()


@pytest.mark.parametrize("index", range(5))
def test_generation_rejects_missing_required_lock_destination(monkeypatch, index):
    from astrid.packs.h3_av.shared import generation

    lock = generation.load_recipe_model_lock("vibecomfy-h3", "linux-x86_64-cu130")
    missing = lock.models[index]
    monkeypatch.setattr(generation, "load_recipe_model_lock", lambda *_: replace(
        lock, models=tuple(model for model in lock.models if model is not missing),
    ))
    with pytest.raises(ValueError, match="missing destination") as error:
        _locked_model_workflow()
    assert missing.destination in str(error.value)


def request(count=4, **updates):
    raw = {"version": 1, "operation": "generate", "output": {"duration": 15},
           "content": {"prompt": "A subject crosses the scene in <Picture 1>."},
           "changes": {"video": [], "audio": []},
           "references": [{"asset": f"image-{i}", "purpose": "appearance"} for i in range(count)],
           "overrides": {"steps": 8, "seed": 42}}
    raw.update(updates)
    return normalize_request(raw)


@pytest.mark.parametrize("count", [1, 4, 9])
@pytest.mark.integration
def test_dynamic_references_emit_one_portable_sampling_graph(tmp_path: Path, count: int, monkeypatch):
    from vibecomfy.cli_loader import load_bundle
    from vibecomfy.security.provenance import Provenance
    paths = {}
    for i in range(count):
        path = tmp_path / f"image-{i}.png"
        Image.new("RGB", (32, 32), color=(i * 20, 0, 0)).save(path)
        paths[f"image-{i}"] = str(path)
    prompt = " \tA café scene with <Picture 1>.  \n"
    expected = prompt.encode("utf-8")
    req = request(count, output={"duration": 9}, content={"prompt": prompt},
                  overrides={"steps": 12, "seed": 7, "sampler": "euler", "guidance": 0.5})
    assert req.value["content"]["prompt"].encode("utf-8") == expected
    prepared = prepare_request(req, asset_map=paths)
    assert prepared["request"]["content"]["prompt"].encode("utf-8") == expected
    assert prepared["mask_schedule"]["video"]["generated_intervals"] == [[0, 9]]
    assert prepared["mask_schedule"]["audio"]["protected_intervals"] == []
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    actual = compiled["workflow_inputs"]["prompt"].encode("utf-8")
    assert (actual, len(actual), hashlib.sha256(actual).digest()) == (expected, len(expected), hashlib.sha256(expected).digest())
    assert compiled["request_digest"] == prepared["request_digest"] == req.digest
    assert compiled["capabilities"]["generation_timing"] == generation_timing(9)
    portable = tmp_path / "relocated"
    shutil.copytree(tmp_path / "compiled/workflow-bundle", portable)
    shutil.rmtree(tmp_path / "compiled")
    bundle = load_bundle(portable, trust=Provenance.USER_CONFIRMED)
    api = bundle.workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(n for n in api.values() if n["class_type"] == "MiniMaxH3ReferenceToVideo")
    refs = {k: v for k, v in target["inputs"].items() if k.startswith("ref_images.")}
    assert len(refs) == count
    for i in range(count):
        loader = api[refs[f"ref_images.ref_image_{i}"][0]]
        assert loader["inputs"]["image"] == compiled["workflow_inputs"][f"reference_{i}"]
    assert target["inputs"]["prompt"].encode("utf-8") == expected
    assert target["inputs"]["length"] == 226
    by_class = {node["class_type"]: node["inputs"] for node in api.values()}
    assert by_class["BasicScheduler"]["steps"] == 12
    assert by_class["RandomNoise"]["noise_seed"] == 7
    assert by_class["KSamplerSelect"]["sampler_name"] == "euler"
    assert by_class["LoraLoaderModelOnly"]["strength_model"] == 0.5
    assert sum(n["class_type"] == "SamplerCustomAdvanced" for n in api.values()) == 1
    assert sum(n["class_type"] == "VHS_VideoCombine" for n in api.values()) == 1
    assert not any(n["class_type"] in {"MiniMaxH3CustomKeyframes", "MiniMaxH3GeneratedAVMaskedContext"} for n in api.values())
    assert len(bundle.workflow.outputs) == 1
    assert bundle.workflow.outputs[0].expected_cardinality == "one"
    assert len(_generation_intent(compiled)["groups"][0]["selectors"]) == 1
    assert compiled["capabilities"]["reference_order"] == [f"image-{i}" for i in range(count)]
    source = (portable / "workflow.py").read_text()
    assert "mink" not in source.lower() and "runpy" not in source and "/workspace/" not in source
    assert all(bundle.workflow.inputs[f"reference_{i}"].default == "" for i in range(count))
    if count == 4:
        import importlib
        monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
        validator = importlib.import_module("astrid.packs.vibecomfy.actions.validate.run")
        report = validator._canonical_bundle_validation(portable / "workflow.py", python_execution_consent="confirmed")
        assert report["ok"] is True
        assert report["validation_mode"] == "canonical_bundle_structural"
        from astrid.packs.vibecomfy.invocation_preflight import preflight_invocation
        preflight_invocation(portable / "workflow.py", run_inputs=compiled["workflow_inputs"],
                             expected_prompt=req.value["content"]["prompt"])


@pytest.mark.parametrize("updates,match", [
    ({"source": {"asset": "video"}}, "source must be omitted"),
    ({"references": []}, "one to nine"),
    ({"references": [{"asset": "a", "purpose": "audio"}]}, "image guidance"),
    ({"references": [{"asset": "a", "purpose": "keyframe"}]}, "timed keyframes"),
    ({"output": {"duration": 15.01}}, "24 fps"),
    ({"output": {"duration": 16}}, "5..362"),
    ({"output": {"duration": float("inf")}}, "finite"),
    ({"references": [{"asset": "a", "purpose": "style"}] * 10}, "nine"),
    ({"changes": {"video": [], "audio": [{"during": [0, 1], "action": "preserve"}]}}, "changes/masks"),
])
def test_unsupported_generation_requests_fail_at_admission(updates, match):
    with pytest.raises(H3RequestError, match=match):
        request(**updates)


def test_generation_timing_covers_original_fifteen_second_request():
    assert generation_timing(15) == {"fps": 24, "requested_frames": 360,
                                   "raw_frames": 362, "trim_tail_frames": 2, "duration": 15}


@pytest.mark.integration
def test_reference_must_decode_as_still_image(tmp_path: Path):
    path = tmp_path / "fake.png"
    path.write_bytes(b"not an image")
    with pytest.raises(CompilationError, match="decodable still image"):
        compile_preparation(prepare_request(request(1), asset_map={"image-0": str(path)}), out_dir=tmp_path / "compiled")


@pytest.fixture
def canonical_call(tmp_path):
    from astrid.packs.h3_av.actions.transform.operation import OperationJournal
    journal = OperationJournal(tmp_path / "operation-state.json", request_digest="request-1")
    return {
        "inputs": {"python": {"object_id": "workflow", "filename": "workflow.py"},
                   "workflow_inputs": ' { "prompt" : "exact\\n", "seed": 0 } '},
        "execution_request": None,
        "out": tmp_path / "04-run", "project": "project-1",
        "saved_result": tmp_path / "04-run/run-result.json",
        "journal": journal, "resume": False,
        "idempotency_context": {"h3_submission_id": journal.submission_id},
    }


@pytest.fixture
def facade_spy(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.packs.h3_av.actions.transform import run as transform
    spy = Mock(wraps=transform.generate.workflow)
    monkeypatch.setattr(transform, "generate", SimpleNamespace(workflow=spy))
    return spy


def _runpod_request():
    return {
        "target": {"kind": "runpod", "pod_id": "original-pod",
                   "provider_account_ref": "account-1",
                   "storage": {"network_volume_id": "original-volume"}},
        "remote_activation_required": True,
        "lifecycle": {"mode": "leave_running"},
    }


def _canonical_result(**changes):
    from astrid.sdk.results import InvocationResult
    return InvocationResult(
        capability_id="vibecomfy.run", capability_type="executor", native_kind="executor",
        kernel_task_id="task-1", kernel_run_id="run-1", kernel_attempt_id="attempt-1",
        **{"ok": True, **changes},
    )


@pytest.fixture
def prepared_client(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.packs.runpod.prepared_task import PreparedWorkerSelection
    final = _canonical_result()
    underlying = SimpleNamespace(invoke_result=Mock(side_effect=[_canonical_result(), final]))

    def show(task_id):
        assert task_id == "task-1"
        return {"task_id": task_id, "run_id": "run-1", "attempt_id": "attempt-1",
                "capability_id": "vibecomfy.run",
                "state": "queued" if underlying.invoke_result.call_count == 1 else "succeeded",
                "execution_binding": {"effective_target": _runpod_request()["target"]}}

    underlying.tasks = SimpleNamespace(show=show)
    owner = SimpleNamespace(ensure_for_task=Mock(), retire_after_task=Mock())
    factory = Mock(return_value=owner)
    selection = PreparedWorkerSelection(
        claim_handle_path=tmp_path / "claim.json", deployment_profile_path=tmp_path / "profile.json",
        staging_inputs_path=tmp_path / "staging.json", journal_dir=tmp_path / "owner-journal",
        target=_runpod_request()["target"], deployment_profile={}, staging_inputs={},
        composition_factory=factory,
    )
    prepared = selection.bind_client(
        underlying, workflow_path=tmp_path / "workflow.py",
        workflow_bundle_path=tmp_path, workflow_inputs={"seed": 0},
    )
    prepared.invoke_result = Mock(wraps=prepared.invoke_result)
    return prepared, underlying, owner, factory, final


@pytest.mark.parametrize("execution,placement", [
    (None, "local"), ({"target": {"kind": "default"}}, "local"),
    (_runpod_request(), "runpod"),
])
def test_canonical_child_calls_real_workflow_facade_once(canonical_call, facade_spy, execution, placement):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.packs.h3_av.actions.transform import run as transform
    final = _canonical_result()
    client = SimpleNamespace(invoke_result=Mock(return_value=final))
    canonical_call["execution_request"] = execution
    result = transform._invoke_canonical_run(client, **canonical_call)
    assert result is final
    facade_spy.assert_called_once()
    forwarded = facade_spy.call_args.kwargs
    assert forwarded["client"] is client
    assert forwarded["placement"] == placement
    assert forwarded["execution_request"] is execution
    assert forwarded["python"] is canonical_call["inputs"]["python"]
    assert forwarded["workflow_inputs"] is canonical_call["inputs"]["workflow_inputs"]
    for name in ("out", "project", "idempotency_context"):
        assert forwarded[name] == canonical_call[name]
    assert forwarded["recovery_path"] == transform._recovery_receipt_path(
        canonical_call["saved_result"], canonical_call["journal"])
    assert (forwarded["resume"], forwarded["wait"], forwarded["read_managed_outputs"],
            forwarded["timeout_seconds"], forwarded["poll_seconds"]) == (False, True, True, 3600.0, 1.0)
    client.invoke_result.assert_called_once()
    assert client.invoke_result.call_args.kwargs["inputs"] == canonical_call["inputs"]
    assert canonical_call["journal"].admission_digest == transform._stable_digest({
        "capability_id": "vibecomfy.run", "inputs": canonical_call["inputs"],
        "execution_request": execution,
    })


def test_canonical_child_preserves_bound_prepared_wrapper_and_one_owner(
    canonical_call, facade_spy, prepared_client,
):
    from astrid.packs.h3_av.actions.transform import run as transform
    prepared, underlying, owner, factory, final = prepared_client
    canonical_call["execution_request"] = _runpod_request()
    assert transform._invoke_canonical_run(prepared, **canonical_call) is final
    facade_spy.assert_called_once()
    assert facade_spy.call_args.kwargs["client"] is prepared
    assert facade_spy.call_args.kwargs["placement"] == "runpod"
    prepared.invoke_result.assert_called_once()
    factory.assert_called_once()
    owner.ensure_for_task.assert_called_once()
    owner.retire_after_task.assert_called_once()
    first, second = underlying.invoke_result.call_args_list
    assert first.kwargs["wait"] is False and first.kwargs["resume"] is False
    assert second.kwargs["wait"] is True and second.kwargs["resume"] is True
    assert first.kwargs["recovery_path"] == second.kwargs["recovery_path"]
    assert first.kwargs["execution_request"] is second.kwargs["execution_request"] is canonical_call["execution_request"]
    assert first.kwargs["inputs"] == second.kwargs["inputs"] == canonical_call["inputs"]


@pytest.mark.parametrize("execution,placement", [
    (_runpod_request(), "local"), (None, "runpod"), (_runpod_request(), "unknown"),
    ({"target": {"kind": "profile", "id": "unresolved"}}, None),
])
def test_canonical_placement_rejects_before_facade_admission_or_owner_effects(
    canonical_call, facade_spy, prepared_client, execution, placement,
):
    from astrid.core.generation.engine_contracts import UnsupportedPlacement
    from astrid.packs.h3_av.actions.transform import run as transform
    prepared, underlying, owner, _factory, _final = prepared_client
    canonical_call["execution_request"] = execution
    with pytest.raises(UnsupportedPlacement):
        transform._invoke_canonical_run(prepared, placement=placement, **canonical_call)
    facade_spy.assert_not_called()
    underlying.invoke_result.assert_not_called()
    owner.ensure_for_task.assert_not_called()
    owner.retire_after_task.assert_not_called()
    assert not canonical_call["journal"].has_history
    assert not canonical_call["saved_result"].exists()


@pytest.mark.parametrize("status,route", [
    ("admission_started", None), ("uncertain", None), ("completed", None),
    ("uncertain", "generation.workflow.v1/runpod"),
])
def test_canonical_resume_refuses_old_or_incompatible_route_even_with_sdk_receipt(
    canonical_call, facade_spy, status, route,
):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform import run as transform
    journal = canonical_call["journal"]
    journal.freeze_admission(transform._stable_digest({
        "capability_id": "vibecomfy.run", "inputs": canonical_call["inputs"],
        "execution_request": canonical_call["execution_request"],
    }))
    journal.set_admission_phase("uncertain")
    journal.record("run", status, **({"workflow_route": route} if route else {}))
    receipt = transform._recovery_receipt_path(canonical_call["saved_result"], journal)
    receipt.parent.mkdir(parents=True)
    receipt.write_text("old SDK receipt must not authorize replay")
    before = journal.path.read_bytes()
    client = SimpleNamespace(invoke_result=Mock())
    with pytest.raises(AstridError, match="incompatible workflow route"):
        transform._invoke_canonical_run(client, **{**canonical_call, "resume": True})
    facade_spy.assert_not_called()
    client.invoke_result.assert_not_called()
    assert journal.path.read_bytes() == before


def test_canonical_failed_result_keeps_error_check_and_uncertain_admission(canonical_call, facade_spy):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform import run as transform
    client = SimpleNamespace(invoke_result=Mock(return_value=_canonical_result(ok=False, error="child failed")))
    with pytest.raises(AstridError, match="vibecomfy.run failed: child failed"):
        transform._invoke_canonical_run(client, **canonical_call)
    facade_spy.assert_called_once()
    assert canonical_call["journal"].admission_phase == "uncertain"
    with pytest.raises(AstridError, match="may have occurred"):
        transform._invoke_canonical_run(client, **{**canonical_call, "resume": True})
    client.invoke_result.assert_called_once()


@pytest.mark.integration
def test_compiled_four_image_generation_reaches_facade_with_exact_prompt_and_assets(
    tmp_path, canonical_call, facade_spy, prepared_client,
):
    from vibecomfy.cli_loader import load_bundle
    from vibecomfy.security.provenance import Provenance
    from astrid.packs.h3_av.actions.transform import run as transform
    from astrid.packs.vibecomfy.asset_manifest import read_archive
    prompt = (Path(__file__).parent / "fixtures/generation-prompt.txt").read_bytes()
    assert len(prompt) == 1108 and prompt.endswith(b"\n")
    assert hashlib.sha256(prompt).hexdigest() == "3da36a0f4d4596c77a3a09d1104c1c610f629faca8975476abb754ca12235ad8"
    paths = {}
    for i in range(4):
        path = tmp_path / f"img{i + 1}.png"
        Image.new("RGB", (32, 32), color=(i * 40, 0, 0)).save(path)
        paths[f"img{i + 1}"] = str(path)
    req = request(4, output={"duration": 9}, content={"prompt": prompt.decode("utf-8")},
                  references=[{"asset": f"img{i + 1}", "purpose": "appearance" if i < 3 else "style"}
                              for i in range(4)], overrides={})
    prepared_doc = prepare_request(req, asset_map=paths)
    compiled = compile_preparation(prepared_doc, out_dir=tmp_path / "compiled")
    workflow_inputs = compiled["workflow_inputs"]
    bundle = load_bundle(tmp_path / "compiled/workflow-bundle", trust=Provenance.USER_CONFIRMED)
    api = bundle.workflow.compile("api", run_inputs=workflow_inputs)
    node = next(n for n in api.values() if n["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert node["inputs"]["prompt"].encode("utf-8") == prompt
    assert (node["inputs"]["width"], node["inputs"]["height"]) == (1024, 576)
    assert node["inputs"]["length"] == 226
    assert compiled["capabilities"]["reference_order"] == list(paths)
    archive = read_archive(Path(compiled["managed_assets"]["path"]))
    for i, path in enumerate(paths.values()):
        record = next(row for row in archive.manifest["assets"] if row["binding"] == f"reference_{i}")
        assert Path(record["member"]).name == workflow_inputs[f"reference_{i}"]
        assert archive.members[record["member"]] == Path(path).read_bytes()
    descriptors = {port: {"object_id": "sha256:" + row["sha256"], "filename": name}
                   for port, name in (("python", "workflow.py"), ("companion", "workflow.vibe.json"),
                                      ("source", "source.json"))
                   for row in [compiled["workflow"][name]]}
    descriptors["managed_assets"] = {"object_id": "sha256:" + compiled["managed_assets"]["sha256"],
                                      "filename": "managed-assets.zip"}
    serialized = json.dumps(workflow_inputs, sort_keys=True, separators=(",", ":"))
    intent = _generation_intent(compiled)
    canonical_call.update(inputs={**descriptors, "workflow_inputs": serialized, "generation_intent": intent},
                          execution_request=_runpod_request())
    prepared, underlying, owner, factory, final = prepared_client
    assert transform._invoke_canonical_run(prepared, **canonical_call) is final
    facade_spy.assert_called_once()
    forwarded = facade_spy.call_args.kwargs
    assert forwarded["client"] is prepared and forwarded["placement"] == "runpod"
    assert forwarded["workflow_inputs"] is serialized
    for port, descriptor in descriptors.items():
        assert forwarded[port] is descriptor
    assert forwarded["generation_intent"] is intent
    for call in underlying.invoke_result.call_args_list:
        assert call.kwargs["inputs"]["workflow_inputs"].encode("utf-8") == serialized.encode("utf-8")
        assert json.loads(call.kwargs["inputs"]["workflow_inputs"])["prompt"].encode("utf-8") == prompt
    prepared.invoke_result.assert_called_once()
    factory.assert_called_once()
    owner.ensure_for_task.assert_called_once()
    owner.retire_after_task.assert_called_once()


def test_canonical_facade_recovery_observes_original_sdk_receipt_after_lost_reply(
    canonical_call, facade_spy,
):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.packs.h3_av.actions.transform import run as transform
    from astrid.packs.h3_av.actions.transform.operation import OperationJournal
    final = _canonical_result()

    def invoke(_capability, **kwargs):
        receipt = kwargs["recovery_path"]
        if not kwargs["resume"]:
            receipt.parent.mkdir(parents=True)
            receipt.write_text("SDK committed original admission")
            raise TimeoutError("lost reply")
        assert receipt.read_text() == "SDK committed original admission"
        return final

    client = SimpleNamespace(invoke_result=Mock(side_effect=invoke))
    canonical_call["execution_request"] = _runpod_request()
    with pytest.raises(TimeoutError, match="lost reply"):
        transform._invoke_canonical_run(client, **canonical_call)
    original_journal = canonical_call["journal"]
    resumed_journal = OperationJournal(original_journal.path, request_digest="request-1")
    result = transform._invoke_canonical_run(
        client, **{**canonical_call, "journal": resumed_journal, "resume": True},
    )
    assert result is final
    assert (result.kernel_task_id, result.kernel_run_id, result.kernel_attempt_id) == (
        "task-1", "run-1", "attempt-1")
    first, resumed = facade_spy.call_args_list
    assert first.kwargs["resume"] is False and resumed.kwargs["resume"] is True
    for name in ("client", "placement", "execution_request", "recovery_path", "idempotency_context"):
        assert first.kwargs[name] == resumed.kwargs[name]
    assert resumed_journal.submission_id == original_journal.submission_id
    assert resumed_journal.admission_digest == original_journal.admission_digest
    assert resumed_journal.admission_phase == "settled"
    assert client.invoke_result.call_count == facade_spy.call_count == 2


def test_canonical_resume_rejects_changed_inputs_before_receipt_readback(canonical_call, facade_spy):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.core.contracts.errors import AstridError
    from astrid.packs.h3_av.actions.transform import run as transform

    def lose_reply(_capability, **kwargs):
        kwargs["recovery_path"].parent.mkdir(parents=True)
        kwargs["recovery_path"].write_text("original SDK identity")
        raise TimeoutError("lost reply")

    client = SimpleNamespace(invoke_result=Mock(side_effect=lose_reply))
    with pytest.raises(TimeoutError):
        transform._invoke_canonical_run(client, **canonical_call)
    before = canonical_call["journal"].path.read_bytes()
    changed = {**canonical_call["inputs"], "workflow_inputs": '{"prompt":"changed"}'}
    with pytest.raises(AstridError, match="frozen operation"):
        transform._invoke_canonical_run(client, **{**canonical_call, "inputs": changed, "resume": True})
    client.invoke_result.assert_called_once()
    facade_spy.assert_called_once()
    assert canonical_call["journal"].path.read_bytes() == before
