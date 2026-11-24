import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.media_swap import capture_document_swaps
from tests.timeline.test_occurrence_media_swap import (
    CLIP, OWNER, CatalogTransport, _candidate, _legacy_bundle, _parsed_swap,
)


def test_swap_with_changed_placement_is_refused():
    base = _legacy_bundle()
    candidate = _candidate(base, 'image')
    candidate['placements'][0]['duration_ms'] += 100
    State(candidate).registry(OWNER)['assets']['new-footage']['type'] = 'audio/wav'
    with pytest.raises(ValueError, match='closure membership or provenance changed'):
        capture_document_swaps(base, candidate, CatalogTransport())


def test_swap_rejects_runtime_media_kind_mismatch():
    base = _legacy_bundle()
    with pytest.raises(ValueError, match='Runtime metadata'):
        capture_document_swaps(base, _candidate(base, 'image'), CatalogTransport(kind='audio'))


def test_ordinary_and_mixed_edits_leave_non_swap_changes_for_emitter():
    base = _legacy_bundle()
    ordinary = copy.deepcopy(base)
    State(ordinary).entity(CLIP)['label'] = 'new label'
    commands, working, _ = capture_document_swaps(base, ordinary, CatalogTransport())
    assert commands == [] and working == base
    mixed = _candidate(base, 'image')
    State(mixed).entity(CLIP)['label'] = 'new label'
    commands, working, _ = capture_document_swaps(base, mixed, CatalogTransport())
    assert commands
    assert State(working).entity(CLIP)['asset'] == 'new-footage'
    assert State(working).entity(CLIP).get('label') == State(base).entity(CLIP).get('label')


def test_sdk_ordinary_edit_keeps_document_step_label():
    from astrid.sdk.timeline.commit_session import Timeline
    base = _legacy_bundle()
    session = SimpleNamespace(client=SimpleNamespace(_remote=SimpleNamespace(_transport=CatalogTransport())),
                              project_id='project-1', identity={'realm_id': 'realm-1'})
    timeline = Timeline(session, base, base.get('base_parent'), evaluating=True)
    State(timeline.bundle).entity(CLIP)['label'] = 'new label'
    timeline._capture_free_edits()
    assert len(timeline._steps) == 1
    assert timeline._steps[0]['label'] == 'Edit document'


@pytest.mark.parametrize(('selector', 'expected'), [('@62.5', ('cut', '@62.5')), ('1:02', ('cut', '1:02')), ('picture-id', ('cut', 'picture-id')), (' C1 ', ('picture', 'c01')), ('2', ('cut', 2))])
def test_cli_swap_cut_selection_and_provenance(monkeypatch, selector, expected):
    from astrid.packs.timeline.cli_native import commit_swap
    from astrid.sdk.timeline.commit_session import Timeline
    calls = []
    clip = SimpleNamespace(swap_asset=lambda value: calls.append(('swap', value)))
    class FakeTimeline:
        def _cut_id(self, value):
            return 'c01' if value == 'c1' else value
        def cut_picture(self, value):
            calls.append(('picture', value))
            return clip
        def cut(self, value):
            calls.append(('cut', value))
            return SimpleNamespace(picture=clip)
        def commit(self, fn, **kwargs):
            fn(self)
            calls.append(('via', self._commit_via))
    timeline = FakeTimeline()
    monkeypatch.setattr(Timeline, 'open', lambda *args, **kwargs: timeline)
    session = SimpleNamespace(project_id='p', timeline_id='t', client=None,
                              outbox=SimpleNamespace(path='outbox'))
    commit_swap(session, _parsed_swap(clip=None, cut=selector), {'via': 'cli'})
    assert calls == [expected, ('swap', 'new-footage'), ('via', 'cli')]


def test_session_submit_passes_cli_provenance_to_frozen_request(monkeypatch):
    from astrid.sdk.timeline.sync import TimelineSession
    session = object.__new__(TimelineSession)
    seen = {}
    monkeypatch.setattr(session, '_flush', lambda **kwargs: seen.update(kwargs))
    session.submit([], label='swap', steps=[], expected_state={}, via='cli')
    assert seen['via'] == 'cli'
