"""Native sheet CLI keeps the original export in the existing scoped outbox."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.core.timeline.authoring_bundle import _digest, open_authoring_bundle
from astrid.packs.timeline import cli
from astrid.sdk import storage_root
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.outbox import request_bytes
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from tests.packs.timeline.test_cli_commit_move import _install_history
from tests.sdk.test_timeline_sheet_native import edit_cell
from tests.sdk.test_timeline_sync import connected, initial_bundle  # noqa: F401
from tests.timeline.test_authoring_bundle import _closure

RATIONAL_FPS = {'numerator': 30000, 'denominator': 1001}
OCCURRENCE_CLIP = 'occurrence:occ-1/clip:picture-1'
RATIONAL_ON = 1001 / 30000


def rational_occurrence_bundle():
    """The smallest real occurrence closure needed by the three native frontends."""
    parent, shots, internals = _closure(shared=True)
    parent.update(project_id='p', timeline_id='t')
    parent['payload']['config']['output'] = {'fps': copy.deepcopy(RATIONAL_FPS)}
    for index, row in enumerate(parent['payload']['occurrences']):
        row.update(placement={'start_ms': index * 10000}, duration_ms=10000,
                   source_offset=0, speed=1)
    for image in [*shots, *internals]:
        image['project_id'] = 'p'
        image['timeline_id'] = 't'
    clip = next(row for row in internals[0]['payload']['clips'] if row['id'] == 'picture-1')
    clip.update({'from': 2, 'to': 6, 'hold': 4,
                 'opaque': {'source': [None, False, 1, {'keep': 'yes'}]}})
    for image in [parent, *shots, *internals]:
        image['content_digest'] = _digest(image['payload'])
    bundle = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    bundle['base_parent']['revision_id'] = 'r0'
    return bundle


def assert_rational_occurrence_move(generated, original, publication):
    moved = generated.bundles['r1']
    target = State(moved).entity(OCCURRENCE_CLIP)
    sibling = State(moved).entity('occurrence:occ-2/clip:picture-1')
    source = State(original).entity(OCCURRENCE_CLIP)
    assert target['at'] == pytest.approx(RATIONAL_ON)
    assert target['asset'] == source['asset']
    assert target['from'] == source['from'] and target['to'] == source['to']
    assert target['hold'] == source['hold']
    assert target['opaque'] == source['opaque']
    assert target['app'] == source.get('app', {}) | {'astrid': {'fixed_start': True, 'for': 4.0}}
    assert target['at'] + target['hold'] < 10  # half-open interval stays inside this occurrence
    assert sibling == State(original).entity('occurrence:occ-2/clip:picture-1')
    assert publication == 'pub1'


@pytest.fixture
def native_cli(connected, monkeypatch):  # noqa: F811
    client, generated, path = connected
    _install_history(generated)
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: path.parent)
    scope = {"realm_id": "realm", "actor_id": "actor", "project_id": "p", "timeline_id": "t"}
    generated.path = path.parent / "timeline-outbox" / (hashlib.sha256(request_bytes(scope)).hexdigest() + ".json")

    def forbidden(*args, **kwargs):
        pytest.fail("legacy checkout/write called")

    monkeypatch.setattr(Checkout, "__init__", forbidden)
    monkeypatch.setattr(Checkout, "save", forbidden)
    monkeypatch.setattr(cli, "_edit_checkout", forbidden)

    def run(*args):
        parsed = cli.build_parser(client).parse_args([*args, "--project", "p"])
        return parsed.handler(parsed)

    return run, client, generated


def export(native_cli, capsys):
    run, _, _ = native_cli
    assert run("show", "t", "--as", "sheet", "--native") == 0
    return capsys.readouterr().out


def test_export_preview_apply_and_undo(native_cli, tmp_path, capsys):
    run, _, generated = native_cli
    text = export(native_cli, capsys)
    state = json.loads(generated.path.read_bytes())
    expected_export = initial_bundle()
    expected_export["base_parent"]["publication_id"] = "pub0"
    assert state["sheet"] == {"bundle": expected_export, "basis": generated.head}
    edited = edit_cell(text, "parent/clip:a", "5")
    edited = edit_cell(edited, "parent/track:picture", '"Picture"', label=True)
    path = tmp_path / "edited.txt"
    path.write_text(edited)
    before = generated.path.read_bytes()
    assert run("apply", "t", str(path), "--dry-run", "--json") == 0
    preview = json.loads(capsys.readouterr().out)
    assert len(preview["data"]["commands"]) == 2 and preview["receipt"] is None
    assert generated.calls == [] and generated.path.read_bytes() == before
    assert run("apply", "t", str(path), "--json") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"]["accepted_publication_id"] == "pub1"
    body = json.loads(generated.calls[0][0])
    assert len(body["commands"]) == 2 and body["base_revision_id"] == "r0"
    assert body["base_publication_id"] == "pub0" and body["source"] == {"via": "sheet"}
    assert generated.bundles["r1"]["parent"]["clips"][0]["at"] == 5
    assert run("undo", "t", "--commit", "pub1", "--json") == 0
    capsys.readouterr()
    assert generated.bundles["r2"]["parent"] == initial_bundle()["parent"]
    assert path.read_text() == edited


def test_original_detached_apply_uses_retained_duration_target(connected, tmp_path, monkeypatch, capsys):  # noqa: F811
    from tests.sdk.test_timeline_sheet_native import detached_draft, duration_edit

    client, generated, _ = connected
    _install_history(generated)
    tl, text, ref, _ = detached_draft(connected, tmp_path)
    base = copy.deepcopy(generated.head)
    generated.bundles[base["revision_id"]]["base_parent"].update(base)
    tl.bundle["base_parent"].update(base)
    tl.save(quiet=True)
    from astrid.sdk.timeline_sheet import render_sheet
    text = render_sheet(tl, cuts=["c1"])
    path = tmp_path / 'detached.sheet'
    edited = duration_edit(text, 2)
    path.write_text(edited)
    monkeypatch.setattr(cli, '_edit_checkout', lambda parsed: Checkout.load(tl.path))
    monkeypatch.setattr('astrid.sdk.timeline_sheet._apply_parsed', lambda *a: pytest.fail('legacy duration writer'))
    parsed = cli.build_parser(client).parse_args(['apply', 't', str(path), '--project', 'p'])
    assert parsed.handler(parsed) == 0
    assert capsys.readouterr().out == 'applied sheet\n'
    body = json.loads(generated.calls[-1][0])
    assert body['base_revision_id'] == 'r0' and body['base_publication_id'] == 'pub0'
    assert body['source'] == {'via': 'sheet'}
    assert body['commands'][0]['select'] == ref
    assert State(generated.bundles['r1']).entity(ref)['hold'] == 2
    assert path.read_text() == edited


@pytest.mark.parametrize("relevant", [False, True])
def test_stale_export_refuses_without_recapturing(native_cli, tmp_path, capsys, relevant):
    run, client, generated = native_cli
    text = export(native_cli, capsys)
    original = json.loads(generated.path.read_bytes())["sheet"]
    path = tmp_path / "edited.txt"
    path.write_text(edit_cell(text, "parent/clip:a", "5"))
    generated.bundles["peer"] = execute(initial_bundle(), [capture_move(
        initial_bundle(), "parent/clip:a" if relevant else "parent/clip:b", 7)]).bundle
    generated.head = {"revision_id": "peer", "publication_id": "peer-pub"}
    reads = list(client.reads)
    assert run("apply", "t", str(path), "--json") == 1
    output = json.loads(capsys.readouterr().out)
    assert "original project, timeline and basis" in output["error"]["message"]
    assert "edited sheet kept" in output["error"]["message"]
    assert generated.calls == [] and client.reads == reads
    assert json.loads(generated.path.read_bytes())["sheet"] == original
    assert path.read_text() == edit_cell(text, "parent/clip:a", "5")


def test_lost_ack_immutable_retry_and_pending_export_refusal(native_cli, tmp_path, capsys, monkeypatch):
    run, _, generated = native_cli
    text = export(native_cli, capsys)
    path = tmp_path / "edited.txt"
    edited = edit_cell(text, "parent/clip:a", "5")
    path.write_text(edited)
    args = ("apply", "t", str(path), "--json")
    generated.lose_ack = True
    assert run(*args, "--idempotency-key", "sheet-once") == 1
    capsys.readouterr()
    before = generated.path.read_bytes()
    assert run("show", "t", "--as", "sheet", "--native", "--json") == 1
    assert "pending intent" in capsys.readouterr().out
    assert generated.path.read_bytes() == before
    assert run(*args, "--idempotency-key", "different") == 1
    assert "immutable" in capsys.readouterr().out
    path.write_text(edit_cell(text, "parent/clip:a", "6"))
    assert run(*args) == 1
    assert "pending intent" in capsys.readouterr().out
    assert generated.path.read_bytes() == before and len(generated.calls) == 1
    path.write_text(edited)

    def forbidden(*args, **kwargs):
        pytest.fail("retry fetched a bundle")

    monkeypatch.setattr(TimelineSession, "_bundle", forbidden)
    assert run(*args) == 0
    capsys.readouterr()
    assert generated.calls[0] == generated.calls[1]
    assert len(generated.accepted) == 1 and path.read_text() == edited


@pytest.mark.parametrize("change", [
    lambda text: text.replace("  type", "  other", 1),
    lambda text: text.replace('"parent/clip:a"', '"parent/clip:b"', 1),
    lambda text: text.replace('"publication_id": "pub0"', '"publication_id": "other"'),
])
def test_unsupported_edits_leave_text_and_outbox_intact(native_cli, tmp_path, capsys, change):
    run, _, generated = native_cli
    text = change(export(native_cli, capsys))
    path = tmp_path / "edited.txt"
    path.write_text(text)
    before = generated.path.read_bytes()
    assert run("apply", "t", str(path), "--json") == 1
    assert "edited sheet kept" in capsys.readouterr().out
    assert generated.path.read_bytes() == before and generated.calls == []
    assert path.read_text() == text


@pytest.mark.parametrize("identity", ["realm_id", "actor_id"])
def test_export_artifact_is_scoped_to_connection(native_cli, tmp_path, capsys, identity):
    run, client, generated = native_cli
    text = export(native_cli, capsys)
    path = tmp_path / "edited.txt"
    path.write_text(edit_cell(text, "parent/clip:a", "5"))
    original = generated.path.read_bytes()
    client._remote._transport._last_handshake[identity] = "other"
    assert run("apply", "t", str(path), "--json") == 1
    assert "no original native sheet export" in capsys.readouterr().out
    assert generated.path.read_bytes() == original and generated.calls == []


@pytest.mark.parametrize("options", [
    ("--as", "sheet", "--range", "0..2"),
    ("--as", "sheet", "--draft", "main"),
    ("--as", "sheet", "--revision-id", "r0"),
    (),
])
def test_native_export_rejects_incompatible_views(native_cli, capsys, options):
    run, _, generated = native_cli
    assert run("show", "t", "--native", "--json", *options) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]
    assert not generated.path.exists() and generated.calls == []


def test_unchanged_export_has_no_commit(native_cli, tmp_path, capsys):
    run, _, generated = native_cli
    path = tmp_path / "edited.txt"
    path.write_text(export(native_cli, capsys))
    assert run("apply", "t", str(path), "--json") == 0
    output = json.loads(capsys.readouterr().out)
    assert output["data"] == {"status": "unchanged", "commands": []}
    assert output["receipt"] is None and generated.calls == []


def test_clip_row_omission_is_native_delete_and_keeps_other_rows(native_cli, tmp_path, capsys):
    run, _, generated = native_cli
    text = export(native_cli, capsys)
    path = tmp_path / "deleted.txt"
    path.write_text("\n".join(line for line in text.splitlines()
                               if not line.endswith(' # ref "parent/clip:a"')) + "\n")

    assert run("apply", "t", str(path), "--json") == 0
    output = json.loads(capsys.readouterr().out)
    assert [command["op"] for command in output["data"]["commands"]] == ["clip.delete"]
    assert [row["id"] for row in generated.bundles["r1"]["parent"]["clips"]] == ["b"]
    assert generated.bundles["r1"]["parent"]["config"]["tracks"] == generated.bundles["r0"]["parent"]["config"]["tracks"]
    assert run("undo", "t", "--commit", output["receipt"]["receipt_id"], "--json") == 0
    capsys.readouterr()
    assert [row["id"] for row in generated.bundles["r2"]["parent"]["clips"]] == ["a", "b"]


@pytest.mark.parametrize("changed", ["pending", "bundle"])
def test_original_identity_never_coerces_boolean_to_number(native_cli, tmp_path, capsys, changed):
    run, _, generated = native_cli
    generated.bundles["r0"]["parent"]["clips"][0]["opaque"] = 1
    text = export(native_cli, capsys)
    path = tmp_path / "edited.txt"
    path.write_text(edit_cell(text, "parent/clip:a", "1"))
    state = json.loads(generated.path.read_bytes())
    if changed == "pending":
        command = capture_move(generated.bundles["r0"], "parent/clip:a", 1)
        command["args"]["on"] = True
        state["pending"] = [command]
    else:
        state["sheet"]["bundle"]["parent"]["clips"][0]["opaque"] = True
    generated.path.write_bytes(request_bytes(state))
    before = generated.path.read_bytes()
    assert run("apply", "t", str(path), "--json") == 1
    assert "edited sheet kept" in capsys.readouterr().out
    assert generated.path.read_bytes() == before and generated.calls == []


def test_clip_fragment_is_pinned_and_only_selected_row_is_editable(native_cli, tmp_path, capsys):
    run, client, generated = native_cli
    assert run("show", "t", "--as", "sheet", "--native", "--clip", "a") == 0
    text = capsys.readouterr().out
    session = TimelineSession.open('p', 't', client=client)
    from astrid.sdk.timeline_sheet import NativeSheet

    sheet = NativeSheet.load(session)
    assert sheet.text == text and list(sheet._rows) == ['parent/clip:a']
    assert json.loads(generated.path.read_bytes())['sheet']['selection'] == {'clip': 'a'}
    path = tmp_path / 'fragment.txt'
    path.write_text(edit_cell(text, 'parent/clip:a', '5'))
    assert run('apply', 't', str(path), '--json') == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output['data']['commands']) == 1
    assert State(generated.bundles['r1']).entity('parent/clip:b') == State(initial_bundle()).entity('parent/clip:b')


def test_cli_trim_clear_and_params_are_one_captured_batch(native_cli, tmp_path, capsys):
    from tests.editing_conformance.test_native_sheet_current import property_cell

    run, client, generated = native_cli
    original = generated.bundles['r0']
    State(original).entity('parent/clip:a').update(clipType='media', asset='video', speed=2,
        **{'from': 0, 'to': 4}, transition=None)
    original['parent']['registry'] = {'assets': {'video': {'duration': 12, 'type': 'video'}}}
    original['parent']['config']['output']['fps'] = {'numerator': 30000, 'denominator': 1001}
    text = export(native_cli, capsys)
    session = TimelineSession.open('p', 't', client=client)
    from astrid.sdk.timeline_sheet import NativeSheet

    sheet = NativeSheet.load(session)
    edited = property_cell(text, 'parent/clip:a', 'trim', '2..8')
    for key in ('asset', 'hold', 'transition'):
        edited = property_cell(edited, 'parent/clip:a', key, 'clear')
    edited = property_cell(edited, 'parent/clip:b', 'text', '""')
    expected = sheet.preview(edited)
    path = tmp_path / 'ordinary.sheet'
    path.write_text(edited)
    assert run('apply', 't', str(path), '--json') == 0
    output = json.loads(capsys.readouterr().out)
    assert output['data']['commands'] == expected.commands
    assert [c['op'] for c in expected.commands] == ['clip.trim', 'clip.set', 'clip.set-params']
    body = json.loads(generated.calls[-1][0])
    assert (body['base_revision_id'], body['base_publication_id']) == ('r0', 'pub0')
    assert {key: generated.bundles['r1']['base_parent'][key]
            for key in ('revision_id', 'publication_id')} == {
                'revision_id': 'r0', 'publication_id': 'pub0'}
    assert len(generated.accepted) == 1 and generated.bundles['r1'] == expected.bundle
    assert run('undo', 't', '--commit', output['receipt']['receipt_id'], '--json') == 0
    capsys.readouterr()
    inverse = json.loads(generated.calls[-1][0])
    assert (inverse['base_revision_id'], inverse['base_publication_id']) == ('r1', 'pub1')
    assert {key: generated.bundles['r2']['base_parent'][key]
            for key in ('revision_id', 'publication_id')} == {
                'revision_id': 'r1', 'publication_id': 'pub1'}
    # History pins the accepted input; inverse restores all authored content at that base.
    restored = copy.deepcopy(original)
    restored['base_parent'].update(revision_id='r1', publication_id='pub1')
    assert generated.bundles['r2'] == restored
    assert path.read_text() == edited


@pytest.mark.parametrize('entry', ['cli', 'python', 'sheet'])
def test_rational_explicit_occurrence_move_is_lossless_across_native_frontends(
        native_cli, capsys, entry):
    """One scoped child move uses the same clip.move recipe at a non-integral rate."""
    run, client, generated = native_cli
    original = rational_occurrence_bundle()
    generated.bundles['r0'] = copy.deepcopy(original)
    session = TimelineSession.open(original['project_id'], original['timeline_id'], client=client)

    if entry == 'cli':
        assert run('edit', 't', '--clip', OCCURRENCE_CLIP, '--on', str(RATIONAL_ON), '--json') == 0
        result = json.loads(capsys.readouterr().out)
        commands = json.loads(generated.calls[-1][0])['commands']
    elif entry == 'python':
        result = session.move(OCCURRENCE_CLIP, RATIONAL_ON)
        commands = json.loads(generated.calls[-1][0])['commands']
    else:
        from astrid.sdk.timeline_sheet import NativeSheet

        sheet = NativeSheet.export(session, occurrence='occ-1', clip='picture-1')
        edited = edit_cell(sheet.text, OCCURRENCE_CLIP, str(RATIONAL_ON))
        result = sheet.apply(edited, session=session, dry_run=False)
        commands = result['data']['commands']

    assert commands == [capture_move(original, OCCURRENCE_CLIP, RATIONAL_ON)]
    body = json.loads(generated.calls[-1][0])
    assert (body['base_revision_id'], body['base_publication_id']) == ('r0', 'pub0')
    assert_rational_occurrence_move(generated, original, result['receipt']['receipt_id'])

    assert State(generated.bundles['r1']).entity(OCCURRENCE_CLIP)['at'] == pytest.approx(RATIONAL_ON)
    history = client.timelines.history('p', 't', limit=200)
    assert history.ok
    accepted = history.data['items'][-1]
    assert (accepted['revision_id'], accepted['publication_id']) == ('r1', 'pub1')
    assert {key: generated.bundles['r1']['base_parent'][key]
            for key in ('revision_id', 'publication_id')} == {
                'revision_id': 'r1', 'publication_id': 'pub1'}
    TimelineSession.open(original['project_id'], original['timeline_id'], client=client).undo(
        [result['receipt']['receipt_id']])
    inverse = json.loads(generated.calls[-1][0])
    assert (inverse['base_revision_id'], inverse['base_publication_id']) == ('r1', 'pub1')
    assert {key: generated.bundles['r2']['base_parent'][key]
            for key in ('revision_id', 'publication_id')} == {
                'revision_id': 'r1', 'publication_id': 'pub1'}
    # The transport base advances; every other field must equal the original closure.
    restored = copy.deepcopy(original)
    restored['base_parent'].update(revision_id='r1', publication_id='pub1')
    assert generated.bundles['r2'] == restored
