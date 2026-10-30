from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.planner import plan_request
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request


def _image(path: Path, *, size: tuple[int, int] = (1024, 576), color=(255, 255, 255)) -> Path:
    Image.new("L", size, color=color[0]).save(path)
    return path


def _generate(*, duration: float = 5, prompt: str = "A quiet field at dawn."):
    return normalize_request({
        "version": 1, "operation": "generate", "output": {"duration": duration},
        "content": {"prompt": prompt}, "changes": {"video": [], "audio": []},
        "references": [{"asset": "look.png", "purpose": "appearance"}], "overrides": {},
    })


def _continue(*, output_duration: float = 7, prompt: str = "Continue the shot.", overrides=None):
    return normalize_request({
        "version": 1, "operation": "continue", "source": {"asset": "source.mp4", "range": [0, 4]},
        "output": {"duration": output_duration}, "content": {"prompt": prompt},
        "changes": {
            "video": [{"during": [4, output_duration], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [{"during": [4, output_duration], "action": "generate"}],
        }, "references": [], "overrides": overrides or {},
    })


def _source_edit(*, area=None, prompt="Edit the selected region.", extra_video=()):
    source = {"asset": "source.mp4", "range": [0, 4]}
    video = [{"during": [1, 3], "area": area or {"full_frame": True}, "action": "generate"}]
    video.extend(extra_video)
    return normalize_request({
        "version": 1, "operation": "edit", "source": source, "output": {"duration": 4},
        "content": {"prompt": prompt}, "changes": {"video": video, "audio": []},
        "references": [], "overrides": {},
    })


def _assets(tmp_path: Path, *, source=False, look=False, mask=False) -> dict[str, str]:
    result = {}
    if source:
        path = tmp_path / "source.mp4"
        path.write_bytes(b"fixture-video")
        result["source.mp4"] = str(path)
    if look:
        result["look.png"] = str(_image(tmp_path / "look.png", size=(32, 32), color=(32, 64, 128)))
    if mask:
        result["mask.png"] = str(_image(tmp_path / "mask.png"))
    return result


def test_plan_records_canonical_spec_bindings_schedule_and_preserve_pass(tmp_path: Path) -> None:
    request = _source_edit(
        area={"full_frame": True},
        extra_video=({"during": [0, 1], "area": {"full_frame": True}, "action": "preserve"},),
    )
    plan = plan_request(request, asset_map=_assets(tmp_path, source=True), out_dir=tmp_path / "plan")

    assert plan["status"] == "planned"
    assert plan["spec"]["prompt"] == request.value["content"]["prompt"]
    assert plan["bindings"]["assets"][0]["asset"] == "source.mp4"
    assert plan["bindings"]["runtime"]
    assert plan["mask_schedule"]["video"]["generated_intervals"] == [[1, 3]]
    assert plan["pass_count"] == 1
    assert plan["passes"][0]["reads"] == "source"
    assert plan["passes"][0]["preserves"][0]["op"] == "preserve"
    assert plan["passes"][0]["preserve_reads"] == "original_source"
    assert json.loads(Path(plan["plan_path"]).read_text()) == json.loads(json.dumps(plan))


def test_plan_static_image_mask_has_a_preview_and_runtime_binding(tmp_path: Path) -> None:
    request = _source_edit(area={"mask_asset": "mask.png"})
    plan = plan_request(request, asset_map=_assets(tmp_path, source=True, mask=True), out_dir=tmp_path / "plan")

    assert plan["bindings"]["regions"][0]["asset"] == "mask.png"
    assert plan["previews"][0]["region"] == "region-1"
    assert Path(plan["previews"][0]["path"]).is_file()
    assert any(row["binding"].startswith("video_mask") for row in plan["bindings"]["runtime"])
    assert "h3.plan.gpu_unqualified" in {row["code"] for row in plan["warnings"]}


def test_box_region_is_previewed_and_bound_by_native_compilation(tmp_path: Path) -> None:
    request = _source_edit(area={"rectangle": [10, 20, 80, 60]})
    plan = plan_request(request, asset_map=_assets(tmp_path, source=True), out_dir=tmp_path / "plan")

    assert plan["previews"] and Path(plan["previews"][0]["path"]).is_file()
    assert plan["compilation"]["status"] == "compiled"
    assert any(row["binding"] == "video_mask_0" for row in plan["bindings"]["runtime"])
    assert "h3.plan.compile_unavailable" not in {row["code"] for row in plan["warnings"]}


@pytest.mark.parametrize(
    ("h3_request", "assets", "requested", "raw", "trim"),
    [
        (_generate(duration=5), "look", 120, 124, 4),
        (_generate(duration=1.625), "look", 39, 39, 0),
        (_continue(output_duration=7), "source", 168, 124, 13),
        (_continue(output_duration=6.125), "source", 147, 90, 0),
    ],
)
def test_plan_requested_raw_and_trim_timing(tmp_path: Path, h3_request, assets: str, requested: int, raw: int, trim: int) -> None:
    asset_map = _assets(tmp_path, source=assets == "source", look=assets == "look")
    plan = plan_request(h3_request, asset_map=asset_map, out_dir=tmp_path / "plan")

    timing = plan["timing"]
    assert timing["requested"]["frames"] == requested
    assert timing["raw"]["frames"] == raw
    assert timing["trim"]["tail_frames"] == trim
    codes = {row["code"] for row in plan["warnings"]}
    assert "h3.plan.gpu_unqualified" in codes
    assert ("h3.plan.tail_trim" in codes) is bool(trim)


def test_plan_orders_multiple_modifying_passes_and_rejects_unresolved_assets(tmp_path: Path) -> None:
    multiple = _source_edit(extra_video=(
        {"during": [3, 4], "area": {"full_frame": True}, "action": "generate"},
    ))
    plan = plan_request(multiple, asset_map=_assets(tmp_path, source=True), out_dir=tmp_path / "multi")
    assert plan["pass_count"] == 2
    assert [item["operation"]["during"] for item in plan["passes"]] == [(1.0, 3.0), (3.0, 4.0)]
    assert [item["reads"] for item in plan["passes"]] == ["source", "previous_verified_output"]
    assert plan["runtime_submission"] == "eligible"

    with pytest.raises(H3RequestError) as caught:
        plan_request(_generate(), asset_map={}, out_dir=tmp_path / "unresolved")
    assert caught.value.code == "h3.plan.unresolved_assets"


@pytest.mark.parametrize("operation", ["continue", "edit"])
def test_plan_reports_ignored_v1_constant_overrides_and_keeps_supported_controls(tmp_path: Path, operation: str) -> None:
    if operation == "continue":
        request = _continue(overrides={"sampler": "res_multistep", "guidance": 0.95, "steps": 11, "seed": 42})
        expected_steps, expected_seed = 11, 42
    else:
        raw = dict(_source_edit().value)
        raw["overrides"] = {"sampler": "res_multistep", "guidance": 0.95, "steps": 12, "seed": 7}
        request = normalize_request(raw)
        expected_steps, expected_seed = 12, 7
    plan = plan_request(request, asset_map=_assets(tmp_path, source=True), out_dir=tmp_path / "plan")

    ignored = {row["field"]: row["value"] for row in plan["ignored_fields"]}
    assert ignored == ({"overrides.sampler": "res_multistep", "overrides.guidance": 0.95}
                       if operation == "continue" else {})
    assert plan["compilation"]["workflow_inputs"]["steps"] == expected_steps
    assert plan["compilation"]["workflow_inputs"]["seed"] == expected_seed


def _v2_source_free(*, settings=None, prompt="A subject crosses the field."):
    return normalize_request({
        "version": 2, "prompt": prompt, "duration": 5,
        "media": [{"id": "look", "asset": "look.png", "role": "reference", "modality": "image"}],
        "settings": settings or {},
    })


def test_repeated_identical_v2_planning_preserves_workflow_bytes_and_compilation_digest(tmp_path: Path) -> None:
    assets = _assets(tmp_path, look=True)
    plans = []
    manifests = []
    sources = []
    for _ in range(3):
        plan = plan_request(_v2_source_free(), asset_map=assets, out_dir=tmp_path / "plan")
        manifest = json.loads(Path(plan["compilation"]["manifest_path"]).read_text())
        plans.append(plan)
        manifests.append(manifest)
        sources.append(Path(manifest["workflow"]["workflow.py"]["path"]).read_bytes())

    assert sources[0] == sources[1] == sources[2]
    workflow_sha = hashlib.sha256(sources[0]).hexdigest()
    for manifest in manifests:
        assert manifest["workflow"]["workflow.py"]["sha256"] == workflow_sha
    assert manifests[0]["compilation_digest"] == manifests[1]["compilation_digest"] == manifests[2]["compilation_digest"]
    assert plans[0] == plans[1] == plans[2]


def _v2_continue(*, settings=None, prompt="Continue the shot."):
    return normalize_request({
        "version": 2, "prompt": prompt, "duration": 4, "continuation": True,
        "media": [{"id": "source", "asset": "source.mp4", "role": "timeline", "modality": "video",
                   "at": {"frame": 0}, "range": [0, 2]}],
        "settings": settings or {},
    })


def _v2_source_backed(*, mask=None, prompt="Edit the source region."):
    edit_mask = mask or {"full_frame": True}
    return normalize_request({
        "version": 2, "prompt": prompt, "duration": 4,
        "media": [{"id": "source", "asset": "source.mp4", "role": "timeline", "modality": "video",
                   "at": {"frame": 0}, "range": [0, 4],
                   "edit": [{"stream": "video", "during": [1, 3], "mask": edit_mask}]}],
        "settings": {},
    })


@pytest.mark.parametrize("case", ["generation", "continuation", "masked", "lanpaint"])
def test_v2_creative_prompt_contains_only_the_author_prompt(tmp_path: Path, case: str) -> None:
    prompt = f"Author prompt for {case}."
    if case == "generation":
        request, assets = _v2_source_free(prompt=prompt), _assets(tmp_path, look=True)
    elif case == "continuation":
        request, assets = _v2_continue(prompt=prompt), _assets(tmp_path, source=True)
    elif case == "masked":
        request = _v2_source_backed(mask={"asset": "mask.png"}, prompt=prompt)
        assets = _assets(tmp_path, source=True, mask=True)
    else:
        request, assets = _v2_source_backed(prompt=prompt), _assets(tmp_path, source=True)

    plan = plan_request(request, asset_map=assets, out_dir=tmp_path / case)
    assert plan["compilation"]["status"] == "compiled"
    assert plan["compilation"]["workflow_inputs"]["prompt"] == prompt
    assert "Native-v2 media schedule" not in plan["compilation"]["workflow_inputs"]["prompt"]
    assert "[{" not in plan["compilation"]["workflow_inputs"]["prompt"]
    if case == "masked":
        manifest = json.loads(Path(plan["compilation"]["manifest_path"]).read_text())
        workflow_path = Path(manifest["workflow"]["workflow.py"]["path"])
        workflow_source = workflow_path.read_text()
        assert prompt in workflow_source
        assert "Native-v2 media schedule" not in workflow_source


def test_v2_ignored_constant_settings_and_disabled_turbo_strength(tmp_path: Path) -> None:
    continuation = _v2_continue(settings={
        "sampler": "res_multistep", "scheduler": "simple", "steps": 9, "seed": 123,
        "turbo": {"enabled": True, "strength": 0.95},
    })
    plan = plan_request(continuation, asset_map=_assets(tmp_path, source=True), out_dir=tmp_path / "continue")
    ignored = {row["field"]: row["value"] for row in plan["ignored_fields"]}
    assert ignored == {
        "settings.sampler": "res_multistep", "settings.scheduler": "simple",
        "settings.turbo.enabled": True, "settings.turbo.strength": 0.95,
    }
    assert plan["compilation"]["workflow_inputs"]["steps"] == 9
    assert plan["compilation"]["workflow_inputs"]["seed"] == 123

    generate = _v2_source_free(settings={
        "steps": 7, "seed": 88, "turbo": {"enabled": False, "strength": 0.7},
    })
    plan = plan_request(generate, asset_map=_assets(tmp_path, look=True), out_dir=tmp_path / "generate")
    assert {row["field"] for row in plan["ignored_fields"]} == {"settings.turbo.strength"}
    assert plan["compilation"]["workflow_inputs"]["steps"] == 7
    assert plan["compilation"]["workflow_inputs"]["seed"] == 88


def test_plan_binds_per_edit_reference_selection(tmp_path: Path) -> None:
    h3_request = normalize_request({
        "version": 2, "prompt": "Edit using the first look.", "duration": 4, "settings": {},
        "media": [
            {"id": "source", "asset": "source.mp4", "role": "timeline", "modality": "video",
             "at": {"frame": 0}, "range": [0, 4], "edit": [
                 {"stream": "video", "during": [1, 3], "mask": {"asset": "mask.png"}, "guides": ["one"]}]},
            {"id": "one", "asset": "look.png", "role": "reference", "modality": "image"},
            {"id": "two", "asset": "second.png", "role": "reference", "modality": "image"},
        ],
    })
    assets = _assets(tmp_path, source=True, mask=True, look=True)
    assets["second.png"] = str(_image(tmp_path / "second.png", size=(32, 32)))
    plan = plan_request(h3_request, asset_map=assets, out_dir=tmp_path / "plan")
    assert plan["compilation"]["status"] == "compiled"
    assert plan["ignored_fields"] == []
    assert plan["spec"]["ops"][0]["refs"] == ("ref-1",)
    assert {row["binding"] for row in plan["bindings"]["runtime"]} == {"source_video", "video_mask_0", "reference_0"}
    assert plan["compilation"]["capabilities"]["reference_order"] == ["look.png"]


def test_plan_rejects_a_preserve_only_spec(tmp_path: Path) -> None:
    request = _source_edit()
    raw = dict(request.value)
    raw["changes"] = {"video": [{"during": [1, 3], "area": {"full_frame": True}, "action": "preserve"}], "audio": []}
    with pytest.raises(H3RequestError) as caught:
        plan_request(normalize_request(raw), asset_map=_assets(tmp_path, source=True), out_dir=tmp_path / "preserve-only")
    assert caught.value.code == "h3.plan.single_pass"


@pytest.mark.parametrize(
    ("placement_args", "placement"),
    [(("--placement", "local"), "local"), (("--placement", "runpod"), "runpod"),
     (("--prepared-worker", "worker.json"), "runpod")],
)
def test_transform_dry_run_never_touches_runtime_or_worker_selection(
    tmp_path: Path, monkeypatch, placement_args: tuple[str, ...], placement: str,
) -> None:
    from astrid.packs.h3_av.actions.transform import run as transform
    from astrid.packs.h3_av.actions.transform.run import build_parser, run_transform
    import astrid.packs.runpod.prepared_task as prepared_task

    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(_generate().value), encoding="utf-8")
    asset_path = tmp_path / "look.png"
    _image(asset_path, size=(32, 32), color=(20, 40, 80))
    asset_map_path = tmp_path / "assets.json"
    asset_map_path.write_text(json.dumps({"look.png": str(asset_path)}), encoding="utf-8")
    out = tmp_path / "operation"
    args_values = ["--request", str(request_path), "--asset-map", str(asset_map_path), "--out", str(out),
                   "--dry-run", *placement_args]
    if placement_args == ("--prepared-worker", "worker.json"):
        args_values[args_values.index("worker.json")] = str(tmp_path / "worker.json")
    args = build_parser().parse_args(args_values)
    patched = ("_resolve_local_engine", "_run_remote_placement", "_invoke_stage", "_invoke_canonical_run",
               "ensure_runpod_worker", "load_worker_qualification", "AstridClient", "generate",
               "prepared_worker_selection")
    calls = dict.fromkeys(patched, 0)

    def bomb(name):
        def invoke(*_args, **_kwargs):
            calls[name] = calls.get(name, 0) + 1
            return None
        return invoke

    for name in ("_resolve_local_engine", "_run_remote_placement", "_invoke_stage", "_invoke_canonical_run",
                 "ensure_runpod_worker", "load_worker_qualification"):
        monkeypatch.setattr(transform, name, bomb(name))
    monkeypatch.setattr(transform.AstridClient, "open_from_launcher", bomb("AstridClient"))
    monkeypatch.setattr(transform, "generate", bomb("generate"))
    monkeypatch.setattr(prepared_task, "load_prepared_worker_selection", bomb("prepared_worker_selection"))

    result = run_transform(args)

    assert result["placement"] == placement
    assert Path(result["plan_path"]).is_file()
    assert set(calls) == set(patched)
    assert all(count == 0 for count in calls.values())
    assert not (out / "operation.json").exists()
    assert not (out / "result.json").exists()
    assert not list(out.glob("0[0-9]-*"))
