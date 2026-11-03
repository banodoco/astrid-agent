from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk import timeline_import
from astrid.sdk.remote import RemoteMedia
from astrid.sdk.timeline_export import export_timeline
from astrid.sdk.workspace_client import WorkspaceClient


def _archive(path: Path, *, payload: bytes = b"media-content", media_rows=None, transcript_names=(), elements=()):
    digest = hashlib.sha256(payload).hexdigest()
    manifest = {
        "format": "astrid.timeline/1",
        "name": "Fixture",
        "media": media_rows if media_rows is not None else [
            {"sha256": digest, "size": len(payload), "media_type": "application/octet-stream", "original_name": "clip.bin"}
        ],
        "elements": list(elements),
        "transcripts": list(transcript_names),
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("manifest.json", json.dumps(manifest).encode())
        archive.writestr("timeline.json", json.dumps({"project_id": "source", "parent": {}, "placements": [], "shots": {}}).encode())
        archive.writestr(f"media/{digest}", payload)
    return manifest, digest


def _client(*, objects=(), prepared=None, uploads=None, commit_result=None):
    uploads = uploads if uploads is not None else []
    prepared = prepared or {"timeline_id": "fresh", "name": "Imported", "publication": {"dependency_manifest": {"media": []}}, "text_bindings": [], "differences": {"missing_effects": [], "different_effects": []}}
    calls = []

    class Media:
        def import_stream(self, **kwargs):
            raw = bytearray()
            while True:
                chunk = kwargs["stream"].read(5)
                if not chunk:
                    break
                raw.extend(chunk)
            uploads.append((kwargs, bytes(raw)))
            return SimpleNamespace(ok=True, data={"object_id": kwargs["expected_digest"]})

    class Timelines:
        def import_timeline(self, project_id, body, *, idempotency_key):
            calls.append((project_id, body, idempotency_key))
            return SimpleNamespace(ok=True, data=commit_result or {
                "timeline_id": "fresh", "name": "Imported", "new_head": "head",
                "media": {"count": len(uploads), "bytes": sum(len(raw) for _, raw in uploads)},
            })

    client = SimpleNamespace(
        projects=SimpleNamespace(show=lambda ref: SimpleNamespace(ok=True, data={"project_id": "target"})),
        media=Media(),
        timelines=Timelines(),
        _transport=SimpleNamespace(list_project_objects=lambda *_a, **_k: (list(objects), None)),
    )
    return client, uploads, calls, prepared


def test_import_streams_verified_objects_and_augments_receipt(tmp_path, monkeypatch):
    path = tmp_path / "fixture.astrid"
    manifest, digest = _archive(path)
    uploads = []
    prepared = {"timeline_id": "fresh", "name": "Imported", "publication": {"dependency_manifest": {"media": [{"media_id": "sha256:" + digest}]}}, "text_bindings": [], "differences": {"missing_effects": [], "different_effects": []}}
    client, _uploads, commits, _ = _client(uploads=uploads)
    monkeypatch.setattr(timeline_import, "prepare_import", lambda *args, **kwargs: prepared)
    result = timeline_import.import_timeline(client, path, "target")
    assert len(uploads) == 1
    upload_kwargs, raw = uploads[0]
    assert raw == b"media-content"
    assert upload_kwargs["expected_digest"] == "sha256:" + digest
    assert upload_kwargs["filename"] == "clip.bin"
    assert result["media"]["uploaded"] == 1
    assert result["media"]["uploaded_bytes"] == len(raw)
    assert commits and commits[0][0] == "target"
    assert commits[0][1] == {**prepared, "source": {"via": "cli"}}
    assert manifest["media"][0]["sha256"] == digest


def test_import_skips_only_target_project_managed_objects(tmp_path, monkeypatch):
    path = tmp_path / "fixture.astrid"
    _manifest, digest = _archive(path)
    uploads = []
    prepared = {"timeline_id": "fresh", "name": "Imported", "publication": {"dependency_manifest": {"media": [{"media_id": "sha256:" + digest}]}}, "text_bindings": [], "differences": {"missing_effects": [], "different_effects": []}}
    client, _uploads, _commits, _ = _client(
        objects=[{"object_id": "sha256:" + digest, "digest": digest, "relation": "managed"}], uploads=uploads,
    )
    monkeypatch.setattr(timeline_import, "prepare_import", lambda *args, **kwargs: prepared)
    result = timeline_import.import_timeline(client, path, "target")
    assert uploads == []
    assert result["media"]["uploaded"] == 0
    assert result["media"]["uploaded_bytes"] == 0


def test_import_does_not_skip_nonmanaged_or_relationless_project_object(tmp_path, monkeypatch):
    path = tmp_path / "referenced-only.astrid"
    _manifest, digest = _archive(path)
    prepared = {"timeline_id": "fresh", "name": "Imported", "publication": {"dependency_manifest": {"media": [{"media_id": "sha256:" + digest}]}}, "text_bindings": [], "differences": {"missing_effects": [], "different_effects": []}}
    uploads = []
    client, _uploads, _commits, _ = _client(
        objects=[
            {"object_id": "sha256:" + digest, "digest": digest, "relation": "promoted"},
            {"object_id": "sha256:" + digest, "digest": digest},
        ],
        uploads=uploads,
    )
    monkeypatch.setattr(timeline_import, "prepare_import", lambda *args, **kwargs: prepared)
    result = timeline_import.import_timeline(client, path, "target")
    assert len(uploads) == 1
    assert result["media"]["uploaded"] == 1


def test_import_uses_project_object_reference_for_large_commit_body(tmp_path, monkeypatch):
    path = tmp_path / "large-commit.astrid"
    _manifest, digest = _archive(path)
    uploads = []
    prepared = {"timeline_id": "fresh", "name": "Imported", "publication": {"dependency_manifest": {"media": [{"media_id": "sha256:" + digest}]}}, "text_bindings": [], "differences": {"missing_effects": [], "different_effects": []}}
    client, _uploads, commits, _ = _client(uploads=uploads)
    prepared["large_metadata"] = "x" * (768 * 1024)
    monkeypatch.setattr(timeline_import, "prepare_import", lambda *args, **kwargs: prepared)
    result = timeline_import.import_timeline(client, path, "target")
    assert len(uploads) == 2
    upload_kwargs, raw = uploads[-1]
    digest = hashlib.sha256(raw).hexdigest()
    assert upload_kwargs["media_type"] == "application/json"
    assert upload_kwargs["filename"] == "prepared-timeline-import.json"
    assert upload_kwargs["expected_digest"] == "sha256:" + digest
    assert commits[0][1] == {"prepared_object_id": "sha256:" + digest, "source": {"via": "cli"}}
    assert result["media"]["uploaded"] == 1


def test_import_rejects_bad_digest_and_custom_effect_before_upload(tmp_path, monkeypatch):
    path = tmp_path / "bad.astrid"
    expected = b"expected"
    actual = b"tampered"
    digest = hashlib.sha256(expected).hexdigest()
    manifest = {"format": "astrid.timeline/1", "name": "Fixture", "media": [
        {"sha256": digest, "size": len(expected), "media_type": "application/octet-stream", "original_name": "clip.bin"}
    ], "elements": [], "transcripts": []}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("timeline.json", json.dumps({"project_id": "source", "parent": {}, "placements": [], "shots": {}}))
        archive.writestr("media/" + digest, actual)
    uploads = []
    client, _uploads, _commits, _prepared = _client(uploads=uploads)
    with pytest.raises(ValueError, match="failed size or SHA-256"):
        timeline_import.import_timeline(client, path, "target")
    assert uploads == []

    path = tmp_path / "custom.astrid"
    _archive(path, elements=[{"packId": "project_pack", "kind": "effect", "id": "custom", "revision": "", "pinned": False}])
    with pytest.raises(Exception, match="custom effect custom"):
        timeline_import.import_timeline(client, path, "target")
    assert uploads == []


def test_import_requires_exact_prepared_media_closure_and_upload_identity(tmp_path, monkeypatch):
    path = tmp_path / "extra.astrid"
    _manifest, digest = _archive(path)
    client, uploads, _commits, _ = _client()
    prepared = {"timeline_id": "fresh", "name": "Imported", "publication": {"dependency_manifest": {"media": []}}, "text_bindings": [], "differences": {"missing_effects": [], "different_effects": []}}
    monkeypatch.setattr(timeline_import, "prepare_import", lambda *args, **kwargs: prepared)
    with pytest.raises(ValueError, match="unreferenced"):
        timeline_import.import_timeline(client, path, "target")
    assert not uploads

    prepared["publication"]["dependency_manifest"]["media"] = [{"media_id": "sha256:" + digest}]
    client.media.import_stream = lambda **kwargs: SimpleNamespace(ok=True, data={"filename": kwargs["filename"]})
    with pytest.raises(ValueError, match="no object identity"):
        timeline_import.import_timeline(client, path, "target")


def test_import_rejects_unsafe_archive_paths_and_compressed_entries(tmp_path):
    path = tmp_path / "unsafe.astrid"
    digest = hashlib.sha256(b"x").hexdigest()
    manifest = {"format": "astrid.timeline/1", "media": [], "elements": [], "transcripts": []}
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("timeline.json", "{}")
        archive.writestr("../media/" + digest, b"x")
    with pytest.raises(ValueError, match="unsafe entry name"):
        timeline_import.import_timeline(SimpleNamespace(), path, "target")
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        archive.writestr("timeline.json", "{}")
    with pytest.raises(ValueError, match="compressed; stored entries"):
        timeline_import.import_timeline(SimpleNamespace(), path, "target")


def test_workspace_client_stream_and_import_methods_keep_publish_signature():
    calls = []

    class Generated:
        def publish_parent_composition(self, *args, **kwargs):
            calls.append(("publish", args, kwargs))
            return {"ok": True}
        def ingest_project_object_stream(self, *args, **kwargs):
            calls.append(("stream", args, kwargs))
            return {"ok": True}
        def import_timeline(self, *args, **kwargs):
            calls.append(("import", args, kwargs))
            return {"ok": True}

    workspace = WorkspaceClient.__new__(WorkspaceClient)
    workspace._generated = Generated()
    workspace.publish_parent_composition("project", "timeline", {"clips": []}, idempotency_key="publish-key")
    stream = io.BytesIO(b"stream")
    workspace.ingest_project_object_stream("project", stream, size=6, media_type="application/octet-stream",
                                           filename="a.bin", expected_digest="sha256:" + "a" * 64,
                                           idempotency_key="upload-key")
    workspace.import_timeline("project", {"timeline_id": "new"}, idempotency_key="import-key")
    assert calls[0] == ("publish", ("project", "timeline", {"clips": []}), {"idempotency_key": "publish-key"})
    assert calls[1][0] == "stream" and calls[1][1] == ("project", stream)
    assert calls[1][2]["expected_digest"] == "sha256:" + "a" * 64
    assert calls[2] == ("import", ("project", {"timeline_id": "new"}), {"idempotency_key": "import-key"})


def test_sdk_export_then_import_round_trips_unknown_unpinned_effect(tmp_path):
    fixture_path = Path(__file__).parents[1] / "editing_scenarios" / "fixtures" / "empty.json"
    source = json.loads(fixture_path.read_text(encoding="utf-8"))
    source["project_id"] = "source-project"
    source["timeline_id"] = "source-timeline"
    source["placements"] = []
    source["shots"] = {}
    unknown_id = "effect-not-in-source-catalog"
    clip = {"id": "unknown-occurrence", "clipType": unknown_id, "track": "effects", "at": 0, "hold": 1}
    source["parent"]["clips"] = [clip]
    source["parent"]["config"]["clips"] = [clip]

    calls = []

    class Transport:
        def list_timelines(self, _project, *, cursor=None, limit=50):
            return ([{"timeline_id": "source-timeline", "name": "Unknown effect fixture"}], None)

        def list_documents(self, _project, *, cursor=None, limit=50, kind=None):
            return ([], None)

        def list_project_objects(self, _project, *, cursor=None, limit=100):
            return ([], None)

    class Timelines:
        def open_bundle(self, _project, _timeline):
            return SimpleNamespace(ok=True, data={
                "bundle": source, "project_id": "source-project", "timeline_id": "source-timeline",
            })

        def import_timeline(self, project_id, body, *, idempotency_key):
            calls.append((project_id, body, idempotency_key))
            return SimpleNamespace(ok=True, data={"timeline_id": body["timeline_id"], "name": body["name"]})

    class Media:
        def import_stream(self, **kwargs):
            pytest.fail(f"empty fixture unexpectedly uploaded media: {kwargs.get('filename')}")

    client = SimpleNamespace(
        projects=SimpleNamespace(show=lambda _project: SimpleNamespace(ok=True, data={"project_id": "target-project"})),
        timelines=Timelines(), media=Media(), _transport=Transport(),
    )
    archive_path = tmp_path / "unknown-effect.astrid"
    export_timeline(client, "source-project", "source-timeline", archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        exported_manifest = json.loads(archive.read("manifest.json"))
    unresolved = next(row for row in exported_manifest["elements"] if row["id"] == unknown_id)
    assert unresolved == {"packId": "", "kind": "effect", "id": unknown_id, "revision": "", "pinned": False}

    receipt = timeline_import.import_timeline(client, archive_path, "target-project")
    assert len(calls) == 1
    project_id, body, _key = calls[0]
    assert project_id == "target-project"
    assert any(clip.get("clipType") == unknown_id
               for clip in body["publication"]["parent_composition"]["clips"])
    assert body["differences"]["missing_effects"] == [unresolved]
    assert body["differences"]["different_effects"] == []
    assert receipt["missing_effects"] == [unresolved]


def test_export_cli_uses_selected_project_when_option_is_omitted(tmp_path, monkeypatch):
    from astrid.packs.timeline import cli as timeline_cli

    class Client:
        def __init__(self):
            self.selected = []
            self.projects = SimpleNamespace(show=lambda ref: SimpleNamespace(ok=True, data={"project_id": ref}))
            self.timelines = SimpleNamespace(_resolve_project_ref=self.resolve)

        def resolve(self, ref):
            self.selected.append(ref)
            return SimpleNamespace(ok=True, data="selected-project")

    client = Client()
    parser = timeline_cli.build_parser(client)
    parsed = parser.parse_args(["export", "main", "--out", str(tmp_path / "empty.astrid")])
    assert parsed.project is None
    monkeypatch.setattr(timeline_import, "prepare_import", lambda *_args, **_kwargs: {})
    from astrid.sdk import timeline_export
    monkeypatch.setattr(timeline_export, "export_plan", lambda _client, project, _timeline: (
        timeline_export._resolve_project(_client, project) and {
            "manifest": {"format": "astrid.timeline/1", "name": "Fixture", "media": [], "transcripts": []},
            "timeline": {}, "transcripts": {}, "media": [],
        }
    ))
    assert parsed.handler(parsed) == 0
    assert client.selected == [None]


def test_remote_media_import_file_streams_path_and_keeps_thumbnail_path(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video bytes")
    calls = []

    class Generated:
        def ingest_project_object_stream(self, project, stream, **kwargs):
            chunks = []
            while True:
                chunk = stream.read(4)
                if not chunk:
                    break
                chunks.append(chunk)
            calls.append((project, b"".join(chunks), kwargs))
            return {"object_id": "sha256:" + "a" * 64, "filename": "clip.mp4"}

    workspace = WorkspaceClient.__new__(WorkspaceClient)
    workspace._generated = Generated()
    workspace.actor_id = None
    workspace._last_handshake = None
    captured = []
    monkeypatch.setattr("astrid.core.execution.thumbnail_backfill.ensure_imported_source_thumbnail",
                        lambda _client, **kwargs: captured.append(kwargs) or None)
    result = RemoteMedia(workspace).import_file(project="project", path=path, idempotency_key="file-key")
    assert result.ok
    assert calls[0][0:2] == ("project", b"video bytes")
    assert calls[0][2]["size"] == path.stat().st_size
    assert captured[0]["source_path"] == path
    assert "source_bytes" not in captured[0]


@pytest.mark.integration
def test_cross_realm_timeline_export_import_reload_and_reimport(tmp_path, real_runtime, capsys):
    from astrid.packs.timeline import cli as timeline_cli
    from astrid.sdk.remote import RemoteAstridClient
    from astrid.sdk.timeline_checkout import Checkout
    from astrid.sdk.timeline_duplicate import assert_copy_equals_source, id_map
    from astrid.sdk.timeline_import_prepare import _document_id
    from astrid.sdk.workspace_client import WorkspaceClient
    from tests.timeline.test_authoring_bundle import AUDIO, OLD, _closure, _digest, _replace_values

    source_daemon = real_runtime(tmp_path / "source-realm", support_root=tmp_path / "source-support")
    target_daemon = real_runtime(tmp_path / "target-realm", support_root=tmp_path / "target-support")
    source_workspace = WorkspaceClient(source_daemon.endpoint, source_daemon.token)
    target_workspace = WorkspaceClient(target_daemon.endpoint, target_daemon.token)
    source = RemoteAstridClient(source_workspace)
    target = RemoteAstridClient(target_workspace)

    source_project = source.projects.create(slug="xfer-source", name="Transfer source", idempotency_key="source-project")
    target_project = target.projects.create(slug="xfer-target", name="Transfer target", idempotency_key="target-project")
    assert source_project.ok and target_project.ok
    source_id, target_id = source_project.data["project_id"], target_project.data["project_id"]
    source.projects.select(source_id, idempotency_key="select-source")
    target.projects.select(target_id, idempotency_key="select-target")

    picture = source.media.import_bytes(project=source_id, data=b"roundtrip-picture", filename="picture.png",
                                        media_type="image/png", idempotency_key="picture")
    audio = source.media.import_bytes(project=source_id, data=b"roundtrip-voice", filename="voice.wav",
                                      media_type="audio/wav", idempotency_key="voice")
    music = source.media.import_bytes(project=source_id, data=b"roundtrip-music", filename="music.wav",
                                      media_type="audio/wav", idempotency_key="music")
    assert picture.ok and audio.ok and music.ok
    picture_id, audio_id, music_id = (picture.data["object_id"], audio.data["object_id"], music.data["object_id"])

    source_workspace.create_timeline(source_id, "main", name="Source film", idempotency_key="source-timeline")
    source.shots.create(project=source_id, shot={"shot_id": "shot-shared", "name": "Opening"},
                        idempotency_key="source-shot")
    narration = "Narration — café"
    binding_result = source.shots.set_text_binding(project=source_id, shot_id="shot-shared",
                                                    kind="voiceover_script", text=narration,
                                                    expected_head=0, idempotency_key="source-binding")
    assert binding_result.ok
    binding_pin = binding_result.data

    parent, shots, internals = _closure(shared=True)
    for occurrence in parent["payload"]["occurrences"]:
        occurrence["source_offset"] = {"start": 0, "end": 0}
    shots[0]["payload"]["text_bindings"] = [{**binding_pin, "text": narration}]
    shots[0]["payload"]["items"].append({"item_id": "music-item", "media_id": music_id,
                                          "source_frame": None, "metadata": {"role": "music"}})
    replacements = {OLD: picture_id, AUDIO: audio_id, "project-1": source_id}
    parent = _replace_values(parent, replacements)
    shots = _replace_values(shots, replacements)
    internals = _replace_values(internals, replacements)
    internal = internals[0]["payload"]
    internal["tracks"].append({"id": "music", "kind": "audio"})
    internal["registry"]["assets"]["music"] = {"media_id": music_id, "type": "audio"}
    internal["clips"].append({"id": "music-bed", "track": "music", "clipType": "media",
                              "asset": "music", "at": 0, "hold": 2})
    internal["clips"][1]["app"] = {"astrid": {"words": [[0.1, 0.5, "café"]]}}

    transcript = {"schema": "media.transcript/v1", "media_id": audio_id, "source_id": "whisper",
                  "segments": [{"id": "line-1", "start": 0.1, "end": 0.5, "text": "café",
                                "words": [{"id": "word-1", "start": 0.1, "end": 0.5, "text": "café"}]}]}
    transcript_doc = source.documents.create(project=source_id, kind="media.transcript", content=transcript,
                                             idempotency_key="source-transcript")
    assert transcript_doc.ok
    parent["payload"]["config"]["bundle"] = {
        "chapters": [{"name": "01 Opening", "from": "occ-1"}],
        "transcriptReferences": {"schema": "media.transcript.references/v1", "associations": [
            {"assetId": "voice", "documentId": transcript_doc.data["document_id"]}
        ]},
    }
    parent["content_digest"] = _digest(parent["payload"])
    shots[0]["content_digest"] = _digest(shots[0]["payload"])
    internals[0]["content_digest"] = _digest(internals[0]["payload"])
    publication = {
        "project_id": source_id, "timeline_id": "main", "expected_head": None,
        "parent_revision_id": "source-parent", "parent_composition": parent["payload"],
        "shot_revisions": shots, "internal_timeline_revisions": internals,
    }
    published = source_workspace.publish_parent_composition(source_id, "main", publication, idempotency_key="source-publish")
    assert published

    archive_path = tmp_path / "film.astrid"
    export_parsed = timeline_cli.build_parser(source).parse_args(["export", "main", "--out", str(archive_path), "--json"])
    assert export_parsed.project is None
    assert export_parsed.handler(export_parsed) == 0
    capsys.readouterr()
    assert archive_path.is_file()

    # The public CLI resolves the selected target project when --project is absent.
    import_parsed = timeline_cli.build_parser(target).parse_args(["import", str(archive_path), "--json"])
    assert import_parsed.project is None
    assert import_parsed.handler(import_parsed) == 0
    first_cli = json.loads(capsys.readouterr().out)
    first = first_cli["data"]
    first_id = first["timeline_id"]
    assert first_id != "main" and first["media"]["uploaded"] >= 3

    from astrid.sdk.timeline_transfer import export_plan, import_timeline
    source_plan = export_plan(source, source_id, "main")
    target_reload = Checkout.open(target_id, first_id, client=target)
    source_bundle = source_plan["timeline"]
    target_bundle = target_reload.bundle
    shot_map = id_map(dict.fromkeys(str(row["shot_id"]) for row in source_bundle["placements"]), first_id)
    assert_copy_equals_source(source_bundle, target_bundle, shot_map)
    source_chapter = source_bundle["parent"]["config"]["bundle"]["chapters"][0]
    target_chapter = target_bundle["parent"]["config"]["bundle"]["chapters"][0]
    assert target_chapter["name"] == source_chapter["name"]
    assert target_chapter["from"] == target_bundle["placements"][0]["occurrence_id"]
    target_internal = target_bundle["shots"][target_bundle["placements"][0]["shot_id"]]["internal_timeline"]
    assert any(row["track"] == "music" and row["id"] == "music-bed" for row in target_internal["clips"])
    assert any(row.get("app", {}).get("astrid", {}).get("words") == [[0.1, 0.5, "café"]]
               for row in target_internal["clips"])
    target_shot = target_bundle["placements"][0]["shot_id"]
    shot_row = target.shots.show(target_id, target_shot)
    assert shot_row.ok and shot_row.data.get("items")
    text_rows = target.shots.list_text_bindings(target_id, shot_id=target_shot, include_text=True)
    assert text_rows.ok and text_rows.data[0][0]["text"] == narration
    target_binding_media = text_rows.data[0][0]["media_id"]
    assert target.media.show(target_id, target_binding_media).ok

    transcript_id = _document_id(target_id, audio_id, "whisper")
    loaded_reference = target_bundle["parent"]["config"]["bundle"]["transcriptReferences"]["associations"][0]["documentId"]
    assert loaded_reference == transcript_id
    assert target.documents.show(transcript_id, project=target_id).ok

    second = import_timeline(target, archive_path, target_id)
    assert second["timeline_id"] != first_id
    assert second["media"]["uploaded"] == 0
    assert not second.get("transcript_conflicts")
    changed = {**transcript, "segments": [{**transcript["segments"][0], "start": 0.2, "end": 0.6, "text": "different",
                                            "words": [{"id": "word-2", "start": 0.2, "end": 0.6,
                                                       "text": "different"}]}]}
    existing_doc = target.documents.show(transcript_id, project=target_id)
    updated = target.documents.update(transcript_id, project=target_id, content=changed,
                                      expected_version=existing_doc.data["version"], idempotency_key="change-transcript")
    assert updated.ok
    third = import_timeline(target, archive_path, target_id)
    assert third["timeline_id"] not in {first_id, second["timeline_id"]}
    assert third["media"]["uploaded"] == 0
    assert third["transcript_conflicts"] and third["transcript_conflicts"][0]["document_id"] == transcript_id

    invalid = target.timelines.import_timeline(target_id, {"timeline_id": "bad-import"}, idempotency_key="bad-import")
    assert not invalid.ok and invalid.error.code in {"validation_error", "not_found"}
