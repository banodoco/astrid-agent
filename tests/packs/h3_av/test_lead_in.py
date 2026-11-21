"""Continuation take: lead-in (drop/keep), range derivation, and output cardinality."""
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
from tests.v10.test_media_trim import _source

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


def test_lead_in_validates_and_schema_agrees() -> None:
    jsonschema.validate(_continue(lead_in="keep"), SCHEMA)
    with pytest.raises(H3RequestError, match="one of"):
        normalize_request(_continue(lead_in="trim"))
    edit = _request(operation="edit", output={"duration": 8, "lead_in": "keep"})
    with pytest.raises(H3RequestError, match="only for operation continue"):
        normalize_request(edit)
    with pytest.raises(Exception):
        jsonschema.validate(_request(operation="edit", output={"duration": 8, "lead_in": "keep"}), SCHEMA)


@pytest.mark.parametrize("flag", [True, False])
def test_clip_lead_in_is_refused_by_requests_and_schemas(flag) -> None:
    for normalize, raw, schema in (
        (normalize_request, _continue(clip_lead_in=flag), SCHEMA),
        (normalize_v2, _v2(output={"clip_lead_in": flag}), SCHEMA_V2),
    ):
        with pytest.raises(H3RequestError, match=r"unsupported field.*clip_lead_in.*astrid media trim"):
            normalize(raw)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(raw, schema)


def test_v2_output_is_continuation_only() -> None:
    value = normalize_v2(_v2(output={"lead_in": "keep"}))
    assert value.value["output"] == {"lead_in": "keep"}
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


# -- the hook: verify the whole take first, then report ----------------------


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
        assert not (tmp_path / "job" / "h3" / "take.json").exists(), "range written before verification"
        return {"status": "verified"}

    verify.verify_candidate = verify_candidate
    for name, module in {"compose": compose, "verify": verify, "stages": stages}.items():
        monkeypatch.setitem(sys.modules, f"_h3_av_pack.shared.{name}", module)
    monkeypatch.setattr(hooks, "_shared", lambda name: importlib.import_module(f"_h3_av_pack.shared.{name}"))
    return hooks


def test_after_outputs_returns_one_file_per_output_and_writes_take_json(tmp_path, monkeypatch) -> None:
    source = _source(tmp_path / "candidate.mkv")
    before = source.read_bytes()
    job = tmp_path / "job"
    job.mkdir()
    value = normalize_request(_continue()).value
    calls: list[str] = []
    hooks = _install_hook_fakes(monkeypatch, tmp_path, source, calls, value)

    secondary = tmp_path / "secondary.txt"
    secondary.write_text("output")
    outputs = hooks.after_outputs(str(job), {}, {}, ["generated.mkv", str(secondary)])

    assert calls == ["verify"]
    assert outputs == [str(source), str(secondary)]
    assert source.read_bytes() == before, "the full take is never rewritten"
    report = json.loads((job / "h3" / "take.json").read_text())
    assert report["range"] == [3.25, 6.5] and report["lead_in"] == "drop"
    assert not (job / "h3" / "take-clip.mp4").exists()


# -- review fixes: always report, never fail a verified job, keep end, digests ----


def _run_hook(tmp_path, monkeypatch, value, composition_video=((3.25, 6.5),)):
    source = _source(tmp_path / "candidate.mkv")
    job = tmp_path / "job"
    job.mkdir()
    hooks = _install_hook_fakes(monkeypatch, tmp_path, source, [], value)
    compose = sys.modules["_h3_av_pack.shared.compose"]
    compose.compose_candidate = lambda **kwargs: {**_manifest(composition_video),
                                                  "candidate": {"path": str(source)}}
    outputs = hooks.after_outputs(str(job), {}, {}, [str(source)])
    report = json.loads((job / "h3" / "take.json").read_text())
    return source, outputs, report


@pytest.mark.parametrize("mode, expected", [("drop", [3.25, 6.5]), ("keep", [0.0, 8.0])])
def test_take_json_is_written_for_every_continuation(tmp_path, monkeypatch, mode, expected) -> None:
    value = normalize_request(_continue(lead_in=mode)).value
    source, outputs, report = _run_hook(tmp_path, monkeypatch, value)
    assert outputs == [str(source)]
    assert report["range"] == expected and report["lead_in"] == mode
    assert "clip" not in report and "error" not in report


def test_no_take_json_for_a_non_continuation(tmp_path, monkeypatch) -> None:
    value = normalize_request(_request(operation="edit", output={"duration": 8})).value
    source = _source(tmp_path / "candidate.mkv")
    job = tmp_path / "job"
    job.mkdir()
    hooks = _install_hook_fakes(monkeypatch, tmp_path, source, [], value)
    assert hooks.after_outputs(str(job), {}, {}, [str(source)]) == [str(source)]
    assert not (job / "h3" / "take.json").exists()


def test_two_generated_spans_are_recorded_not_fatal(tmp_path, monkeypatch) -> None:
    value = normalize_request(_continue()).value
    source, outputs, report = _run_hook(tmp_path, monkeypatch, value, ((0.0, 1.0), (2.0, 3.0)))
    assert outputs == [str(source)]
    assert "TakeError" in report["error"] and "exactly one" in report["error"]
    assert "range" not in report and "clip" not in report


def test_keep_ends_at_the_last_video_frame_not_the_container(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace
    audio_long = SimpleNamespace(frames=192, fps_rational=(24, 1), duration_seconds=8.04)
    assert take.video_duration(audio_long) == 8.0
    assert take.video_duration(SimpleNamespace(frames=None, fps_rational=(24, 1), duration_seconds=8.04)) == 8.04
    value = normalize_request(_continue(lead_in="keep")).value
    source = _source(tmp_path / "candidate.mkv")
    job = tmp_path / "job"
    job.mkdir()
    hooks = _install_hook_fakes(monkeypatch, tmp_path, source, [], value)
    facts = comfyui_facts()
    real = facts.probe_media

    def probe(path):
        found = real(path)
        found.frames, found.duration_seconds = 192, 8.04
        return found

    engine = importlib.import_module("_h3_av_pack.shared.engine_facts")
    engine.install(type(facts)(**{**facts.__dict__, "probe_media": probe}) if hasattr(facts, "__dict__")
                   else facts)
    hooks.after_outputs(str(job), {}, {}, [str(source)])
    report = json.loads((job / "h3" / "take.json").read_text())
    assert report["range"] == [0.0, 8.0] and "error" not in report


def test_explicit_drop_keeps_the_digest_of_the_default() -> None:
    plain = normalize_request(_continue())
    explicit = normalize_request(_continue(lead_in="drop"))
    assert explicit.digest == plain.digest and explicit.value == plain.value
    assert normalize_request(_continue(lead_in="keep")).digest != plain.digest
    plain2, explicit2 = normalize_v2(_v2()), normalize_v2(_v2(output={"lead_in": "drop"}))
    assert explicit2.digest == plain2.digest and "output" not in explicit2.value
    with pytest.raises(H3RequestError, match="one of"):
        normalize_request(_continue(lead_in="nope"))
