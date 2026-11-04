"""P4's CLI/sheet front door through the normal Runtime HTTP boundary.

This is prepared for the compatible v2 Runtime/client adoption.  The shared
fixture supplies the real daemon, provider, generated client and HTTP adapter;
the test does not replace any of those boundaries.
"""
from __future__ import annotations

import copy
import json
import shlex

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
from astrid.packs.timeline.cli import build_parser
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    _digest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_sheet import NativeSheet
from tests.editing_conformance import test_native_batch_ingress
from tests.helpers.trusted_seed import seed_trusted_parent
from tests.sdk.test_timeline_sheet_native import edit_cell
from tests.sdk.test_timeline_sync import initial_bundle

pytestmark = pytest.mark.integration
runtime = test_native_batch_ingress.runtime


def _run(world, capsys, *args):
    parsed = build_parser(world.client()).parse_args([
        *args, "--project", world.project, "--json",
    ])
    code = parsed.handler(parsed)
    output = capsys.readouterr()
    return code, json.loads(output.out)


def _seed_initial_bundle(world):
    """Install the original SDK fixture as Runtime's saved parent closure."""
    source = initial_bundle()
    # The authoring bundle omits derived occurrences.  Compile it first so the
    # seed uses the normal publication constructor, then add only Runtime's
    # required managed registry envelope; no media is registered here.
    parent = copy.deepcopy(
        compile_authoring_candidate(source).publication["parent_composition"]
    )
    parent.setdefault("registry", {})
    publication = {
        "project_id": world.project,
        "timeline_id": "film",
        "expected_head": None,
        "parent_revision_id": "p4-cli-sheet-baseline",
        "parent_composition": parent,
        "shot_revisions": [],
        "internal_timeline_revisions": [],
    }
    seeded = seed_trusted_parent(
        world.service(), publication, idempotency_key="p4-cli-sheet-baseline",
    )
    world.restart()
    return source, seeded


