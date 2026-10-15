"""Astra39 scoped producers. Runtime acceptance uses an explicit authority double."""

import copy
import json
from pathlib import Path

import pytest
from test_timeline_commit import StaleHead, call, core, request

from astrid.sdk.timeline import commands
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit import _digest
from astrid.sdk.timeline.rebase import rebase


@pytest.fixture(params=["parent", "occurrence:occ-A"])
def source(request):
    data = json.loads(
        (Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json").read_text()
    )
    clip = dict(
        id="literal",
        clipType="media",
        track="sprite",
        at=0.5,
        **{"from": 1.0, "to": 5.0},
        speed=2,
        asset="P",
        params={},
        opaque={"null": None, "clear-shaped": {"op": "clear"}},
    )
    if request.param == "parent":
        data["parent"].update(
            tracks=[dict(id="sprite", kind="visual")],
            registry=copy.deepcopy(data["shots"]["A"]["internal_timeline"]["registry"]),
            clips=[clip],
        )
    else:
        data["shots"]["A"]["internal_timeline"]["clips"].append(clip)
    return Checkout(data).document(), request.param + "/clip:literal"


def internal(bundle, scope):
    return bundle["parent"] if scope == "parent" else bundle["shots"]["A"]["internal_timeline"]


def setup(source):
    data, clip = copy.deepcopy(source)
    scope = clip.split("/clip:")[0]
    tracks = internal(data, scope)["tracks"]
    tracks.append(
        dict(id="extra", kind="visual", label="Extra", app={"x": {"null": None, "opaque": 7}})
    )
    return data, scope, scope + "/track:extra"


def accepted(data, *calls):
    body = request(*calls)
    service, port = core(body, data)
    result = service.commit(body, port)
    assert result["status"] in {"accepted", "no_change"}, result["error"]
    return result, port


def test_track_add_complete_row_absence_append_and_replay(source):
    data, scope, sid = setup(source)
    new = scope + "/track:new"
    row = dict(id="new", kind="audio", label="Audio", role="source", app={"opaque": None})
    cmd = {**call("timeline.add", new, track=row), "expect": {new + "::": {"state": "absent"}}}
    result, port = accepted(data, cmd)
    rec = result["change"]["commands"][0]
    assert rec["created"] == [new] and rec["resolved"] == [new]
    assert internal(result["canonical_document"], scope)["tracks"][-1] == row
    assert port.saved["result"] == result
    head = copy.deepcopy(data)
    head["parent"]["config"]["unrelated"] = None
    replay = rebase([rec], head)
    assert not replay.conflicts, replay.conflicts
    assert replay.id_map == {}
    assert rebase([rec], result["canonical_document"]).conflicts


@pytest.mark.parametrize("bad", ["missing-expect", "id", "kind", "label", "existing"])
def test_track_add_refusals(source, bad):
    data, scope, sid = setup(source)
    new = scope + "/track:new"
    row = dict(id="new", kind="visual", label="New")
    cmd = {**call("timeline.add", new, track=row), "expect": {new + "::": {"state": "absent"}}}
    if bad == "missing-expect":
        cmd.pop("expect")
    elif bad == "existing":
        cmd["select"] = sid
    elif bad == "label":
        row.pop("label")
    else:
        row[bad] = "bad"
    body = request(cmd)
    service, port = core(body, data)
    result = service.commit(body, port)
    assert result["status"] == "refused" and not port.accepts


@pytest.mark.parametrize(
    "path,value",
    [
        ("label", "Renamed"),
        ("role", "output"),
        ("scale", 1.3),
        ("fit", "cover"),
        ("opacity", 0.5),
        ("volume", 0.3),
        ("muted", True),
        ("blendMode", "screen"),
        ("app.x.null", None),
        ("app.x.new", {"op": "clear"}),
    ],
)
def test_track_supported_properties_guard_and_opaque_preservation(source, path, value):
    data, scope, sid = setup(source)
    before = commands.entity_states(Checkout(copy.deepcopy(data)))
    cmd = {
        **call("timeline.set", sid, values={path: value}),
        "expect": {sid + "::" + path: commands.state_at(before, sid, path)},
    }
    result, _ = accepted(data, cmd)
    row = internal(result["canonical_document"], scope)["tracks"][-1]
    assert row["app"]["x"]["opaque"] == 7
    assert commands.state_at(
        commands.entity_states(Checkout(result["canonical_document"])), sid, path
    ) == {"state": "present", "value": value}
    rec = result["change"]["commands"][0]
    head = copy.deepcopy(data)
    internal(head, scope)["tracks"][-1]["label"] = "Changed"
    replay = rebase([rec], head)
    if path == "label":
        assert replay.conflicts
    else:
        assert not replay.conflicts, replay.conflicts


def test_track_clear_preserves_null_and_no_resurrection(source):
    data, scope, sid = setup(source)
    cmd = {
        **call("timeline.set", sid, clear=["app.x.opaque"]),
        "expect": {"app.x.opaque": {"state": "present", "value": 7}},
    }
    result, _ = accepted(data, cmd)
    row = internal(result["canonical_document"], scope)["tracks"][-1]
    assert row["app"]["x"] == {"null": None}
    assert result["change"]["commands"][0]["authorship"]["app.x.opaque"] == "direct"


@pytest.mark.parametrize(
    "values,clear",
    [
        ({"id": "bad"}, []),
        ({"kind": "audio"}, []),
        ({}, ["label"]),
        ({"label": "x"}, ["label"]),
        ({"opacity": None}, []),
        ({"muted": 1}, []),
        ({"fit": "bad"}, []),
        ({"role": "bad"}, []),
        ({"app.x": {}}, ["app.x.null"]),
    ],
)
def test_track_property_refusal_rolls_back(source, values, clear):
    data, scope, sid = setup(source)
    states = commands.entity_states(Checkout(copy.deepcopy(data)))
    cmd = {
        **call("timeline.set", sid, values=values, clear=clear),
        "expect": {p: commands.state_at(states, sid, p) for p in set(values) | set(clear)},
    }
    body = request(call(select="a-rocket", x=99), cmd)
    service, port = core(body, data)
    result = service.commit(body, port)
    assert result["status"] == "refused" and not port.accepts and port.saved is None


@pytest.mark.parametrize("bad", ["duplicate", "partial", "foreign", "stale", "missing-expected"])
def test_reorder_complete_unique_owner_and_stale_guards(source, bad):
    data, scope, sid = setup(source)
    order = [scope + "/track:" + t["id"] for t in internal(data, scope)["tracks"]]
    cmd = call("timeline.reorder", sid, order=order[::-1], expected_order=order)
    if bad == "duplicate":
        cmd["args"]["order"] = [order[0]] * len(order)
    if bad == "partial":
        cmd["args"]["order"] = order[:-1]
    if bad == "foreign":
        cmd["args"]["order"][0] = "occurrence:foreign/track:extra"
    if bad == "stale":
        cmd["args"]["expected_order"] = order[::-1]
    if bad == "missing-expected":
        del cmd["args"]["expected_order"]
    body = request(cmd)
    service, port = core(body, data)
    result = service.commit(body, port)
    assert result["status"] == "refused" and not port.accepts


def test_reorder_record_and_rebase_guard(source):
    data, scope, sid = setup(source)
    order = [scope + "/track:" + t["id"] for t in internal(data, scope)["tracks"]]
    result, _ = accepted(
        data, call("timeline.reorder", sid, order=order[::-1], expected_order=order)
    )
    record = result["change"]["commands"][0]
    assert record["expect"][sid + "::track_order"] == {"state": "present", "value": order}
    assert not rebase([record], data).conflicts
    assert rebase([record], result["canonical_document"]).conflicts


def test_occupied_removal_explicit_batch_rollback_replay_and_one_undo(
    source, tmp_path, monkeypatch
):
    data, scope, sid = setup(source)
    content = internal(data, scope)["clips"][-1]
    content["track"] = "extra"
    clip = scope + "/clip:" + content["id"]
    track = copy.deepcopy(internal(data, scope)["tracks"][-1])
    remove = {
        **call("timeline.remove", sid),
        "expect": {sid + "::": {"state": "present", "value": track}},
    }
    result, port = accepted(data, call("clip.remove", clip), remove)
    assert len(port.accepts) == 1 and len(result["change"]["commands"]) == 2
    replay = rebase(result["change"]["commands"], data)
    assert not replay.conflicts, replay.conflicts
    failed = request(
        call("clip.remove", clip), {**remove, "expect": {sid + "::": {"state": "absent"}}}
    )
    service, port = core(failed, data)
    assert service.commit(failed, port)["status"] == "refused" and not port.accepts
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    tl = Checkout(copy.deepcopy(data), path=tmp_path / "draft.json")
    tl.save(quiet=True)
    with tl.edit("remove contents and track", quiet=True):
        commands.execute(tl, call("clip.remove", clip))
        commands.execute(tl, remove)
    assert sid not in commands.entity_states(tl)
    assert tl.undo()
    assert sid in commands.entity_states(tl) and clip in commands.entity_states(tl)
    with pytest.raises(Exception, match="nothing to undo"):
        tl.undo()


def media_command(scope, record, **kwargs):
    media = dict(
        asset="selected",
        generation_id=record["generation_id"],
        variant_id=record["variant_id"],
        object_id=record["object_id"],
        catalog_digest=_digest(record),
    )
    return {
        **call(
            "timeline.add", scope + "/track:sfx", media=media, at=0.5, clip_id="reserved", **kwargs
        ),
        "expect": {scope + "/clip:reserved::": {"state": "absent"}},
    }


def media_setup(source, duration=2):
    data, scope, _ = setup(source)
    internal(data, scope)["tracks"].append(dict(id="sfx", kind="audio", label="SFX"))
    record = dict(
        realm_id="realm",
        project_id="p",
        asset="selected",
        generation_id="g",
        variant_id="v",
        object_id="sha256:" + "a" * 64,
        generation={
            "type": "audio",
            "source_task_id": None,
            "metadata": {"params": {"sample_rate": 48000}, "opaque": None},
        },
        variant={"variant_type": "original", "metadata": {"op": "clear"}},
        entry={
            "object_id": "sha256:" + "a" * 64,
            "media_type": "audio/wav",
            "size": 4,
            "filename": "selected.wav",
            "duration_seconds": duration,
        },
    )
    return data, scope, record


def media_port(body, data, record, head=None):
    service, port = core(body, data, head)
    port.catalog = copy.deepcopy(record)
    port.media_reads = []

    def selected(selection):
        port.media_reads.append(copy.deepcopy(selection))
        return {**copy.deepcopy(port.catalog), "catalog_digest": _digest(port.catalog)}

    port.selected_media = selected
    # Explicit double for C's atomic dependency+head-check transaction.
    original = port.accept

    def accept(prepared):
        for cmd in body["change"]["commands"]:
            if cmd["op"] == "timeline.add" and "media" in cmd["args"]:
                assert cmd["args"]["media"]["catalog_digest"] == _digest(port.catalog)
                assert port.catalog["project_id"] == body["project_id"]
        return original(prepared)

    port.accept = accept
    return service, port


def test_media_detached_identity_registry_replay_after_catalog_change(source):
    data, scope, record = media_setup(source)
    cmd = media_command(scope, record)
    body = request(cmd)
    service, port = media_port(body, data, record)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    rec = result["change"]["commands"][0]
    assert rec["created"] == [scope + "/clip:reserved"] and rec["args"]["media_record"] == {
        **record,
        "catalog_digest": _digest(record),
    }
    assert rec["select"] == scope + "/track:sfx"
    assert result["id_map"] == {} and len(port.media_reads) == 1
    entry = internal(result["canonical_document"], scope)["registry"]["assets"]["selected"]
    assert entry["authoring_metadata"] == {key: record[key] for key in ("generation", "variant")}
    port.catalog["entry"]["duration_seconds"] = 100
    assert service.commit(body, port) == result and len(port.media_reads) == 1
    replay = rebase([rec], data)
    assert not replay.conflicts, replay.conflicts
    assert internal(replay.document, scope)["registry"]["assets"]["selected"] == entry


@pytest.mark.parametrize(
    "bad",
    [
        "foreign",
        "selected-change",
        "wrong-object",
        "missing-port",
        "forged-record",
        "collision",
        "missing-duration",
        "range",
        "no-absence",
    ],
)
def test_media_refusals_atomic(source, bad):
    data, scope, record = media_setup(source)
    cmd = media_command(scope, record)
    if bad == "missing-duration":
        record["entry"]["duration_seconds"] = None
        cmd = media_command(scope, record)
    if bad == "range":
        cmd = media_command(scope, record, source_range=[0, 3])
    if bad == "forged-record":
        cmd["args"]["media_record"] = record
    if bad == "no-absence":
        cmd.pop("expect")
    if bad == "collision":
        internal(data, scope)["registry"]["assets"]["selected"] = {"media_id": "other"}
    body = request(call(select="a-rocket", x=22), cmd)
    service, port = media_port(body, data, record)
    if bad == "foreign":
        port.catalog["project_id"] = "foreign"
    if bad == "selected-change":
        port.catalog["generation"]["metadata"]["params"]["sample_rate"] = 44100
    if bad == "wrong-object":
        port.catalog["object_id"] = "sha256:" + "b" * 64
    if bad == "missing-port":
        del port.selected_media
    before = copy.deepcopy(data)
    result = service.commit(body, port)
    assert result["status"] == "refused", result
    assert data == before and not port.accepts and port.saved is None


def test_media_retry_reserves_identity_and_reads_once(source):
    data, scope, record = media_setup(source)
    head = copy.deepcopy(data)
    head["parent"]["config"]["unrelated"] = None
    body = request(media_command(scope, record))
    service, port = media_port(body, data, record)
    port.bundles["head-new"] = head

    def race(port, prepared):
        if len(port.accepts) == 1:
            port.current = "head-new"
            raise StaleHead()

    port.on_accept = race
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    assert len(port.accepts) == 2 and len(port.media_reads) == 1
    assert {p["result"]["change"]["commands"][0]["args"]["clip_id"] for p in port.accepts} == {
        "reserved"
    }
    assert result["change"]["commands"][0]["created"] == [scope + "/clip:reserved"]


def test_clear_unused_authoritative_refs_defaults_last_and_rebase(source):
    from astrid.sdk.timeline.music_beds import collection

    data, scope, sid = setup(source)
    tracks = internal(data, scope)["tracks"]
    tracks.extend(
        [
            dict(id="A1", kind="audio", label="Default"),
            dict(id="hidden", kind="audio", label="Hidden"),
            dict(id="unused-audio", kind="audio", label="Empty"),
        ]
    )
    tl = Checkout(copy.deepcopy(data))
    owner = scope + "/clip:zero"
    collection(tl, create=True)[owner] = dict(
        owner=owner,
        source=dict(
            id="zero", clipType="media", track="hidden", asset="P", at=0, **{"from": 0, "to": 2}
        ),
        outputs=[],
        ripple_basis=[dict(at_s=0, delta_s=-2)],
    )
    planned = commands.unused_track_removals(tl, sid)
    selected = {c["select"] for c in planned}
    assert scope + "/track:extra" in selected and scope + "/track:unused-audio" in selected
    assert scope + "/track:A1" not in selected and scope + "/track:hidden" not in selected
    body = request(*planned)
    service, port = core(body, tl.document())
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    assert owner in commands.entity_states(Checkout(result["canonical_document"]))
    head = copy.deepcopy(tl.document())
    internal(head, scope)["clips"][-1]["track"] = "extra"
    assert rebase(result["change"]["commands"], head).conflicts


@pytest.mark.parametrize("protected", ["placement", "group", "last-kind"])
def test_track_removal_protected_or_last_kind(source, protected):
    data, scope, sid = setup(source)
    if protected == "placement":
        if scope != "parent":
            data["parent"]["tracks"] = [dict(id="extra", kind="visual", label="Parent placement")]
        data["placements"][0]["track"] = "extra"
    elif protected == "group":
        internal(data, scope)["pinnedShotGroups"] = [
            {"shotId": "pinned", "trackId": "extra", "clipIds": []}
        ]
    else:
        internal(data, scope)["tracks"][:] = [
            t for t in internal(data, scope)["tracks"] if t["kind"] != "audio"
        ]
        internal(data, scope)["tracks"][-1]["kind"] = "audio"
    body = request(call("timeline.remove", sid))
    service, port = core(body, data)
    result = service.commit(body, port)
    if protected == "placement" and scope != "parent":
        assert result["status"] == "accepted", result["error"]
    else:
        assert result["status"] == "refused" and not port.accepts


@pytest.mark.parametrize(
    "duration,source_range,kind",
    [
        (None, None, "image/png"),
        (2, [0, 3], "video/mp4"),
        (2, [-1, 1], "audio/wav"),
        (0, None, "audio/wav"),
        (-1, None, "audio/wav"),
    ],
)
def test_media_intrinsic_and_explicit_range_negatives(source, duration, source_range, kind):
    data, scope, record = media_setup(source, duration)
    record["entry"]["media_type"] = kind
    cmd = media_command(scope, record, source_range=source_range)
    body = request(cmd)
    service, port = media_port(body, data, record)
    result = service.commit(body, port)
    assert result["status"] == "refused" and not port.accepts


@pytest.mark.parametrize(
    "retained", ["none", "clip", "zero-output", "changed-entry", "preexisting"]
)
def test_media_registry_undo_consequence_preserves_shared_references(source, retained):
    from astrid.sdk.timeline.music_beds import collection

    data, scope, record = media_setup(source)
    cmd = media_command(scope, record)
    body = request(cmd)
    service, port = media_port(body, data, record)
    first = service.commit(body, port)
    assert first["status"] == "accepted", first["error"]
    if retained == "preexisting":
        # A second insertion reuses the authored entry and never owns its removal.
        data = first["canonical_document"]
        data = copy.deepcopy(data)
        internal(data, scope)["registry"]["assets"]["selected"]["opaque"] = {"sibling": None}
        cmd["args"]["clip_id"] = "reused"
        cmd["expect"] = {scope + "/clip:reused::": {"state": "absent"}}
        body = request(cmd)
        service, port = media_port(body, data, record)
        first = service.commit(body, port)
        assert first["status"] == "accepted", first["error"]
        target = "reused"
    else:
        target = "reserved"
    tl = Checkout(copy.deepcopy(first["canonical_document"]))
    if retained == "clip":
        internal(tl.bundle, scope)["clips"].append(
            dict(
                id="other", clipType="media", track="sfx", asset="selected", at=3, hold=1, params={}
            )
        )
    if retained == "zero-output":
        owner = scope + "/clip:hidden"
        collection(tl, create=True)[owner] = dict(
            owner=owner,
            source=dict(id="hidden", clipType="media", track="sfx", asset="selected", at=0, hold=1),
            outputs=[],
            ripple_basis=[],
        )
    if retained == "changed-entry":
        tl._registry(None if scope == "parent" else "A")["selected"]["opaque"] = "later"
    before = commands.entity_states(tl)
    rows = internal(tl.bundle, scope)["clips"]
    rows[:] = [r for r in rows if r["id"] != target]
    commands.undo_media_registry_consequences(tl, before)
    assert ("selected" in tl._registry(None if scope == "parent" else "A")) == (retained != "none")
    assert port.catalog == record  # Shared project objects/catalog remain untouched.


def test_media_other_duration_range_and_pinned_occurrence_origin(source):
    data, _, record = media_setup(source)
    scope = "occurrence:occ-B"
    data["shots"]["B"]["internal_timeline"]["tracks"].append(
        dict(id="sfx", kind="audio", label="SFX")
    )
    record["entry"]["media_type"] = "video/mp4"
    cmd = media_command(scope, record, duration=1, source_range=[0.5, 1.5])
    cmd["args"]["at"] = 4.5
    body = request(cmd)
    body["scope"]["target"] = scope
    service, port = media_port(body, data, record)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    row = next(
        c
        for c in result["canonical_document"]["shots"]["B"]["internal_timeline"]["clips"]
        if c["id"] == "reserved"
    )
    assert row["at"] == 0.5 and row["from"] == 0.5 and row["to"] == 1.5


def test_publication_inverse_restores_order_and_populated_track_in_one_apply(source):
    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.inverse import inverse_command

    data, scope, sid = setup(source)
    order = [scope + "/track:" + row["id"] for row in internal(data, scope)["tracks"]]
    result, _ = accepted(
        data, call("timeline.reorder", sid, order=order[::-1], expected_order=order)
    )
    after = result["canonical_document"]
    inverse = inverse_command(result["change"]["commands"], data, after, "pub-order")
    restored, plan, _ = RuntimeCommitService._apply(after, [inverse])
    assert [r["id"] for r in internal(restored, scope)["tracks"]] == [
        r["id"] for r in internal(data, scope)["tracks"]
    ]
    assert len(plan.calls) == 1
    content = internal(data, scope)["clips"][-1]
    content["track"] = "extra"
    clip = scope + "/clip:" + content["id"]
    result, _ = accepted(data, call("clip.remove", clip), call("timeline.remove", sid))
    inverse = inverse_command(
        result["change"]["commands"], data, result["canonical_document"], "pub-delete"
    )
    restored, plan, _ = RuntimeCommitService._apply(result["canonical_document"], [inverse])
    states = commands.entity_states(Checkout(restored))
    assert sid in states and clip in states and states[clip]["track"] == "extra"
    assert len(plan.calls) == 1


@pytest.mark.parametrize("shared", [False, True])
def test_publication_media_inverse_preserves_shared_project_and_referenced_registry(source, shared):
    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.inverse import inverse_command

    data, scope, record = media_setup(source)
    body = request(media_command(scope, record))
    service, port = media_port(body, data, record)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    after = result["canonical_document"]
    inverse = inverse_command(result["change"]["commands"], data, after, "pub-media")
    if shared:
        after = copy.deepcopy(after)
        internal(after, scope)["clips"].append(
            dict(
                id="still-used",
                clipType="media",
                track="sfx",
                asset="selected",
                at=3,
                hold=1,
                params={},
            )
        )
    restored, plan, _ = RuntimeCommitService._apply(after, [inverse])
    assert scope + "/clip:reserved" not in commands.entity_states(Checkout(restored))
    assert ("selected" in internal(restored, scope)["registry"]["assets"]) == shared
    assert port.catalog == record and len(plan.calls) == 1


def test_subsequent_batch_expectations_capture_preceding_state_and_replay(source):
    data, scope, sid = setup(source)
    first = {
        **call("timeline.set", sid, values={"label": "First"}),
        "expect": {"label": {"state": "present", "value": "Extra"}},
    }
    second = {
        **call("timeline.set", sid, values={"label": "Second"}),
        "expect": {"label": {"state": "present", "value": "First"}},
    }
    result, _ = accepted(data, first, second)
    assert not rebase(result["change"]["commands"], data).conflicts
    assert internal(result["canonical_document"], scope)["tracks"][-1]["label"] == "Second"
    second["expect"]["label"]["value"] = "Extra"
    body = request(first, second)
    service, port = core(body, data)
    assert service.commit(body, port)["status"] == "refused" and not port.accepts


def test_zero_output_content_removal_is_explicit_and_undoable_with_track(source):
    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.inverse import inverse_command
    from astrid.sdk.timeline.music_beds import collection

    data, scope, sid = setup(source)
    tl = Checkout(copy.deepcopy(data))
    owner = scope + "/clip:zero"
    collection(tl, create=True)[owner] = dict(
        owner=owner,
        source=dict(
            id="zero", clipType="media", track="extra", asset="P", at=0, **{"from": 0, "to": 2}
        ),
        outputs=[],
        ripple_basis=[dict(at_s=0, delta_s=-2)],
    )
    before = tl.document()
    result, _ = accepted(before, call("clip.remove", owner), call("timeline.remove", sid))
    assert owner not in commands.entity_states(Checkout(result["canonical_document"]))
    inverse = inverse_command(
        result["change"]["commands"], before, result["canonical_document"], "pub-zero"
    )
    restored, _, _ = RuntimeCommitService._apply(result["canonical_document"], [inverse])
    assert commands.entity_states(Checkout(restored))[owner]["music_bed"]["outputs"] == []


def test_audio_intrinsic_duration_does_not_infer_track_names(source):
    data, scope, record = media_setup(source)
    internal(data, scope)["tracks"][-1]["id"] = "A1"
    cmd = media_command(scope, record)
    cmd["select"] = scope + "/track:A1"
    body = request(cmd)
    service, port = media_port(body, data, record)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]
    row = next(
        c for c in internal(result["canonical_document"], scope)["clips"] if c["id"] == "reserved"
    )
    assert row["hold"] == 2 and row["track"] == "A1"


