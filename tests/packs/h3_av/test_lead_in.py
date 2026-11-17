"""Continuation take: lead-in (drop/keep), range derivation, and the optional frame-exact clip."""
from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest

from astrid.core.generation.engine_facts import comfyui_facts
from astrid.packs.h3_av.shared import take
from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.request_v2 import normalize_v2
from tests.packs.h3_av.test_request_contract import SCHEMA, _request
from tests.v10.test_media_trim import _decode_frames, _source

PACK = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av"
SCHEMA_V2 = json.loads((PACK / "schemas/request.v2.json").read_text())


def _continue(**output: object) -> dict[str, object]:
    return _request(output={"duration": 8, **output})


def _v2(**extra: object) -> dict[str, object]:
    raw = {"version": 2, "prompt": "Continue.", "duration": 8,
           "media": [{"id": "src", "asset": "source.mp4", "role": "timeline", "modality": "video", "at": {"frame": 0}}],
           "settings": {}, "continuation": True}
    raw.update(extra)
    return raw


# -- request knobs ----------------------------------------------------------


def test_lead_in_defaults_to_drop_and_leaves_existing_digests_alone() -> None:
    plain = normalize_request(_continue())
    assert "lead_in" not in plain.value["output"]
    assert take.lead_in_of(plain.value) == "drop"
    assert plain.digest == normalize_request(_continue()).digest
    keep = normalize_request(_continue(lead_in="keep"))
    assert keep.value["output"]["lead_in"] == "keep"
    assert take.lead_in_of(keep.value) == "keep"
    assert keep.digest != plain.digest


def test_lead_in_and_clip_validate_and_schema_agrees() -> None:
    for raw in (_continue(lead_in="keep"), _continue(clip_lead_in=True), _continue(lead_in="drop", clip_lead_in=False)):
        jsonschema.validate(raw, SCHEMA)
    jsonschema.validate(_continue(lead_in="keep"), SCHEMA)
    with pytest.raises(H3RequestError, match="one of"):
        normalize_request(_continue(lead_in="trim"))
    with pytest.raises(H3RequestError, match="boolean"):
        normalize_request(_continue(clip_lead_in="yes"))
    edit = _request(operation="edit", output={"duration": 8, "lead_in": "keep"})
    with pytest.raises(H3RequestError, match="only for operation continue"):
        normalize_request(edit)
    with pytest.raises(H3RequestError, match="only for operation continue"):
        normalize_request(_request(operation="edit", output={"duration": 8, "clip_lead_in": True}))
    with pytest.raises(Exception):
        jsonschema.validate(_request(operation="edit", output={"duration": 8, "lead_in": "keep"}), SCHEMA)


def test_v2_output_is_continuation_only() -> None:
    value = normalize_v2(_v2(output={"lead_in": "keep", "clip_lead_in": True}))
    assert value.value["output"] == {"lead_in": "keep", "clip_lead_in": True}
    jsonschema.validate(_v2(output={"lead_in": "keep"}), SCHEMA_V2)
    with pytest.raises(H3RequestError, match="only for a continuation"):
        normalize_v2(_v2(continuation=False, output={"lead_in": "keep"}))
    with pytest.raises(Exception):
        jsonschema.validate(_v2(continuation=False, output={"lead_in": "keep"}), SCHEMA_V2)


# -- range derivation -------------------------------------------------------


def _manifest(video=((3.25, 6.5),)) -> dict:
    return {"kind": "h3_av_composition", "changed_permissions": {"video": [list(v) for v in video], "audio": []}}


def test_drop_range_is_the_generated_span_and_keep_is_the_whole_file() -> None:
    assert take.take_range(_manifest(), lead_in="drop", media_duration=8.0) == [3.25, 6.5]
    assert take.take_range(_manifest(), lead_in="keep", media_duration=8.0) == [0.0, 8.0]


@pytest.mark.parametrize("video, message", [
    (((0.0, 1.0), (2.0, 3.0)), "exactly one"),
    ((), "exactly one"),
    (((5.0, 5.0),), "forward range"),
])
def test_drop_refuses_ambiguous_spans(video, message) -> None:
    with pytest.raises(take.TakeError, match=message):
        take.take_range(_manifest(video), lead_in="drop", media_duration=8.0)


