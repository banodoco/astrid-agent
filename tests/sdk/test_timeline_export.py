from __future__ import annotations

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate, open_authoring_bundle
from astrid.sdk import timeline_export
from astrid.sdk.timeline_export import _strip_machine_paths, _write_object_stream, export_plan, export_timeline, inventory_elements


def test_inventory_covers_effect_pins_animations_and_transitions():
    timeline = {
        "parent": {"clips": [{"clipType": "am-callout", "elementRef": {
            "packId": "astrid_motion", "kind": "effect", "id": "am-callout",
            "revision": "sha256:" + "a" * 64,
        }, "entrance": {"id": "fade-in", "revision": "sha256:" + "b" * 64},
        "exit": "fade-out", "continuous": {"id": "pulse"}, "animations": [{"id": "slide"}],
        "transition": {"packId": "astrid_motion", "kind": "transition", "id": "wipe"}}]},
        "shots": {"s": {"internal_timeline": {"tracks": [], "clips": [{"clipType": "am-title"}]}}},
    }
    catalog = [
        {"packId": "astrid_motion", "kind": kind, "id": name, "revision": "sha256:" + "c" * 64}
        for kind, name in (("effect", "am-callout"), ("effect", "am-title"),
                           ("animation", "fade-in"), ("animation", "fade-out"),
                           ("animation", "pulse"), ("animation", "slide"),
                           ("transition", "wipe"))
    ]
    rows = inventory_elements(timeline, elements=catalog)
    assert {(row["kind"], row["id"]) for row in rows} == {
        ("effect", "am-callout"), ("effect", "am-title"), ("animation", "fade-in"),
        ("animation", "fade-out"), ("animation", "pulse"), ("animation", "slide"),
        ("transition", "wipe"),
    }
    assert next(row for row in rows if row["id"] == "am-callout")["pinned"] is True


def test_inventory_rejects_conflicting_pins_without_losing_pack_identity():
    timeline = {"parent": {"clips": [
        {"clipType": "am-callout", "elementRef": {
            "packId": "astrid_motion", "kind": "effect", "id": "am-callout", "revision": "rev-a",
        }},
        {"clipType": "am-callout", "elementRef": {
            "packId": "astrid_motion", "kind": "effect", "id": "am-callout", "revision": "rev-b",
        }},
    ]}}
    with pytest.raises(ValueError, match="am-callout.*astrid_motion.*rev-a.*rev-b"):
        inventory_elements(timeline, elements=[])


@pytest.mark.parametrize("element_ref,missing", [
    ({"kind": "effect", "revision": "rev-a"}, "id"),
    ({"id": "am-shimmer", "revision": "rev-a"}, "kind"),
    ({"id": "am-shimmer", "kind": "effect"}, "revision"),
])
def test_inventory_rejects_incomplete_explicit_element_refs_by_name(element_ref, missing):
    timeline = {"parent": {"clips": [{"clipType": "am-shimmer", "elementRef": element_ref}]}}
    with pytest.raises(ValueError, match=f"am-shimmer.*{missing}"):
        inventory_elements(timeline, elements=[])


def test_machine_paths_and_loopback_urls_are_removed_or_normalized():
    value = {"parent": {"config": {"source": "/Users/alice/movie.mp4", "bundle": {
        "slots": {"music": {"media": "/Users/alice/music.wav"}}}},
        "registry": {"assets": {"a": {"media_id": "http://127.0.0.1:17340/v1/objects/sha256%3A" + "a" * 64}}}}}
    cleaned = _strip_machine_paths(value)
    assert "source" not in cleaned["parent"]["config"]
    assert "media" not in cleaned["parent"]["config"]["bundle"]["slots"]["music"]
    assert cleaned["parent"]["registry"]["assets"]["a"]["media_id"] == "sha256:" + "a" * 64


def test_transcript_document_listing_reads_every_page():
    calls = []

    class Transport:
        def list_documents(self, project_id, *, cursor=None, limit=50, kind=None):
            calls.append((project_id, cursor, limit, kind))
            page = int(cursor or 0)
            start = page * limit
            stop = min(start + limit, 205)
            rows = [{"document_id": f"doc-{index}", "kind": "media.transcript"}
                    for index in range(start, stop)]
            return rows, str(page + 1) if stop < 205 else None

    rows = timeline_export._list_transcript_documents(
        SimpleNamespace(_transport=Transport()), "project"
    )
    assert len(rows) == 205 and rows[-1]["document_id"] == "doc-204"
    assert [call[1] for call in calls] == [None, "1", "2"]
    assert all(call[0] == "project" and call[2:] == (100, "media.transcript") for call in calls)


