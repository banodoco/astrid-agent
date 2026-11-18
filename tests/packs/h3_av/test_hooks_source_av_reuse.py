"""The build hook reuses digest-bound conform measurements."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _hooks():
    path = (Path(__file__).resolve().parents[3] / "astrid/packs/h3_av/extensions/"
            "vibecomfy.workflows/hooks.py")
    spec = importlib.util.spec_from_file_location("h3_source_av_reuse_hooks", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_source_av_uses_digest_bound_conform_measurement_without_redecoding(tmp_path):
    measurement = {"has_audio": True, "video_frames": 48, "audio_samples": 96000}

    class Facts:
        def measure_source_av(self, _source):
            pytest.fail("digest-bound derivative must not be decoded twice")

    assert _hooks()._source_av_measurement(
        {"source_conform": {"decoded_av": measurement}}, tmp_path / "source.mp4", Facts()
    ) == measurement


def test_source_av_falls_back_to_measurement_without_conform_record(tmp_path):
    expected = {"has_audio": True, "video_frames": 24}

    class Facts:
        def measure_source_av(self, source):
            assert source == tmp_path / "source.mp4"
            return expected

    assert _hooks()._source_av_measurement({}, tmp_path / "source.mp4", Facts()) == expected


def test_source_av_keeps_silent_source_rejection_without_redecoding(tmp_path):
    class Facts:
        def measure_source_av(self, _source):
            pytest.fail("sealed silent derivative must not be decoded again")

    with pytest.raises(ValueError, match="H3_SOURCE_AV_MISSING_AUDIO"):
        _hooks()._source_av_measurement(
            {"source_conform": {"decoded_av": {"has_audio": False}}},
            tmp_path / "source.mp4", Facts(),
        )
