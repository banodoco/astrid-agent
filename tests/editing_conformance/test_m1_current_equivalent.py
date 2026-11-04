"""NEW-OURS current equivalent for the M1 word-bound commit journey.

This file deliberately does not alter ``test_commit_runtime_m1.py``.  The
current source is the native word/transcript fixture: media, the transcript,
its pin, and the word binding are all admitted by the registered provider
through real Runtime commits before the SDK outbox journey starts.

Source-only candidate: native word-choice dependency 2154168d is HELD.
This journey is not functional proof until that dependency is released.
"""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline.sync import TimelineSession
from tests.editing_conformance import current_word_baseline_fixture as current_inputs
from tests.editing_conformance.test_native_word_anchor import bundle as current_bundle
from tests.editing_conformance.test_native_word_anchor import pick as current_pick

pytestmark = pytest.mark.integration
retained = current_inputs.retained


@pytest.mark.parametrize("touched_basis", [False, True],
                         ids=["disjoint-rebase-undo", "touched-basis-conflict"])
def test_new_ours_current_equivalent_preserves_m1_commit_journey(
    retained, real_runtime, tmp_path, monkeypatch, capsys, touched_basis,
):
    """Exercise the original M1 gesture order against current word authority.

    NEW-OURS identity: this is one current-equivalent journey, not a rewrite
    or a reclassification of any ``original550`` selector.  The binding and
    the original music-bed fixture are authored through the registered
    provider before the public SDK outbox journey starts.
    """
    from runtime_protocol.server import RuntimeHandler
    from runtime_protocol.timeline_commit_port import route_path

    from astrid.sdk.client import AstridClient
    from astrid.sdk.exceptions import ServiceUnavailableError
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline.sync import CommitConflict
    from astrid.sdk.workspace_client import WorkspaceClientError

    case = retained
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "client-data"))
    source_document = copy.deepcopy(case.seed["data"]["canonical_document"])
    expected_media = source_document["dependency_manifest"]["media"]
    media_id = case.source["content"]["media_id"]
    assert expected_media == [{"media_id": media_id, "content_digest": media_id}]
    # Keep the original 1/2/3/4-second move inputs. The current fixture's
    # 30000/1001 rate lands those inputs at frames 30/60/90/120 (capture.move).
    assert current_inputs.FPS == {"numerator": 30000, "denominator": 1001}
    landed = {1: 1.001, 2: 2.002, 3: 3.003, 4: 4.004}

    def current_commit(commands, key, label):
        """Submit the complete C0 v2 body through the registered provider."""
        base = case.head()
        body = {
            "api_version": 1,
            "project_id": case.project,
            "timeline_id": "film",
            "base_revision_id": base["revision_id"],
            "base_publication_id": base["publication_id"],
            "commands": copy.deepcopy(commands),
            "label": label,
            "merge": "fields",
            "reads": [],
            "dry_run": False,
            "source": {"via": "python"},
            "rationale": "NEW-OURS current-equivalent M1",
            "group_id": key,
            "steps": [{
                "id": key,
                "label": label,
                "intent": label,
                "tags": ["m1", "current"],
                "command_ordinals": list(range(len(commands))),
                "patch": [copy.deepcopy(change)
                          for command in commands
                          if command["op"] == "doc.patch"
                          for change in command["args"]["changes"]],
                "reads": [],
            }],
        }
        result = case.service.commit_timeline(
            case.project,
            "film",
            body,
            idempotency_key=key,
            publisher="timing-editor",
            invocation_path=route_path(case.project, "film"),
            authorize=lambda: None,
        )
        document = result["data"]["canonical_document"]
        case.documents[document["parent_revision"]["revision_id"]] = document
        return result

    def history(client):
        result = client.timelines.history(case.project, "film", limit=100)
        assert result.ok, result.as_dict()
        return result.data

    def parent_of(result):
        return result["data"]["canonical_document"]["parent_revision"]["payload"]

    def assert_move(before, after, clip_id, on):
        expected = copy.deepcopy(before)
        clip = next(row for row in expected["clips"] if row["id"] == clip_id)
        clip["at"] = landed[on]
        app = clip.setdefault("app", {})
        authored = app.setdefault("astrid", {})
        for container in (app, authored):
            for key in ("on", "until", "until_film_end"):
                container.pop(key, None)
        authored.update(fixed_start=True, **{"for": 5.5})
        expected["config"]["clips"] = copy.deepcopy(expected["clips"])
        assert after == expected

    def assert_identity(document):
        parent = document["parent_revision"]["payload"]
        assert parent["config"]["clips"] == parent["clips"]
        music = next(row for row in parent["clips"] if row["id"] == "music-bed")
        assert music["clipType"] == "audio"
        assert music["asset"] == "music-bed"
        assert music["track"] == "music-other"
        assert music["hold"] == 5.5
        assert music["extension"] == {"opaque": [None, {"keep": True}]}
        music_asset = parent["registry"]["assets"]["music-bed"]
        assert music_asset["type"] == "audio"
        assert music_asset["media_id"] == media_id
        peer = next(row for row in parent["clips"] if row["id"] == "music-other")
        assert {key: value for key, value in peer.items() if key not in {"at", "app"}} == {
            "id": "music-other", "clipType": "audio", "asset": "music-bed",
            "track": "music-other", "hold": 5.5,
            "extension": {"opaque": [None, {"keep": True}]},
        }
        card = next(row for row in parent["clips"] if row["id"] == "card")
        assert card["app"]["astrid"]["on"] == current_inputs.DECLARATION
        assert card["app"]["astrid"]["word_anchors"]["on"] == binding["args"]["choice"]
        assert document["dependency_manifest"]["documents"] == [case.pin]
        assert document["dependency_manifest"]["media"] == expected_media
        for key in ("shot_revisions", "internal_timeline_revisions"):
            assert document[key] == source_document[key]
        for key in ("shots", "internal_timelines"):
            assert document["dependency_manifest"][key] == source_document["dependency_manifest"][key]

    def assert_readback(client, accepted, expected_body=None):
        from astrid.core.timeline.authoring_bundle import open_authoring_bundle

        data, receipt = accepted["data"], accepted["receipt"]
        document = data["canonical_document"]
        assert data["merge_mode"] == "fields"
        assert receipt["result"] == {
            key: value for key, value in data.items()
            if key not in {"accepted_publication_id", "show_command"}
        }
        revision = data["accepted_revision_id"]
        assert revision == document["parent_revision"]["revision_id"]
        if expected_body is not None:
            assert (data["base_revision_id"], data["base_publication_id"]) == (
                expected_body["base_revision_id"], expected_body["base_publication_id"])
            assert data["commands"] == expected_body["commands"]
        stored = client._remote._transport.get_project_parent_composition_revision(
            case.project, "film", revision)
        assert stored["project_id"] == document["project_id"] == case.project
        assert stored["timeline_id"] == document["timeline_id"] == "film"
        for key, value in document["parent_revision"].items():
            assert stored[key] == value
        assert stored["dependency_manifest"] == document["dependency_manifest"]
        expected_bundle = open_authoring_bundle(
            {**document["parent_revision"], "project_id": case.project, "timeline_id": "film"},
            shot_revisions=[{**row, "project_id": case.project}
                            for row in document["shot_revisions"]],
            internal_timeline_revisions=[{**row, "project_id": case.project}
                                         for row in document["internal_timeline_revisions"]],
        )
        opened = client.timelines.open_bundle(case.project, "film", revision_id=revision)
        assert opened.ok, opened.as_dict()
        assert opened.data["revision_id"] == revision
        assert opened.data["bundle"] == expected_bundle
        entry = next(row for row in history(client)["items"]
                     if row["publication_id"] == receipt["receipt_id"])
        assert entry["revision_id"] == revision
        assert entry["receipt"] == receipt
        assert [row["command"] for row in entry["records"]["commands"]] == data["commands"]
        assert entry["publisher"] == "timing-editor"
        assert_identity(document)

    def cli(client, *args):
        from astrid.packs.timeline.cli import build_parser

        parsed = build_parser(client).parse_args([
            *args, "--project", case.project, "--json",
        ])
        code = parsed.handler(parsed)
        output = capsys.readouterr()
        value = json.loads(output.out)
        assert code == 0, (code, value, output.err)
        assert value["ok"], value
        return value

    # Capture the held current word choice from the registered source and
    # submit it before any SDK outbox activity.  The source pin and the
    # declaration are the existing product choice, not test-owned substitutes.
    binding = current_pick(case, 1, text=current_inputs.DECLARATION)
    binding_result = current_commit(
        [binding], "new-ours-word-binding", "NEW-OURS current word binding")
    assert binding_result["data"]["status"] == "accepted"
    assert binding_result["data"]["commands"] == [binding]
    assert binding_result["data"]["merge_mode"] == "fields"
    assert binding["args"]["choice"]["document"] == case.pin
    assert binding["args"]["choice"]["source"] == "occurrence:occ/clip:voice"
    assert binding["args"]["declaration"] == current_inputs.DECLARATION
    assert binding["args"]["choice"]["word_ids"] == [
        case.source["content"]["segments"][0]["words"][1]["id"]]
    assert binding["args"]["choice"]["offset_frames"] == 1
    binding_document = binding_result["data"]["canonical_document"]
    assert binding_document["parent_revision"]["payload"]["clips"][0]["app"]["astrid"]["on"] == (
        current_inputs.DECLARATION
    )
    assert binding_document["dependency_manifest"]["documents"] == [case.pin]
    assert binding_document["dependency_manifest"]["media"] == expected_media
    assert binding_result["receipt"]["command_kind"] == "timeline.commit"

    # Recreate the original independent clip through the current document
    # emitter, retaining its exact audio, track, timing and opaque extension.
    from astrid.sdk.timeline.emitter import emit_document_commands

    authored = current_bundle(case)
    desired = copy.deepcopy(authored)
    desired["parent"]["registry"]["assets"]["music-bed"] = {
        "media_id": media_id, "type": "audio",
    }
    desired["parent"]["config"]["tracks"].append({"id": "music-other", "kind": "audio"})
    desired["parent"]["clips"].append({
        "id": "music-bed", "clipType": "audio", "asset": "music-bed",
        "track": "music-other", "at": 0, "hold": 5.5,
        "extension": {"opaque": [None, {"keep": True}]},
    })
    desired["parent"]["clips"].append({
        "id": "music-other", "clipType": "audio", "asset": "music-bed",
        "track": "music-other", "at": 0, "hold": 5.5,
        "extension": {"opaque": [None, {"keep": True}]},
    })
    desired["parent"]["config"]["clips"] = copy.deepcopy(desired["parent"]["clips"])
    setup_commands = emit_document_commands(authored, desired)
    added = current_commit(
        setup_commands, "new-ours-independent-clip", "NEW-OURS independent clip")
    original_parent = copy.deepcopy(parent_of(added))
    music = next(row for row in original_parent["clips"] if row["id"] == "music-bed")
    assert music == {
        "id": "music-bed", "clipType": "audio", "asset": "music-bed",
        "track": "music-other", "at": 0, "hold": 5.5,
        "extension": {"opaque": [None, {"keep": True}]},
    }
    assert_identity(added["data"]["canonical_document"])

    # Hand the persisted current source to the real Runtime server and use the
    # public SDK transport.  Startup registers the same provider used above.
    case.service.close()
    daemon = real_runtime(case.root, support_root=tmp_path / "support", authoring_profile="astrid")
    scopes = ["handshake", "projects:read", "projects:write", "objects:read", "objects:write",
              "tasks:read", "tasks:write"]
    _, editor_credential = daemon.credentials.provision("timing-editor", scopes)

    def open_client(actor="timing-editor", realm=None, credential=None):
        return AstridClient.open(
            endpoint=daemon.endpoint,
            credential=credential or editor_credential,
            realm_id=realm or daemon.service.realm["id"],
            actor_id=actor,
            client_name="m1-current-equivalent",
            client_version="1",
            protocol_version="workspace.v1",
        )

    client = open_client()
    observed = []
    lost = False
    original_send = RuntimeHandler._send

    def lose_a_ack(handler, status, payload=None, **kwargs):
        nonlocal lost
        if (handler.command == "POST" and handler.path.endswith("/commit")
                and getattr(handler, "_defer_response", False)):
            row = {
                "path": handler.path,
                "key": handler.headers["Idempotency-Key"],
                "body": handler._prefetched_body,
                "status": status,
                "payload": copy.deepcopy(payload),
            }
            observed.append(row)
            if row["key"] == "new-ours-a" and status == 200 and not lost:
                lost = True
                raise ConnectionError("NEW-OURS lost ACK after durable current commit")
        return original_send(handler, status, payload, **kwargs)

    monkeypatch.setattr(RuntimeHandler, "_send", lose_a_ack)

    # Preserve the original CLI write and exact canonical readback first.
    base_history = history(client)
    # The current retained seed has three genuine bootstrap-phase entries:
    # the Runtime bootstrap, initial document admission, and first-use word
    # anchor. The remaining five rows are authored scenario setup: clear and
    # correct the current seed, retain the initial track, bind the word, and
    # add the independent music fixture. Keep their exact order and labels.
    assert len(base_history["items"]) == 8
    assert base_history["items"][0]["intent"] == "bootstrap"
    assert base_history["items"][0]["records"] is None
    assert base_history["items"][-1]["receipt"] == added["receipt"]
    assert base_history["items"][-1]["intent"] == "recorded"
    moved = cli(client, "edit", "film", "--clip", "parent/clip:music-bed", "--on", "1")
    assert_move(original_parent, parent_of(moved), "music-bed", 1)
    assert observed[-1]["payload"]["data"]["canonical_document"] == moved["data"]["canonical_document"]
    assert json.loads(observed[-1]["body"])["source"] == {"via": "cli"}
    assert_readback(client, moved, json.loads(observed[-1]["body"]))
    assert_identity(moved["data"]["canonical_document"])
    base_after_cli = {
        "revision_id": moved["data"]["accepted_revision_id"],
        "publication_id": moved["receipt"]["receipt_id"],
    }

    outbox = tmp_path / "current-equivalent-outbox.json"
    session = TimelineSession.open(case.project, "film", client=client, outbox_path=outbox)
    a = session.queue_move("parent/clip:music-bed", 2)
    assert a["expect"]["parent/clip:music-bed::at"] == {"state": "present", "value": landed[1]}
    with pytest.raises(WorkspaceClientError):
        session.flush(idempotency_key="new-ours-a")
    persisted = json.loads(outbox.read_text(encoding="utf-8"))
    assert persisted["flight"]["key"] == "new-ours-a"
    assert persisted["pending"] == [a]
    first_wire = next(row for row in observed if row["key"] == "new-ours-a")
    first_response = first_wire["payload"]
    first_body = persisted["flight"]["body"]
    assert first_wire["status"] == 200
    assert first_wire["body"] == first_body.encode()
    assert persisted["flight"]["path"] == first_wire["path"] == route_path(case.project, "film")
    assert persisted["flight"]["key"] == first_wire["key"]
    assert first_response["data"]["status"] == "accepted"
    assert first_response["data"]["base_revision_id"] == base_after_cli["revision_id"]
    assert first_response["data"]["base_publication_id"] == base_after_cli["publication_id"]
    assert json.loads(first_body)["commands"] == [a]
    assert json.loads(first_body)["source"] == {"via": "python"}
    assert_readback(client, first_response, json.loads(first_body))
    assert_move(moved["data"]["canonical_document"]["parent_revision"]["payload"],
                parent_of(first_response), "music-bed", 2)

    # B is queued while A is still unacknowledged, against local A.  It must
    # remain pending verbatim through the peer write and the restart.
    b = session.queue_move("parent/clip:music-bed", 3)
    assert b["expect"]["parent/clip:music-bed::at"] == {"state": "present", "value": landed[2]}
    guard_b = json.dumps(b, sort_keys=True)
    queued = json.loads(outbox.read_text(encoding="utf-8"))
    assert queued["pending"] == [a, b]
    assert queued["flight"]["body"] == first_body

    other = TimelineSession.open(
        case.project, "film", client=client, outbox_path=tmp_path / "other-outbox.json")
    disjoint = other.move("parent/clip:music-other", 2, idempotency_key="new-ours-disjoint")
    assert_move(parent_of(first_response), parent_of(disjoint), "music-other", 2)
    assert parent_of(disjoint)["clips"][-1]["extension"] == {
        "opaque": [None, {"keep": True}]
    }
    if touched_basis:
        relevant = other.move("parent/clip:music-bed", 4, idempotency_key="new-ours-relevant")
        assert_move(parent_of(disjoint), parent_of(relevant), "music-bed", 4)

    before_restart = copy.deepcopy(history(client))
    assert len(before_restart["items"]) == len(base_history["items"]) + (4 if touched_basis else 3)
    old_epoch = client._remote._transport.health()["runtime_epoch"]
    instance = daemon.instance_id
    port = daemon.httpd.server_port
    daemon.stop()
    client = None

    # Both realm and actor mismatches are refused by the authenticated server.
    daemon = real_runtime(case.root, support_root=tmp_path / "support", port=port,
                          authoring_profile="astrid")
    assert daemon.instance_id != instance
    with pytest.raises(ServiceUnavailableError):
        open_client(realm="foreign-realm")
    with pytest.raises(ServiceUnavailableError):
        open_client(actor="foreign-actor")
    client = open_client()
    assert client._remote._transport.health()["runtime_epoch"] > old_epoch
    session = TimelineSession.open(case.project, "film", client=client, outbox_path=outbox)
    assert json.loads(outbox.read_text(encoding="utf-8"))["flight"] == persisted["flight"]
    assert history(client) == before_restart
    retried = session.flush()
    retry_wire = next(row for row in reversed(observed) if row["key"] == "new-ours-a")
    assert {key: retry_wire[key] for key in ("path", "key", "body")} == {
        key: first_wire[key] for key in ("path", "key", "body")}
    assert retry_wire["payload"] == first_response
    assert retried["receipt"] == first_response["receipt"]
    assert history(client) == before_restart
    pending = json.loads(outbox.read_text(encoding="utf-8"))
    accepted_pair = {
        "revision_id": first_response["data"]["accepted_revision_id"],
        "publication_id": first_response["receipt"]["receipt_id"],
    }
    assert pending["base"] == accepted_pair
    assert pending["pending"] == [b]
    assert pending["flight"] is None
    assert json.dumps(pending["pending"][0], sort_keys=True) == guard_b
    _, second_credential = daemon.credentials.provision("second-editor", scopes)
    second = open_client(actor="second-editor", credential=second_credential)
    with pytest.raises(TimelineEditError, match="different realm, actor, project or timeline"):
        TimelineSession.open(case.project, "film", client=second, outbox_path=outbox)
    assert json.loads(outbox.read_text(encoding="utf-8")) == pending
    assert history(client) == before_restart

    # A changed relevant basis refuses B without writing, retaining B and the
    # exact refusal/flight/history evidence in the outbox.
    if touched_basis:
        with pytest.raises(CommitConflict) as error:
            session.flush(idempotency_key="new-ours-b")
        assert error.value.failure["details"]["status"] == "conflict"
        assert error.value.failure["details"]["reason"] == "value_conflict"
        refused = json.loads(outbox.read_text(encoding="utf-8"))
        assert refused["pending"] == [b]
        assert refused["conflict"] == error.value.failure
        assert refused["flight"]["key"] == "new-ours-b"
        assert refused["flight"]["key"] != persisted["flight"]["key"]
        body_b = json.loads(refused["flight"]["body"])
        assert body_b["commands"] == [b]
        assert (body_b["base_revision_id"], body_b["base_publication_id"]) == (
            accepted_pair["revision_id"], accepted_pair["publication_id"])
        assert history(client) == before_restart
        assert parent_of(disjoint) != parent_of(relevant)
        return

    accepted_b = session.flush(idempotency_key="new-ours-b")
    assert accepted_b["data"]["status"] == "merged"
    assert accepted_b["data"]["merge_mode"] == "fields"
    body_b = json.loads(observed[-1]["body"])
    wire_b = copy.deepcopy(observed[-1])
    assert observed[-1]["key"] == "new-ours-b"
    assert observed[-1]["key"] != persisted["flight"]["key"]
    assert (body_b["base_revision_id"], body_b["base_publication_id"]) == (
        accepted_pair["revision_id"], accepted_pair["publication_id"])
    assert body_b["commands"] == [b]
    assert json.dumps(body_b["commands"][0], sort_keys=True) == guard_b
    assert body_b["source"] == {"via": "python"}
    assert_move(parent_of(disjoint), parent_of(accepted_b), "music-bed", 3)
    assert parent_of(accepted_b)["clips"][-1]["at"] == landed[2]
    assert json.loads(outbox.read_text(encoding="utf-8"))["pending"] == []
    assert_readback(client, accepted_b, body_b)
    after_b = history(client)["items"]
    assert after_b[:-1] == before_restart["items"]
    assert after_b[-1]["receipt"] == accepted_b["receipt"]
    assert len(after_b) == len(before_restart["items"]) + 1

    # Guarded inverse and redo preserve the peer field, opaque extension, and
    # exact accepted canonical document while adding only authenticated rows.
    # G5 retired --publication. --commit selects the same exact receipt and
    # reaches TimelineSession.undo via cli_native.commit_edit(inverse=True).
    undone = cli(client, "undo", "film", "--commit", accepted_b["receipt"]["receipt_id"],
                 "--idempotency-key", "new-ours-undo")
    inverse_body = json.loads(observed[-1]["body"])
    assert inverse_body["commands"] == [{
        "schema_version": 1, "op": "timeline.inverse", "version": "1", "select": None,
        "args": {"publications": [accepted_b["receipt"]["receipt_id"]]},
    }]
    assert inverse_body["source"] == {"via": "cli"}
    assert parent_of(undone) == parent_of(disjoint)
    assert_readback(client, undone, inverse_body)
    after_undo = history(client)
    assert after_undo["items"][:-1] == after_b
    assert len(after_undo["items"]) == len(after_b) + 1
    assert after_undo["items"][-1]["records"]["undo_of"] == [accepted_b["receipt"]["receipt_id"]]
    assert_readback(client, accepted_b, body_b)
    replay_b = client._remote._transport.commit_timeline(
        case.project, "film", body_b, idempotency_key=wire_b["key"])
    assert replay_b == wire_b["payload"]
    assert {key: observed[-1][key] for key in ("path", "key", "body")} == {
        key: wire_b[key] for key in ("path", "key", "body")}
    assert history(client) == after_undo

    shown = cli(client, "show", "film", "--published", "--revision-id",
                undone["data"]["accepted_revision_id"])
    assert shown["data"]["revision_id"] == undone["data"]["accepted_revision_id"]
    assert shown["data"]["working_copy"] is None
    layers = cli(client, "show", "film", "--published", "--revision-id",
                 undone["data"]["accepted_revision_id"], "--layers", "--clip", "music-bed")
    inspection = layers["data"]["native_inspection"]
    assert inspection["revision_id"] == undone["data"]["accepted_revision_id"]
    clip = inspection["selected_parent_clips"][0]
    assert clip["clip_id"] == "music-bed"
    assert clip["start"] == [1001, 500]
    visualized = cli(client, "visualize", "film", "--published", "--revision-id",
                     undone["data"]["accepted_revision_id"], "--mode", "inputs", "--format", "md")
    assert visualized["data"]["outputs"]["inspection"]["revision_id"] == (
        undone["data"]["accepted_revision_id"]
    )

    session = TimelineSession.open(case.project, "film", client=client, outbox_path=outbox)
    assert json.loads(outbox.read_text(encoding="utf-8"))["base"] == {
        "revision_id": undone["data"]["accepted_revision_id"],
        "publication_id": undone["receipt"]["receipt_id"],
    }
    redone = session.undo([undone["receipt"]["receipt_id"]], idempotency_key="new-ours-redo")
    assert redone["data"]["canonical_document"] == accepted_b["data"]["canonical_document"]
    assert_readback(client, redone)
    final_history = history(client)
    ids = [row["publication_id"] for row in final_history["items"]]
    assert ids.count(first_response["receipt"]["receipt_id"]) == 1
    assert final_history["items"][:-1] == after_undo["items"]
    assert final_history["items"][-1]["records"]["undo_of"] == [undone["receipt"]["receipt_id"]]
