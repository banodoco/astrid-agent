"""Ordinary editable rows retain exact originals and use the existing recipes."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _json
from astrid.sdk.timeline.outbox import request_bytes
from astrid.sdk.timeline_sheet import NativeSheet, SheetError
from tests.editing_conformance.test_native_sheet_current import property_cell
from tests.editing_conformance.test_recipe_clip_scalar_fields import CHILD, app_bundle
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.editing_conformance.test_recipe_m2_clip_trim import REF, bundle
from tests.sdk.test_timeline_sheet_native import sheet_for
from tests.sdk.test_timeline_sync import initial_bundle


def edited(sheet):
    text = property_cell(sheet.text, REF, 'trim', '1.000001..5.000002')
    for key in ('hold', 'asset', 'transition'):
        text = property_cell(text, REF, key, 'clear')
    return text


@pytest.mark.parametrize('start', [0, 0.0, 0.1, 1e-6])
def test_exported_gutter_accepts_hold_edit_without_rewriting_timestamp(start):
    original = initial_bundle()
    row = State(original).entity('parent/clip:a')
    row.update(at=start, transition=None, opaque={'values': [None, False, 1, 1.0]})
    row['params'].update(size=None, italicWord=False)
    sheet = sheet_for(original)
    assert sheet.preview(sheet.text).commands == []
    plan = sheet.preview(property_cell(sheet.text, 'parent/clip:a', 'hold', '2'))
    expected = copy.deepcopy(row)
    expected['hold'] = 2
    assert request_bytes(State(plan.bundle).entity('parent/clip:a')) == request_bytes(expected)
    assert plan.commands == [capture_clip_set(original, 'parent/clip:a', values={'hold': 2})]
    assert _json(inverse(plan.bundle, plan.records).bundle) == _json(original)


def test_source_trim_clear_batch_uses_capturers_and_restores_exact_original():
    original = bundle(transition={'type': 'crossfade', 'duration': .25})
    before = request_bytes(original)
    sheet = sheet_for(original)
    assert sheet.preview(sheet.text).commands == sheet.preview(sheet.text).records == []
    assert request_bytes(sheet.preview(sheet.text).bundle) == before
    trim = capture_clip_trim(original, REF, in_s=1.000001, out_s=5.000002)
    intermediate = execute(original, [trim]).bundle
    clear = capture_clip_set(intermediate, REF, clear=['asset', 'hold', 'transition'])
    plan = sheet.preview(edited(sheet))
    assert plan.commands == [trim, clear]
    assert State(plan.bundle).entity(REF)['from'] == 1.000001
    assert State(plan.bundle).entity(REF)['to'] == 5.000002
    assert State(plan.bundle).entity(REF)['speed'] == 2
    assert all(key not in State(plan.bundle).entity(REF) for key in ('hold', 'asset', 'transition'))
    assert _json(inverse(plan.bundle, plan.records).bundle) == _json(original)
    assert request_bytes(original) == before
    assert not any(write['ref'] == 'parent' and not write['path']
                   for record in plan.records for write in record['writes'])


@pytest.mark.parametrize('start,end', [(1e-6, 1.000002), (0, 5.0), (0.0, 5)])
def test_source_numeric_spellings_noop_without_normalizing_int_float(start, end):
    original = bundle(**{'from': start, 'to': end, 'hold': 3})
    sheet = sheet_for(original)
    before = request_bytes(original)
    assert sheet.preview(sheet.text).commands == []
    assert request_bytes(sheet.preview(sheet.text).bundle) == before
    text = property_cell(sheet.text, REF, 'trim', f'{start:.6f}..{end:.6f}')
    assert sheet.preview(text).commands == []
    assert request_bytes(sheet.preview(text).bundle) == before


@pytest.mark.parametrize('field,value', [('trim', '0..11'), ('trim', 'clear'),
    ('trim', None), ('slip', '2'), ('speed', '2'), ('until', 'null')])
def test_unsupported_window_edits_keep_original(field, value):
    original = bundle()
    before = request_bytes(original)
    sheet = sheet_for(original)
    with pytest.raises((SheetError, CommandError)):
        sheet.preview(property_cell(sheet.text, REF, field, value))
    assert request_bytes(original) == before


def test_trim_can_introduce_a_source_window_using_registry_duration():
    original = bundle()
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0].pop('from')
        rows[0].pop('to')
    sheet = sheet_for(original)
    plan = sheet.preview(property_cell(sheet.text, REF, 'trim', '2..10'))
    assert plan.commands == [capture_clip_trim(original, REF, in_s=2, out_s=10)]
    assert inverse(plan.bundle, plan.records).bundle == original


def test_explicit_occurrence_fragment_preserves_shared_sibling_and_opaque_params():
    original = app_bundle()
    row = State(original).entity(CHILD)
    row.update(clipType='am-type', params={'text': 'Before', 'size': 32, 'italicWord': None,
        'opaque': {'units': [None, False, 1, 1.0]}, 'org.example/data': {'keep': True}})
    row['app']['org.example'] = {'null': None, 'count': 1.0}
    sheet = NativeSheet(original, basis={'revision_id': original['base_parent']['revision_id'],
                                        'publication_id': 'baseline'},
                        occurrence='f2-occurrence', clip='f2-agent-clip')
    assert list(sheet._rows) == [CHILD]
    assert sheet.preview(sheet.text).commands == []
    text = property_cell(sheet.text, CHILD, 'hold', '4.013')
    text = property_cell(text, CHILD, 'text', '""')
    text = property_cell(text, CHILD, 'size', 'clear')
    text = property_cell(text, CHILD, 'italicWord', 'clear')
    held = capture_clip_set(original, CHILD, values={'hold': 4.013})
    intermediate = execute(original, [held]).bundle
    params = capture_clip_set_params(intermediate, CHILD, values={'text': ''}, clear=['italicWord', 'size'])
    plan = sheet.preview(text)
    assert plan.commands == [held, params]
    assert request_bytes(State(plan.bundle).config('occurrence:shared-sibling')) == request_bytes(
        State(original).config('occurrence:shared-sibling'))
    after = State(plan.bundle).entity(CHILD)
    assert after['params']['opaque'] == row['params']['opaque']
    assert after['params']['org.example/data'] == {'keep': True}
    assert after['app'] == row['app']
    assert _json(inverse(plan.bundle, plan.records).bundle) == _json(original)
    # A fragment cannot acquire another row by changing its display identity or binding.
    with pytest.raises(SheetError):
        sheet.preview(text.replace(json.dumps(CHILD), json.dumps('occurrence:shared-sibling/clip:f2-agent-clip')))
    with pytest.raises(CommandError, match='parent/clip'):
        sheet.preview(property_cell(sheet.text, CHILD, 'trim', '0..1'))


def test_unchanged_known_numeric_param_does_not_rewrite_stored_integer():
    original = app_bundle()
    row = State(original).entity(CHILD)
    row.update(clipType='am-type', params={'size': 32, 'text': 'hello'})
    sheet = sheet_for(original)
    plan = sheet.preview(property_cell(sheet.text, CHILD, 'size', '32.0'))
    assert plan.commands == []
    assert request_bytes(plan.bundle) == request_bytes(original)


def test_trim_conflict_is_not_hidden_by_disjoint_parameters():
    from astrid.sdk.timeline.rebase import replay

    original = bundle()
    sheet = sheet_for(original)
    plan = sheet.preview(edited(sheet))
    head = copy.deepcopy(original)
    State(head).entity(REF)['params'] = {'unknown': [None, False, 1, 1.0]}
    result = replay(plan, head)
    assert State(result.bundle).entity(REF)['params'] == {'unknown': [None, False, 1, 1.0]}
    assert _json(inverse(result.bundle, result.records).bundle) == _json(head)
    State(head).entity(REF)['to'] = 7
    with pytest.raises(CommandError):
        replay(plan, head)
