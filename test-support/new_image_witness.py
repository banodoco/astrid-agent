"""Bounded A4 witness for one real App-produced new-image request.

This is an execution harness, not a product helper.  It opens the same
historical Runtime realm used by the parent-media conformance helper, adds the
visual track through that helper's public capture path, and leaves the raw
track fields in their canonical locations while the App producer runs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AGENT = Path(__file__).resolve().parents[1]
RUNTIME = Path("/home/user/work/wt/rt-pin-8ea0766f")
WORKER = Path(
    "/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a"
    "/scratchpad/orch/workers/agt-A4-image-live-0554"
)
EVIDENCE = WORKER / "evidence"
TMP_ROOT = Path(os.environ.get("TMPDIR", "/tmp"))
REALM_ROOT = TMP_ROOT / f"a4-image-live-0554-realm-{os.getpid()}"
OUTPUT = Path(
    "/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a"
    "/scratchpad/orch/design/app-A4-image-live-0554-output.json"
)
APP_SOURCE = "f4f6b4f95c430956b1a5c05750ac3e4573286785"
PRODUCER_SOURCE = "8f686e3e"
RUNTIME_SOURCE = "8ea0766f5e9fb23cea26c7398770a8aaac06b258"
RUNTIME_PIN = "8ea0766f"

os.environ["ASTRID_RUNTIME_CHECKOUT"] = str(RUNTIME)
sys.path[:0] = [str(AGENT), str(RUNTIME), str(RUNTIME / "tests")]

from _pytest.monkeypatch import MonkeyPatch  # noqa: E402
from runtime_protocol.errors import RuntimeErrorBase  # noqa: E402
from runtime_protocol.service import RuntimeService  # noqa: E402
from runtime_protocol.timeline_commit_port import route_path, validate_request  # noqa: E402
from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider  # noqa: E402

from tests.editing_conformance import test_commit_runtime_parent_media as parent  # noqa: E402


def compact_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def write_json(path: Path, value: Any) -> bytes:
    data = compact_json(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return data


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def current_head(service: Any, project: str) -> dict[str, str]:
    value = service.get_project_timeline(project, "film")
    return {"revision_id": value["head_revision_id"], "publication_id": value["head_publication_id"]}


def current_document(editing: Any) -> dict[str, Any]:
    return parent.current(editing)


def db_snapshot(service: Any) -> list[str]:
    return list(service.store.conn.iterdump())


def commit(service: Any, editing: Any, request: dict[str, Any], key: str) -> Any:
    return service.commit_timeline(
        editing.project,
        "film",
        request,
        idempotency_key=key,
        publisher="app-image-witness",
        invocation_path=route_path(editing.project, "film"),
        authorize=editing.authorize,
    )


def inverse_request(editing: Any, head: dict[str, str], receipt_id: str) -> dict[str, Any]:
    command = {
        "schema_version": 1,
        "op": "timeline.inverse",
        "version": "1",
        "select": None,
        "args": {"publications": [receipt_id]},
    }
    return {
        "api_version": 1,
        "project_id": editing.project,
        "timeline_id": "film",
        "base_revision_id": head["revision_id"],
        "base_publication_id": head["publication_id"],
        "commands": [command],
        "label": "Undo Add media",
        "steps": [
            {
                "id": "undo-add-media",
                "label": "Undo Add media",
                "intent": "",
                "tags": [],
                "command_ordinals": [0],
                "patch": [],
                "reads": [],
            }
        ],
        "merge": "fields",
        "reads": [],
        "dry_run": False,
        "source": {"via": "app"},
    }


def refusal(
    service: Any,
    editing: Any,
    request: dict[str, Any],
    key: str,
    name: str,
) -> dict[str, Any]:
    before = {"head": current_head(service, editing.project), "db": db_snapshot(service)}
    try:
        commit(service, editing, request, key)
    except RuntimeErrorBase as exc:
        result = {
            "name": name,
            "status": "refused",
            "exception_type": type(exc).__name__,
            "message": str(exc),
            "code": getattr(exc, "code", None),
            "details": copy.deepcopy(getattr(exc, "details", None)),
            "head_unchanged": current_head(service, editing.project) == before["head"],
            "database_unchanged": db_snapshot(service) == before["db"],
        }
        assert result["head_unchanged"] and result["database_unchanged"]
        write_json(EVIDENCE / f"refusal-{name}.json", result)
        return result
    raise AssertionError(f"{name}: malformed control was accepted")


def _assert_five_field_selection(selection: dict[str, Any]) -> None:
    assert set(selection) == {
        "asset",
        "generation_id",
        "variant_id",
        "object_id",
        "catalog_digest",
    }
    assert all(isinstance(selection[key], str) and selection[key] for key in selection)


def _assert_dynamic(value: Any, label: str) -> None:
    assert isinstance(value, str) and value, label
    assert not value.startswith(("fixed-", "static-", "synthetic-")), label


def _assert_uuidish(value: Any, label: str) -> None:
    _assert_dynamic(value, label)
    assert re.fullmatch(r"[0-9a-fA-F-]{16,}", value), label


def _assert_submitted_items(
    submitted: dict[str, Any], authoritative: dict[str, Any], label: str
) -> None:
    """Keep every submitted field/value while checking the recorded envelope."""
    assert set(submitted) <= set(authoritative), label
    for field, value in submitted.items():
        assert authoritative[field] == value, f"{label}.{field} changed"


def _assert_authoritative_command(
    submitted: dict[str, Any],
    authoritative: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    _assert_submitted_items(submitted, authoritative, "authoritative command")
    media = submitted["args"]["media"]
    row = submitted["args"]["row"]
    asset_ref = f"parent/asset:{media['asset']}"
    clip_ref = f"parent/clip:{row['id']}"
    added_fields = {"authorship", "bindings", "expect", "reads", "resolved", "selections"}
    assert set(authoritative) == set(submitted) | added_fields
    assert authoritative["created"] == submitted["created"]
    assert authoritative["authorship"] == {
        asset_ref + "::": "direct",
        clip_ref + "::": "direct",
    }
    assert authoritative["bindings"] == {"media.track": f"parent/track:{row['track']}"}
    assert authoritative["expect"] == {
        asset_ref + "::": {"state": "absent"},
        clip_ref + "::": {"state": "absent"},
    }
    assert authoritative["resolved"] == []
    assert isinstance(authoritative["reads"], list) and authoritative["reads"]
    assert isinstance(authoritative["selections"], list) and authoritative["selections"]
    assert authoritative == expected


def _assert_authoritative_step(
    submitted: dict[str, Any],
    authoritative: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    submitted_metadata = {key: value for key, value in submitted.items() if key != "patch"}
    authoritative_metadata = {
        key: value for key, value in authoritative.items() if key != "patch"
    }
    _assert_submitted_items(submitted_metadata, authoritative_metadata, "authoritative step")
    assert set(authoritative) == set(submitted) | {"dependencies"}
    assert submitted["patch"] == []
    assert authoritative["patch"] != submitted["patch"]
    assert authoritative["patch"] == expected["patch"]
    assert authoritative["dependencies"] == expected["dependencies"]
    assert authoritative == expected


def _source_facts(editing: Any, document: dict[str, Any], head: dict[str, str]) -> dict[str, Any]:
    association = next(item for item in editing.associations if item["variant_key"] == "new")
    pin = copy.deepcopy(editing.pins["new"])
    verified = copy.deepcopy(
        editing.service.get_project_verified_media(editing.project, pin, authorize=editing.authorize)
    )
    entry = {
        "file": association.get("filename") or pin["object_id"],
        "type": verified["media_type"],
        "media_id": pin["object_id"],
        "generationId": pin["generation_id"],
        "variantId": pin["variant_id"],
    }
    if "duration_seconds" in verified:
        entry["duration"] = verified["duration_seconds"]
    assert entry["type"] == "image/png"
    assert verified["selection"] == pin
    _assert_five_field_selection(pin)
    return {
        "agent_checkout": str(AGENT),
        "agent_commit": os.popen(f"git -C {AGENT} rev-parse HEAD").read().strip(),
        "runtime_checkout": str(RUNTIME),
        "runtime_commit": RUNTIME_SOURCE,
        "runtime_pin": RUNTIME_PIN,
        "app_source_commit": APP_SOURCE,
        "producer_source_commit": PRODUCER_SOURCE,
        "project_id": editing.project,
        "timeline_id": "film",
        "head": head,
        "canonical_parent_revision": document["parent_revision"]["revision_id"],
        "canonical_parent_content_digest": document["parent_revision"]["content_digest"],
        "selection": pin,
        "verified_media": verified,
        "registry_entry": entry,
        "managed_output_binding": {
            key: copy.deepcopy(association.get(key))
            for key in (
                "association_id",
                "task_id",
                "attempt_id",
                "generation_id",
                "object_id",
                "output_port",
                "group_key",
                "variant_key",
                "ordinal",
                "role",
                "size",
                "media_type",
                "filename",
                "durability",
                "lifecycle",
                "provenance",
            )
            if key in association
        },
    }


def main() -> None:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists():
        raise RuntimeError(f"refusing stale App output: {OUTPUT}")
    REALM_ROOT.mkdir(parents=True, exist_ok=True)
    monkeypatch = MonkeyPatch()
    realm = parent.media_realm(REALM_ROOT, monkeypatch, "image", historical=True, pristine=False)
    editing = next(realm)
    service = editing.service
    try:
        original_document = copy.deepcopy(current_document(editing))
        original_head = current_head(service, editing.project)
        payload = original_document["parent_revision"]["payload"]
        visual = next(row for row in payload["config"]["tracks"] if row["id"] == "visual")
        assert visual["opaque"] == [None, False]
        assert "app" not in visual
        assert len(original_document["shot_revisions"]) == 3
        assert len(original_document["internal_timeline_revisions"]) == 3
        original_children = {
            "shot_revisions": copy.deepcopy(original_document["shot_revisions"]),
            "internal_timeline_revisions": copy.deepcopy(original_document["internal_timeline_revisions"]),
        }
        original_registry = copy.deepcopy(payload["registry"])
        original_tracks = copy.deepcopy(payload["config"]["tracks"])
        original_history = service.list_timeline_commits(
            editing.project, "film", authorize=editing.authorize
        )
        source = _source_facts(editing, original_document, original_head)
        pin = source["selection"]
        entry = source["registry_entry"]
        producer_input = {
            "projectId": editing.project,
            "timelineId": "film",
            "head": {
                "revisionId": original_head["revision_id"],
                "publicationId": original_head["publication_id"],
            },
            "canonicalDocument": original_document,
            "preparedAsset": {
                "assetKey": pin["asset"],
                "mediaType": "image",
                "durationSeconds": source["verified_media"].get("duration_seconds"),
                "entry": entry,
                "source": "registered",
                "mediaSelection": pin,
            },
            "placement": {"trackId": "visual", "at": 2},
        }
        assert set(producer_input) == {
            "projectId",
            "timelineId",
            "head",
            "canonicalDocument",
            "preparedAsset",
            "placement",
        }
        input_bytes = write_json(EVIDENCE / "producer-input.json", producer_input)
        input_hash = sha256(input_bytes)
        source["input_sha256"] = input_hash
        source["canonical_checks"] = {
            "visual_track_raw_opaque": visual["opaque"],
            "visual_track_app_absent": "app" not in visual,
            "shot_count": len(original_children["shot_revisions"]),
            "internal_count": len(original_children["internal_timeline_revisions"]),
            "registry": original_registry,
            "tracks": original_tracks,
        }
        write_json(EVIDENCE / "runtime-source-facts.json", source)
        deadline = datetime.now(timezone.utc) + timedelta(minutes=25)
        (EVIDENCE / "READY").write_text(
            "\n".join(
                [
                    "A4 authentic NEW IMAGE producer input READY",
                    f"input_sha256={input_hash}",
                    f"project_id={editing.project}",
                    "timeline_id=film",
                    f"head_revision_id={original_head['revision_id']}",
                    f"head_publication_id={original_head['publication_id']}",
                    f"canonical_parent_content_digest={original_document['parent_revision']['content_digest']}",
                    f"selection_asset={pin['asset']}",
                    f"selection_object_id={pin['object_id']}",
                    f"selection_catalog_digest={pin['catalog_digest']}",
                    f"deadline_utc={deadline.isoformat()}",
                    "fixture=public-capture-commit-raw-track-opaque-original-location",
                    f"runtime_realm={REALM_ROOT}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        deadline_mono = time.monotonic() + 25 * 60
        while not OUTPUT.is_file():
            if time.monotonic() >= deadline_mono:
                raise TimeoutError(f"App output did not arrive within 25 minutes: {OUTPUT}")
            assert current_head(service, editing.project) == original_head
            assert current_document(editing) == original_document
            time.sleep(15)

        output_bytes = OUTPUT.read_bytes()
        output = json.loads(output_bytes)
        write_json(EVIDENCE / "app-output.json", output)
        assert set(output) == {"request", "idempotency_key", "request_body_sha256"}
        request = output["request"]
        key = output["idempotency_key"]
        _assert_uuidish(key, "idempotency key must be dynamic")
        body_bytes = compact_json(request)
        assert output["request_body_sha256"] == "sha256:" + sha256(body_bytes)
        assert request["project_id"] == editing.project
        assert request["timeline_id"] == "film"
        assert request["base_revision_id"] == original_head["revision_id"]
        assert request["base_publication_id"] == original_head["publication_id"]
        assert request.get("source") == {"via": "app"}
        validate_request(request)
        assert request["commands"]
        command = request["commands"][0]
        assert command["op"] == "clip.add-media"
        assert command["intent"] == {}
        assert command["args"]["media"] == pin
        row = command["args"]["row"]
        assert row["track"] == "visual"
        # The App's image placement is represented as a five-second hold in
        # this frozen producer; the media selection remains the image
        # authority and the visual track is the intended parent destination.
        assert row["clipType"] in {"hold", "image"}
        if row["clipType"] == "hold":
            assert row["hold"] > 0
        assert row["asset"] == pin["asset"]
        assert row["at"] == 2
        _assert_dynamic(row["id"], "clip identity must be dynamic")
        _assert_dynamic(request["steps"][0]["id"], "step identity must be dynamic")
        assert request["steps"][0]["command_ordinals"] == [0]
        assert isinstance(request["steps"][0]["patch"], list)
        write_json(
            EVIDENCE / "app-request-verification.json",
            {
                "input_sha256": input_hash,
                "source_app_commit": APP_SOURCE,
                "producer_source_commit": PRODUCER_SOURCE,
                "request_body_sha256_reported": output["request_body_sha256"],
                "request_body_sha256_recomputed": "sha256:" + sha256(body_bytes),
                "idempotency_key": key,
                "base_head_match": True,
                "project_timeline_match": True,
                "selection_match": True,
                "command_op": command["op"],
                "clip_id": row["id"],
                "step_id": request["steps"][0]["id"],
                "intent": command["intent"],
            },
        )

        accepted = commit(service, editing, request, key)
        write_json(EVIDENCE / "accepted-response.json", accepted)
        assert accepted["data"]["status"] in {"accepted", "merged", "rebased"}
        assert accepted["receipt"]
        prepared = editing.candidates[-1]
        provider_command = prepared["records"]["commands"][0]["command"]
        provider_step = prepared["records"]["steps"][0]
        _assert_authoritative_command(command, accepted["data"]["commands"][0], provider_command)
        _assert_authoritative_step(request["steps"][0], accepted["data"]["steps"][0], provider_step)
        accepted_document = current_document(editing)
        accepted_head = current_head(service, editing.project)
        accepted_payload = accepted_document["parent_revision"]["payload"]
        added = next(clip for clip in accepted_payload["clips"] if clip["id"] == row["id"])
        assert added == row
        assert accepted_payload["registry"]["assets"][pin["asset"]] == {
            "media_id": pin["object_id"],
            "type": "image",
            "generationId": pin["generation_id"],
            "variantId": pin["variant_id"],
        }
        for asset, value in original_registry.get("assets", {}).items():
            assert accepted_payload["registry"]["assets"][asset] == value
        assert next(track for track in accepted_payload["config"]["tracks"] if track["id"] == "visual") == visual
        assert accepted_document["shot_revisions"] == original_children["shot_revisions"]
        assert accepted_document["internal_timeline_revisions"] == original_children["internal_timeline_revisions"]
        assert service.get_project_timeline(editing.project, "film")["head_revision_id"] == accepted_head["revision_id"]
        exact_retry = commit(service, editing, request, key)
        assert exact_retry == accepted
        write_json(EVIDENCE / "accepted-exact-retry.json", exact_retry)

        foreign_request = copy.deepcopy(request)
        foreign_request["commands"][0]["args"]["media"] = parent.foreign_pin(editing)
        changed_body = copy.deepcopy(request)
        changed_body["label"] = "changed body guard"
        refusals = [
            refusal(service, editing, foreign_request, "image-foreign-selection", "foreign-selection"),
            refusal(service, editing, changed_body, key, "changed-body"),
        ]

        current = current_head(service, editing.project)
        undo = inverse_request(editing, current, accepted["receipt"]["receipt_id"])
        validate_request(undo)
        preview = commit(service, editing, {**undo, "dry_run": True}, "image-undo-preview")
        assert preview["receipt"] is None
        assert preview["data"]["would_status"] in {"accepted", "merged"}
        write_json(EVIDENCE / "undo-preview.json", preview)
        undone = commit(service, editing, undo, "image-undo")
        write_json(EVIDENCE / "undo-response.json", undone)
        assert current_document(editing) == original_document
        assert current_head(service, editing.project) != original_head
        undo_retry = commit(service, editing, undo, "image-undo")
        assert undo_retry == undone
        write_json(EVIDENCE / "undo-exact-retry.json", undo_retry)
        history = service.list_timeline_commits(editing.project, "film", authorize=editing.authorize)
        history_text = json.dumps(history, sort_keys=True)
        assert accepted["receipt"]["receipt_id"] in history_text
        assert undone["receipt"]["receipt_id"] in history_text
        assert len(history) >= len(original_history)
        service.close()
        service = RuntimeService(editing.root)
        service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
        editing.service = service
        assert current_document(editing) == original_document
        write_json(EVIDENCE / "public-readback-reopen.json", current_document(editing))
        write_json(
            EVIDENCE / "results.json",
            {
                "status": "PASS",
                "authority": "normal RuntimeService.commit_timeline + current Agent provider",
                "source": {
                    "agent_commit": source["agent_commit"],
                    "runtime_commit": RUNTIME_SOURCE,
                    "app_commit": APP_SOURCE,
                },
                "input_sha256": input_hash,
                "output_sha256": sha256(output_bytes),
                "request_body_sha256": output["request_body_sha256"],
                "head_before": original_head,
                "head_after_accept": accepted_head,
                "accepted_receipt_id": accepted["receipt"]["receipt_id"],
                "undo_receipt_id": undone["receipt"]["receipt_id"],
                "added_clip_id": row["id"],
                "canonical_restored": current_document(editing) == original_document,
                "raw_track_opaque_preserved": True,
                "children_preserved": True,
                "refusals": refusals,
            },
        )
    finally:
        try:
            editing.service.close()
        finally:
            realm.close()
            monkeypatch.undo()


if __name__ == "__main__":
    main()
