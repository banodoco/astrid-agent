from __future__ import annotations

from copy import deepcopy

import pytest

from astrid.core.timeline.shot_composition_migration import (
    MIGRATION_RECEIPT_TYPE,
    MigrationAmbiguityError,
    MigrationInterruptedError,
    inventory_legacy_shot_compositions,
    migrate_shot_compositions,
    plan_shot_composition_migration,
    rehearse_shot_composition_migration,
)

MEDIA = "sha256:" + "a" * 64


def _source(*, pinned: bool = False, interleaved: bool = False) -> dict:
    clips = [
        {"id": "clip-a", "clipType": "media", "asset": "media-a", "at": 0, "hold": 1, "track": "video"},
        {"id": "clip-b", "clipType": "media", "asset": "media-a", "at": 1, "hold": 1, "track": "video"},
    ]
    if pinned:
        groups = [{"id": "group-1", "shotId": "shot-1", "name": "Opening", "clipIds": ["clip-a", "clip-b"]}]
        if interleaved:
            clips = [clips[0], {"id": "between", "clipType": "media", "at": 0.5, "hold": 0.25, "track": "video"}, clips[1]]
    else:
        groups = []
        clips = [{"id": "occ-1", "clipType": "shot", "at": 2, "hold": 2, "track": "video", "params": {"shot_id": "shot-1", "timeline_document_id": "child-1"}}]
    registry = {"assets": {"media-a": {"media_id": MEDIA, "digest": MEDIA, "type": "video"}}}
    child = {"timeline_id": "child-1", "config": {"tracks": [{"id": "video", "kind": "visual"}], "clips": deepcopy(clips if pinned else [{"id": "child-clip", "clipType": "media", "asset": "media-a", "at": 0, "hold": 2, "track": "video"}])}, "registry": registry}
    return {
        "projects": [{
            "project_id": "project-1",
            "shots": {"shot-1": {"name": "Opening", "pools": [{"id": "pool-1"}], "alternatives": [{"id": "alt-1"}], "selected_variants": {"pool-1": "alt-1"}, "audio": {"track_id": "vo", "object_id": "voice", "digest": MEDIA}, "provenance": {"lineage": "source-cut"}}},
            "timelines": [{
                "timeline_id": "main",
                "document_id": "parent-doc",
                "config": {"tracks": [{"id": "video", "kind": "visual"}], "clips": clips, "pinnedShotGroups": groups},
                "registry": registry,
                "child_timelines": {"child-1": child},
            }],
        }]
    }


def test_inventory_and_dry_run_are_read_only_and_rehearsal_is_deterministic():
    source = _source()
    before = deepcopy(source)
    inventory = inventory_legacy_shot_compositions(source).as_dict()
    rehearsal_a = rehearse_shot_composition_migration(source)
    rehearsal_b = rehearse_shot_composition_migration(source)

    assert source == before
    assert inventory["items"][0]["affected"] is True
    assert rehearsal_a == rehearsal_b
    assert rehearsal_a["mode"] == "dry_run"
    assert rehearsal_a["plans"][0]["publication"]["parent_composition"]["migration_activation"]["state"] == "active"


def test_child_migration_preserves_identity_metadata_and_uses_canonical_graph():
    source = _source()
    source["projects"][0]["timelines"][0]["child_timelines"]["child-1"]["registry"]["assets"]["media-a"].update(
        width=1672, height=941, resolution="1672x941"
    )
    plan = plan_shot_composition_migration(source, project_id="project-1", timeline_id="main")
    assert plan.identity_mapping["occurrences"]["occ-1"]["occurrence_id"] == "occ-1"
    assert plan.graph["occurrences"][0]["occurrence_id"] == "occ-1"
    shot = plan.publication["shot_revisions"][0]
    assert shot["payload"]["metadata"]["name"] == "Opening"
    assert shot["payload"]["pools"] == [{"id": "pool-1"}]
    assert shot["payload"]["selected_variants"] == {"pool-1": "alt-1"}
    assert shot["payload"]["provenance"]["lineage"] == "source-cut"
    assert plan.publication["dependency_manifest"]["shots"][0]["internal_timeline_revision_id"] == shot["internal_timeline_revision_id"]
    internal_assets = shot["payload"]["internal_timeline"]["registry"]["assets"]
    assert internal_assets["media-a"]["resolution"] == "1672x941"
    assert "width" not in internal_assets["media-a"] and "height" not in internal_assets["media-a"]
    assert "pinnedShotGroups" not in plan.publication["parent_composition"]["config"]
    assert all(clip.get("clipType") != "shot" for clip in plan.publication["parent_composition"]["clips"])