def test_range_needs_a_composition_manifest() -> None:
    with pytest.raises(take.TakeError, match="composition manifest"):
        take.take_range({"kind": "other"}, lead_in="drop", media_duration=8.0)


# -- the hook: verify the whole take first, then clip ----------------------


def _install_hook_fakes(monkeypatch, tmp_path: Path, candidate: Path, calls: list[str], request_value: dict):
    from astrid.packs.h3_av.shared.request import H3Request
    spec = importlib.util.spec_from_file_location(
        "h3_lead_in_hooks", PACK / "extensions/vibecomfy.workflows/hooks.py")
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    hooks._pack()  # registers the private package the hook's shared modules load from

    engine = importlib.import_module("_h3_av_pack.shared.engine_facts")
    engine.install(comfyui_facts())
    composition = {**_manifest(), "candidate": {"path": str(candidate)}}

    stages = ModuleType("fake_stages")
    stages.load_build_state = lambda work: {"preparation": {}, "source": None,
                                            "request_object": H3Request(value=request_value, digest="0" * 64)}
    compose = ModuleType("fake_compose")
    compose.compose_candidate = lambda **kwargs: composition
    verify = ModuleType("fake_verify")

    def verify_candidate(**kwargs):
        calls.append("verify")
        assert kwargs["candidate"] == str(candidate)
        assert not (tmp_path / "job" / "h3" / "take-clip.mp4").exists(), "clip written before verification"
        return {"status": "verified"}

    verify.verify_candidate = verify_candidate
    for name, module in {"compose": compose, "verify": verify, "stages": stages}.items():
        monkeypatch.setitem(sys.modules, f"_h3_av_pack.shared.{name}", module)
    monkeypatch.setattr(hooks, "_shared", lambda name: importlib.import_module(f"_h3_av_pack.shared.{name}"))
    return hooks


@pytest.mark.parametrize("clip", [False, True])
def test_after_outputs_verifies_the_full_take_and_clips_only_when_asked(tmp_path, monkeypatch, clip: bool) -> None:
    source = _source(tmp_path / "candidate.mkv")
    before = source.read_bytes()
    job = tmp_path / "job"
    job.mkdir()
    value = normalize_request(_continue(lead_in="drop", **({"clip_lead_in": True} if clip else {}))).value
    calls: list[str] = []
    hooks = _install_hook_fakes(monkeypatch, tmp_path, source, calls, value)

    outputs = hooks.after_outputs(str(job), {}, {}, [str(source)])

    assert calls == ["verify"]
    assert outputs[0] == str(source)
    assert source.read_bytes() == before, "the full take is never rewritten"
    if not clip:
        assert outputs == [str(source)]
        assert not (job / "h3" / "take-clip.mp4").exists()
        return
    clip_path = Path(outputs[-1])
    assert clip_path.name == "take-clip.mp4" and clip_path.is_file()
    report = json.loads((job / "h3" / "take.json").read_text())
    assert report["range"] == [3.25, 6.5] and report["lead_in"] == "drop"
    assert report["clip"]["start_frame"] == 78 and report["clip"]["frames"] == 78
    full = _decode_frames(source)
    clipped = _decode_frames(clip_path)
    assert abs(clipped.shape[0] - 78) <= 1
    distances = [abs(clipped[0] - full[i]).mean() for i in (77, 78, 79)]
    assert int(distances.index(min(distances))) == 1, "first clip frame must be the first generated frame"


def test_keep_clips_the_whole_file(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path / "candidate.mkv")
    job = tmp_path / "job"
    job.mkdir()
    value = normalize_request(_continue(lead_in="keep", clip_lead_in=True)).value
    hooks = _install_hook_fakes(monkeypatch, tmp_path, source, [], value)
    hooks.after_outputs(str(job), {}, {}, [str(source)])
    report = json.loads((job / "h3" / "take.json").read_text())
    assert report["range"] == [0.0, 8.0] and report["clip"]["frames"] == 192