def test_compiler_media_closure_is_the_export_closure():
    from tests.timeline.test_authoring_bundle import _closure

    parent, shots, internals = _closure(shared=True)
    bundle = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    compiler_media = {row["media_id"] for row in compile_authoring_candidate(bundle).publication["dependency_manifest"]["media"]}
    assert compiler_media == {"sha256:" + "1" * 64, "sha256:" + "3" * 64}


@pytest.mark.integration
def test_export_plan_closure_equals_runtime_publish_manifest(tmp_path):
    from runtime_protocol import service as runtime_service
    from runtime_protocol import store as runtime_store
    from tests.timeline.test_authoring_bundle import AUDIO, OLD, _closure, _digest, _replace_values

    store_root = tmp_path / "realm"
    runtime_store.RealmStore.initialize(store_root).close()
    service = runtime_service.RuntimeService(store_root)
    try:
        project_id = service.create_project(
            {"slug": "export-fixture", "name": "Export fixture"}, idempotency_key="project"
        )["id"]
        service.create_timeline(project_id, "main", idempotency_key="timeline")
        picture = service.ingest(project_id, b"picture-bytes", media_type="image/png", idempotency_key="picture")["data"]["object_id"]
        audio = service.ingest(project_id, b"audio-bytes", media_type="audio/wav", idempotency_key="audio")["data"]["object_id"]
        parent, shots, internals = _closure(shared=True)
        service.create_project_shot(
            project_id, {"shot_id": shots[0]["shot_id"], "name": "Opening"},
            idempotency_key="shot",
        )
        binding_text = "Narration — café"
        binding = service.set_project_shot_text_binding(project_id, {
            "shot_id": shots[0]["shot_id"], "kind": "voiceover_script",
            "text": binding_text, "expected_head": 0,
        }, idempotency_key="binding")
        binding_pin = binding["data"]
        shots[0]["payload"]["text_bindings"] = [{**binding_pin, "text": binding_text}]
        replacements = {OLD: picture, AUDIO: audio, "project-1": project_id}
        parent = _replace_values(parent, replacements)
        shots = _replace_values(shots, replacements)
        internals = _replace_values(internals, replacements)
        parent["content_digest"] = _digest(parent["payload"])
        shots[0]["content_digest"] = _digest(shots[0]["payload"])
        internals[0]["content_digest"] = _digest(internals[0]["payload"])
        published = service.publish_parent_composition(project_id, "main", {
            "project_id": project_id, "timeline_id": "main", "expected_head": None,
            "parent_revision_id": "parent-1", "parent_composition": parent["payload"],
            "shot_revisions": shots, "internal_timeline_revisions": internals,
        }, idempotency_key="publish")
        published_data = published.get("data", published)
        published_manifest = published_data["dependency_manifest"]
        saved_parent = service.get_project_parent_composition_revision(project_id, "main", "parent-1")
        saved_shot = service.get_project_shot_revision(project_id, "shot-shared", "shot-rev-1")
        saved_internal = service.get_project_timeline_revision(project_id, "main", "internal-1")
        bundle = open_authoring_bundle(saved_parent, shot_revisions=[saved_shot], internal_timeline_revisions=[saved_internal])
        class Transport:
            def __init__(self):
                self.location_reads = []
            def get_project_object_location(self, _project, object_id):
                self.location_reads.append((_project, object_id))
                return service.object_location(_project, object_id)
            def list_timelines(self, _project, *, cursor=None, limit=50):
                return [{"timeline_id": "main", "name": "Fixture"}], None
            def list_documents(self, _project, *, cursor=None, limit=50, kind=None):
                result = service.list_documents(_project, cursor=cursor, limit=limit, kind=kind)
                return result["items"], result["next_cursor"]

        opened_calls = []

        transport = Transport()
        client = SimpleNamespace(
            projects=SimpleNamespace(show=lambda _project: SimpleNamespace(ok=True, data={"project_id": project_id})),
            _remote=SimpleNamespace(_transport=transport),
            timelines=SimpleNamespace(open_bundle=lambda project, ref: (
                opened_calls.append((project, ref)) or SimpleNamespace(ok=True, data={
                    "bundle": bundle, "project_id": project_id, "timeline_id": "main",
                })
            )),
        )
        plan = export_plan(client, project_id, "main")
        assert {"sha256:" + row["sha256"] for row in plan["media"]} == {
            row["media_id"] for row in published_manifest["media"]
        }
        assert binding_pin["media_id"] in {row["media_id"] for row in published_manifest["media"]}
        assert plan["media"] == plan["manifest"]["media"]
        assert any(row["sha256"] == binding_pin["media_id"].removeprefix("sha256:")
                   and row["size"] == len(binding_text.encode("utf-8")) for row in plan["media"])
        exported_shot = next(shot for shot in plan["timeline"]["shots"].values()
                             if shot["payload"].get("text_bindings"))
        exported_binding = exported_shot["payload"]["text_bindings"][0]
        assert exported_binding["binding_id"] == binding_pin["binding_id"]
        assert exported_binding["media_id"] == binding_pin["media_id"]
        assert exported_binding["text"] == binding_text
        assert set(transport.location_reads) == {
            (project_id, row["media_id"]) for row in published_manifest["media"]
        }
        assert all("local_path" not in json.dumps(value) for value in (plan, plan["manifest"]))
        assert opened_calls == [(project_id, "main")]
    finally:
        service.close()


