"""Projected clip volume: absent stays absent unless the gain or a muted flag says otherwise."""

from __future__ import annotations

import pytest

from astrid.core.timeline import shot_composition_projection as scp


def _occurrence(**extra):
    base = {
        "occurrence_id": "occ-1",
        "project_id": "p",
        "timeline_id": "t",
        "output_identity": "out",
        "stable_deep_link": "link",
        "start": 0.0,
        "end": 10.0,
        "gain": 1,
        "muted": False,
    }
    base.update(extra)
    return base


def _revision():
    return {
        "shot_id": "shot-1",
        "revision_id": "rev-1",
        "internal_timeline_revision": {"revision_id": "irev-1"},
    }


def _project(clip, **occurrence_extra):
    return scp._project_clip(
        clip,
        occurrence=_occurrence(**occurrence_extra),
        revision=_revision(),
        occurrence_start=0.0,
        occurrence_end=10.0,
        occurrence_speed=1.0,
        path="clip",
    )


def test_element_clip_without_volume_and_gain_one_gets_no_volume_key():
    out = _project({"id": "fx", "track": "v1", "clipType": "am-footage", "at": 0, "hold": 2})
    assert "volume" not in out


def test_existing_volume_is_kept_at_gain_one():
    out = _project({"id": "a", "track": "a1", "clipType": "audio", "at": 0, "hold": 2, "volume": 0.5})
    assert out["volume"] == pytest.approx(0.5)


def test_absent_volume_with_gain_half_is_half():
    out = _project({"id": "a", "track": "a1", "clipType": "audio", "at": 0, "hold": 2}, gain=0.5)
    assert out["volume"] == pytest.approx(0.5)


def test_muted_occurrence_projects_zero():
    out = _project({"id": "fx", "track": "v1", "clipType": "am-footage", "at": 0, "hold": 2}, muted=True)
    assert out["volume"] == 0
