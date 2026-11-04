"""Inactive asset-stage tests using real native decode and public audio helpers.

The URL resolver exercises containment only; these are not normal-render or
trusted-producer positives. The original ENVELOPE_UNRENDERED requirement stays.
"""

import copy
import hashlib
import io
import json
import os
import shutil
import struct
import wave
from pathlib import Path

import pytest

from astrid.core.rendering.assets import AssetMaterializer
from astrid.core.rendering.remotion_runtime import RemotionRuntimeTools
from astrid.packs.rendering.rendering.renderers.remotion.normal_audio import (
    prepare_normal_audio_sources,
)


class ContainedURLs:
    def __init__(self, root):
        self.root = root

    def local_url(self, path):
        relative = Path(path).resolve(strict=True).relative_to(self.root)
        return "http://attempt.invalid/" + relative.as_posix()


@pytest.fixture
def stage(tmp_path, monkeypatch):
    # The suite redirects Python caches; the real launch policy rejects that
    # interpreter override, so keep it out of this native subprocess fixture.
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    pcm = io.BytesIO()
    with wave.open(pcm, "wb") as writer:
        writer.setparams((2, 2, 30000, 0, "NONE", "not compressed"))
        writer.writeframes(struct.pack("<hh", 8192, -4096) * 30000)
    payload = pcm.getvalue()
    other = bytearray(payload)
    for offset in range(44, len(other), 4):
        struct.pack_into("<hh", other, offset, -16384, 2048)
    other = bytes(other)
    registry = tmp_path / "assets.json"
    registry.write_text(json.dumps({"assets": {"sound": {
        "object_id": "object-sound", "digest": hashlib.sha256(payload).hexdigest(),
        "type": "audio/wav",
    }, "other": {
        "object_id": "object-other", "digest": hashlib.sha256(other).hexdigest(),
        "type": "audio/wav",
    }}}))
    with AssetMaterializer(registry, materialized_objects={
        "object-sound": payload, "object-other": other,
    }) as materializer:
        urls = ContainedURLs(materializer.serving_root)
        attempt = materializer.serving_root / "envelopes"
        attempt.mkdir()
        project = Path(os.environ.get("ASTRID_REMOTION_PROJECT_DIR", "remotion")).resolve()
        tools = RemotionRuntimeTools(Path(shutil.which("node")), "test-only", project / "unused-cli")
        props = {
            "timeline": {"tracks": [{"id": "A1", "kind": "audio", "volume": 0.5}], "clips": [{
                "id": "sound", "track": "A1", "asset": "sound", "at": 0.25,
                "from": 0.05, "to": 0.55, "speed": 1.5, "volume": 3,
                "params": {"fadeIn": 0.1, "fadeOut": 0.1, "fadeInCurve": "equal-power"},
                "app": {"astrid_shot_composition": {"source_offset": 350, "speed": 2}},
            }]},
            "assets": materializer.resolved_registry(urls),
            "compiledTransitions": {"fps": 30, "tracks": []},
        }

        def prepare(value, **kwargs):
            return prepare_normal_audio_sources(
                value, materializer=materializer, asset_server=urls, attempt_directory=attempt,
                project_dir=project, runtime_tools=tools, **kwargs,
            )

        yield props, prepare, attempt, registry, payload


@pytest.mark.parametrize("fps,origin", [(30, 2 / 30), (30000 / 1001, 1 / (30000 / 1001))])
def test_full_stereo_wav_and_exact_compiler_clock_without_authored_writes(stage, fps, origin):
    props, prepare, attempt, registry, payload = stage
    props["compiledTransitions"]["fps"] = fps
    before = copy.deepcopy(props)
    registry_before = registry.read_bytes()
    output = prepare(props)
    prepared = output["timeline"]["clips"][0]["audio"]["render_source"]
    assert prepared["source_from"] == origin
    assert prepared["playback_rate"] == 1.5
    assert json.loads(prepared["envelope_key"])["multiply"]
    wav = next(attempt.glob("normal-audio-*/envelope-*/source.wav")).read_bytes()
    assert wav[:4] == b"RIFF"
    assert struct.unpack_from("<HHI", wav, 20) == (3, 2, 30000)
    assert len(wav) == 44 + 30000 * 2 * 4
    assert struct.unpack_from("<ff", wav, 44 + 8000 * 8) == (0.25, -0.125)
    assert struct.unpack_from("<ff", wav, 44) == (0, 0)
    assert props == before
    assert registry.read_bytes() == registry_before
    assert hashlib.sha256(payload).hexdigest() in registry_before.decode()
    del output["timeline"]["clips"][0]["audio"]
    assert output == props  # includes scalar gain, metadata, at/from/to/speed and full registry


def test_wrong_root_asset_url_is_rejected_before_pcm(stage):
    props, prepare, attempt, _, _ = stage
    props["assets"]["assets"]["sound"]["file"] = "http://foreign.invalid/same-clock.wav"
    with pytest.raises(ValueError, match="registry differs"):
        prepare(props)
    assert list(attempt.iterdir()) == []


