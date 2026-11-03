"""One bounded sheet gesture across the ordinary CLI, Python and sheet paths."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.rebase import replay
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_sheet import NativeSheet, _tokens
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.packs.timeline import test_cli_native_sheet
from tests.sdk import test_timeline_sync
from tests.sdk.test_timeline_sheet_native import edit_cell, sheet_for
from tests.sdk.test_timeline_sync import initial_bundle

native_cli = test_cli_native_sheet.native_cli
connected = test_timeline_sync.connected
REF = 'parent/clip:a'
TRACK = 'parent/track:picture'


def command(op, select, args):
    return {'schema_version': 1, 'op': op, 'version': '1', 'select': select, 'args': args}


def property_cell(text, ref, key, value):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        body, separator, binding = line.rpartition(' # ref ')
        if separator and json.loads(binding) == ref:
            tokens = _tokens(body.strip())
            tokens = [token for token in tokens if not token.startswith(key + '=')]
            if value is not None:
                tokens.append(key + '=' + value)
            lines[index] = '  ' + '  '.join(tokens) + separator + binding
            return '\n'.join(lines) + '\n'
    raise AssertionError(ref)


def gesture(sheet, *, clear_hold=False, ref=REF, track=TRACK):
    text = edit_cell(sheet.text, ref, '3.017')
    for key, value in [('hold', 'clear' if clear_hold else '2.013'),
                       ('asset', 'clear'), ('transition', 'clear'), ('text', '""')]:
        text = property_cell(text, ref, key, value)
    return edit_cell(text, track, '""', label=True)


def requests(*, clear_hold=False, ref=REF, track=TRACK):
    return [command('clip.move', ref, {'on': 3.017}),
            command('clip.set', ref, {'values': {} if clear_hold else {'hold': 2.013},
                                    'clear': ['asset', 'hold', 'transition'] if clear_hold else ['asset', 'transition']}),
            command('clip.set-params', ref, {'values': {'text': ''}, 'clear': []}),
            command('track.set', track, {'values': {'label': ''}, 'clear': []})]


def steps_for(commands, label, tags=()):
    return [{'id': f'step-{index + 1}', 'label': label, 'intent': '', 'tags': list(tags),
             'command_ordinals': [index], 'patch': []}
            for index, _command in enumerate(commands)]


def assert_current_commit_request(body, commands):
    """The four frontends must submit one complete v2 commit request."""
    assert body['api_version'] == 1
    assert body['project_id'] == 'p' and body['timeline_id'] == 't'
    assert (body['base_revision_id'], body['base_publication_id']) == ('r0', 'pub0')
    assert body['commands'] == commands
    assert isinstance(body['label'], str) and body['label']
    assert body['merge'] == 'fields' and body['dry_run'] is False
    assert body['source']['via'] in {'python', 'cli', 'sheet'}
    assert len(body['steps']) == len(commands)
    assert [ordinal for step in body['steps'] for ordinal in step['command_ordinals']] == list(range(len(commands)))
    assert all(step['id'] and step['label'] and step['intent'] == ''
               and isinstance(step['tags'], list) and step['patch'] == []
               for step in body['steps'])


@pytest.mark.parametrize('entry', ['python', 'cli', 'sheet-python', 'sheet-cli'])
@pytest.mark.parametrize('clear_hold', [False, True])
def test_same_semantics_original_pins_exact_commands_reload_inverse(native_cli, tmp_path, capsys, entry, clear_hold):
    run, client, generated = native_cli
    original = generated.bundles['r0']
    row = State(original).entity(REF)
    row.update(hold=1.013, speed=2, **{'from': 0, 'to': 4},
               asset='', transition={'opaque': [None, False]}, opaque={'bool': True, 'number': 1})
    row['params']['opaque'] = {'text': 'keep …', 'list': [None, False, 1]}
    original['parent']['config']['output']['fps'] = {'numerator': 30000, 'denominator': 1001}
    original = copy.deepcopy(original)
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    text = capsys.readouterr().out
    session = TimelineSession.open('p', 't', client=client)
    sheet = NativeSheet.load(session)
    edited = gesture(sheet, clear_hold=clear_hold)
    plan = execute(original, requests(clear_hold=clear_hold))
    assert sheet.preview(edited).commands == plan.commands
    path = tmp_path / 'edit.txt'
    if entry == 'python':
        python_commands = requests(clear_hold=clear_hold)
        result = session.commit_many(
            python_commands, label='Timeline edit', steps=steps_for(python_commands, 'Timeline edit'),
            merge='fields', reads=[],
        )
    elif entry == 'cli':
        path.write_text(edited)
        assert run('apply', 't', str(path), '--json') == 0
        result = json.loads(capsys.readouterr().out)
    elif entry == 'sheet-python':
        result = sheet.apply(edited, session=session, dry_run=False)
    else:
        path.write_text(edited)
        assert run('apply', 't', str(path), '--json') == 0
        result = json.loads(capsys.readouterr().out)
        assert path.read_text() == edited
    body = json.loads(generated.calls[-1][0])
    assert_current_commit_request(body, plan.commands)
    assert len(generated.accepted) == 1
    assert generated.bundles['r1'] == plan.bundle
    after = State(plan.bundle).entity(REF)
    assert after['opaque'] == row['opaque'] and after['params']['opaque'] == row['params']['opaque']
    assert 'asset' not in after and 'transition' not in after
    if clear_hold:
        assert 'hold' not in after and after['app']['astrid']['for'] == 2
    else:
        assert after['hold'] == 2.013 and after['app']['astrid']['for'] == 2.013 / 2
    restarted = TimelineSession.open('p', 't', client=client)
    restarted.undo([result['receipt']['receipt_id']])
    restored = copy.deepcopy(original)
    restored['base_parent'].update(revision_id='r1', publication_id='pub1')
    assert generated.bundles['r2'] == restored
    restarted.undo(['pub2'])
    restored_plan = copy.deepcopy(plan.bundle)
    restored_plan['base_parent'].update(revision_id='r1', publication_id='pub1')
    assert generated.bundles['r3'] == restored_plan
    assert NativeSheet.load(restarted).text == text  # retained export never becomes the new head
    assert sheet.preview(sheet.text).commands == []
    # Equal final values permit ordinary replay, but never erase intervening writes for undo.
    history = [later(TRACK, ['label'], 'away'), later(TRACK, ['label'], '')]
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, history)
    assert replay(plan, original).commands == plan.commands


@pytest.mark.parametrize('field', ['asset', 'transition'])
@pytest.mark.parametrize('value', [None, '', {'opaque': [None, False]}])
def test_explicit_clear_restores_tagged_value(field, value):
    original = initial_bundle()
    State(original).entity(REF)[field] = value
    sheet = sheet_for(original)
    plan = sheet.preview(property_cell(sheet.text, REF, field, 'clear'))
    assert plan.commands[0]['args'] == {'values': {}, 'clear': [field]}
    assert field not in State(plan.bundle).entity(REF)
    assert inverse(plan.bundle, plan.records).bundle == original


@pytest.mark.parametrize('key,value', [('hold', 'null'), ('hold', '""'), ('hold', '"clear"'),
    ('asset', 'null'), ('asset', '""'), ('transition', 'null'), ('transition', '""'),
    ('hold', None), ('text', None), ('text', 'null'), ('text', 'ƒ(mark)'), ('text', '{"a":1}'),
    ('opaque', '2'), ('x', '1 ~ignored')])
def test_unsupported_cells_refuse_whole_batch_before_queue(native_cli, capsys, tmp_path, key, value):
    run, _, generated = native_cli
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    text = capsys.readouterr().out
    text = edit_cell(text, REF, '5')  # A valid edit must not be partially queued.
    edited = property_cell(text, REF, key, value)
    path = tmp_path / 'edited.txt'
    path.write_text(edited)
    before = generated.path.read_bytes()
    assert run('apply', 't', str(path), '--json') == 1
    assert 'edited sheet kept' in capsys.readouterr().out
    assert generated.path.read_bytes() == before and generated.calls == []
    assert path.read_text() == edited


def test_custom_pack_param_refuses_without_client_inputs(native_cli, capsys, tmp_path):
    run, _, generated = native_cli
    State(generated.bundles['r0']).entity(REF)['elementRef'] = {'packId': 'project-pack', 'id': 'type'}
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    text = property_cell(capsys.readouterr().out, REF, 'text', '"Changed"')
    path = tmp_path / 'edited.txt'
    path.write_text(text)
    before = generated.path.read_bytes()
    assert run('apply', 't', str(path), '--json') == 1
    assert 'exact inputs' in capsys.readouterr().out
    assert generated.path.read_bytes() == before and generated.calls == []


def test_basis_fenced_again_inside_atomic_batch(native_cli, capsys, monkeypatch):
    run, client, generated = native_cli
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    capsys.readouterr()
    session = TimelineSession.open('p', 't', client=client)
    sheet = NativeSheet.load(session)
    original = session.commit_many

    def concurrent_ack(commands, **options):
        with session.outbox.locked():
            state = session._state()
            state['base'] = {'revision_id': 'peer', 'publication_id': 'peer-pub'}
            session.outbox.write(state)
        return original(commands, **options)

    monkeypatch.setattr(session, 'commit_many', concurrent_ack)
    with pytest.raises(ValueError, match='original sheet basis'):
        sheet.apply(edit_cell(sheet.text, REF, '5'), session=session, dry_run=False)
    assert session.outbox.read()['pending'] == [] and generated.calls == []


def test_builtin_canvas_units_reuse_existing_literal_grammar_and_preserve_opaque_keys():
    original = initial_bundle()
    clip = State(original).entity(REF)
    clip.update(clipType='am-sprite', params={'x': 27, 'scale': 6, 'opaque key': {'keep': True}})
    sheet = sheet_for(original)
    assert 'x=162' in sheet.text
    assert sheet.preview(sheet.text).commands == []
    plan = sheet.preview(property_cell(sheet.text, REF, 'x', '600'))
    assert plan.commands[0]['args'] == {'values': {'x': 100}, 'clear': []}
    assert State(plan.bundle).entity(REF)['params'] == dict(clip['params'], x=100)
    assert inverse(plan.bundle, plan.records).bundle == original


@pytest.mark.parametrize('value', ['clear', '"clear"', '""', '"null"'])
def test_literal_builtin_param_clear_is_distinct_from_string_values(value):
    original = initial_bundle()
    sheet = sheet_for(original)
    plan = sheet.preview(property_cell(sheet.text, REF, 'text', value))
    expected = {'values': {}, 'clear': ['text']} if value == 'clear' else {
        'values': {'text': json.loads(value)}, 'clear': []}
    assert plan.commands[0]['args'] == expected
    assert inverse(plan.bundle, plan.records).bundle == original


@pytest.mark.parametrize('container', ['flat', 'nested'])
def test_formula_bound_params_are_a_named_requirement_and_refuse_before_queue(connected, container):
    from tests.sdk.test_timeline_sync import open_session

    session = open_session(connected)
    _, generated, path = connected
    app = {'formulas': {'params.text': {'words_of': 'n1'}}}
    State(generated.bundles['r0']).entity(REF)['app'] = app if container == 'flat' else {'astrid': app}
    sheet = NativeSheet.export(session)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='formula-bound'):
        sheet.apply(property_cell(sheet.text, REF, 'text', '"Replacement"'), session=session, dry_run=False)
    assert path.read_bytes() == before and generated.calls == []


def test_unchanged_text_cannot_hide_an_existing_inflight_sheet(native_cli, capsys, tmp_path):
    run, _, generated = native_cli
    assert run('show', 't', '--as', 'sheet', '--native') == 0
    text = capsys.readouterr().out
    path = tmp_path / 'sheet.txt'
    path.write_text(edit_cell(text, REF, '5'))
    generated.lose_ack = True
    assert run('apply', 't', str(path), '--json') == 1
    capsys.readouterr()
    before = generated.path.read_bytes()
    path.write_text(text)
    assert run('apply', 't', str(path), '--json') == 1
    assert 'pending intent' in capsys.readouterr().out
    assert generated.path.read_bytes() == before and len(generated.calls) == 1