def test_export_timeline_writes_stored_entries_and_transcripts_atomically(tmp_path, monkeypatch):
    content = b"media bytes"
    digest = hashlib.sha256(content).hexdigest()
    binding_bytes = "Narration — café".encode("utf-8")
    binding_digest = hashlib.sha256(binding_bytes).hexdigest()
    transcript_entry = "transcripts/" + "a" * 64 + ".whisper.json"
    plan = {
        "manifest": {"format": "astrid.timeline/1", "name": "Film", "media": [
            {"sha256": digest, "size": len(content)},
            {"sha256": binding_digest, "size": len(binding_bytes)},
        ], "transcripts": [transcript_entry]},
        "timeline": {"parent": {"clips": []}, "shots": {"s1": {"payload": {"text_bindings": [
            {"binding_id": "binding-source", "kind": "voiceover_script", "media_id": "sha256:" + binding_digest,
            "content_hash": "sha256:" + binding_digest, "byte_size": len(binding_bytes), "text": binding_bytes.decode("utf-8")}
        ]}}}},
        "transcripts": {transcript_entry: {"document_id": "transcript:source", "kind": "media.transcript", "content": {"source_id": "whisper"}}},
        "media": [{"sha256": digest, "size": len(content), "original_name": "clip.mov"},
                  {"sha256": binding_digest, "size": len(binding_bytes), "original_name": "binding.txt"}],
    }
    monkeypatch.setattr(timeline_export, "export_plan", lambda *_args: plan)

    class Generated:
        def _request(self, method, path, *, expected, response_reader):
            response_reader(io.BytesIO(binding_bytes if binding_digest in path else content))

    client = SimpleNamespace(_remote=SimpleNamespace(_transport=SimpleNamespace(_generated=Generated())))
    path = tmp_path / "film.astrid"
    receipt = export_timeline(client, "p", "t", path)
    assert receipt["media_bytes"] == len(content) + len(binding_bytes)
    with zipfile.ZipFile(path) as archive:
        assert archive.namelist() == ["manifest.json", "timeline.json", transcript_entry, "media/" + digest, "media/" + binding_digest]
        assert all(archive.getinfo(name).compress_type == zipfile.ZIP_STORED for name in archive.namelist())
        assert archive.read("media/" + digest) == content
        assert archive.read("media/" + binding_digest) == binding_bytes
        assert json.loads(archive.read(transcript_entry)) == {"source_id": "whisper"}