@pytest.mark.parametrize("kind", ["image/png", "video/mp4"])
def test_non_audio_media_requires_explicit_duration(source, kind):
    data, scope, record = media_setup(source)
    record["entry"]["media_type"] = kind
    cmd = media_command(scope, record)
    body = request(cmd)
    service, port = media_port(body, data, record)
    assert service.commit(body, port)["status"] == "refused" and not port.accepts
    cmd["args"]["duration"] = 1
    body = request(cmd)
    service, port = media_port(body, data, record)
    result = service.commit(body, port)
    assert result["status"] == "accepted", result["error"]


def test_media_lost_response_returns_stored_result_without_reread(source):
    data, scope, record = media_setup(source)
    body = request(media_command(scope, record))
    service, port = media_port(body, data, record)
    original = port.accept

    def lost(prepared):
        original(prepared)
        raise TimeoutError("accepted response lost")

    port.accept = lost
    with pytest.raises(TimeoutError):
        service.commit(body, port)
    assert port.saved is not None
    port.catalog["entry"]["duration_seconds"] = 99
    assert service.commit(body, port) == port.saved["result"]
    assert len(port.accepts) == 1 and len(port.media_reads) == 1


@pytest.mark.parametrize("change", ["selected", "membership", "unrelated"])
def test_media_atomic_dependency_refusal_or_unrelated_catalog_update(source, change):
    class RuntimeConflict(Exception):
        code = "conflict"

    data, scope, record = media_setup(source)
    body = request(media_command(scope, record))
    service, port = media_port(body, data, record)
    original = port.accept

    def atomic(prepared):
        # C's existing acceptance transaction owns this check. This double
        # exercises Astrid's handling; C006 proves the real SQL transaction.
        if change == "selected":
            port.catalog["variant"]["metadata"]["retained-change"] = True
        if change in {"selected", "membership"}:
            raise RuntimeConflict("selected media changed or lost membership")
        port.unrelated = {"new-generation": True}
        return original(prepared)

    port.accept = atomic
    if change != "unrelated":
        with pytest.raises(RuntimeConflict):
            service.commit(body, port)
        assert port.saved is None
    else:
        result = service.commit(body, port)
        assert result["status"] == "accepted", result["error"]
    assert len(port.media_reads) == 1