def test_cli_sheet_v2_runtime_preserves_document_and_reads_back(
    runtime, tmp_path, capsys
):
    source, seeded = _seed_initial_bundle(runtime)
    client = runtime.client()
    session = TimelineSession.open(runtime.project, "film", client=client)
    original = copy.deepcopy(session._bundle(session.outbox.read()))
    # `registry` is a Runtime-managed canonical envelope field; all authored
    # source fields remain the exact SDK fixture payload.
    expected_parent = copy.deepcopy(source["parent"])
    expected_parent.setdefault("registry", {})
    assert original["parent"] == expected_parent

    sheet = NativeSheet.export(session)
    sheet_path = tmp_path / "edited.sheet"
    sheet_path.write_text(edit_cell(sheet.text, "parent/clip:a", "5"), encoding="utf-8")

    parsed = build_parser(client).parse_args([
        "apply", "film", str(sheet_path), "--project", runtime.project,
        "--json", "--idempotency-key", "p4-cli-sheet-v2",
    ])
    assert parsed.native is False
    assert parsed.handler(parsed) == 0
    reply = json.loads(capsys.readouterr().out)

    assert reply["ok"] is True
    assert reply["data"]["status"] == "accepted"
    assert len(runtime.calls) == 1
    request = json.loads(runtime.calls[0][0])
    assert runtime.calls[0][1] == "p4-cli-sheet-v2"

    expected_command = capture_move(original, "parent/clip:a", 5)
    expected_bundle = execute(original, [expected_command]).bundle
    accepted_document = reply["data"]["canonical_document"]
    accepted = bundle_from_document(accepted_document)
    assert accepted["parent"] == expected_bundle["parent"]
    assert accepted["parent"]["clips"][1] == original["parent"]["clips"][1]
    assert accepted["parent"]["config"] == original["parent"]["config"]
    assert accepted["placements"] == original["placements"]
    assert accepted["shots"] == original["shots"]
    # The ordinary revision read has no authenticated document cache; execute
    # preserves that boundary. Opening an accepted canonical document adds it.
    assert "_runtime_document" not in source["source_mapping"]
    assert "_runtime_document" not in original["source_mapping"]
    assert "_runtime_document" not in expected_bundle["source_mapping"]
    assert original["source_mapping"] == source["source_mapping"]
    assert expected_bundle["source_mapping"] == original["source_mapping"]
    assert accepted["source_mapping"] == {
        **expected_bundle["source_mapping"], "_runtime_document": accepted_document,
    }

    base_head = {
        "revision_id": seeded["data"]["accepted_revision_id"],
        "publication_id": seeded["receipt"]["receipt_id"],
    }
    base_read = client.timelines.open_bundle(
        runtime.project, "film", revision_id=base_head["revision_id"],
        publication_id=base_head["publication_id"],
    )
    assert base_read.ok
    authenticated_base = base_read.data["bundle"]
    base_document = authenticated_base["source_mapping"]["_runtime_document"]
    assert authenticated_base == {
        **original,
        "source_mapping": {**original["source_mapping"], "_runtime_document": base_document},
        "base_parent": {**original["base_parent"],
                        "publication_id": base_head["publication_id"]},
    }
    assert base_document["parent_revision"]["revision_id"] == base_head["revision_id"]
    # The provider reopens the verified closure without the client's base_parent
    # publication annotation; the full request/head still bind the original pair.
    expected_plan = execute(
        bundle_from_document(base_document), [expected_command], commit_request=request,
        evaluated_head=base_head,
    )
    assert expected_plan.bundle["source_mapping"] == authenticated_base["source_mapping"]
    assert accepted_document == document_from_bundle(expected_plan.bundle)

    expected_step = {
        "id": "sheet-1",
        "label": "Apply sheet",
        "intent": "",
        "tags": ["sheet"],
        "command_ordinals": [0],
        "patch": [],
    }
    assert request["api_version"] == 1
    assert {key: request[key] for key in (
        "project_id", "timeline_id", "base_revision_id", "base_publication_id",
    )} == {
        "project_id": runtime.project,
        "timeline_id": "film",
        "base_revision_id": seeded["data"]["accepted_revision_id"],
        "base_publication_id": seeded["receipt"]["receipt_id"],
    }
    assert request["commands"] == [expected_command]
    assert request["label"] == "Apply sheet"
    assert request["steps"] == [expected_step]
    assert request["merge"] == "fields"
    assert request["source"] == {"via": "sheet"}
    assert request["dry_run"] is False
    assert request.get("reads") is None

    data = reply["data"]
    assert data["canonical_document"] == accepted_document
    for field in (
        "canonical_document", "document_digest", "accepted_revision_id", "commands",
        "steps", "merge_mode", "previous_revision_id", "previous_publication_id",
    ):
        assert field in data
    assert data["commands"] == [expected_command]
    assert data["steps"][0]["id"] == "sheet-1"
    assert data["steps"][0]["command_ordinals"] == [0]
    assert data["merge_mode"] == "fields"
    assert reply["receipt"]["receipt_id"]
    assert data["document_digest"] == _digest(accepted_document)
    assert data["accepted_revision_id"] == accepted_document["parent_revision"]["revision_id"]
    assert data["previous_revision_id"] == base_head["revision_id"]
    assert data["previous_publication_id"] == base_head["publication_id"]
    assert data["steps"] == expected_plan.steps
    assert data == {
        **reply["receipt"]["result"],
        "accepted_publication_id": reply["receipt"]["receipt_id"],
        "show_command": shlex.join([
            "astrid", "timelines", "show", "film", "--project", runtime.project,
            "--published", "--revision-id", data["accepted_revision_id"],
        ]),
    }

    show_code, shown = _run(
        runtime, capsys, "show", "film", "--published",
        "--revision-id", data["accepted_revision_id"],
    )
    assert show_code == 0
    assert shown["ok"] is True
    assert shown["data"]["revision_id"] == data["accepted_revision_id"]
    assert any(
        row["clip_id"] == "a" and row["start"] == 5
        for row in shown["data"]["rows"]
    )

    history_code, history = _run(runtime, capsys, "history", "film", "--limit", "10")
    assert history_code == 0
    assert history["ok"] is True
    revision = next(
        item for item in history["data"]["revisions"]
        if item["id"] == data["accepted_revision_id"]
    )
    assert revision["message"] == "Apply sheet"
    assert revision["publication_id"] == reply["receipt"]["receipt_id"]

    raw_history = client.timelines.history(runtime.project, "film", limit=10)
    assert raw_history.ok
    item = next(
        item for item in raw_history.data["items"]
        if item["publication_id"] == reply["receipt"]["receipt_id"]
    )
    assert item["label"] == "Apply sheet"
    assert item["publisher"]
    assert item["steps"][0]["patch"] == data["steps"][0]["patch"]
    assert item["merge_mode"] == "fields"
    assert item["records"]["commands"] == expected_plan.records
    assert item["steps"] == data["steps"]
    assert item["previous"] == base_head
    assert item["receipt"] == reply["receipt"]
    accepted_head = {
        "revision_id": data["accepted_revision_id"],
        "publication_id": reply["receipt"]["receipt_id"],
    }
    assert raw_history.data["head"] == accepted_head
    stored = client.timelines.open_bundle(
        runtime.project, "film", revision_id=accepted_head["revision_id"],
        publication_id=accepted_head["publication_id"],
    )
    assert stored.ok
    assert stored.data["is_current_head"] is True
    assert stored.data["bundle"] == {
        **accepted,
        "base_parent": {**accepted["base_parent"],
                        "publication_id": accepted_head["publication_id"]},
    }
