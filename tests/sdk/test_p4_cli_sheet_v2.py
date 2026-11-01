"""P4's real CLI/sheet front door against the semantic commit path.

The generated-client method is the only fake boundary here.  Its acceptance
path still runs the repository command engine and canonical document compiler;
the parser, NativeSheet adapter, and durable outbox are not stubbed.
"""
from __future__ import annotations

import hashlib
import json

from astrid.packs.timeline import cli
from astrid.sdk import storage_root
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.outbox import request_bytes
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_sheet import NativeSheet
from tests.sdk.test_timeline_sheet_native import edit_cell
from tests.sdk.test_timeline_sync import connected, initial_bundle  # noqa: F401


def test_cli_sheet_v2_front_door_preserves_provenance_and_unrelated_fields(
    tmp_path, capsys, monkeypatch, request
):
    """An edited sheet must become one guarded v2 semantic commit."""
    client, generated, path = request.getfixturevalue("connected")
    monkeypatch.setattr(storage_root, "resolve_runtime_data_root", lambda: path.parent)
    scope = {"realm_id": "realm", "actor_id": "actor", "project_id": "p", "timeline_id": "t"}
    outbox_path = path.parent / "timeline-outbox" / (
        hashlib.sha256(request_bytes(scope)).hexdigest() + ".json"
    )
    generated.path = outbox_path
    original = initial_bundle()
    generated.bundles = {"r0": original}
    generated.head = {"revision_id": "r0", "publication_id": "pub0"}

    # The real adapter creates the pinned editable sheet and stores its source
    # bundle in the same durable outbox used by the CLI commit.
    session = TimelineSession.open("p", "t", client=client)
    sheet = NativeSheet.export(session)
    edited = edit_cell(sheet.text, "parent/clip:a", "5")
    sheet_path = tmp_path / "edited.sheet"
    sheet_path.write_text(edited, encoding="utf-8")

    # This is the installed product parser/handler.  No --native flag is
    # supplied; the handler itself chooses the current sheet route.
    parsed = cli.build_parser(client).parse_args([
        "apply", "t", str(sheet_path), "--project", "p", "--json",
        "--idempotency-key", "p4-cli-sheet-v2",
    ])
    assert parsed.handler(parsed) == 0
    reply = json.loads(capsys.readouterr().out)
    assert reply["ok"] is True
    assert reply["data"]["status"] == "accepted"

    assert len(generated.calls) == 1
    body = json.loads(generated.calls[0][0])
    assert generated.calls[0][1] == "p4-cli-sheet-v2"

    # The real acceptance path applied exactly the requested edit.  Every
    # unrelated closure field remains byte-for-byte equal to the original.
    accepted = generated.bundles["r1"]
    expected_command = capture_move(original, "parent/clip:a", 5)
    expected_bundle = execute(original, [expected_command]).bundle
    assert accepted == expected_bundle
    assert accepted["parent"]["clips"][0]["at"] == 5
    assert accepted["parent"]["clips"][0]["app"] == {
        "astrid": {"fixed_start": True, "for": 1.0}
    }

    # Independent of execute: preserve every original clip field and sibling,
    # allowing only the authored start and the native move's derived markers.
    assert accepted["parent"]["clips"][1] == original["parent"]["clips"][1]
    expected_clips = [
        {**original["parent"]["clips"][0], "at": 5,
         "app": {"astrid": {"fixed_start": True, "for": 1.0}}},
        *original["parent"]["clips"][1:],
    ]
    assert accepted["parent"]["clips"] == expected_clips
    assert accepted["parent"]["config"] == original["parent"]["config"]
    # Whole-bundle equality also guards raw base payloads, closure, ordering,
    # and absent fields (including occurrences), without projecting a subset.
    assert accepted == {
        **original,
        "parent": {**original["parent"], "clips": expected_clips},
    }

    expected_step = {
        "id": "sheet-1",
        "label": "Apply sheet",
        "intent": "",
        "tags": ["sheet"],
        "command_ordinals": [0],
        "patch": [],
    }

    assert body["api_version"] == 1
    assert {key: body[key] for key in ("project_id", "timeline_id", "base_revision_id",
                                       "base_publication_id")} == {
        "project_id": "p", "timeline_id": "t", "base_revision_id": "r0",
        "base_publication_id": "pub0",
    }
    assert body["commands"] == [expected_command]
    assert body["label"] == "Apply sheet"
    assert body["steps"] == [expected_step]
    assert body["merge"] == "fields"
    assert body["source"] == {"via": "sheet"}
    assert body["dry_run"] is False
    assert body.get("reads") is None

    # The generated transport fixture returns the current accepted envelope;
    # canonical closure/digest/head/receipt/authoritative commands are real
    # fixture fields.  v2 step dependencies/merge_mode and conflict/invalid/
    # preview/history variants are not supplied by this generated boundary yet.
    for field in ("canonical_document", "document_digest", "accepted_revision_id", "commands"):
        assert field in reply["data"]
    assert reply.get("receipt")

    # Required mutation idempotency is carried by the generated call's header,
    # not overlaid into the wire body or reconstructed in this test.