def test_wrong_bytes_under_right_managed_asset_identity_are_rejected(stage):
    _, _, _, registry, payload = stage
    with pytest.raises(ValueError, match="integrity check"):
        AssetMaterializer(registry, materialized_objects={"object-sound": payload[:-4] + b"nope"})


def test_prepared_output_cannot_be_applied_twice(stage):
    props, prepare, _, _, _ = stage
    output = prepare(props)
    with pytest.raises(ValueError, match="already prepared"):
        prepare(output)


def test_no_envelope_does_not_create_render_source_or_pcm(stage):
    props, prepare, attempt, _, _ = stage
    props["timeline"]["clips"][0].pop("params")
    assert prepare(props) == props
    assert list(attempt.iterdir()) == []


def test_replaced_staged_bytes_cannot_keep_the_original_managed_identity(stage):
    props, prepare, attempt, _, _ = stage
    url = props["assets"]["assets"]["sound"]["file"]
    materialized = attempt.parent / url.removeprefix("http://attempt.invalid/")
    materialized.write_bytes(materialized.read_bytes()[:-4] + b"nope")
    with pytest.raises(ValueError, match="integrity check"):
        prepare(props)
    assert list(attempt.iterdir()) == []


def test_forged_render_source_is_not_admission_even_with_a_valid_clock(stage):
    props, prepare, attempt, _, _ = stage
    props["timeline"]["clips"][0]["audio"] = {"render_source": {
        "file": "http://attempt.invalid/wrong.wav", "source_from": 2 / 30,
        "playback_rate": 1.5, "envelope_key": "{}",
    }}
    with pytest.raises(ValueError, match="already prepared"):
        prepare(props)
    assert list(attempt.iterdir()) == []


