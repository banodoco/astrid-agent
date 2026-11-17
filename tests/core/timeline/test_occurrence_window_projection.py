"""One conformance fixture for the source_offset window-origin definition (Agent projection and check)."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.timeline.shot_composition_projection import (
    ShotCompositionProjectionError,
    _project_clip,
)

FIXTURE = json.loads((Path(__file__).parents[2] / 'fixtures' / 'occurrence-window'
                      / 'start-trimmed-multi-clip.json').read_text())


def project(occurrence, clip):
    start = occurrence['placement']['start_ms'] / 1000
    return _project_clip(copy.deepcopy(clip), occurrence=occurrence, revision={'shot_id': 'shot-1', 'revision_id': 'r1',
                                          'internal_timeline_revision': {'revision_id': 'i1'}},
                         occurrence_start=start, occurrence_end=start + occurrence['duration_ms'] / 1000,
                         occurrence_speed=1.0, track_definitions=[{'id': 'picture', 'kind': 'visual'}],
                         path='fixture')


def test_agent_projection_follows_the_window_origin():
    occurrence = FIXTURE['occurrence']
    projected = {}
    for clip in FIXTURE['clips']:
        out = project(occurrence, clip)
        if out is not None:
            projected[clip['id']] = out
    assert sorted(projected) == sorted(row['id'] for row in FIXTURE['expected'])
    assert set(FIXTURE['hidden']).isdisjoint(projected)
    for row in FIXTURE['expected']:
        got = projected[row['id']]
        assert got['id'] == 'occ-1:' + row['id']
        for key in ('at', 'hold', 'from'):
            assert got[key] == pytest.approx(row[key]), (row['id'], key)


def test_scalar_and_mapping_spellings_agree_and_a_negative_origin_is_refused():
    scalar = dict(FIXTURE['occurrence'], source_offset=2000)
    for clip in FIXTURE['clips']:
        got, want = project(scalar, clip), project(FIXTURE['occurrence'], clip)
        assert (got and {k: got[k] for k in ('at', 'hold', 'from')}) == (
            want and {k: want[k] for k in ('at', 'hold', 'from')})
    with pytest.raises(ShotCompositionProjectionError):
        project(dict(scalar, source_offset=-1), FIXTURE['clips'][0])