def test_astrid_group_conversion_preserves_group_name_and_deduplicates_media():
    plan = plan_shot_composition_migration(_source(pinned=True), project_id="project-1", timeline_id="main")
    occurrence = plan.publication["parent_composition"]["occurrences"][0]
    assert occurrence["occurrence_id"] == "group-1"
    assert occurrence["provenance"]["legacy_source"]["name"] == "Opening"
    assert plan.media_digests == (MEDIA,)
    assert plan.publication["dependency_manifest"]["media"] == [{"media_id": MEDIA, "content_digest": MEDIA}]


def test_interleaved_group_is_refused_before_any_runtime_write():
    with pytest.raises(MigrationAmbiguityError, match="interleaved") as error:
        plan_shot_composition_migration(_source(pinned=True, interleaved=True), project_id="project-1", timeline_id="main")
    assert error.value.details["recovery"].startswith("Make the group's clipIds")


class _Writer:
    def __init__(self, *, interrupt_once: bool = False):
        self.calls = []
        self.receipts = {}
        self.interrupt_once = interrupt_once

    def get_migration_receipt(self, migration_id):
        return self.receipts.get(migration_id)

    def publish_parent_composition(self, project_id, timeline_id, publication, *, idempotency_key):
        self.calls.append((project_id, timeline_id, publication, idempotency_key))
        result = {"revision_id": publication["parent_revision_id"], "new_head": publication["parent_revision_id"], "replayed": len(self.calls) > 1}
        if self.interrupt_once:
            self.interrupt_once = False
            self.receipts[publication["migration"]["migration_id"]] = {"source_fingerprint": publication["migration"]["source_fingerprint"], "receipt_type": MIGRATION_RECEIPT_TYPE, "status": "activated", "publication": result}
            raise RuntimeError("simulated process interruption")
        return result

    def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
        return {"revision_id": revision, "payload": {}}

    def record_shot_composition_migration(self, migration_id, receipt):
        self.receipts[migration_id] = receipt
        return receipt


def test_activation_receipt_contains_mapping_marker_reload_and_no_media_copy():
    writer = _Writer()
    receipt = migrate_shot_compositions(_source(), writer, project_id="project-1", timeline_id="main", migration_id="fixed-migration")
    assert receipt["receipt_type"] == MIGRATION_RECEIPT_TYPE
    assert receipt["status"] == "activated"
    assert receipt["identity_mapping"]["occurrences"]["occ-1"]["occurrence_id"] == "occ-1"
    assert receipt["activation_marker"]["migration_id"] == "fixed-migration"
    assert receipt["reload"]["verified"] is True
    assert receipt["managed_media"]["created"] == 0
    assert writer.calls[0][3] == "astrid-shot-composition-migration-fixed-migration"


def test_interrupted_publication_recovers_by_runtime_idempotency_receipt():
    writer = _Writer(interrupt_once=True)
    with pytest.raises(MigrationInterruptedError):
        migrate_shot_compositions(_source(), writer, project_id="project-1", timeline_id="main", migration_id="recoverable")
    receipt = migrate_shot_compositions(_source(), writer, project_id="project-1", timeline_id="main", migration_id="recoverable")
    assert receipt["replayed"] is True
    assert len(writer.calls) == 1


def test_successful_replay_uses_the_same_durable_receipt_without_republication():
    writer = _Writer()
    first = migrate_shot_compositions(_source(), writer, project_id="project-1", timeline_id="main", migration_id="replayable")
    second = migrate_shot_compositions(_source(), writer, project_id="project-1", timeline_id="main", migration_id="replayable")
    assert first["identity_mapping"] == second["identity_mapping"]
    assert second["replayed"] is True
    assert len(writer.calls) == 1
