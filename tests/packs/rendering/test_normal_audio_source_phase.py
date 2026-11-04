"""Slicing an audio clip must preserve its original fade phase.

The inactive producer retains the original compiled clip across the host
slice. These local WAV tests do not qualify HTTP serving or normal rendering.
"""

import copy
import struct

import pytest

from astrid.core.rendering.contracts import FrameWindow
from astrid.core.rendering.service import RenderService
from tests.packs.rendering import test_normal_audio_sources

WINDOW = FrameWindow(start_frame=15, end_frame=45, fps_rational=(30, 1))
stage = test_normal_audio_sources.stage


def _timeline():
    return {
        "tracks": [{"id": "A1", "kind": "audio"}],
        "clips": [{"id": "sound", "track": "A1", "asset": "sound", "at": 0,
                   "from": 0, "to": 2,
                   "params": {"fadeIn": 0.5, "fadeOut": 0.5}}],
    }


def test_slice_records_original_phase_that_the_cut_clip_cannot_recover():
    first = _timeline()
    second = copy.deepcopy(first)
    second["clips"][0].update(at=0.5, **{"from": 0.5})
    before = copy.deepcopy(first)
    a = RenderService._window_timeline(first, WINDOW)["clips"][0]
    b = RenderService._window_timeline(second, WINDOW)["clips"][0]
    # Same cut clip; original gain at source .5 is 1 in first, 0 in second.
    assert {k: v for k, v in a.items() if k != "app"} == {k: v for k, v in b.items() if k != "app"}
    assert a["app"]["astrid_slice_origin"] == {"id": "sound", "at": 0, "from": 0, "to": 2}
    assert b["app"]["astrid_slice_origin"] == {"id": "sound", "at": 0.5, "from": 0.5, "to": 2}
    assert a["at"] == 0 and a["from"] == 0.5
    assert first == before


def test_slice_origin_is_host_owned_and_only_for_fading_clips():
    timeline = _timeline()
    timeline["clips"][0]["app"] = {"astrid_slice_origin": {"at": 9}, "keep": 1}
    timeline["clips"].append({"id": "plain", "track": "A1", "asset": "x", "at": 0,
                              "from": 0, "to": 2,
                              "app": {"astrid_slice_origin": {"at": 9}}})
    fading, plain = RenderService._window_timeline(timeline, WINDOW, tracks=["A1"])["clips"]
    assert fading["app"] == {"keep": 1, "astrid_slice_origin": {"id": "sound", "at": 0, "from": 0, "to": 2}}
    assert plain["app"] == {}


@pytest.mark.parametrize("asset", ["sound", "other"])
@pytest.mark.parametrize("tracks", [None, ("A1", "A2")], ids=["window", "layer"])
def test_half_second_slice_pcm_equals_original_on_attempt_wav(stage, tracks, asset):
    props, prepare, attempt, _, _ = stage
    props["timeline"]["clips"] = _timeline()["clips"]
    clip = props["timeline"]["clips"][0]
    clip["asset"] = asset
    clip["volume"] = 3
    clip["app"] = {"astrid_slice_origin": {"id": "forged", "from": 99, "to": 100}}
    before = copy.deepcopy(props)
    whole = prepare(props)
    sliced = prepare(props, window=WINDOW, tracks=tracks)

    def wav(value):
        source = value["timeline"]["clips"][0]["audio"]["render_source"]
        path = attempt.parent / source["file"].removeprefix("http://attempt.invalid/")
        path.relative_to(attempt)
        assert source["playback_rate"] == 1
        return path.read_bytes()

    whole_wav, slice_wav = wav(whole), wav(sliced)
    assert len(slice_wav) == len(whole_wav) == 44 + 30000 * 2 * 4
    for actual, expected in zip(struct.iter_unpack("<f", slice_wav[44:]),
                                struct.iter_unpack("<f", whole_wav[44:]), strict=True):
        assert actual[0] == pytest.approx(expected[0], abs=1e-7)
    expected = (0.25, -0.125) if asset == "sound" else (-0.5, 0.0625)
    assert struct.unpack_from("<ff", slice_wav, 44 + 15000 * 8) == expected
    sliced_clip = sliced["timeline"]["clips"][0]
    assert sliced_clip["audio"]["render_source"]["source_from"] == 0.5
    assert sliced_clip["app"]["astrid_slice_origin"] == {
        "id": "sound", "at": 0, "from": 0, "to": 2,
    }
    assert sliced_clip["volume"] == 3
    assert sliced["timeline"]["tracks"][0]["volume"] == 0.5
    assert sliced["assets"] == props["assets"]
    assert props == before