def test_track_and_media_created_identity_mapping_is_explicit(source):
    data, scope, _ = setup(source)
    sid = scope + "/track:new"
    cmd = {
        **call("timeline.add", sid, track=dict(id="new", kind="audio", label="New")),
        "expect": {sid + "::": {"state": "absent"}},
    }
    result, _ = accepted(data, cmd)
    mapped = scope + "/track:mapped"
    replay = rebase(result["change"]["commands"], data, id_map={sid: mapped})
    assert not replay.conflicts, replay.conflicts
    assert mapped in commands.entity_states(Checkout(replay.document)) and replay.id_map == {
        sid: mapped
    }
    data, scope, record = media_setup(source)
    body = request(media_command(scope, record))
    service, port = media_port(body, data, record)
    result = service.commit(body, port)
    old, new = scope + "/clip:reserved", scope + "/clip:mapped"
    replay = rebase(result["change"]["commands"], data, id_map={old: new})
    assert not replay.conflicts, replay.conflicts
    assert new in commands.entity_states(Checkout(replay.document)) and replay.id_map == {old: new}


def test_malformed_media_command_array_uses_existing_typed_refusal(source):
    data, _, _ = setup(source)
    body = request()
    body["change"]["commands"] = None
    service, port = core(body, data)
    result = service.commit(body, port)
    assert (
        result["status"] == "refused" and result["error"]["message"] == "commands must be an array"
    )
    assert not port.accepts


