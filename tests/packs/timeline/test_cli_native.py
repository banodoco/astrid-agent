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
