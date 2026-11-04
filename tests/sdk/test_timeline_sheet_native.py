"""Bound cut-sheet cells use native capture and the real durable session, offline."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._scalar_commands import capture_track_set
from astrid.sdk.timeline.capture import capture_clip_delete, capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.sync import CommitConflict
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_sheet import NativeSheet, SheetError, apply_sheet, render_sheet
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk.test_timeline_sync import connected, open_session  # noqa: F401
from tests.sdk.test_timeline_sync import initial_bundle as _initial_bundle


def detached_duration_bundle():
    from tests.packs.timeline.test_cli_native_sheet import rational_occurrence_bundle

    bundle = rational_occurrence_bundle()
    for index, shot in enumerate(bundle["shots"].values(), 1):
        internal = shot["internal_timeline"]
        internal["tracks"][0]["id"] = "plate"
        internal["tracks"].append({"id": "type", "kind": "visual"})
        picture = internal["clips"][0]
        picture.update(track="plate", hold=10, **{"from": 2, "to": 12},
                       app={"astrid": {"cut": f"c{index}", "layer": "plate"}})
        internal["clips"].append({"id": "card", "clipType": "am-type", "track": "type", "at": 1,
            "hold": 1, "params": {"text": "LIVE", "size": 32, "font": "display", "color": "#25241F"}, "transition": None,
            "opaque": {"keep": [None, False, 1, 1.0]},
            "app": {"astrid": {"cut": f"c{index}", "layer": "card", "for": 1}}})
    return bundle


def _attach_authenticated_source(bundle):
    """Pair the local sheet fixture with the source image used by inverse checks."""
    bundle['source_mapping']['_runtime_document'] = {
        'schema_version': 1,
        'project_id': bundle['project_id'],
        'timeline_id': bundle['timeline_id'],
        'parent_revision': {
            'revision_id': bundle['base_parent']['revision_id'],
            'content_digest': bundle['base_parent']['content_digest'],
            'payload': copy.deepcopy(bundle['base_parent_payload']),
        },
        'shot_revisions': [],
        'internal_timeline_revisions': [],
        'dependency_manifest': {'shots': [], 'internal_timelines': [], 'media': [], 'documents': []},
    }
    return bundle


def initial_bundle():
    return _attach_authenticated_source(_initial_bundle())


@pytest.fixture(autouse=True)
def _authenticate_connected_source(connected):  # noqa: F811
    _attach_authenticated_source(connected[1].bundles["r0"])


def detached_draft(connected, tmp_path, *, params=None):  # noqa: F811
    client, generated, _ = connected
    original = detached_duration_bundle()
    if params:
        State(original).entity("occurrence:occ-1/clip:card")["params"].update(copy.deepcopy(params))
    generated.bundles["r0"] = copy.deepcopy(original)
    path = tmp_path / "detached.json"
    generated.path = path.with_name(path.name + ".outbox.json")
    tl = Checkout(copy.deepcopy(original))
    tl.save(path, quiet=True)
    tl._scene_client = client
    text = render_sheet(tl, cuts=["c1"])
    assert "for 1s" in text
    ref = "occurrence:occ-1/clip:card"
    return tl, text, ref, original


def duration_edit(text, value):
    return "\n".join(line.replace("for 1s", f"for {value}s") if "  card  " in line else line
                     for line in text.splitlines()) + "\n"


def text_edit(text, value):
    return "\n".join(line.replace('"LIVE"', json.dumps(value), 1)
                     if "  card  " in line else line for line in text.splitlines()) + "\n"


def size_edit(text, value):
    return "\n".join(line.replace("size=32", f"size={value}", 1)
                     if "  card  " in line else line for line in text.splitlines()) + "\n"


def param_edit(text, field, value):
    return "\n".join(line.replace(f"{field}=" + next(token.split("=", 1)[1] for token in line.split()
                                                  if token.startswith(field + "=")),
                                f"{field}={value}", 1)
                     if "  card  " in line else line for line in text.splitlines()) + "\n"


def append_param(text, field, value):
    token = f" {field}={value}"
    return "\n".join(line.replace(" # ref ", token + " # ref ", 1)
                         if "  card  " in line and " # ref " in line else
                         line + token if "  card  " in line else line
                     for line in text.splitlines()) + "\n"


def test_detached_static_duration_keeps_the_original_local_grammar(tmp_path):
    tl = Checkout(detached_duration_bundle())
    tl.save(tmp_path / "local.json", quiet=True)
    original = render_sheet(tl, cuts=["c1"])
    assert apply_sheet(tl, duration_edit(original, 2))
    assert tl.clip("c1.card").duration == 2
    assert tl.clip("c2.card").duration == 1
    assert not list(tmp_path.glob("*.outbox.json"))


@pytest.mark.parametrize("value", ["CHANGED", "", "line\nnext C:\\tmp\\file \\1"])
def test_detached_static_text_uses_retained_native_params_command(connected, tmp_path, value):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    edited = text_edit(original_text, value)
    expected = execute(original, [capture_clip_set_params(original, ref, values={"text": value})])

    tl = Checkout.load(tl.path)
    assert apply_sheet(tl, edited, client=client)
    state = json.loads(generated.path.read_bytes())
    assert generated.calls == []
    assert state["pending"] == expected.commands
    assert State(tl.bundle).entity(ref)["params"]["text"] == value
    assert State(tl.bundle).entity("occurrence:occ-2/clip:card")["params"]["text"] == "LIVE"


@pytest.mark.parametrize("value", [48, "clear"])
def test_detached_static_size_uses_retained_native_params_command(connected, tmp_path, value):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    edited = size_edit(original_text, value)
    expected = execute(original, [capture_clip_set_params(
        original, ref, values={} if value == "clear" else {"size": value},
        clear=["size"] if value == "clear" else [])])

    tl = Checkout.load(tl.path)
    assert apply_sheet(tl, edited, client=client)
    state = json.loads(generated.path.read_bytes())
    assert generated.calls == []
    assert state["sheet"]["bundle"] == original
    assert state["pending"] == expected.commands
    card = State(tl.bundle).entity(ref)
    if value == "clear":
        assert "size" not in card["params"]
    else:
        assert card["params"]["size"] == value
    assert State(tl.bundle).entity("occurrence:occ-2/clip:card")["params"]["size"] == 32


@pytest.mark.parametrize(("field", "value"), [("font", "label"), ("color", '"#FF6B23"')])
def test_detached_static_type_string_uses_retained_native_params_command(connected, tmp_path, field, value):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    parsed_value = value.strip('"') if field == "color" else value
    edited = param_edit(original_text, field, value)
    expected = execute(original, [capture_clip_set_params(original, ref, values={field: parsed_value})])

    tl = Checkout.load(tl.path)
    assert apply_sheet(tl, edited, client=client)
    state = json.loads(generated.path.read_bytes())
    assert generated.calls == []
    assert state["sheet"]["bundle"] == original
    assert state["pending"] == expected.commands
    assert State(tl.bundle).entity(ref)["params"][field] == parsed_value
    assert State(tl.bundle).entity("occurrence:occ-2/clip:card")["params"][field] == State(original).entity(
        "occurrence:occ-2/clip:card")["params"][field]


def test_detached_combined_size_and_type_string_edit_preserves_literal_neighbors(connected, tmp_path):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    edited = param_edit(size_edit(original_text, 48), "font", "label")
    expected = execute(original, [capture_clip_set_params(original, ref, values={"size": 48, "font": "label"})])

    tl = Checkout.load(tl.path)
    assert apply_sheet(tl, edited, client=client)
    state = json.loads(generated.path.read_bytes())
    assert state["pending"] == expected.commands
    card = State(tl.bundle).entity(ref)
    assert list(card["params"]) == ["text", "size", "font", "color"]
    assert card["params"]["size"] == 48 and card["params"]["font"] == "label"
    assert card["params"]["color"] == "#25241F"


def test_detached_type_string_clear_uses_presence_aware_native_command(connected, tmp_path):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    edited = param_edit(original_text, "color", "clear")
    expected = execute(original, [capture_clip_set_params(original, ref, values={}, clear=["color"])])

    tl = Checkout.load(tl.path)
    assert apply_sheet(tl, edited, client=client)
    state = json.loads(generated.path.read_bytes())
    assert state["pending"] == expected.commands
    card = State(tl.bundle).entity(ref)
    assert "color" not in card["params"]
    assert card["params"]["font"] == "display"


@pytest.mark.parametrize(("field", "initial", "edited", "values", "clear"), [
    ("nullable", "value", "null", {"nullable": None}, []),  # set-null
    ("nullable", None, "clear", {}, ["nullable"]),            # clear-null
    ("font", "display", "clear", {}, ["font"]),              # clear-value
    ("text", "LIVE", "RENAMED", {"text": "RENAMED"}, []),   # rename
])
def test_detached_parameter_cells_keep_presence_and_literal_text(
        connected, tmp_path, field, initial, edited, values, clear):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    params = {field: initial}
    tl, original_text, ref, original = detached_draft(connected, tmp_path, params=params)
    client, generated, _ = connected
    text = (text_edit(original_text, edited) if field == "text"
            else param_edit(original_text, field, edited))
    expected = execute(original, [capture_clip_set_params(original, ref, values=values, clear=clear)])

    draft = Checkout.load(tl.path)
    assert apply_sheet(draft, text, client=client)
    state = json.loads(generated.path.read_bytes())
    assert state["pending"] == expected.commands
    result = State(draft.bundle).entity(ref)
    if clear:
        assert field not in result["params"]
    else:
        assert result["params"][field] == next(iter(values.values()))


def test_detached_size_preserves_neighbor_presence_and_order(connected, tmp_path):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    client, generated, _ = connected
    original = detached_duration_bundle()
    ref = "occurrence:occ-1/clip:card"
    card = State(original).entity(ref)
    card["params"] = {"text": "LIVE", "opaque": {"keep": [None, False]}, "size": 32, "nullable": None}
    generated.bundles["r0"] = copy.deepcopy(original)
    path = tmp_path / "detached.json"
    generated.path = path.with_name(path.name + ".outbox.json")
    tl = Checkout(copy.deepcopy(original))
    tl.save(path, quiet=True)
    tl._scene_client = client
    text = render_sheet(tl, cuts=["c1"])
    assert "size=32" in text

    expected = execute(original, [capture_clip_set_params(original, ref, values={"size": 48})])
    draft = Checkout.load(path)
    assert apply_sheet(draft, size_edit(text, 48), client=client)
    state = json.loads(generated.path.read_bytes())
    assert state["pending"] == expected.commands
    result = State(draft.bundle).entity(ref)
    assert list(result["params"]) == ["text", "opaque", "size", "nullable"]
    assert result["params"]["opaque"] == {"keep": [None, False]}
    assert result["params"]["nullable"] is None
    assert "absent" not in result["params"]
    assert result["opaque"] == {"keep": [None, False, 1, 1.0]}


def test_detached_size_reloads_rebases_and_undoes_through_native_session(connected, tmp_path, monkeypatch):  # noqa: F811
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
    from astrid.sdk.timeline.checkout import _sheet_session

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    peer_ref = "occurrence:occ-2/clip:card"
    peer = execute(original, [capture_clip_set_params(original, peer_ref, values={"size": 40})]).bundle
    generated.bundles["peer"] = peer
    generated.head = {"revision_id": "peer", "publication_id": "peer-pub"}
    before = copy.deepcopy(peer)
    edited = size_edit(original_text, 48)
    expected = execute(original, [capture_clip_set_params(original, ref, values={"size": 48})])
    monkeypatch.setattr("astrid.sdk.timeline_sheet.merge_sheet", lambda *a: pytest.fail("client rebase"))
    monkeypatch.setattr("astrid.sdk.timeline_sheet._apply_parsed", lambda *a: pytest.fail("duplicate local consumer"))

    draft = Checkout.load(tl.path)
    assert apply_sheet(draft, edited, client=client)
    assert json.loads(generated.path.read_bytes())["pending"] == expected.commands
    draft.save(quiet=True)
    accepted = generated.bundles["r1"]
    assert State(accepted).entity(ref)["params"]["size"] == 48
    assert State(accepted).entity(peer_ref)["params"]["size"] == 40
    _sheet_session(draft).undo(["pub1"])
    assert generated.bundles["r2"] == before


def test_detached_mixed_text_and_duration_is_one_retained_native_batch(connected, tmp_path):  # noqa: F811
    from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    edited = text_edit(duration_edit(original_text, 2), "CHANGED")
    expected = execute(original, [
        capture_clip_set(original, ref, values={"hold": 2.0}),
        capture_clip_set_params(original, ref, values={"text": "CHANGED"}),
    ])

    tl = Checkout.load(tl.path)
    assert apply_sheet(tl, edited, client=client)
    state = json.loads(generated.path.read_bytes())
    assert generated.calls == []
    assert state["sheet"]["bundle"] == original
    assert state["sheet"]["basis"] == {"revision_id": "r0", "publication_id": "pub0"}
    assert state["pending"] == expected.commands
    assert State(tl.bundle).entity(ref)["hold"] == 2.0
    assert State(tl.bundle).entity(ref)["params"]["text"] == "CHANGED"


def test_detached_mixed_edit_with_supported_font_is_staged(connected, tmp_path):  # noqa: F811
    from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    _, generated, _ = connected
    edited = param_edit(text_edit(duration_edit(original_text, 2), "CHANGED"), "font", "label")
    expected = execute(original, [
        capture_clip_set(original, ref, values={"hold": 2.0}),
        capture_clip_set_params(original, ref, values={"text": "CHANGED", "font": "label"}),
    ])

    assert apply_sheet(tl, edited)
    assert json.loads(generated.path.read_bytes())["pending"] == expected.commands
    card = State(tl.bundle).entity(ref)
    assert card["hold"] == 2.0 and card["params"]["text"] == "CHANGED" and card["params"]["font"] == "label"


def test_detached_mixed_edit_with_opaque_neighbor_is_staged(connected, tmp_path):  # noqa: F811
    from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
    from astrid.sdk.timeline._scalar_commands import capture_clip_set_params

    tl, original_text, ref, original = detached_draft(
        connected, tmp_path, params={"opaque": {"keep": [None, False]}})
    _, generated, _ = connected
    edited = text_edit(duration_edit(original_text, 2), "CHANGED")
    supported = param_edit(edited, "opaque", '{"keep":[true]}')
    unsupported = param_edit(edited, "opaque", "…")

    before_bundle = copy.deepcopy(tl.bundle)
    before_outbox = generated.path.read_bytes()
    with pytest.raises(SheetError, match="unsupported retained sheet edit"):
        apply_sheet(tl, unsupported)
    assert tl.bundle == before_bundle
    assert generated.path.read_bytes() == before_outbox

    native_bundle = detached_duration_bundle()
    native_ref = "occurrence:occ-1/clip:card"
    State(native_bundle).entity(native_ref)["params"]["opaque"] = {"keep": [None, False]}
    native = NativeSheet(native_bundle, basis={"revision_id": "r0", "publication_id": "pub0"})
    old = "\n".join(line + " unknown=label" if 'clip:card"' in line else line
                       for line in native.text.splitlines()) + "\n"
    with pytest.raises(SheetError, match="invalid sheet row binding"):
        NativeSheet._read(old)
    native_text = native.text.replace('opaque={"keep":[null,false]}', 'opaque={"keep":[true]}')
    rows, _ = NativeSheet._read(native_text)
    assert native_ref in rows and rows[native_ref][2]["params"]["opaque"] == {"keep": [True]}
    native_plan = native.preview(native_text)
    assert native_plan.commands[0]["args"] == {
        "values": {"opaque": {"keep": [True]}}, "clear": []}

    assert apply_sheet(tl, supported)
    expected = execute(original, [
        capture_clip_set(original, ref, values={"hold": 2.0}),
        capture_clip_set_params(original, ref, values={
            "text": "CHANGED", "opaque": {"keep": [True]}}),
    ])
    assert json.loads(generated.path.read_bytes())["pending"] == expected.commands
    card = State(tl.bundle).entity(ref)
    assert card["hold"] == 2.0 and card["params"]["text"] == "CHANGED"
    assert card["params"]["opaque"] == {"keep": [True]}


@pytest.mark.parametrize("peer", ["none", "disjoint", "same"])
def test_detached_duration_retains_author_basis_retry_reload_and_guarded_inverse(
        connected, tmp_path, monkeypatch, peer):  # noqa: F811
    from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
    from astrid.sdk.timeline.checkout import _sheet_session
    from astrid.sdk.timeline.commit import _json

    tl, original_text, ref, original = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    artifact = json.loads(generated.path.read_bytes())["sheet"]
    assert artifact["bundle"] == original
    assert artifact["basis"] == {"revision_id": "r0", "publication_id": "pub0"}
    assert artifact["detached"]["bindings"]["c1.card"] == ref
    edited = duration_edit(original_text, 2)
    expected = execute(original, [capture_clip_set(original, ref, values={"hold": 2.0})])
    if peer != "none":
        other_ref = ref if peer == "same" else "occurrence:occ-2/clip:card"
        generated.bundles["peer"] = execute(original, [capture_clip_set(original, other_ref, values={"hold": 3})]).bundle
        generated.head = {"revision_id": "peer", "publication_id": "peer-pub"}
    before = copy.deepcopy(generated.bundles[generated.head["revision_id"]])
    monkeypatch.setattr("astrid.sdk.timeline_sheet.merge_sheet", lambda *a: pytest.fail("client rebase"))
    monkeypatch.setattr("astrid.sdk.timeline_sheet._apply_parsed", lambda *a: pytest.fail("duplicate local consumer"))
    # The retained basis survives a reload before any intent is parsed.
    tl = Checkout.load(tl.path)
    assert apply_sheet(tl, edited, client=client)
    assert generated.calls == []
    assert json.loads(generated.path.read_bytes())["pending"] == expected.commands
    if peer == "same":
        with pytest.raises(CommitConflict):
            tl.save(quiet=True)
        state = json.loads(generated.path.read_bytes())
        assert state["base"] == artifact["basis"] and state["sheet"]["bundle"] == original
        assert state["pending"] == [] and state["flight"] is None
        record = next(iter(state["pending_changes"].values()))
        assert record["parts"][0]["commands"] == expected.commands
        assert record["failure"]["details"]["status"] == "conflict"
        assert Checkout.load(tl.path).clip("c1.card").duration == 2
        assert generated.bundles["peer"] == before and not generated.accepted
        return
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        tl.save(quiet=True)
    first = generated.calls[-1]
    durable = tl.path.read_bytes()
    tl = Checkout.load(tl.path)
    monkeypatch.setattr("astrid.sdk.timeline.sync.TimelineSession._bundle", lambda *a: pytest.fail("retry recaptured"))
    assert apply_sheet(tl, edited, client=client)
    tl.save(quiet=True)
    assert generated.calls[-1] == first and len(generated.accepted) == 1
    assert tl.path.read_bytes() == durable
    for restore in (tl.undo, tl.redo):
        with pytest.raises(TimelineEditError, match="use publications"):
            restore()
    accepted = generated.bundles["r1"]
    assert _json(State(accepted).entity(ref)) == _json(State(expected.bundle).entity(ref))
    assert State(accepted).entity("occurrence:occ-2/clip:card") == State(before).entity("occurrence:occ-2/clip:card")
    response = next(iter(generated.accepted.values()))[1]
    assert response["data"]["status"] == ("rebased" if peer == "disjoint" else "accepted")
    assert json.loads(first[0])["base_publication_id"] == "pub0"
    session = _sheet_session(tl)
    session.undo(["pub1"])
    assert _json(generated.bundles["r2"]) == _json(before)
    session.undo(["pub2"])
    assert _json(generated.bundles["r3"]) == _json(accepted)


def test_detached_duration_invalid_or_changed_pending_text_keeps_draft(connected, tmp_path):  # noqa: F811
    tl, text, _, _ = detached_draft(connected, tmp_path)
    _, generated, _ = connected
    disk, outbox = tl.path.read_bytes(), generated.path.read_bytes()
    with pytest.raises(SheetError):
        apply_sheet(tl, duration_edit(text, "nonsense"))
    assert tl.path.read_bytes() == disk and generated.path.read_bytes() == outbox
    apply_sheet(tl, duration_edit(text, 2))
    outbox = generated.path.read_bytes()
    with pytest.raises(SheetError, match="pending"):
        apply_sheet(tl, duration_edit(text, 3))
    assert tl.path.read_bytes() == disk and generated.path.read_bytes() == outbox


@pytest.mark.parametrize("identity", ["realm_id", "actor_id", "project_id", "timeline_id"])
def test_detached_duration_cannot_change_its_authored_target(connected, tmp_path, identity):  # noqa: F811
    tl, text, _, _ = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    before = generated.path.read_bytes()
    if identity in {"realm_id", "actor_id"}:
        client._remote._transport._last_handshake[identity] = "other"
    else:
        tl.bundle[identity] = "other"
    reads = list(client.reads)
    with pytest.raises(TimelineEditError, match="identity"):
        apply_sheet(tl, duration_edit(text, 2))
    assert generated.path.read_bytes() == before and generated.calls == [] and client.reads == reads


def test_retained_export_without_intent_can_still_be_saved_locally(connected, tmp_path):  # noqa: F811
    tl, _, _, _ = detached_draft(connected, tmp_path)
    client, generated, _ = connected
    disk, reads = tl.path.read_bytes(), list(client.reads)
    Checkout.load(tl.path).save(quiet=True)
    assert tl.path.read_bytes() == disk and client.reads == reads and generated.calls == []


def sheet_for(bundle):
    return NativeSheet(bundle, basis={"revision_id": bundle["base_parent"]["revision_id"],
                                      "publication_id": "pub0"})


def edit_cell(text, ref, value, *, label=False):
    """Act like an editor keeping the hidden ID binding beside its cell."""
    lines = text.splitlines()
    suffix = " # ref " + json.dumps(ref)
    for i, line in enumerate(lines):
        if line.endswith(suffix):
            body = line[:-len(suffix)]
            if label:
                body = body[:body.index("label=")] + "label=" + value
            else:
                body = body.replace(body.split()[0], value, 1)
            lines[i] = body + suffix
            return "\n".join(lines) + "\n"
    raise AssertionError(f"no bound row for {ref}")


def remove_row(text, ref):
    suffix = " # ref " + json.dumps(ref)
    lines = text.splitlines()
    kept = [line for line in lines if not line.endswith(suffix)]
    assert len(kept) == len(lines) - 1
    return "\n".join(kept) + "\n"


def test_preview_and_unchanged_cells_have_no_writes_or_io(connected, monkeypatch):  # noqa: F811
    session = open_session(connected)
    client, generated, path = connected
    before = path.read_bytes()
    bundle = initial_bundle()
    original = copy.deepcopy(bundle)
    sheet = sheet_for(bundle)
    monkeypatch.setattr(Checkout, "save", lambda *a, **kw: pytest.fail("legacy save"))
    monkeypatch.setattr(Checkout, "__init__", lambda *a, **kw: pytest.fail("legacy projection"))
    bundle["parent"]["clips"].clear()  # caller mutation cannot change the pinned original
    plan = sheet.apply(sheet.text, session=session)
    assert plan.commands == plan.records == []
    assert plan.bundle == original
    edited = edit_cell(sheet.text, "parent/clip:a", "5")
    plan = sheet.apply(edited, session=session)
    assert plan.commands == [capture_move(original, "parent/clip:a", 5)]
    assert State(plan.bundle).entity("parent/clip:b") == State(original).entity("parent/clip:b")
    assert generated.calls == client.reads == []
    assert path.read_bytes() == before
    assert sheet.preview(edit_cell(sheet.text, "parent/clip:a", "0.00")).commands == []


@pytest.mark.parametrize("before", ["absent", None, "", "Old", "clear"])
@pytest.mark.parametrize("value", ['""', "clear", '"clear"'])
def test_label_clear_is_distinct_from_empty_and_literal_clear(before, value):
    original = initial_bundle()
    track = State(original).entity("parent/track:picture")
    if before != "absent":
        track["label"] = before
    sheet = sheet_for(original)
    edited = edit_cell(sheet.text, "parent/track:picture", value, label=True)
    plan = sheet.preview(edited)
    expected = copy.deepcopy(track)
    if value == "clear":
        expected.pop("label", None)
    else:
        expected["label"] = json.loads(value)
    assert State(plan.bundle).entity("parent/track:picture") == expected
    if expected == track:
        assert plan.commands == []
    else:
        assert plan.commands == [capture_track_set(original, "parent/track:picture",
            values={} if value == "clear" else {"label": json.loads(value)},
            clear=["label"] if value == "clear" else [])]
    assert sheet.preview(sheet.text).commands == []


@pytest.mark.parametrize("label", ['say " # keep', 'path\\" # keep', "café", "line\nnew", "clear"])
def test_label_json_text_round_trips_without_comment_truncation(label):
    original = initial_bundle()
    sheet = sheet_for(original)
    plan = sheet.preview(edit_cell(sheet.text, "parent/track:picture", json.dumps(label), label=True))
    assert plan.commands == [capture_track_set(original, "parent/track:picture", values={"label": label})]
    State(original).entity("parent/track:picture")["label"] = label
    sheet = sheet_for(original)
    assert sheet.preview(sheet.text).commands == []


@pytest.mark.parametrize("change", [
    lambda text: text.replace("  type", "  other", 1),
    lambda text: text.replace("picture  a", "elsewhere  a", 1),
    lambda text: text.replace('"parent/clip:a"', '"parent/clip:missing"', 1),
    lambda text: text.replace('"parent/clip:a"', '"parent/clip:b"', 1),
    lambda text: text.replace(' # ref "parent/clip:a"', "", 1),
    lambda text: text.replace('"publication_id": "pub0"', '"publication_id": "different"'),
    lambda text: text + '  2  picture new media # ref "parent/clip:new"\n',
    lambda text: text.replace(" # ref", " extra=lost # ref", 1),
    lambda text: text.replace(" # ref", " ~ignored # ref", 1),
])
def test_unsupported_or_unbound_edits_are_refused_before_queueing(connected, change):  # noqa: F811
    session = open_session(connected)
    _, generated, path = connected
    sheet = sheet_for(initial_bundle())
    before = path.read_bytes()
    with pytest.raises(SheetError):
        sheet.apply(change(sheet.text), session=session, dry_run=False)
    assert path.read_bytes() == before and generated.calls == []


def test_omitting_native_clip_row_deletes_exact_bound_clip_and_preserves_sibling_state():
    original = initial_bundle()
    sibling = State(original).entity("parent/clip:b")
    sibling["opaque"] = {"keep": [None, False, 1, 1.0]}
    State(original).entity("parent/clip:a")["opaque"] = {"remove": True}
    sheet = sheet_for(original)

    plan = sheet.preview(remove_row(sheet.text, "parent/clip:a"))

    assert plan.commands == [capture_clip_delete(original, "parent/clip:a")]
    assert [row["id"] for row in State(plan.bundle).collection("parent", "clip")] == ["b"]
    assert State(plan.bundle).entity("parent/clip:b") == sibling
    assert State(plan.bundle).collection("parent", "track") == State(original).collection("parent", "track")


def test_omitted_native_clip_row_uses_normal_commit_retry_undo_redo_and_rebases(connected):  # noqa: F811
    session = open_session(connected)
    client, generated, path = connected
    sheet = sheet_for(initial_bundle())
    edited = remove_row(sheet.text, "parent/clip:a")
    expected = sheet.preview(edited)
    generated.lose_ack = True

    with pytest.raises(WorkspaceClientError):
        sheet.apply(edited, session=session, dry_run=False, idempotency_key="delete-once")
    first = generated.calls[0]
    reads = list(client.reads)
    durable = path.read_bytes()
    restarted = open_session(connected)
    with pytest.raises(TimelineEditError, match="immutable"):
        sheet.apply(edited, session=restarted, dry_run=False, idempotency_key="changed")
    assert path.read_bytes() == durable
    result = sheet.apply(edited, session=restarted, dry_run=False)
    assert generated.calls[-1] == first
    assert client.reads == reads  # exact retry uses the retained flight, not a fresh head
    assert generated.bundles["r1"] == expected.bundle
    assert State(generated.bundles["r1"]).entity("parent/clip:b") == State(initial_bundle()).entity("parent/clip:b")

    restarted.undo([result["receipt"]["receipt_id"]])
    assert generated.bundles["r2"] == initial_bundle()
    from tests.editing_conformance.test_recipe_m1_commands import accepted_boundaries, inverse

    undone = inverse(expected.bundle, expected.records)
    redo_records = accepted_boundaries(undone.records, expected.bundle, undone.bundle)
    assert inverse(undone.bundle, redo_records).bundle == expected.bundle


def test_omitted_native_clip_row_rebases_over_unrelated_row_edit(connected):  # noqa: F811
    session = open_session(connected)
    _, generated, _ = connected
    sheet = sheet_for(initial_bundle())
    edited = remove_row(sheet.text, "parent/clip:a")
    other = capture_move(initial_bundle(), "parent/clip:b", 7)
    generated.bundles["other"] = execute(initial_bundle(), [other]).bundle
    generated.head = {"revision_id": "other", "publication_id": "other-pub"}
    result = sheet.apply(edited, session=session, dry_run=False)
    assert result["data"]["status"] == "rebased"
    assert State(generated.bundles["r1"]).entity("parent/clip:b")["at"] == 7


def test_omitted_native_clip_row_refuses_membership_change_without_accepting(connected):  # noqa: F811
    session = open_session(connected)
    sheet = sheet_for(initial_bundle())
    _, generated, path = connected
    edited = remove_row(sheet.text, "parent/clip:a")
    generated.bundles["membership"] = copy.deepcopy(initial_bundle())
    generated.bundles["membership"]["parent"]["clips"].append(
        {"id": "new", "track": "picture", "clipType": "am-type", "at": 2, "hold": 1,
         "params": {"text": "new"}})
    generated.head = {"revision_id": "membership", "publication_id": "membership-pub"}
    before = copy.deepcopy(generated.bundles["membership"])
    calls = len(generated.calls)
    with pytest.raises(CommitConflict):
        sheet.apply(edited, session=session, dry_run=False)
    assert generated.bundles["membership"] == before
    assert len(generated.calls) == calls + 1
    assert generated.accepted == {}


def test_native_rows_cannot_fall_into_the_legacy_sheet_writer():
    with pytest.raises(SheetError, match="NativeSheet"):
        apply_sheet(Checkout({}), sheet_for(initial_bundle()).text)
    with pytest.raises(SheetError, match="NativeSheet"):
        apply_sheet(Checkout({}), "clips\ntracks\n")


def test_bad_basis_and_unsupported_native_timing_fail_explicitly():
    original = initial_bundle()
    with pytest.raises(SheetError, match="revision"):
        NativeSheet(original, basis={"revision_id": "wrong", "publication_id": "pub0"})
    for basis in ({}, {"revision_id": "r0", "publication_id": ""}):
        with pytest.raises(ValueError, match="verified publication"):
            NativeSheet(original, basis=basis)
    original["parent"]["clips"][0]["time_map"] = {"kind": "piecewise"}
    sheet = sheet_for(original)
    with pytest.raises(CommandError, match="time_map"):
        sheet.preview(edit_cell(sheet.text, "parent/clip:a", "5"))


def test_atomic_batch_commit_lost_ack_restart_and_undo(connected):  # noqa: F811
    session = open_session(connected)
    client, generated, path = connected
    sheet = sheet_for(initial_bundle())
    edited = edit_cell(sheet.text, "parent/clip:a", "5")
    edited = edit_cell(edited, "parent/track:picture", '"Picture"', label=True)
    expected = sheet.preview(edited).commands
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        sheet.apply(edited, session=session, dry_run=False, idempotency_key="sheet-once")
    flight = json.loads(path.read_bytes())["flight"]
    assert json.loads(flight["body"])["commands"] == expected
    assert json.loads(flight["body"])["source"] == {"via": "sheet"}
    assert len(generated.accepted) == 1
    session = open_session(connected)
    reads = list(client.reads)
    result = sheet.apply(edited, session=session, dry_run=False)
    assert client.reads == reads  # retry uses original capture without fetching a newer head
    assert generated.calls[0] == generated.calls[1]
    assert result["data"]["accepted_revision_id"] == "r1"
    assert json.loads(path.read_bytes())["pending"] == []
    session.undo(["pub1"])
    assert generated.bundles["r2"] == initial_bundle()


@pytest.mark.parametrize("mismatch", ["project", "timeline", "revision", "publication", "bundle", "pending"])
def test_commit_requires_exact_pinned_session_and_never_replaces_pending(connected, mismatch):  # noqa: F811
    session = open_session(connected)
    _, generated, path = connected
    original = initial_bundle()
    if mismatch == "bundle":
        original["parent"]["clips"][0]["params"]["opaque"] = True
    sheet = sheet_for(original)
    if mismatch in ("project", "timeline"):
        sheet._bundle[mismatch + "_id"] = "other"
    if mismatch in ("revision", "publication"):
        sheet._basis[mismatch + "_id"] = "other"
    if mismatch == "pending":
        session.queue_move("parent/clip:b", 8)
    before = path.read_bytes()
    with pytest.raises(SheetError):
        sheet.apply(edit_cell(sheet.text, "parent/clip:a", "5"), session=session, dry_run=False)
    assert path.read_bytes() == before and generated.calls == []


def test_stale_original_guards_conflict_without_refreshing(connected):  # noqa: F811
    session = open_session(connected)
    _, generated, path = connected
    original = initial_bundle()
    sheet = sheet_for(original)
    edited = edit_cell(sheet.text, "parent/clip:a", "5")
    generated.bundles["other"] = execute(original, [capture_move(original, "parent/clip:a", 7)]).bundle
    generated.head = {"revision_id": "other", "publication_id": "other-pub"}
    with pytest.raises(CommitConflict):
        sheet.apply(edited, session=session, dry_run=False)
    state = json.loads(path.read_bytes())
    assert state["pending"] == [] and state["flight"] is None
    record = next(iter(state["pending_changes"].values()))
    assert record["parts"][0]["commands"] == [capture_move(original, "parent/clip:a", 5)]
    assert record["failure"]["details"]["status"] == "conflict"
    assert generated.accepted == {}


def test_disjoint_head_edit_rebases_without_writing_unchanged_cells(connected):  # noqa: F811
    session = open_session(connected)
    _, generated, _ = connected
    original = initial_bundle()
    sheet = sheet_for(original)
    other = capture_move(original, "parent/clip:b", 7)
    generated.bundles["other"] = execute(original, [other]).bundle
    generated.head = {"revision_id": "other", "publication_id": "other-pub"}
    result = sheet.apply(edit_cell(sheet.text, "parent/clip:a", "5"), session=session, dry_run=False)
    assert result["data"]["status"] == "rebased"
    assert len(result["data"]["commands"]) == 1
    assert State(generated.bundles["r1"]).entity("parent/clip:b")["at"] == 7