def test_add_at_top_and_reorder_is_one_publication_inverse(source):
    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.inverse import inverse_command

    data, scope, sid = setup(source)
    new = scope + "/track:new"
    order = [scope + "/track:" + row["id"] for row in internal(data, scope)["tracks"]]
    add = {
        **call("timeline.add", new, track=dict(id="new", kind="visual", label="New")),
        "expect": {new + "::": {"state": "absent"}},
    }
    result, _ = accepted(
        data,
        add,
        call("timeline.reorder", new, expected_order=order + [new], order=[new] + order[::-1]),
    )
    inverse = inverse_command(
        result["change"]["commands"], data, result["canonical_document"], "pub-add-at-top"
    )
    restored, plan, _ = RuntimeCommitService._apply(result["canonical_document"], [inverse])
    assert new not in commands.entity_states(Checkout(restored))
    assert [r["id"] for r in internal(restored, scope)["tracks"]] == [
        r["id"] for r in internal(data, scope)["tracks"]
    ]
    assert len(plan.calls) == 1


def test_publication_reorder_inverse_rebases_with_explicit_track_identity_map(source):
    from astrid.sdk.timeline.commit import RuntimeCommitService
    from astrid.sdk.timeline.inverse import inverse_command

    data, scope, sid = setup(source)
    order = [scope + "/track:" + r["id"] for r in internal(data, scope)["tracks"]]
    result, _ = accepted(
        data, call("timeline.reorder", sid, order=order[::-1], expected_order=order)
    )
    inv = inverse_command(
        result["change"]["commands"], data, result["canonical_document"], "pub-mapped-order"
    )
    _, plan, _ = RuntimeCommitService._apply(result["canonical_document"], [inv])
    head = copy.deepcopy(result["canonical_document"])
    next(r for r in internal(head, scope)["tracks"] if r["id"] == "extra")["id"] = "mapped"
    mapped = scope + "/track:mapped"
    replay = rebase(plan.calls, head, id_map={sid: mapped})
    assert not replay.conflicts, replay.conflicts
    assert replay.id_map == {sid: mapped}
    assert [r["id"] for r in internal(replay.document, scope)["tracks"]] == [
        r["id"].replace("extra", "mapped") for r in internal(data, scope)["tracks"]
    ]
