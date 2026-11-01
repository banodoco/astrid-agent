import hashlib
import json

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commit import bundle_from_document, create_runtime_commit_service
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from astrid.sdk.timeline.native_text_binding import capture_text_binding_set
from astrid.sdk.timeline_script import read_composition_script


def binding(text, head=1):
    raw = text.encode()
    return {"binding_id": "binding", "kind": "voiceover_script", "head": head,
            "media_id": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "byte_size": len(raw), "event_stream_id": "binding:shot.text_binding"}


def test_pinned_script_order_missing_empty_and_no_latest_read():
    old, empty = binding("Pinned wording"), binding("")
    def placed(oid, start, bindings):
        return {"role": "target", "occurrence": {"occurrence_id": oid, "shot_id": "s",
            "shot_revision_id": "shot-old", "start": [start, 1], "duration": [2, 1],
            "name": oid, "text_bindings": bindings}}
    class Timeline:
        calls = []
        def inspect(self, *args, **kwargs):
            self.calls.append(kwargs)
            return DomainResult.success({"revision_id": "parent-old", "snapshot_digest": "snapshot",
                "selected": [placed("later", 3, []), placed("empty", 2, [empty]), placed("first", 0, [old])],
                "next_cursor": None})
    class Transport:
        def get_object(self, ref):
            return {"data": b"Pinned wording" if ref == old["media_id"] else b""}
        def list_project_shot_text_bindings(self, *a, **kw):
            raise AssertionError("must never read latest or unplaced shots")
    timeline = Timeline()
    result = read_composition_script(timeline, Transport(), "p", "tl", revision_id="parent-old")
    assert result.ok
    assert [r["occurrence_id"] for r in result.data["occurrences"]] == ["first", "empty", "later"]
    assert [r["status"] for r in result.data["occurrences"]] == ["present", "empty", "missing"]
    assert result.data["occurrences"][0]["text"] == "Pinned wording"
    assert timeline.calls[0]["revision_id"] == "parent-old"


def test_script_pagination_pins_first_head_and_verifies_exact_bytes():
    pin = binding("old")
    class Timeline:
        def inspect(self, *args, **kw):
            if kw["cursor"]:
                assert kw["revision_id"] == "parent"
                rows, cursor = [], None
            else:
                rows, cursor = [{"role": "target", "occurrence": {"occurrence_id": "o", "text_bindings": [pin]}}], "page2"
            return DomainResult.success({"revision_id": "parent", "snapshot_digest": "d", "selected": rows, "next_cursor": cursor})
    class Transport:
        def get_object(self, ref):
            return {"data": b"changed"}
    result = read_composition_script(Timeline(), Transport(), "p", "tl")
    assert not result.ok and result.error.code == "integrity_error"


def test_script_paginates_from_first_revision_and_snapshot_without_reading_latest():
    first, second = binding("first"), binding("second", head=2)
    calls = []
    class Timeline:
        def inspect(self, project, ref, **kwargs):
            calls.append(kwargs.copy())
            if kwargs.get("cursor"):
                return DomainResult.success({
                    "revision_id": "parent-7", "snapshot_digest": "snapshot-7",
                    "selected": [{"role": "target", "occurrence": {"ordinal": 1, "occurrence_id": "second",
                        "shot_id": "s2", "shot_revision_id": "s2-r1", "start": [3, 1], "duration": [1, 1],
                        "name": "Second", "text_bindings": [second]}}], "next_cursor": None,
                })
            return DomainResult.success({
                "revision_id": "parent-7", "snapshot_digest": "snapshot-7",
                "selected": [{"role": "target", "occurrence": {"ordinal": 0, "occurrence_id": "first",
                    "shot_id": "s1", "shot_revision_id": "s1-r4", "start": [1, 1], "duration": [1, 1],
                    "name": "First", "text_bindings": [first]}}], "next_cursor": "page-2",
            })
    class Transport:
        def __init__(self): self.reads = []
        def get_object(self, ref):
            self.reads.append(ref)
            return {"data": b"first" if ref == first["media_id"] else b"second"}
        def list_project_shot_text_bindings(self, *args, **kwargs):
            raise AssertionError("the reader must use the selected revision's pins")
    transport = Transport()
    result = read_composition_script(Timeline(), transport, "p", "tl")
    assert result.ok
    assert result.data["revision_id"] == "parent-7"
    assert result.data["snapshot_digest"] == "snapshot-7"
    assert [row["text"] for row in result.data["occurrences"]] == ["first", "second"]
    assert calls[0]["revision_id"] is None
    assert calls[1]["revision_id"] == "parent-7"
    assert calls[1]["cursor"] == "page-2"
    assert transport.reads == [first["media_id"], second["media_id"]]