def test_failed_batch_removes_only_its_own_files(stage):
    props, prepare, attempt, _, _ = stage
    sentinel = attempt / "unrelated"
    sentinel.write_bytes(b"keep")
    second = copy.deepcopy(props["timeline"]["clips"][0])
    second["id"] = "bad"
    second["params"]["fadeInCurve"] = "unknown"
    props["timeline"]["clips"].append(second)
    before = copy.deepcopy(props)
    with pytest.raises(RuntimeError, match="ENVELOPE_UNRENDERED"):
        prepare(props)
    assert list(attempt.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"keep"
    assert props == before


def test_url_registration_failure_cleans_the_prepared_batch(stage, monkeypatch):
    props, prepare, attempt, _, _ = stage
    real_resolve = ContainedURLs.local_url

    def fail_prepared(self, path):
        if Path(path).name == "source.wav":
            raise ValueError("cannot register WAV")
        return real_resolve(self, path)

    monkeypatch.setattr(ContainedURLs, "local_url", fail_prepared)
    with pytest.raises(ValueError, match="cannot register"):
        prepare(props)
    assert list(attempt.iterdir()) == []


@pytest.mark.parametrize("fps", [30, 30000 / 1001])
def test_internal_original_clip_seam_preserves_phase_without_trusting_app(stage, fps):
    props, prepare, attempt, _, _ = stage
    props["compiledTransitions"]["fps"] = fps
    original = props["timeline"]["clips"][0]
    original.update(at=0, **{"from": 0.05, "to": 2})
    original["params"] = {"fadeIn": 0.5, "fadeOut": 0.5, "fadeInCurve": "equal-power"}
    original = copy.deepcopy(original)
    whole = prepare(props)
    whole_file = next(attempt.glob("normal-audio-*/envelope-*/source.wav"))
    whole_pcm = whole_file.read_bytes()[44:]
    sliced = props["timeline"]["clips"][0]
    sliced.update(at=0, **{"from": 0.8, "to": 1.8})
    # This caller-authored field is deliberately false; the separate owner input
    # retains the original compiled occurrence, with occurrence gain already in it.
    sliced["app"]["astrid_slice_origin"] = {"at": 99, "from": 99, "to": 99}
    before = copy.deepcopy(props)
    output = prepare(props, original_clips={"sound": original})
    prepared = output["timeline"]["clips"][0]["audio"]["render_source"]
    source_path = attempt.parent / prepared["file"].removeprefix("http://attempt.invalid/")
    part_pcm = source_path.read_bytes()[44:]
    # Translation can change floating operation ordering, so use the existing
    # attempt-PCM precision (seven decimal places), with no time/sample fitting.
    for whole_sample, part_sample in zip(struct.iter_unpack("<f", whole_pcm),
                                       struct.iter_unpack("<f", part_pcm), strict=True):
        assert part_sample[0] == pytest.approx(whole_sample[0], abs=1e-7)
    assert prepared["source_from"] == round(0.8 * fps) / fps
    assert prepared["playback_rate"] == 1.5
    assert whole["timeline"]["clips"][0]["volume"] == 3
    assert props == before


def test_successful_files_survive_until_the_attempt_owner_cleans_up(stage):
    props, prepare, attempt, _, _ = stage
    output = prepare(props)
    url = output["timeline"]["clips"][0]["audio"]["render_source"]["file"]
    path = attempt.parent / url.removeprefix("http://attempt.invalid/")
    assert path.is_file()
    # The source owner uses its existing ExitStack/TemporaryDirectory lifetime.
    shutil.rmtree(attempt)
    assert not path.exists()
    assert any(attempt.parent.glob("*.wav"))  # original materialized source remains


@pytest.mark.parametrize("tracks", [None, ("A1",)], ids=["window", "layer"])
def test_actual_slice_needs_original_compiled_clip_transport(stage, tracks):
    """The inactive adapter retains the compiled input across the host slice."""
    from astrid.core.rendering.contracts import FrameWindow

    props, prepare, attempt, _, _ = stage
    props["timeline"]["clips"][0].update(at=0, speed=1, **{"from": 0, "to": 2})
    props["timeline"]["clips"][0]["params"] = {"fadeIn": 0.5, "fadeOut": 0.5}
    before = copy.deepcopy(props)
    output = prepare(
        props, window=FrameWindow(start_frame=15, end_frame=45, fps_rational=(30, 1)),
        tracks=tracks,
    )
    url = output["timeline"]["clips"][0]["audio"]["render_source"]["file"]
    wav = (attempt.parent / url.removeprefix("http://attempt.invalid/")).read_bytes()
    assert struct.unpack_from("<ff", wav, 44 + 15000 * 8) == (0.25, -0.125)
    assert output["timeline"]["clips"][0]["from"] == 0.5
    assert props == before


@pytest.mark.parametrize("fps", [30, 30000 / 1001])
def test_window_transports_explicit_envelope_without_slice_origin_metadata(stage, fps):
    from astrid.core.rendering.contracts import FrameWindow

    props, prepare, attempt, _, _ = stage
    props["compiledTransitions"]["fps"] = fps
    clip = props["timeline"]["clips"][0]
    clip.update(id="sound_15_45", at=0, **{"from": 0.05, "to": 2})
    clip.pop("params")
    clip["audio"] = {"envelope": {"keyframes": [
        {"at": 0, "value": 0, "curve": "equal-power"}, {"at": 0.5, "value": 1},
    ]}}
    # A different track's invalid asset must never enter this layer's decode.
    props["timeline"]["tracks"].append({"id": "A2", "kind": "audio"})
    props["timeline"]["clips"].append({**clip, "id": "excluded", "track": "A2", "asset": "missing"})
    before = copy.deepcopy(props)
    selected = copy.deepcopy(props)
    selected["timeline"]["clips"].pop()
    whole = prepare(selected)
    sliced = prepare(props, window=FrameWindow(
        start_frame=15, end_frame=45,
        fps_rational=(30, 1) if fps == 30 else (30000, 1001),
    ), tracks=("A1",))

    def pcm(value):
        source = value["timeline"]["clips"][0]["audio"]["render_source"]
        return (attempt.parent / source["file"].removeprefix("http://attempt.invalid/")).read_bytes()[44:]

    for actual, expected in zip(struct.iter_unpack("<f", pcm(sliced)),
                                struct.iter_unpack("<f", pcm(whole)), strict=True):
        assert actual[0] == pytest.approx(expected[0], abs=1e-7)
    sliced_clip = sliced["timeline"]["clips"][0]
    assert "astrid_slice_origin" not in sliced_clip["app"]
    assert sliced_clip["audio"]["render_source"]["source_from"] == round(sliced_clip["from"] * fps) / fps
    assert sliced_clip["audio"]["render_source"]["playback_rate"] == 1.5
    assert sliced["timeline"]["tracks"] == [props["timeline"]["tracks"][0]]
    assert len(sliced["timeline"]["clips"]) == 1
    assert props == before


def test_exact_asset_keys_bind_distinct_managed_bytes_to_their_own_wav_urls(stage):
    props, prepare, attempt, _, _ = stage
    second = copy.deepcopy(props["timeline"]["clips"][0])
    second.update(id="other", asset="other")
    props["timeline"]["clips"].append(second)
    before = copy.deepcopy(props)
    output = prepare(props)
    first, second = [clip["audio"]["render_source"] for clip in output["timeline"]["clips"]]
    assert first["file"] != second["file"]
    for key in ("envelope_key", "source_from", "playback_rate"):
        assert first[key] == second[key]
    for row, expected in ((first, (0.25, -0.125)), (second, (-0.5, 0.0625))):
        path = attempt.parent / row["file"].removeprefix("http://attempt.invalid/")
        path.relative_to(attempt)
        assert struct.unpack_from("<ff", path.read_bytes(), 44 + 8000 * 8) == expected
    assert props == before
    assert output["assets"] == props["assets"]
