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