def test_script_rejects_changed_snapshot_and_malformed_descriptors():
    class ChangedSnapshot:
        def inspect(self, *args, **kwargs):
            return DomainResult.success({"revision_id": "parent", "snapshot_digest": "", "selected": [], "next_cursor": None})
    class Transport:
        def get_object(self, ref): raise AssertionError("malformed descriptors must fail before object reads")
    empty_snapshot = read_composition_script(ChangedSnapshot(), Transport(), "p", "tl")
    assert not empty_snapshot.ok and empty_snapshot.error.code == "protocol_error"

    class Malformed:
        def inspect(self, *args, **kwargs):
            return DomainResult.success({"revision_id": "parent", "snapshot_digest": "s", "selected": [
                {"role": "target", "occurrence": {"occurrence_id": "o", "text_bindings": [None]}}
            ], "next_cursor": None})
    malformed = read_composition_script(Malformed(), Transport(), "p", "tl")
    assert not malformed.ok and malformed.error.code == "protocol_error"

    class ChangingSnapshot:
        def inspect(self, *args, **kwargs):
            digest = "snapshot-a" if not kwargs.get("cursor") else "snapshot-b"
            return DomainResult.success({"revision_id": "parent", "snapshot_digest": digest,
                                         "selected": [], "next_cursor": "next" if not kwargs.get("cursor") else None})
    changed = read_composition_script(ChangingSnapshot(), Transport(), "p", "tl")
    assert not changed.ok and changed.error.code == "integrity_error"

    invalid_kind = read_composition_script(Malformed(), Transport(), "p", "tl", kind=None)
    assert not invalid_kind.ok and invalid_kind.error.code == "validation_error"

    class MissingBindingKind:
        def inspect(self, *args, **kwargs):
            return DomainResult.success({"revision_id": "parent", "snapshot_digest": "s", "selected": [
                {"role": "target", "occurrence": {"occurrence_id": "o", "text_bindings": [{"binding_id": "b"}]}}
            ], "next_cursor": None})
    invalid_descriptor = read_composition_script(MissingBindingKind(), Transport(), "p", "tl")
    assert not invalid_descriptor.ok and invalid_descriptor.error.code == "protocol_error"


def test_timeline_script_cli_forwards_revision_occurrence_and_kind(monkeypatch):
    from types import SimpleNamespace

    import astrid.packs.timeline.cli as cli

    calls = []
    client = SimpleNamespace(timelines=SimpleNamespace(script=lambda *args, **kwargs: calls.append((args, kwargs)) or "result"))
    monkeypatch.setattr(cli, "print_result", lambda result, as_json: 0)
    parser = cli.build_parser(client)
    parsed = parser.parse_args(["script", "timeline", "--project", "project", "--revision", "parent-9",
                                "--occurrence", "occ-2", "--kind", "voiceover_script"])
    assert parsed.handler(parsed) == 0
    assert calls == [(("project", "timeline"), {"revision_id": "parent-9", "occurrence": "occ-2", "kind": "voiceover_script"})]


