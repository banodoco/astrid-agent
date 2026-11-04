from __future__ import annotations

import pytest

from astrid.core.timeline import banodoco_schema
from astrid.core.timeline.validators.timeline import _validate_clip_transitions, validate_timeline


def _clip(clip_id: str, at: float, *, track: str = "v1", **fields):
    clip = {
        "id": clip_id,
        "at": at,
        "track": track,
        "clipType": "media",
        "hold": 2,
    }
    clip.update(fields)
    return clip


def _timeline(clips):
    return {
        "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
        "clips": clips,
    }


def test_timeline_without_transitions_does_not_load_transition_catalog(monkeypatch):
    def fail_if_called():
        raise AssertionError("transition catalog is not needed without transition-bearing clips")

    monkeypatch.setattr(banodoco_schema, "_transition_ids", fail_if_called)
    validate_timeline(_timeline([_clip("a", 0)]))


def test_transition_validation_consults_catalog_once(monkeypatch):
    calls = []
    monkeypatch.setattr(banodoco_schema, "_transition_ids", lambda: calls.append(True) or {"cross-fade"})

    _validate_clip_transitions(
        [
            _clip("a", 0, transition={"id": "cross-fade", "duration": 0.5}),
            _clip("b", 2),
        ],
        30,
    )

    assert calls == [True]


def test_transition_catalog_refusal_is_preserved(monkeypatch):
    refusal = RuntimeError("transition catalog integrity failure")

    def refuse_catalog():
        raise refusal

    monkeypatch.setattr(banodoco_schema, "_transition_ids", refuse_catalog)

    with pytest.raises(RuntimeError, match="integrity failure"):
        _validate_clip_transitions([_clip("a", 0, transition="cross-fade")], 30)


@pytest.mark.parametrize(
    "transition",
    [None, "", {}, {"id": ""}, {"params": {}}, {"id": "cross-fade", "duration": "slow"}],
)
def test_malformed_transition_values_still_consult_catalog_and_refuse(monkeypatch, transition):
    calls = []
    monkeypatch.setattr(banodoco_schema, "_transition_ids", lambda: calls.append(True) or {"cross-fade"})

    with pytest.raises(ValueError):
        _validate_clip_transitions([_clip("a", 0, transition=transition)], 30)

    assert calls == [True]


def test_unknown_transition_id_is_refused_by_catalog(monkeypatch):
    monkeypatch.setattr(banodoco_schema, "_transition_ids", lambda: {"cross-fade"})

    with pytest.raises(ValueError, match="not present in the transitions catalog"):
        _validate_clip_transitions([_clip("a", 0, transition="missing")], 30)


def test_transition_duration_uses_fps_and_same_track_adjacent_clip(monkeypatch):
    monkeypatch.setattr(banodoco_schema, "_transition_ids", lambda: {"cross-fade"})

    with pytest.raises(ValueError, match="fit both adjacent same-track clips"):
        _validate_clip_transitions(
            [
                _clip("a", 0, hold=1, transition={"id": "cross-fade", "durationFrames": 31}),
                _clip("b", 1),
            ],
            30,
        )

    _validate_clip_transitions(
        [
            _clip("a", 0, hold=1, transition={"id": "cross-fade", "durationFrames": 31}),
            _clip("b", 1, track="v2"),
        ],
        30,
    )
    _validate_clip_transitions(
        [_clip("a", 0, hold=1, transition={"id": "cross-fade", "durationFrames": 31})],
        30,
    )


def test_transition_adjacency_is_sorted_by_timeline_position(monkeypatch):
    monkeypatch.setattr(banodoco_schema, "_transition_ids", lambda: {"cross-fade"})

    # The input order is deliberately not timeline order. The transition's
    # following same-track clip is the one at 3, not the short clip at 0.
    _validate_clip_transitions(
        [
            _clip("after", 3, hold=2),
            _clip("transition", 2, hold=2, transition={"id": "cross-fade", "duration": 1.5}),
            _clip("before", 0, hold=0.5),
        ],
        30,
    )


def test_catalog_integrity_precedes_malformed_transition_clip_sort(monkeypatch):
    refusal = RuntimeError("transition catalog integrity failure")

    def refuse_catalog():
        raise refusal

    monkeypatch.setattr(banodoco_schema, "_transition_ids", refuse_catalog)

    with pytest.raises(RuntimeError, match="integrity failure"):
        _validate_clip_transitions(
            [_clip("a", "not-a-number", transition="cross-fade")],
            30,
        )


def test_transition_on_non_string_track_still_loads_catalog(monkeypatch):
    refusal = RuntimeError("transition catalog integrity failure")

    def refuse_catalog():
        raise refusal

    monkeypatch.setattr(banodoco_schema, "_transition_ids", refuse_catalog)

    with pytest.raises(RuntimeError, match="integrity failure"):
        _validate_clip_transitions(
            [_clip("ignored", 0, track=42, transition="cross-fade")],
            30,
        )