def test_export_timeline_missing_media_names_object_and_removes_temporary_output(tmp_path, monkeypatch):
    digest = hashlib.sha256(b"x").hexdigest()
    plan = {"manifest": {"name": "Film"}, "timeline": {}, "transcripts": {},
            "media": [{"sha256": digest, "size": 1, "original_name": "missing-source.mov"}]}
    monkeypatch.setattr(timeline_export, "export_plan", lambda *_args: plan)
    class Generated:
        def _request(self, *_args, **_kwargs):
            raise FileNotFoundError("not present")
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=SimpleNamespace(_generated=Generated())))
    destination = tmp_path / "missing.astrid"
    with pytest.raises(ValueError, match="missing-source.mov"):
        export_timeline(client, "p", "t", destination)
    assert not destination.exists()
    assert list(tmp_path.glob(".missing.astrid.*.tmp")) == []


def test_export_plan_refuses_project_pack_effect_by_name(monkeypatch):
    bundle = {"parent": {"clips": [{"clipType": "project-shimmer", "elementRef": {
        "packId": "project_0123456789abcdefabcd", "kind": "effect", "id": "project-shimmer",
        "revision": "sha256:" + "a" * 64,
    }}]}}
    monkeypatch.setattr(timeline_export, "compile_authoring_candidate", lambda _bundle: SimpleNamespace(publication={"dependency_manifest": {"media": []}}))
    client = SimpleNamespace(timelines=SimpleNamespace(open_bundle=lambda *_args: SimpleNamespace(ok=True, data={
        "bundle": bundle, "project_id": "project-1", "timeline_id": "main",
    })))
    with pytest.raises(ValueError, match="project-shimmer"):
        export_plan(client, "project-1", "main")


def test_export_object_stream_is_chunked_and_checks_digest_and_size():
    content = b"abcdefghij"
    class Response:
        def __init__(self):
            self.stream = io.BytesIO(content)
            self.read_sizes = []
        def read(self, size):
            self.read_sizes.append(size)
            return self.stream.read(min(size, 3))
    response = Response()
    class Generated:
        def _request(self, method, path, *, expected, response_reader):
            assert method == "GET" and path.endswith("sha256%3A" + "0" * 64)
            response_reader(response)
    transport = SimpleNamespace(_generated=Generated())
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport))
    output = io.BytesIO()
    _write_object_stream(client, "sha256:" + "0" * 64, output, len(content), hashlib.sha256(content).hexdigest())
    assert output.getvalue() == content
    assert response.read_sizes and set(response.read_sizes) == {1024 * 1024}


def test_zip64_writer_streams_media_above_four_gib_into_sparse_archive(tmp_path, monkeypatch):
    size = 4 * 1024**3 + 123
    path = tmp_path / "large-sparse.astrid"

    class SparseFile:
        def __init__(self, raw):
            self.raw = raw
        def tell(self):
            return self.raw.tell()
        def seek(self, offset, whence=0):
            return self.raw.seek(offset, whence)
        def write(self, data):
            if len(data) >= 1024 and not any(data):
                self.raw.seek(len(data), 1)
                return len(data)
            return self.raw.write(data)
        def flush(self):
            return self.raw.flush()

    class Hash:
        def update(self, _chunk):
            pass
        def hexdigest(self):
            return "synthetic"

    class Response:
        def __init__(self):
            self.remaining = size
        def read(self, count):
            amount = min(count, self.remaining)
            self.remaining -= amount
            return bytes(amount)

    response = Response()
    class Generated:
        def _request(self, method, _route, *, expected, response_reader):
            assert method == "GET" and expected == (200,)
            response_reader(response)

    client = SimpleNamespace(_remote=SimpleNamespace(_transport=SimpleNamespace(_generated=Generated())))
    monkeypatch.setattr(timeline_export, "hashlib", SimpleNamespace(sha256=Hash))
    with path.open("w+b") as raw:
        sparse = SparseFile(raw)
        with zipfile.ZipFile(sparse, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            info = zipfile.ZipInfo("media/" + "a" * 64)
            info.compress_type = zipfile.ZIP_STORED
            with archive.open(info, "w", force_zip64=True) as member:
                _write_object_stream(client, "sha256:" + "a" * 64, member, size, "synthetic")
        sparse.flush()
    assert path.stat().st_size > 4 * 1024**3
    assert path.stat().st_blocks * 512 < 4 * 1024**2
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo("media/" + "a" * 64)
        assert info.file_size == size
        assert info.compress_type == zipfile.ZIP_STORED