@pytest.mark.integration  # a real Runtime from a second checkout
def test_runtime_pin_flow_returns_old_script_after_binding_rebind(tmp_path):
    from runtime_protocol import service as service_module
    from runtime_protocol import store as store_module
    from runtime_protocol.timeline_commit_port import route_path

    realm = tmp_path / "realm"
    store_module.RealmStore.initialize(realm).close()
    service = service_module.RuntimeService(realm)
    try:
        project = service.create_project({"slug": "narration", "name": "Narration"}, idempotency_key="project")
        project_id = project["id"]
        service.register_timeline_commit_provider(create_runtime_commit_service())
        service.create_timeline(project_id, "main", idempotency_key="timeline")

        def publication(parent_id, expected_head, shot_revision, internal_revision, *, text_bindings=None):
            return {
                "project_id": project_id, "timeline_id": "main", "expected_head": expected_head,
                "parent_revision_id": parent_id,
                "internal_timeline_revisions": [{"timeline_id": "main", "revision_id": internal_revision,
                    "payload": {"tracks": [], "clips": [], "effects": [], "audio": [], "layout": {}, "registry": {}, "assets": []}}],
                "shot_revisions": [{"shot_id": "shot-1", "revision_id": shot_revision,
                    "internal_timeline_revision_id": internal_revision,
                    "payload": {"metadata": {"title": "Opening"}, "items": [], "pools": [],
                        "selected_variants": {}, "provenance": {}, "generation_inputs": {},
                        "audio_bindings": [], "text_bindings": text_bindings or []}}],
                "parent_composition": {"config": {}, "registry": {}, "clips": [], "occurrences": [{
                    "occurrence_id": "opening", "shot_id": "shot-1", "shot_revision_id": shot_revision,
                    "placement": {"start_ms": 0}, "duration_ms": 1000, "source_offset": 0,
                    "speed": 1, "track": "voice", "transform": {}, "gain": 1,
                    "mute": False, "provenance": {},
                }]},
            }

        def body(calls):
            head = service.get_project_timeline(project_id, "main")
            label = "Timeline script setup"
            changes = [change for command in calls if command["op"] == "doc.patch"
                       for change in command["args"]["changes"]]
            step = {"id": "timeline-script", "label": label, "intent": label,
                    "tags": ["timeline-script"], "command_ordinals": list(range(len(calls))),
                    "patch": changes, "reads": []}
            return {"api_version": 1, "project_id": project_id, "timeline_id": "main",
                    "base_revision_id": head["head_revision_id"],
                    "base_publication_id": head["head_publication_id"], "commands": calls,
                    "label": label, "steps": [step], "merge": "fields", "reads": [],
                    "dry_run": False, "source": {"via": "python"},
                    "rationale": "Set up the timeline script binding.",
                    "group_id": "D86-timeline-script"}

        def commit(calls, key):
            return service.commit_timeline(
                project_id, "main", body(calls), idempotency_key=key, publisher="timeline-script-test",
                invocation_path=route_path(project_id, "main"), authorize=lambda: None)

        template = publication("parent-1", None, "shot-1-r1", "internal-r1")
        head = service.get_project_timeline(project_id, "main")
        bootstrap = service.get_project_parent_composition_revision(
            project_id, "main", head["head_revision_id"])
        empty = {"schema_version": 1, "project_id": project_id, "timeline_id": "main",
                 "parent_revision": {key: bootstrap[key] for key in ("revision_id", "content_digest", "payload")},
                 "shot_revisions": [], "internal_timeline_revisions": [],
                 "dependency_manifest": bootstrap["dependency_manifest"]}
        bundle = bundle_from_document(empty)
        parent_track = capture_track_create(bundle, "parent", {"id": "voice", "kind": "visual"})
        tracked = commit([parent_track], "parent-track")
        bundle = bundle_from_document(tracked["data"]["canonical_document"])
        occurrence = template["parent_composition"]["occurrences"][0]
        shot = template["shot_revisions"][0]
        birth = capture_occurrence_create(
            bundle, occurrence["occurrence_id"], shot["shot_id"], name=shot["payload"]["metadata"]["title"],
            track="voice", on=occurrence["placement"]["start_ms"] / 1000,
            duration=occurrence["duration_ms"] / 1000)
        born = commit([birth], "publish-no-script")

        raw = b"Pinned before rebind."
        stored = service.ingest(project_id, raw, media_type="text/plain; charset=utf-8",
                                idempotency_key="register-script")
        pin = stored["data"]
        text_pin = {key: pin[key] for key in ("object_id", "digest", "size", "media_type")}
        verified = {**text_pin, "utf8": True}
        rebound_template = publication("parent-2", "parent-1", "shot-1-r2", "internal-r2",
                                       text_bindings=[pin])
        rebound_occurrence = rebound_template["parent_composition"]["occurrences"][0]
        bundle = bundle_from_document(born["data"]["canonical_document"])
        captured = capture_text_binding_set(
            bundle, "occurrence:" + rebound_occurrence["occurrence_id"],
            {"kind": "voiceover_script", "slot": None}, text=text_pin, verified_text=[verified])
        binding_result = commit([captured], "publish-pin")
        assert binding_result["data"]["accepted_revision_id"]
        pin = next(row for row in service.list_project_shot_text_bindings(
            project_id, kind="voiceover_script")["items"] if row["shot_id"] == shot["shot_id"])

        canonical = binding_result["data"]["canonical_document"]
        shot_revision_row = next(row for row in canonical["shot_revisions"]
                                 if row["shot_id"] == shot["shot_id"])
        shot_revision = shot_revision_row["revision_id"]
        pin_patch = {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
                     "args": {"changes": [{"op": "set", "ref": "shot-revision:" + shot_revision,
                         "path": ["text_bindings"],
                         "before": {"state": "present", "value": shot_revision_row["payload"]["text_bindings"]},
                         "value": [pin]}]}}
        binding_result = commit([pin_patch], "pin-script-descriptor")
        assert binding_result["data"]["accepted_revision_id"]

        class Timelines:
            def inspect(self, project_ref, timeline_ref, **options):
                return DomainResult.success(service.inspect_timeline(project_ref, timeline_ref, options))

        class Transport:
            def get_object(self, object_id):
                return {"data": service.cas.path_for(object_id.removeprefix("sha256:")).read_bytes()}

        rebound_raw = b"New mutable head."
        rebound_stored = service.ingest(project_id, rebound_raw, media_type="text/plain; charset=utf-8",
                                        idempotency_key="rebind-script-text")
        rebound = rebound_stored["data"]
        rebound_text = {key: rebound[key] for key in ("object_id", "digest", "size", "media_type")}
        rebound_verified = {**rebound_text, "utf8": True}
        rebound_bundle = bundle_from_document(binding_result["data"]["canonical_document"])
        rebound_command = capture_text_binding_set(
            rebound_bundle, "occurrence:" + rebound_occurrence["occurrence_id"],
            {"kind": "voiceover_script", "slot": None},
            text=rebound_text, verified_text=[rebound_verified])
        rebound_result = commit([rebound_command], "rebind-script")
        assert rebound_result["data"]["accepted_revision_id"]
        live = next(row for row in service.list_project_shot_text_bindings(
            project_id, kind="voiceover_script")["items"] if row["shot_id"] == shot["shot_id"])
        assert live["media_id"] == rebound["object_id"]
        result = read_composition_script(
            Timelines(), Transport(), project_id, "main",
            revision_id=binding_result["data"]["accepted_revision_id"])
        assert result.ok
        canonical = binding_result["data"]["canonical_document"]
        expected_revision = "authoring-parent-revision-" + hashlib.sha256(json.dumps(
            {key: canonical[key] for key in ("project_id", "timeline_id")}
            | {"parent": canonical["parent_revision"]["payload"],
               "manifest": canonical["dependency_manifest"]},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert result.data["revision_id"] == expected_revision
        assert result.data["occurrences"][0]["text"] == "Pinned before rebind."
        assert result.data["occurrences"][0]["bindings"][0]["head"] == pin["head"]
    finally:
        service.close()
