"""Ordinary CLI trim keeps main's grammar and the SDK's original capture."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrid.packs.timeline import cli
from astrid.packs.timeline.cli_native import _trim_selector
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline_checkout import Clip
from tests.editing_conformance.test_recipe_m2_clip_trim import bundle as trim_bundle
from tests.packs.timeline.test_cli_commit_move import command, sync_connection  # noqa: F401

REF = 'parent/clip:a'


@pytest.fixture
def trim(command):  # noqa: F811
    run, generated = command
    bundle = generated.bundles['r0']
    bundle['parent']['registry'] = {'assets': {'film': {'duration': 10, 'type': 'video'}}}
    for row in bundle['parent']['clips']:
        row.update(clipType='media', asset='film', hold=4, speed=2,
                   **{'from': 2, 'to': 6},
                   app={'astrid': {'for': 2, 'opaque': [None, False]}, 'extension': {'keep': True}})
    return run, generated, copy.deepcopy(bundle)


@pytest.mark.parametrize('query', ['a', 'parent/clip:a'])
@pytest.mark.parametrize('flags,args', [
    (['--trim-in', '1'], {'in_s': 1., 'out_s': None}),
    (['--trim-out', '4.000001'], {'in_s': None, 'out_s': 4.000001}),
    (['--trim-in', '0.000001', '--trim-out', '1.000002'], {'in_s': .000001, 'out_s': 1.000002}),
])
def test_ordinary_trim_matches_python_capture_and_main_afterstate(trim, monkeypatch, capsys, query, flags, args):
    run, generated, original = trim
    row = copy.deepcopy(State(original).entity(REF))
    monkeypatch.setattr(Clip, '_source_duration', lambda self: 10)
    Clip(SimpleNamespace(_is_audio=lambda *a: False, _mcache=None), None, row).trim(**args)
    expected = capture_clip_trim(original, REF, **args)
    assert run('edit', 't', '--clip', query, *flags, '--json') == 0
    output = json.loads(capsys.readouterr().out)
    body = json.loads(generated.calls[0][0])
    assert body['commands'] == [expected]
    assert body['source'] == {'via': 'cli'}
    assert State(generated.bundles['r1']).entity(REF) == row
    assert output['data']['accepted_publication_id'] == 'pub1'
    assert output['data']['show_command'].endswith('--published --revision-id r1')


def test_trim_lost_ack_keeps_original_flight_and_undo(trim, capsys):
    run, generated, _ = trim
    generated.lose_ack = True
    args = ('edit', 't', '--clip', 'a', '--trim-out', '5', '--json', '--idempotency-key', 'first')
    assert run(*args) == 1
    capsys.readouterr()
    # The generated fixture asserts that exact body/key were durable before POST.
    flight = json.loads(generated.path.read_bytes())['flight']
    assert run(*args) == 0
    capsys.readouterr()
    assert generated.calls == [(flight['body'].encode(), 'first')] * 2
    assert run('undo', 't', '--commit', 'pub1', '--json') == 0
    capsys.readouterr()
    assert State(generated.bundles['r2']).entity(REF)['to'] == 6


def test_trim_preview_keeps_pending_and_never_accepts(trim, capsys):
    run, generated, _ = trim
    assert run('edit', 't', '--clip', 'a', '--trim-out', '5', '--dry-run', '--json') == 0
    result = json.loads(capsys.readouterr().out)
    assert result['receipt'] is None
    state = json.loads(generated.path.read_bytes())
    assert state['flight'] is None and len(state['pending']) == 1
    assert generated.head['revision_id'] == 'r0'
    assert run('edit', 't', '--clip', 'a', '--trim-out', '5', '--json') == 0
    capsys.readouterr()
    assert generated.calls[0][1] != generated.calls[1][1]


def test_ordinary_lookup_binds_occurrences_without_decoding_display_ids():
    bundle = trim_bundle()
    original = copy.deepcopy(bundle)
    # The same authored ID appears in parent and both shared-shot occurrences.
    direct = SimpleNamespace(clip='occurrence:occ-b/clip:clip-a', near=None, cut=None)
    assert _trim_selector(bundle, direct) == direct.clip
    parent = SimpleNamespace(clip='clip-a', near=None, cut=None)
    with pytest.raises(TimelineEditError, match='matches'):
        _trim_selector(bundle, parent)
    near = SimpleNamespace(clip='clip-a', near='10.5', cut=None)
    assert _trim_selector(bundle, near) == 'occurrence:occ-b/clip:clip-a'
    assert bundle == original


@pytest.mark.parametrize('flag', [['--set', 'x=2'], ['--duration', '2'], ['--verify'], ['--slip', '1']])
def test_unmigrated_combined_flags_keep_legacy_path(trim, monkeypatch, flag):
    run, generated, _ = trim
    sentinel = RuntimeError('legacy family retained')
    def legacy(*a, **kw):
        raise sentinel
    monkeypatch.setattr(cli, '_edit_checkout', legacy)
    with pytest.raises(RuntimeError, match='legacy family retained'):
        run('edit', 't', '--clip', 'a', '--trim-out', '5', *flag)
    assert generated.calls == []


def test_existing_detached_draft_is_used_before_opening_a_session(trim, monkeypatch):
    from astrid.sdk.timeline import store
    from astrid.sdk.timeline.sync import TimelineSession

    run, generated, _ = trim
    monkeypatch.setattr(store, 'find_draft', lambda *a, **kw: generated.path.parent / 'existing.checkout.json')
    monkeypatch.setattr(TimelineSession, 'open', lambda *a, **kw: pytest.fail('detached draft ignored'))
    def legacy(*a, **kw):
        raise RuntimeError('existing draft retained')
    monkeypatch.setattr(cli, '_edit_checkout', legacy)
    with pytest.raises(RuntimeError, match='existing draft retained'):
        run('edit', 't', '--clip', 'a', '--trim-out', '5')
    assert not generated.path.exists()


def test_trim_retry_refuses_changed_endpoints_and_preserves_original_bytes(trim, capsys):
    run, generated, _ = trim
    generated.lose_ack = True
    args = ('edit', 't', '--clip', 'a', '--trim-out', '5', '--json')
    assert run(*args) == 1
    capsys.readouterr()
    persisted = generated.path.read_bytes()
    assert run('edit', 't', '--clip', 'a', '--trim-out', '4', '--json') == 1
    assert not json.loads(capsys.readouterr().out)['ok']
    assert generated.path.read_bytes() == persisted
    assert len(generated.calls) == 1
    assert run(*args) == 0
    capsys.readouterr()
    assert generated.calls[0] == generated.calls[1]
