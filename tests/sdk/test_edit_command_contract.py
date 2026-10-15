"""Astrid's part of Runtime-owned synthetic OpCall and timing fixtures."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from astrid.core.timeline import duration, moments
from astrid.core.rendering.contracts import RenderProfile
from astrid.packs.rendering.shared import rendering_backend as backend
from astrid.sdk.timeline import commands
from banodoco_workspace_client.generated import OPCALL_SCHEMA, validate_edit_command

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(os.environ['CONTRACT_COMMAND_RUNTIME_ROOT'])
FIXTURE = json.loads((RUNTIME / 'conformance/fixtures/edit-command-candidate.json').read_text())


def test_source_is_the_only_edit_command_schema():
    assert OPCALL_SCHEMA == json.loads((ROOT / 'schemas/change-contract/opcall.schema.json').read_text())


@pytest.mark.parametrize('case', FIXTURE['command_cases'], ids=lambda c: c['id'])
def test_shared_command(case):
    value = copy.deepcopy(case['value'])
    before = copy.deepcopy(value)
    assert Draft202012Validator(OPCALL_SCHEMA).is_valid(value) == case['valid']
    if case['valid']:
        assert validate_edit_command(value) is value
        parsed = commands.EditCommand.coerce(value)
        assert (parsed.op, parsed.select, dict(parsed.args)) == (value['op'], value['select'], value['args'])
        # Coercion is lossless. Execution separately verifies recorded metadata.
        for key, item in value.items():
            assert parsed.to_json()[key] == item
        assert json.loads(case['raw']) == value
        assert hashlib.sha256(case['raw'].encode()).hexdigest() == case['sha256']
    else:
        with pytest.raises(commands.CommandError):
            commands.EditCommand.coerce(value)
    assert value == before


@pytest.mark.parametrize('case', FIXTURE['rounding'], ids=lambda c: c['id'])
def test_shared_rounding(case):
    assert duration._js_round(case['value']) == case['frame']
    if case['backend_positive_domain']:
        assert backend._js_round(case['value']) == case['frame']


@pytest.mark.parametrize('case', FIXTURE['frames'], ids=lambda c: c['id'])
def test_shared_frame_clock(case):
    clip = {'at': case['seconds'], 'hold': 1}
    assert duration.clip_start_frame(clip, case['fps']) == case['frame']
    assert duration.clip_start_frame({'at': case['frame'] / case['fps'], 'hold': 1}, case['fps']) == case['frame']


@pytest.mark.parametrize('case', FIXTURE['anchors'], ids=lambda c: c['id'])
def test_shared_anchor_to_frame(case):
    word = SimpleNamespace(text='viral', segment='v1', index=1, start=case['word_start'], end=case['word_end'])
    context = SimpleNamespace(fps=case['fps'], words=lambda: [word])
    resolved = moments.resolve(case['moment'], context)
    assert resolved == pytest.approx(case['resolved_seconds'])
    frame = duration.clip_start_frame({'at': moments.floor_frame(resolved, case['fps']), 'hold': 1}, case['fps'])
    assert frame == case['frame']


@pytest.mark.parametrize('case', FIXTURE['dimensions'], ids=lambda c: c['id'])
def test_backend_actual_rounding_call_site(case):
    profile = RenderProfile(width=case['width'], height=case['height'], fps_rational=(30, 1), time_base=(1, 90000), video_codec='h264', pixel_format='yuv420p')
    assert backend._remotion_scaled_dimensions(profile, case['scale'], even=case['even']) == tuple(case['expected'])


def test_all_inventory_and_current_edit_flags_have_runtime_fixture_rows():
    from astrid.packs.timeline import cli
    from astrid.sdk.timeline import checkout  # noqa: F401 — initializes registered SDK operations
    inventory = json.loads((ROOT / 'tests/parity/inventory.json').read_text())
    fixture = json.loads((RUNTIME / 'conformance/fixtures/edit-command-flags.json').read_text())
    table = fixture['rows']
    admitted = commands.flag_inventory_table(inventory)
    assert table[:len(admitted)] == admitted
    mapping = {flag: row for row in table for flag in row['flags']}
    parser = cli.build_parser(None)._subparsers._group_actions[0].choices['edit']
    for action in parser._actions:
        if action.dest == 'help':
            continue
        for flag in action.option_strings:
            assert flag in mapping
            assert mapping[flag]['destination'] == action.dest
            assert mapping[flag]['operation'] == commands.EDIT_FLAG_OPERATIONS[action.dest]
            assert mapping[flag]['operation'] in commands.REGISTRY
    # No equality assertion on REGISTRY: adding an operation stays legal.


@pytest.mark.parametrize('exchange', FIXTURE['exchanges'], ids=lambda c: c['id'])
def test_shared_exchange_exact_calls_refuse_without_recorded_replay_metadata(exchange, tmp_path):
    """Protocol specimens are not invented into accepted executable histories."""
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.rebase import rebase

    tl = Checkout(json.loads((ROOT / 'tests/fixtures/timeline_editing/tiny_publishable.json').read_text()))
    path = tmp_path / 'shared-exchange.json'
    tl.save(path, quiet=True)
    before = tl.document(), path.read_bytes(), copy.deepcopy(exchange)
    # Request and result both carry these exact calls. Their parent target is
    # absent from the available closure, so apply must refuse without an undo.
    for side in ('request', 'result'):
        calls = exchange[side]['change']['commands']
        for value in calls:
            with pytest.raises(commands.SemanticConflict, match='delete-vs-edit'):
                commands.run(tl, value)
        result = rebase(calls, tl.document())
        assert result.conflicts[0]['kind'] == 'dependent'
        assert 'runner-recorded' in result.conflicts[0]['ours']
        assert result.next_step and result.document is None
        with pytest.raises(commands.TimelineEditError, match='nothing to undo'):
            tl.undo()
    assert tl.document() == before[0] and path.read_bytes() == before[1]
    assert exchange == before[2]


def test_shared_conflict_values_through_apply_inverse_and_rebase(tmp_path):
    """Exercise the executable conflict witness, not its opaque specimen document."""
    from astrid.sdk import timeline_intent as intent
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.rebase import rebase

    exchange = next(row for row in FIXTURE['exchanges'] if row['id'] == 'EXCHANGE-CONFLICT')
    witness = exchange['result']['conflicts'][0]
    original = copy.deepcopy(exchange)
    document = json.loads((ROOT / 'tests/fixtures/timeline_editing/tiny_publishable.json').read_text())
    # Materialize the specimen's existing occurrence/clip address on synthetic
    # repository data; no shared fixture or command is rewritten.
    occurrence, clip_id = witness['id'].split('/clip:')
    document['placements'][0]['occurrence_id'] = occurrence.removeprefix('occurrence:')
    clip = next(c for c in document['shots']['A']['internal_timeline']['clips'] if c['id'] == 'a-rocket')
    clip['id'] = clip_id
    intent.set_on(clip, witness['base'])
    tl = Checkout(document)
    tl.save(tmp_path / 'conflict.json', quiet=True)
    base = tl.document()
    ours = dict(op='clip.on', select=witness['id'], args={'moment': witness['ours']})
    plan = commands.run(tl, ours)
    assert intent.on(tl.clip(clip_id).data) == witness['ours']
    tl.undo()
    assert intent.on(tl.clip(clip_id).data) == witness['base']
    head = Checkout(copy.deepcopy(base))
    commands.run(head, dict(op='clip.on', select=witness['id'], args={'moment': witness['theirs']}))
    result = rebase(plan.calls, head.document())
    actual = result.conflicts[0]
    assert {key: actual[key] for key in ('id', 'path', 'kind')} == {
        key: witness[key] for key in ('id', 'path', 'kind')}
    assert actual['base'] == dict(state='present', value=witness['base'])
    assert actual['theirs'] == dict(state='present', value=witness['theirs'])
    assert actual['ours'] == {'moment': witness['ours']}
    assert result.next_step and exchange == original


def test_shared_clear_command_apply_inverse_and_accepted_rebase(tmp_path):
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.rebase import rebase

    case = next(row for row in FIXTURE['command_cases'] if row['id'] == 'CMD-CLEAR')
    original = copy.deepcopy(case)
    document = json.loads((ROOT / 'tests/fixtures/timeline_editing/tiny_publishable.json').read_text())
    clip = next(c for c in document['shots']['A']['internal_timeline']['clips'] if c['id'] == 'a-rocket')
    clip.update(clipType='am-type', params={'text': 'Word', 'italicWord': 'Word'})
    tl = Checkout(document)
    tl.save(tmp_path / 'shared-clear.json', quiet=True)
    base = tl.document()
    # Execute the exact shared call, including its authoring selector.
    plan = commands.run(tl, case['value'])
    assert 'italicWord' not in tl.clip('a-rocket').params
    applied = tl.document()
    tl.undo()
    assert tl.clip('a-rocket').params['italicWord'] == 'Word'
    # The runner adds original guards; rebase never manufactures them.
    result = rebase(plan.calls, base)
    assert not result.conflicts and result.id_map == {}
    assert result.document == applied
    assert case == original
