"""The official parent-track command through the real CLI parser and outbox."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._track_commands import capture_track_set
from tests.packs.timeline import test_cli_commit_move
from tests.sdk.test_timeline_sheet_native import edit_cell

command = test_cli_commit_move.command
sync_connection = test_cli_commit_move.sync_connection


@pytest.mark.parametrize('args,value', [(('--set', '画面 🎬'), '画面 🎬'), (('--set', ''), ''),
                                       (('--clear',), None)])
def test_track_label_apply_matches_native_capture(command, capsys, tmp_path, args, value):
    run, generated = command
    if value is None:
        State(generated.bundles['r0']).entity('parent/track:picture')['label'] = 'Existing'
    original = copy.deepcopy(generated.bundles['r0'])
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    text = capsys.readouterr().out
    edited = edit_cell(text, 'parent/track:picture', 'clear' if value is None else json.dumps(value), label=True)
    path = tmp_path / 'track-label.sheet'
    path.write_text(edited)
    assert run('apply', 't', str(path), '--json') == 0
    expected = capture_track_set(original, 'parent/track:picture',
                                 values={} if value is None else {'label': value},
                                 clear=['label'] if value is None else [])
    body = json.loads(generated.calls[0][0])
    assert body['commands'] == [expected] and body['source'] == {'via': 'sheet'}
    result = json.loads(capsys.readouterr().out)
    assert result['receipt']['receipt_id'] == result['data']['accepted_publication_id'] == 'pub1'
    track = State(generated.bundles['r1']).entity('parent/track:picture')
    assert ('label' not in track) if value is None else track['label'] == value


def test_apply_matches_full_app_portable_golden(command, capsys, tmp_path):
    run, generated = command
    fixture = json.loads((Path(__file__).parents[2] / 'editing_conformance/fixtures/'
                         'recipe_m2_app_track_label.json').read_text())
    base_revision = fixture['bundle']['base_parent']['revision_id']
    fixture_bundle = copy.deepcopy(fixture['bundle'])
    fixture_bundle.update(project_id='p', timeline_id='t')
    expected_after = copy.deepcopy(fixture['after_bundle'])
    expected_after.update(project_id='p', timeline_id='t')
    generated.bundles[base_revision] = fixture_bundle
    generated.head = {'revision_id': base_revision, 'publication_id': 'pub0'}
    generated.publications = {base_revision: 'pub0'}
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    edited = edit_cell(capsys.readouterr().out, fixture['input']['target_ref'],
                       json.dumps(fixture['input']['label']), label=True)
    path = tmp_path / 'track-label.sheet'
    path.write_text(edited)
    assert run('apply', 't', str(path), '--json') == 0
    assert json.loads(generated.calls[0][0])['commands'] == [fixture['command']]
    assert generated.bundles['r1'] == expected_after
    capsys.readouterr()


def test_cli_sheet_preview_and_lost_ack_retry(command, capsys, tmp_path):
    run, generated = command
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    path = tmp_path / 'track-label.sheet'
    path.write_text(edit_cell(capsys.readouterr().out, 'parent/track:picture', '"Picture"', label=True))
    args = ('apply', 't', str(path))
    assert run(*args, '--dry-run') == 0
    assert capsys.readouterr().out.strip() == 'preview only; no publication accepted'
    assert not generated.accepted
    generated.lose_ack = True
    assert run(*args, '--json') == 1
    assert not json.loads(capsys.readouterr().out)['ok']
    assert run(*args, '--json') == 0
    assert generated.calls[-1] == generated.calls[-2]
    assert json.loads(capsys.readouterr().out)['data']['accepted_publication_id'] == 'pub1'


@pytest.mark.parametrize('args', [(), ('--set', 'A', '--clear'), ('--set', 'A', '--remove'),
                                ('--set', 'A', '--set-values', 'opaque=x')])
def test_edit_requires_a_current_operation_and_refuses_retired_track_flags(command, args):
    run, generated = command
    try:
        result = run('edit', 't', '--track', 'parent/track:picture', *args, '--json')
    except SystemExit as error:
        assert error.code == 2
    else:
        assert result == 2
    assert generated.calls == []


def test_cli_sheet_track_selector_is_parent_scoped(command, capsys, tmp_path):
    run, generated = command
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    edited = capsys.readouterr().out.replace('parent/track:picture', 'picture', 1)
    path = tmp_path / 'wrong-track.sheet'
    path.write_text(edited)
    assert run('apply', 't', str(path), '--json') == 1
    assert not json.loads(capsys.readouterr().out)['ok']
    assert generated.calls == []


def test_help_exposes_current_edit_entry_and_legacy_working_copy(command, capsys):
    run, _ = command
    with pytest.raises(SystemExit) as error:
        run('edit', '--help')
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert 'legacy detached working copy' in output
    assert '--set' in output and '--swap-asset' in output


def test_appearance_clip_edit_uses_guarded_commit(command, capsys):
    run, generated = command
    bundle = generated.bundles['r0']
    bundle['parent']['config']['output'].update(resolution='1280x720', file='output.mp4')
    for clip in bundle['parent']['clips']:
        clip['clipType'] = 'media'
    bundle['parent']['clips'][1]['x'] = 10
    assert run('appearance', 't', '--project', 'p', '--clip', 'parent/clip:a',
               '--set', 'fit=cover', '--json') == 0
    body = json.loads(generated.calls[0][0])
    assert body['commands'][0]['op'] == 'clip.set'
    assert body['merge'] == 'atomic'
    state = State(generated.bundles['r1'])
    assert state.entity('parent/clip:a')['fit'] == 'cover'
    assert state.entity('parent/clip:b')['fit'] == 'cover'
    assert state.entity('parent/track:picture')['app']['clipPlacementVersion'] == 1
    capsys.readouterr()


def test_appearance_inspects_package_placement_without_render(command, capsys):
    run, generated = command
    parent = generated.bundles['r0']['parent']
    parent['config']['output'].update(resolution='1280x720', file='output.mp4')
    parent['registry'] = {'assets': {'matrix': {'resolution': '1920x800'}}}
    parent['clips'][0].update(clipType='media', asset='matrix', fit='cover')
    assert run('appearance', 't', '--project', 'p', '--clip', 'parent/clip:a', '--json') == 0
    data = json.loads(capsys.readouterr().out)['data']
    assert data['placement']['local']['media']['x'] == -224
    assert data['placement']['provenance']['fit'] == 'clip'
    assert generated.calls == []


def test_appearance_track_inspection_reports_canvas_context(command, capsys):
    run, generated = command
    generated.bundles['r0']['parent']['config']['output'].update(resolution='1280x720', file='output.mp4')
    assert run('appearance', 't', '--project', 'p', '--track', 'parent/track:picture', '--json') == 0
    data = json.loads(capsys.readouterr().out)['data']
    assert data['canvas'] == data['source_canvas'] == {'width': 1280, 'height': 720}
    assert data['canvas_owner'] == data['source_canvas_owner'] == 'parent'
    assert data['canvas_timeline_id'] == 't'
    assert generated.calls == []


def test_appearance_inspection_matches_normalized_rect_projection(command, capsys):
    run, generated = command
    parent = generated.bundles['r0']['parent']
    parent['config']['output'].update(resolution='1280x720', file='output.mp4')
    parent['config']['tracks'][0]['scale'] = 2
    parent['registry'] = {'assets': {'matrix': {'resolution': '1920x800'}}}
    parent['clips'][0].update(clipType='media', asset='matrix', rect={'x': 10, 'y': 10, 'width': 100, 'height': 100})
    assert run('appearance', 't', '--project', 'p', '--clip', 'parent/clip:a', '--json') == 0
    data = json.loads(capsys.readouterr().out)['data']
    assert data['placement']['fit'] == 'cover'
    assert data['placement']['local']['full'] == {'x': 325, 'y': 185, 'width': 50, 'height': 50}


@pytest.mark.parametrize('fit', ['contain', 'cover'])
@pytest.mark.parametrize('resolution', ['1920x800x999', '1920.5x800', ' 1920x800'])
def test_appearance_inspect_refuses_malformed_intrinsic_resolution(command, capsys, fit, resolution):
    run, generated = command
    parent = generated.bundles['r0']['parent']
    parent['config']['output'].update(resolution='1280x720', file='output.mp4')
    parent['registry'] = {'assets': {'matrix': {'resolution': resolution}}}
    parent['clips'][0].update(clipType='media', asset='matrix', fit=fit)
    assert run('appearance', 't', '--project', 'p', '--clip', 'parent/clip:a', '--json') != 0
    error = json.loads(capsys.readouterr().out)['error']['message']
    assert 'a' in error and 'matrix' in error and 'resolution' in error
    assert generated.calls == []


def test_appearance_inspects_canvasless_parent_with_named_legacy_warning(command, capsys):
    run, generated = command
    parent = generated.bundles['r0']['parent']
    parent['config'].pop('output', None)
    parent['registry'] = {'assets': {'matrix': {'resolution': '1920x800'}}}
    parent['clips'][0].update(clipType='media', asset='matrix', fit='cover')
    original = copy.deepcopy(parent)
    assert run('appearance', 't', '--project', 'p', '--clip', 'parent/clip:a', '--json') == 0
    data = json.loads(capsys.readouterr().out)['data']
    assert data['canvas'] == {'width': 1920, 'height': 1080}
    assert data['source_canvas_description'] == 'legacy render-resolved; not authored'
    assert data['warnings'][0]['source'] == 'legacy-render-resolved'
    assert 'not written' in data['warnings'][0]['message']
    assert data['warnings'][0]['after']['resolution'] == '1920x1080'
    assert parent == original and generated.calls == []


def test_appearance_warns_for_authored_canvas_override_without_output(command, capsys):
    run, generated = command
    parent = generated.bundles['r0']['parent']
    parent['config'].pop('output', None)
    parent['config']['theme_overrides'] = {'visual': {'canvas': {'width': 1280, 'height': 720, 'fps': 30}}}
    parent['registry'] = {'assets': {'matrix': {'resolution': '1920x800'}}}
    parent['clips'][0].update(clipType='media', asset='matrix', fit='cover')
    original = copy.deepcopy(parent)
    assert run('appearance', 't', '--project', 'p', '--clip', 'parent/clip:a', '--json') == 0
    data = json.loads(capsys.readouterr().out)['data']
    warning = data['warnings'][0]
    assert warning['source'] == 'theme_overrides.visual.canvas'
    assert warning['after']['resolution'] == '1280x720'
    assert 'not written' in warning['message']
    assert parent == original and generated.calls == []


@pytest.mark.parametrize('kind', ['clip', 'track'])
def test_appearance_occurrence_warns_for_child_authored_canvas_without_output(command, capsys, kind):
    from tests.editing_conformance.test_recipe_clip_scalar_fields import app_bundle

    run, generated = command
    bundle = app_bundle()
    bundle.update(project_id='p', timeline_id='t')
    bundle['parent']['config']['output'] = {'resolution': '1920x1080', 'fps': 30, 'file': 'out.mp4'}
    shot_id = next(row['shot_id'] for row in bundle['placements'] if row['occurrence_id'] == 'f2-occurrence')
    child = bundle['shots'][shot_id]['internal_timeline']
    child.pop('output', None)
    child['theme_overrides'] = {'visual': {'canvas': {'width': 1280, 'height': 720, 'fps': 30}}}
    child['registry']['assets']['f2-placeholder']['resolution'] = '1920x1080'
    generated.bundles['r0'] = bundle
    before = copy.deepcopy(bundle)
    target = ('--clip', 'occurrence:f2-occurrence/clip:f2-agent-clip') if kind == 'clip' else (
        '--track', 'occurrence:f2-occurrence/track:V2')
    assert run('appearance', 't', '--project', 'p', *target, '--json') == 0
    data = json.loads(capsys.readouterr().out)['data']
    assert data['source_canvas'] == {'width': 1280, 'height': 720}
    assert data['canvas'] == {'width': 1920, 'height': 1080}
    assert len(data['warnings']) == 1
    warning = data['warnings'][0]
    assert warning['source'] == 'theme_overrides.visual.canvas'
    assert warning['after']['resolution'] == '1280x720' and warning['after']['fps'] == 30
    assert 'not written' in warning['message']
    assert generated.bundles['r0'] == before and generated.calls == []
