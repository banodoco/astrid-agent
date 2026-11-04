from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import shutil
import sys
import wave
from pathlib import Path

import pytest
from PIL import Image

from astrid.sdk.authoring_bundle import (
    diff_authoring_candidate,
    import_authoring_media,
    open_authoring_bundle,
    plan_authoring_media,
    publish_authoring_candidate,
)
from tests.timeline.test_authoring_bundle import AUDIO, OLD, _closure, _runtime_document


def bundle():
    parent, shots, internals = _closure(shared=False)
    return open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=internals
    )


def bundle_with_runtime_source(*, shared=False):
    """Build the authenticated closure image used by the current session reader."""
    parent, shots, internals = _closure(shared=shared)
    result = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=internals
    )
    result["source_mapping"]["_runtime_document"] = _runtime_document(
        parent, shots, internals
    )
    return result


def picture(tmp_path, name="new.png", colour="red"):
    path = tmp_path / name
    Image.new("RGB", (16, 12), colour).save(path)
    return path


def edit(work, path):
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["registry"]["assets"]["old-picture"]["local_path"] = path
    return shot


class Catalog:
    def __init__(self):
        self.calls = []
        self.generations = []
        self.variants = {}

    def list_generations(self, project_id, **kwargs):
        return self.generations, None

    def list_variants(self, generation_id, **kwargs):
        return self.variants[generation_id], None

    def import_project_media(self, project_id, payload, **kwargs):
        self.calls.append((project_id, payload, kwargs))
        gid = f"generation-{len(self.calls)}"
        variant = {
            "variant_id": "variant-" + gid,
            "object_id": kwargs["expected_digest"],
            "variant_type": "original",
        }
        self.generations.append(
            {
                "generation_id": gid,
                "metadata": {
                    "provenance": {"origin": "imported", "source": "external_upload"},
                    "params": {
                        "mime_type": kwargs["media_type"],
                        **{
                            key: kwargs.get(key)
                            for key in ("filename", "width", "height", "duration_seconds")
                        },
                    },
                },
            }
        )
        self.variants[gid] = [variant]
        return {
            "data": {
                "status": "completed",
                "asset_id": kwargs["expected_digest"],
                "generation_id": gid,
                "variant_id": variant["variant_id"],
            }
        }


def test_unchanged_json_roundtrip_has_empty_diff(tmp_path):
    work = json.loads(json.dumps(bundle()))
    assert diff_authoring_candidate(work)["change_count"] == 0
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert plan["imports"] == []
    assert plan["candidate"] == work
    work["placements"][0]["duration_ms"] += 100
    assert diff_authoring_candidate(work)["change_count"] > 0


def test_local_registry_replacement_is_readonly_and_strips_stale_authority(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    shot = edit(work, path.name)
    entry = shot["internal_timeline"]["registry"]["assets"]["old-picture"]
    entry.update(
        content_sha256=OLD.removeprefix("sha256:"), source={"object_id": OLD}, src="/obsolete.png"
    )
    shot["payload"]["items"][0]["media_id"] = OLD.removeprefix("sha256:")
    shot["payload"]["assets"] = [
        {
            "asset_id": "old-picture",
            "object_id": OLD,
            "digest": OLD,
            "role": "image",
            "scope": {"project_id": work["project_id"]},
            "source": {
                "media_id": OLD,
                "content_sha256": OLD.removeprefix("sha256:"),
                "type": "image/png",
                "file": "/obsolete.png",
            },
        }
    ]
    original = copy.deepcopy(work)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert work == original
    row = plan["imports"][0]
    prepared = next(iter(plan["candidate"]["shots"].values()))
    rewritten = prepared["internal_timeline"]["registry"]["assets"]["old-picture"]
    assert rewritten["media_id"] == row["digest"]
    assert rewritten["type"] == "image"
    assert (row["width"], row["height"]) == (16, 12)
    assert "duration_seconds" not in row
    assert rewritten["resolution"] == "16x12"
    assert not {"width", "height", "duration_seconds"} & set(rewritten)
    assert rewritten["opaque_asset"] == "keep"
    assert not set(("file", "local_path", "src", "source", "content_sha256")) & rewritten.keys()
    assert prepared["payload"]["items"][0]["media_id"] == row["digest"]
    descriptor = prepared["payload"]["assets"][0]
    assert descriptor["object_id"] == descriptor["digest"] == row["digest"]
    assert descriptor["source"]["media_id"] == row["digest"]
    assert descriptor["source"]["content_sha256"] == row["digest"].removeprefix("sha256:")
    assert "file" not in descriptor["source"]
    assert prepared["payload"]["generation_inputs"]["visual"]["object_id"] == OLD
    assert prepared["base_payload"] == original["shots"][prepared["shot_id"]]["base_payload"]


def test_import_reuses_catalog_across_retry_keys_and_preserves_distinct_names(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    edit(work, path.name)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    catalog = Catalog()
    first = import_authoring_media(plan, catalog)
    replay = import_authoring_media(plan, catalog)
    assert len(catalog.calls) == 1
    assert replay["imports"][0]["reused"] is True
    assert first["candidate"] == replay["candidate"]
    assert catalog.calls[0][2]["idempotency_key"] == plan["imports"][0]["import_key"]
    assert (catalog.calls[0][2]["width"], catalog.calls[0][2]["height"]) == (16, 12)
    assert "duration_seconds" not in catalog.calls[0][2]
    renamed = tmp_path / "different-name.png"
    shutil.copyfile(path, renamed)
    other = bundle()
    edit(other, renamed.name)
    import_authoring_media(plan_authoring_media(other, base_directory=tmp_path), catalog)
    assert len(catalog.calls) == 2
    assert catalog.calls[0][2]["expected_digest"] == catalog.calls[1][2]["expected_digest"]
    assert catalog.calls[0][2]["idempotency_key"] != catalog.calls[1][2]["idempotency_key"]


@pytest.mark.parametrize(
    "name,content", [("missing.png", None), ("unsupported.txt", b"text"), ("bad.wav", b"invalid")]
)
def test_all_sources_preflight_before_any_import_or_publication(tmp_path, name, content):
    good = picture(tmp_path)
    if content is not None:
        (tmp_path / name).write_bytes(content)
    work = bundle()
    shot = edit(work, good.name)
    shot["internal_timeline"]["registry"]["assets"]["voice"]["local_path"] = name
    catalog = Catalog()
    with pytest.raises((ValueError, FileNotFoundError, RuntimeError)):
        plan_authoring_media(work, base_directory=tmp_path)
    assert catalog.calls == []
    assert work["base_parent"]["revision_id"] == "parent-1"


def test_audio_local_selector_is_probed_and_keeps_audio_mirror(tmp_path):
    path = tmp_path / "voice.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000)
    work = bundle()
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["clips"][1]["asset"] = "./voice.wav"
    plan = plan_authoring_media(work, base_directory=tmp_path)
    row = plan["imports"][0]
    assert row["media_type"].startswith("audio/")
    assert row["duration_seconds"] == 1.0
    assert "width" not in row and "height" not in row
    prepared = next(iter(plan["candidate"]["shots"].values()))
    clip = prepared["internal_timeline"]["clips"][1]
    audio_entry = prepared["internal_timeline"]["registry"]["assets"][clip["asset"]]
    assert audio_entry["type"] == "audio"
    assert audio_entry["duration"] == row["duration_seconds"]
    assert not {"width", "height", "duration_seconds"} & set(audio_entry)
    assert prepared["payload"]["items"][1]["media_id"] == row["digest"]
    assert prepared["payload"]["audio_bindings"][0]["object_id"] == row["digest"]
    assert prepared["base_payload"]["items"][1]["media_id"] == AUDIO
    catalog = Catalog()
    import_authoring_media(plan, catalog)
    assert catalog.calls[0][2]["duration_seconds"] == row["duration_seconds"]
    assert "width" not in catalog.calls[0][2]


def test_changed_file_after_preflight_imports_nothing(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    edit(work, path.name)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    picture(tmp_path, colour="blue")
    catalog = Catalog()
    with pytest.raises(ValueError, match="changed after check"):
        import_authoring_media(plan, catalog)
    assert catalog.calls == []


def test_import_failure_cannot_reach_publication(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    edit(work, path.name)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    catalog = Catalog()
    catalog.import_project_media = lambda *args, **kwargs: {"status": "pending"}
    with pytest.raises(ValueError, match="completed managed catalog pair"):
        import_authoring_media(plan, catalog)


def test_document_publish_forwards_new_stale_head_to_runtime(tmp_path, monkeypatch):
    # Predicate mapping: retired open_bundle/direct commit calls become the
    # opened session's exact closure read and one commit_many v2 request; the
    # stale-head refusal, one import, unchanged source file, and no receipt
    # artifact remain unchanged.
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace

    from astrid.sdk.timeline.sync import CommitConflict

    script = Path(__file__).parents[2] / "astrid/packs/rendering/docs/scripts/timeline_document.py"
    spec = importlib.util.spec_from_file_location("timeline_document_test", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    path = picture(tmp_path)
    work = bundle()
    work["base_parent"]["publication_id"] = "publication-1"
    work["placements"][0].update(source_offset=0, speed=1)
    work["base_placements"]["occ-1"].update(source_offset=0, speed=1)
    old_entry = next(iter(work["shots"].values()))["base_internal_timeline"]["registry"]["assets"]["old-picture"]
    old_entry.update(generationId="generation-old", variantId="variant-old")
    next(iter(work["shots"].values()))["internal_timeline"]["registry"]["assets"]["old-picture"].update(
        generationId="generation-old", variantId="variant-old"
    )
    edit(work, path.name)
    document = tmp_path / "timeline.json"
    document.write_text(json.dumps(work))
    class StaleWorkspace(Catalog):
        def get_project_media_selection(self, _project_id, selection):
            if selection["generation_id"] == "generation-old":
                object_id = OLD
            else:
                object_id = self.calls[0][1] and self.calls[0][2]["expected_digest"]
            return {
                "asset": selection["asset"], "generation_id": selection["generation_id"],
                "variant_id": selection["variant_id"], "object_id": object_id,
                "catalog_digest": "sha256:" + "c" * 64,
            }

        def get_project_verified_media(self, _project_id, selection):
            return {"selection": selection, "media_type": "image/png", "size": 78,
                    "width": 16, "height": 12}

    stale_workspace = StaleWorkspace()
    session_base = bundle_with_runtime_source()
    session_base["base_parent"]["publication_id"] = "publication-1"
    session_base["placements"][0].update(source_offset=0, speed=1)
    session_base["base_placements"]["occ-1"].update(source_offset=0, speed=1)
    session_shot = next(iter(session_base["shots"].values()))
    session_shot["base_internal_timeline"]["registry"]["assets"]["old-picture"].update(
        generationId="generation-old", variantId="variant-old"
    )
    session_shot["internal_timeline"]["registry"]["assets"]["old-picture"].update(
        generationId="generation-old", variantId="variant-old"
    )

    class Session:
        def __init__(self):
            self.calls = []

        def _state(self):
            return {"base": {"revision_id": "parent-1", "publication_id": "publication-1"}}

        def _capture_bundle(self, state):
            assert state["base"] == {"revision_id": "parent-1", "publication_id": "publication-1"}
            return copy.deepcopy(session_base)

        def commit_many(self, commands, **kwargs):
            steps = [{
                "id": "step-1", "label": kwargs["label"], "intent": "", "tags": [],
                "command_ordinals": [0], "patch": [],
            }]
            request = {
                "api_version": 1, "project_id": "project-1", "timeline_id": "main",
                "base_revision_id": kwargs["base"]["revision_id"],
                "base_publication_id": kwargs["base"]["publication_id"],
                "commands": copy.deepcopy(commands), "label": kwargs["label"],
                "steps": steps, "merge": kwargs["merge"], "dry_run": False,
                "source": {"via": kwargs["via"]},
            }
            self.calls.append((request, kwargs))
            raise CommitConflict({
                "code": "conflict", "message": "parent composition head is stale",
                "details": {"status": "conflict"},
            })

    session = Session()
    client = SimpleNamespace(
        _remote=SimpleNamespace(
            _transport=SimpleNamespace(_last_handshake={"realm_id": "test", "actor_id": "author"})
        ),
        timelines=SimpleNamespace(),
    )
    monkeypatch.setattr(
        helper.AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(client)
    )
    monkeypatch.setattr(
        helper, "workspace", lambda: stale_workspace
    )
    monkeypatch.setattr(helper.TimelineSession, "open", lambda *args, **kwargs: session)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(script), "publish", "--file", str(document), "--idempotency-key", "stale-edit"],
    )
    with pytest.raises(CommitConflict) as caught:
        helper.main()
    assert caught.value.detail == "parent composition head is stale"
    assert caught.value.cause["message"] == "parent composition head is stale"
    assert document.read_text() == json.dumps(work)
    assert document.with_suffix(".check.json").exists()
    assert document.with_suffix(".imports.json").exists()
    assert document.with_suffix(".prepared.json").exists()
    assert not document.with_suffix(".publication.json").exists()
    assert len(stale_workspace.calls) == 1
    assert len(session.calls) == 1
    request, options = session.calls[0]
    assert (request["project_id"], request["timeline_id"]) == ("project-1", "main")
    assert options["idempotency_key"] == "stale-edit"
    assert request["api_version"] == 1
    assert request["project_id"] == "project-1"
    assert request["timeline_id"] == "main"
    assert request["base_revision_id"] == "parent-1"
    assert request["base_publication_id"] == "publication-1"
    assert [command["op"] for command in request["commands"]] == ["occurrence.swap-media"]
    assert request["label"] == "Publish document"
    assert request["merge"] == "atomic"
    assert request["dry_run"] is False
    assert request["source"] == {"via": "script"}
    assert len(request["steps"]) == 1
    assert request["steps"][0]["command_ordinals"] == [0]
    assert options["verified_media"]


def test_document_publish_captures_verified_media_before_document_patches():
    script = Path(__file__).parents[2] / "astrid/packs/rendering/docs/scripts/timeline_document.py"
    spec = importlib.util.spec_from_file_location("timeline_document_media_capture_test", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)

    parent, shots, internals = _closure(shared=False)
    base = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=internals
    )
    base["source_mapping"]["_runtime_document"] = _runtime_document(parent, shots, internals)
    base["base_parent"]["publication_id"] = "original-publication"
    base["placements"][0].update(source_offset=0, speed=1)
    base["base_placements"]["occ-1"].update(source_offset=0, speed=1)
    shot = next(iter(base["shots"].values()))
    entry = shot["internal_timeline"]["registry"]["assets"]["old-picture"]
    entry.update(generationId="generation-old", variantId="variant-old")
    old = {
        "asset": "old-picture", "generation_id": "generation-old",
        "variant_id": "variant-old", "object_id": OLD,
        "catalog_digest": "sha256:" + "a" * 64,
    }
    new = {
        "asset": "old-picture", "generation_id": "generation-new",
        "variant_id": "variant-new", "object_id": "sha256:" + "2" * 64,
        "catalog_digest": "sha256:" + "b" * 64,
    }
    verified = [
        {"selection": old, "media_type": "image/png", "size": 10},
        {"selection": new, "media_type": "image/png", "size": 10},
    ]
    owner = "occurrence:occ-1"
    clip = next(row for row in shot["internal_timeline"]["clips"] if row.get("asset") == "old-picture")
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media

    captured = capture_occurrence_swap_media(
        base, owner + "/clip:" + clip["id"], old, new, verified_media=verified
    )
    candidate = execute(base, [captured], verified_media=verified).bundle
    commands = helper.emit_document_publish_commands(
        base, candidate, {(owner, "old-picture"): {"old": old, "new": new}}, verified
    )

    assert [command["op"] for command in commands] == ["occurrence.swap-media"]
    assert commands[0]["args"]["old_media"] == old
    assert commands[0]["args"]["media"] == new
    assert commands[0]["bindings"]["media.target"] == owner + "/clip:" + clip["id"]
    assert commands[0]["reads"]


def _genuine_compound_source(tmp_path):
    """Create the retained-manifest source through the current Runtime API."""
    from tests.helpers.runtime import use_runtime

    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path

    from astrid.sdk.timeline.commit import _digest, create_runtime_commit_service
    from astrid.sdk.timeline.native_occurrence_delete import capture_occurrence_delete
    from tests.editing_conformance.test_commit_runtime_occurrence_media import _create_fixture
    from tests.editing_conformance.test_recipe_occurrence_media import inputs
    from tests.timeline.test_authoring_bundle import _replace_values

    root = tmp_path / "d92-genuine-realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())

    def import_media(project, value, *, media_type, filename, alias, key,
                     duration_seconds=None):
        result = service.import_media(
            project, value, media_type=media_type, original_name=filename,
            actor_id="d92-authoring-test", idempotency_key=key,
            duration_seconds=duration_seconds,
        )["data"]
        selection = service.get_project_media_selection(project, {
            "asset": alias, "generation_id": result["generation_id"],
            "variant_id": result["variant_id"],
        }, authorize=lambda: None)
        return selection, service.get_project_verified_media(
            project, selection, authorize=lambda: None
        )

    def submit(project, commands, key):
        head = service.get_project_timeline(project, "film")
        request = {
            "api_version": 1, "project_id": project, "timeline_id": "film",
            "base_revision_id": head["head_revision_id"],
            "base_publication_id": head["head_publication_id"], "commands": commands,
            "label": key, "merge": "atomic", "reads": [], "dry_run": False,
            "source": {"via": "python"}, "rationale": "D92 genuine compound setup",
            "group_id": key,
            "steps": [{
                "id": str(index), "label": key, "intent": "D92 setup", "tags": ["d92"],
                "command_ordinals": [index],
                "patch": command.get("args", {}).get("changes", []), "reads": [],
            } for index, command in enumerate(commands)],
        }
        return service.commit_timeline(
            project, "film", request, idempotency_key=key, publisher="d92-authoring-test",
            invocation_path=route_path(project, "film"), authorize=lambda: None,
        )["data"]["canonical_document"]

    def public_document(project):
        head = service.get_project_timeline(project, "film")
        parent = service.get_project_parent_composition_revision(
            project, "film", head["head_revision_id"]
        )
        document = {
            "schema_version": 1, "project_id": project, "timeline_id": "film",
            "parent_revision": {key: parent[key] for key in (
                "revision_id", "content_digest", "payload")},
            "shot_revisions": [], "internal_timeline_revisions": [],
            "dependency_manifest": copy.deepcopy(parent["dependency_manifest"]),
        }
        for pin in document["dependency_manifest"]["shots"]:
            image = service.get_project_shot_revision(project, pin["shot_id"], pin["revision_id"])
            document["shot_revisions"].append({key: image[key] for key in (
                "revision_id", "shot_id", "internal_timeline_revision_id", "content_digest", "payload")})
        for pin in document["dependency_manifest"]["internal_timelines"]:
            image = service.get_project_timeline_revision(
                project, "film", pin["revision_id"]
            )
            document["internal_timeline_revisions"].append({key: image[key] for key in (
                "revision_id", "timeline_id", "content_digest", "payload")})
        return document, {
            "revision_id": head["head_revision_id"],
            "publication_id": head["head_publication_id"],
        }

    project = service.create_project(
        {"slug": "d92-compound", "name": "D92 compound"}, idempotency_key="d92-project"
    )["id"]
    service.create_timeline(project, "film", idempotency_key="d92-timeline")
    old_png = io.BytesIO()
    Image.new("RGB", (1, 1), "blue").save(old_png, format="PNG")
    new_png = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(new_png, format="PNG")
    audio = io.BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b"\0\0" * 8000)
    old, old_verified = import_media(
        project, old_png.getvalue(), media_type="image/png", filename="old.png",
        alias="picture", key="d92-old"
    )
    new, new_verified = import_media(
        project, new_png.getvalue(), media_type="image/png", filename="new.png",
        alias="reserved-new", key="d92-new",
    )
    retained, _ = import_media(
        project, audio.getvalue(), media_type="audio/wav", filename="held.wav",
        alias="held-audio", key="d92-held", duration_seconds=1.0,
    )
    document, source_old, _, _ = inputs()
    document["parent_revision"]["payload"]["occurrences"] = \
        document["parent_revision"]["payload"]["occurrences"][:2]
    old_fake = source_old["object_id"]
    # The bootstrap admits this catalog selection as well as its CAS object.
    document["parent_revision"]["payload"]["registry"]["assets"][old["asset"]].update(
        generationId=old["generation_id"], variantId=old["variant_id"]
    )
    for image in [document["parent_revision"], *document["shot_revisions"],
                  *document["internal_timeline_revisions"]]:
        image["payload"] = _replace_values(image["payload"], {old_fake: old["object_id"]})
        image["content_digest"] = _digest(image["payload"])
    document["dependency_manifest"]["media"] = [{
        "media_id": old["object_id"], "content_digest": old["object_id"]
    }]
    _create_fixture(service, project, document, old, authorize=lambda: None)
    current, _ = public_document(project)
    from astrid.sdk.timeline.commit import bundle_from_document
    bundle = bundle_from_document(current)
    delete = capture_occurrence_delete(bundle, "occurrence:occ-b")
    current = submit(project, [delete], "d92-remove-sibling")
    before = bundle_from_document(current)
    consumer = copy.deepcopy(before)
    internal = consumer["shots"]["chapter-shot"]["internal_timeline"]
    original_internal = copy.deepcopy(internal)
    internal["tracks"].append({"id": "held-audio", "kind": "audio"})
    internal["clips"].append({"id": "held-audio-clip", "track": "held-audio",
                              "clipType": "media", "asset": "held-audio", "at": 0, "hold": 1})
    internal["registry"]["assets"]["held-audio"] = {
        "media_id": retained["object_id"], "type": "audio"
    }
    from astrid.sdk.timeline.emitter import emit_document_commands
    admission = {"schema_version": 1, "op": "media.admit", "version": "1", "select": None,
                 "args": {"inputs": [{"pin": {"media_id": retained["object_id"],
                                                   "content_digest": retained["object_id"]}}]}}
    current = submit(project, [*emit_document_commands(before, consumer), admission], "d92-commit-a")
    committed_a, _ = public_document(project)
    assert committed_a == current
    assert {row["media_id"] for row in current["dependency_manifest"]["media"]} >= {
        old["object_id"], retained["object_id"]
    }
    after_a = bundle_from_document(current)
    removed = copy.deepcopy(after_a)
    removed["shots"]["chapter-shot"]["internal_timeline"] = original_internal
    current = submit(project, emit_document_commands(after_a, removed), "d92-commit-b")
    committed_b, pair_b = public_document(project)
    assert committed_b == current
    assert bundle_from_document(current)["shots"]["chapter-shot"]["internal_timeline"] == original_internal
    assert all(retained["object_id"] not in json.dumps(image["payload"]) for image in [
        current["parent_revision"], *current["shot_revisions"], *current["internal_timeline_revisions"]
    ])
    assert {row["media_id"] for row in current["dependency_manifest"]["media"]} >= {
        old["object_id"], retained["object_id"]
    }
    service.close()
    return current, old, new, [old_verified, new_verified], retained["object_id"], pair_b


def _public_document_compound(recorded, tmp_path):
    from types import SimpleNamespace

    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.commands import execute
    from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
    from astrid.sdk.timeline.emitter import DocumentPatchError
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_swap_media
    from astrid.sdk.timeline.store import fetch_bundle
    from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm

    document, old, new, verified, retained, pin = _genuine_compound_source(tmp_path)
    project_id, timeline_id = document["project_id"], document["timeline_id"]
    document["parent_revision"]["payload"]["occurrences"] = document["parent_revision"]["payload"]["occurrences"][:1]
    document["parent_revision"]["content_digest"] = _digest(document["parent_revision"]["payload"])
    new["asset"] = old["asset"]
    verified[1]["selection"] = copy.deepcopy(new)

    class Reader(RemoteTimelines):
        def __init__(self):
            self.calls = []
            self.history_revision = pin["revision_id"]
            self.canonical = document

        def open_composition(self, project, timeline, **kwargs):
            return DomainResult.success({
                "native_inspection": {"project_id": project, "timeline_id": timeline},
                "summary": {"head_revision_id": "later-head"},
            })

        def history(self, project, timeline, *, cursor=None, limit=100):
            self.calls.append(("history", cursor))
            return DomainResult.success({"head": {"revision_id": "later-head", "publication_id": "later"},
                "items": [] if cursor is None else [{**pin, "revision_id": self.history_revision, "receipt": {
                    "receipt_id": pin["publication_id"], "result": {
                            "canonical_document": self.canonical} if recorded else {}}}],
                "next_cursor": "page-2" if cursor is None else None})

        def _typed(self, operation, project, *args, **kwargs):
            self.calls.append((operation, args))
            images = [document["parent_revision"], *document["shot_revisions"],
                      *document["internal_timeline_revisions"]]
            image = next(row for row in images if row["revision_id"] == args[-1])
            return DomainResult.success({**copy.deepcopy(image), "project_id": project,
                **({"timeline_id": timeline_id, "dependency_manifest": document["dependency_manifest"]}
                   if operation == "get_project_parent_composition_revision" else {})})

    reader = Reader()
    client = SimpleNamespace(timelines=reader)
    detached = fetch_bundle(project_id, timeline_id, revision_id=pin["revision_id"], client=client)
    assert "_runtime_document" not in detached["source_mapping"]
    base = fetch_bundle(project_id, timeline_id, **pin, client=client)
    assert base["source_mapping"]["_runtime_document"] == document
    assert base["base_parent"]["publication_id"] == pin["publication_id"]
    target = "occurrence:occ-a/clip:clip-a"
    detached["source_mapping"]["_runtime_document"] = copy.deepcopy(
        base["source_mapping"]["_runtime_document"]
    )
    assert detached["source_mapping"]["_runtime_document"] == document
    swap = capture_occurrence_swap_media(detached, target, old, new, verified_media=verified)
    candidate = execute(detached, [swap], verified_media=verified).bundle
    child = next(iter(candidate["shots"].values()))
    child["internal_timeline"]["clips"].reverse()
    child["internal_timeline"]["clips"][1]["at"] = 1.25
    del child["internal_timeline"]["clips"][1]["app"]["org.example"]["clear"]
    child["internal_timeline"]["opaque"]["v"] = [None, {"id": "literal"}, False]
    child["payload"]["unknown"]["added"] = None
    del child["payload"]["unknown"]["keep"]
    script = Path(__file__).parents[2] / "astrid/packs/rendering/docs/scripts/timeline_document.py"
    spec = importlib.util.spec_from_file_location("timeline_document_compound_test", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    selections = {("occurrence:occ-a", old["asset"]): {"old": old, "new": new}}
    commands = helper.emit_document_publish_commands(base, candidate, selections, verified)
    assert commands[0] == swap
    assert all(command["op"] == "doc.patch" for command in commands[1:])
    assert len(commands) > 1
    # Session staging must read this same original source, too, before posting.
    from astrid.sdk.timeline.sync import TimelineSession

    reader._client = object()
    session = SimpleNamespace(project_id=project_id, timeline_id=timeline_id, client=client)
    staged_base = TimelineSession._bundle(session, {"base": pin, "pending": []}, commands)
    assert staged_base == base
    assert execute(staged_base, commands, verified_media=verified).bundle["shots"] == candidate["shots"]
    steps = [{"id": f"step-{index}", "label": "Publish document", "intent": "", "tags": [],
              "command_ordinals": [index],
              "patch": command["args"]["changes"] if command["op"] == "doc.patch" else []}
             for index, command in enumerate(commands)]
    plan = execute(base, commands, verified_media=verified,
                   commit_request={"label": "Publish document", "merge": "atomic", "steps": steps})
    assert plan.bundle["parent"] == candidate["parent"]
    assert plan.bundle["placements"] == candidate["placements"]
    assert plan.bundle["shots"] == candidate["shots"]
    assert plan.merge_mode == "atomic"
    landed = document_from_bundle(plan.bundle)
    assert document_from_bundle(bundle_from_document(landed)) == landed
    realm = MediaRealm(document, verified)
    context = realm.context

    def labelled_context(calls, **kwargs):
        value = context(calls, **kwargs)
        value["request"].update(label="Publish document", merge="atomic")
        if calls[0]["op"] != "timeline.inverse":
            value["request"]["steps"] = steps
        value["invocation"]["body"] = copy.deepcopy(value["request"])
        value["request_hash"] = _digest(value["invocation"])
        return value

    realm.context = labelled_context
    accepted = realm.call(commands)
    assert accepted["data"]["canonical_document"] == landed
    assert document["dependency_manifest"]["media"][0] in landed["dependency_manifest"]["media"]
    assert {row["media_id"] for row in landed["dependency_manifest"]["media"]} == {old["object_id"], new["object_id"], retained}
    assert ("history", "page-2") in reader.calls
    # Neither a file's source object nor a changed original pair can authorize a post.
    corrupt = copy.deepcopy(candidate)
    corrupt["source_mapping"]["_runtime_document"] = {"fabricated": True}
    with pytest.raises(DocumentPatchError, match="canonical source"):
        helper.emit_document_publish_commands(base, corrupt, selections, verified)
    reader.history_revision = "changed-original"
    with pytest.raises(ValueError, match="original revision/publication pair"):
        fetch_bundle(project_id, timeline_id, revision_id=pin["revision_id"],
                     publication_id=pin["publication_id"], client=client)
    if recorded:
        reader.history_revision = pin["revision_id"]
        reader.canonical = copy.deepcopy(document)
        reader.canonical["parent_revision"]["revision_id"] = "foreign-source"
        with pytest.raises(ValueError, match="canonical document differs"):
            fetch_bundle(project_id, timeline_id, **pin, client=client)
    return realm, accepted, document


@pytest.mark.integration
@pytest.mark.parametrize("recorded", [True, False])
def test_public_document_swap_and_residual_patch_use_original_sequential_closure(recorded, tmp_path):
    _public_document_compound(recorded, tmp_path)


@pytest.mark.integration
def test_public_document_compound_inverse_restores_exact_original(tmp_path):
    from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
    from tests.sdk.test_timeline_commit_provider import inverse

    realm, accepted, document = _public_document_compound(True, tmp_path)
    restored = realm.call([inverse([accepted["receipt"]["receipt_id"]])])
    assert restored["data"]["canonical_document"] == document
    assert document_from_bundle(bundle_from_document(restored["data"]["canonical_document"])) == document
    redone = realm.call([inverse([restored["receipt"]["receipt_id"]])])
    landed = accepted["data"]["canonical_document"]
    assert redone["data"]["canonical_document"] == landed
    assert document_from_bundle(bundle_from_document(redone["data"]["canonical_document"])) == landed
    restored_again = realm.call([inverse([redone["receipt"]["receipt_id"]])])
    assert restored_again["data"]["canonical_document"] == document


@pytest.mark.integration  # a real Runtime from a second checkout
def test_real_runtime_import_and_parent_publication_roundtrip(tmp_path):
    from runtime_protocol import service as service_module
    from runtime_protocol import store as store_module

    from astrid.sdk.timeline.commit import create_runtime_commit_service
    from tests.helpers.trusted_seed import seed_trusted_parent

    root = tmp_path / "isolated-realm"
    store_module.RealmStore.initialize(root).close()
    service = service_module.RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    try:
        project = service.create_project(
            {"slug": "checkin-test", "name": "Checkin test"}, idempotency_key="project"
        )
        project_id = project["id"]
        service.create_timeline(project_id, "main", idempotency_key="timeline")
        parent, shots, internals = _closure(shared=False)
        # Direct shot writes are retired, so no text binding can be registered here;
        # this roundtrip is not testing text registration.
        shots[0]["payload"]["text_bindings"] = []
        old = service.ingest(
            project_id, b"old-picture", media_type="image/png", idempotency_key="old-picture"
        )["data"]["object_id"]
        old_audio = service.ingest(
            project_id, b"old-audio", media_type="audio/wav", idempotency_key="old-audio"
        )["data"]["object_id"]
        # Fixture identities describe a detached shape; use actual sandbox
        # project objects and let Runtime produce authoritative revision digests.
        publication = {
            "project_id": project_id,
            "timeline_id": "main",
            "expected_head": None,
            "parent_revision_id": "parent-1",
            "parent_composition": parent["payload"],
            "shot_revisions": [
                {
                    "shot_id": shots[0]["shot_id"],
                    "revision_id": shots[0]["revision_id"],
                    "internal_timeline_revision_id": "internal-1",
                    "payload": shots[0]["payload"],
                }
            ],
            "internal_timeline_revisions": [
                {
                    "timeline_id": "main",
                    "revision_id": "internal-1",
                    "payload": internals[0]["payload"],
                }
            ],
        }
        publication = json.loads(
            json.dumps(publication).replace(OLD, old).replace(AUDIO, old_audio)
        )
        seed_trusted_parent(service, publication, idempotency_key="seed")
        work = open_authoring_bundle(
            service.get_project_parent_composition_revision(project_id, "main", "parent-1"),
            shot_revisions=[
                service.get_project_shot_revision(project_id, "shot-shared", "shot-rev-1")
            ],
            internal_timeline_revisions=[
                service.get_project_timeline_revision(project_id, "main", "internal-1")
            ],
        )
        image = picture(tmp_path)
        edit(work, image.name)
        audio_path = tmp_path / "voice.wav"
        with wave.open(str(audio_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b"\0\0" * 8000)
        shot = next(iter(work["shots"].values()))
        shot["internal_timeline"]["registry"]["assets"]["voice"]["local_path"] = audio_path.name
        plan = plan_authoring_media(work, base_directory=tmp_path)

        class Transport:
            def list_generations(self, project_id, **kwargs):
                page = service.list_generations(project_id, **kwargs)
                return page["items"], page.get("next_cursor")

            def list_variants(self, generation_id, **kwargs):
                page = service.list_variants(generation_id, **kwargs)
                return page["items"], page.get("next_cursor")

            def import_project_media(self, project_id, payload, **kwargs):
                kwargs["original_name"] = kwargs.pop("filename")
                return service.import_media(project_id, payload, actor_id="test-user", **kwargs)

            def list_timeline_commits(self, project_id, timeline_id, **kwargs):
                return service.list_timeline_commits(project_id, timeline_id, authorize=lambda: None, **kwargs)

            def get_project_parent_composition_revision(self, project_id, timeline_id, revision):
                return service.get_project_parent_composition_revision(project_id, timeline_id, revision)

            def get_project_shot_revision(self, project_id, shot_id, revision):
                return service.get_project_shot_revision(project_id, shot_id, revision)

            def get_project_timeline_revision(self, project_id, timeline_id, revision):
                return service.get_project_timeline_revision(project_id, timeline_id, revision)

            def get_project_media_selection(self, project_id, selection):
                return service.get_project_media_selection(project_id, selection, authorize=lambda: None)

            def commit_timeline(self, project_id, timeline_id, payload, **kwargs):
                return service.commit_timeline(project_id, timeline_id, payload, publisher="test-user",
                    invocation_path=f"/v1/projects/{project_id}/timelines/{timeline_id}/commit",
                    authorize=lambda: None, **kwargs)

        transport = Transport()
        imported = import_authoring_media(plan, transport)
        assert len(service.list_generations(project_id)["items"]) == 2
        assert {row["type"] for row in service.list_generations(project_id)["items"]} == {
            "image",
            "audio",
        }
        replay = import_authoring_media(plan, transport)
        assert all(row["reused"] for row in replay["imports"])
        assert len(service.list_generations(project_id)["items"]) == 2
        receipt = publish_authoring_candidate(
            imported["candidate"], transport, idempotency_key="checkin"
        )
        head = receipt["publication"]["new_head"]
        saved_parent = service.get_project_parent_composition_revision(project_id, "main", head)
        occurrence = saved_parent["payload"]["occurrences"][0]
        saved_shot = service.get_project_shot_revision(
            project_id, occurrence["shot_id"], occurrence["shot_revision_id"]
        )
        saved_internal = service.get_project_timeline_revision(
            project_id, "main", saved_shot["internal_timeline_revision_id"]
        )
        selected = {item["media_id"] for item in saved_shot["payload"]["items"]}
        assert selected == {row["digest"] for row in plan["imports"]}
        assert selected <= {
            row["media_id"] for row in receipt["publication"]["dependency_manifest"]["media"]
        }
        assert "local_path" not in json.dumps(saved_internal["payload"])
        assert str(tmp_path) not in json.dumps(saved_internal["payload"])
        reopened = open_authoring_bundle(
            saved_parent, shot_revisions=[saved_shot], internal_timeline_revisions=[saved_internal]
        )
        assert diff_authoring_candidate(reopened)["change_count"] == 0
    finally:
        service.close()


@pytest.mark.integration
def test_real_runtime_document_script_same_alias_rebind_reload_undo_retry_and_aba(
    tmp_path, monkeypatch, real_runtime, capsys,
):
    """Publish one native same-alias rebind through the real Runtime door.

    The source closure is read from the exact ordinary014 Runtime fixture, while all
    mutable setup lives in a fresh realm and crosses ``trusted_seed``. The
    document script then owns checkout, import, native capture and commit.
    """
    from contextlib import nullcontext

    from astrid.sdk import AstridClient
    from astrid.sdk.timeline.capture import capture_move
    from astrid.sdk.timeline.store import fetch_bundle
    from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
    from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError
    from tests.helpers.runtime import use_runtime
    from tests.helpers.trusted_seed import seed_trusted_parent

    monkeypatch.setenv('ASTRID_RUNTIME_CHECKOUT', '/home/user/work/wt/rt-pin-8ea0766f')
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    source_runtime = use_runtime(commit='a0dca16975dfe8f5e762703b99d9791a16cf7fd7')
    sys.path.insert(0, str(source_runtime / 'tests'))
    from timeline_commit_film_fixture import build_film_fixture, read_object_from_realm

    source_root = tmp_path / 'source'
    source_root.mkdir()
    source_fixture = build_film_fixture(source_root)
    source = RuntimeService(source_fixture.duplicate_root)
    try:
        source_document = source._timeline_commits()[0].revision_closure(
            source_fixture.project_id, source_fixture.timeline_id,
            source_fixture.parent_revision_id,
        )
        source_objects = [
            (row['digest'], row['media_type'], row['original_name'])
            for row in source.store.conn.execute(
                'SELECT digest, media_type, original_name FROM objects'
            )
        ]
    finally:
        source.close()

    realm = tmp_path / 'realm'
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    project = service.create_project(
        {'slug': 'wc6-native', 'name': 'WC6 native'}, idempotency_key='wc6-project'
    )['id']
    service.create_timeline(project, 'film', idempotency_key='wc6-timeline')

    for digest, media_type, original_name in source_objects:
        service.ingest(
            project,
            read_object_from_realm(source_fixture.duplicate_root, 'sha256:' + digest),
            media_type=media_type,
            original_name=original_name,
            idempotency_key='wc6-source-' + digest,
        )

    old_path = picture(tmp_path, 'old.png', 'red')
    new_path = picture(tmp_path, 'new.png', 'blue')
    old_bytes = old_path.read_bytes()
    old_digest = 'sha256:' + hashlib.sha256(old_bytes).hexdigest()
    old_import = service.import_media(
        project, old_bytes, media_type='image/png', original_name='old.png',
        expected_digest=old_digest, actor_id='trusted-test-setup', width=16,
        height=12, idempotency_key='wc6-old',
    )['data']
    old_media = service.get_project_media_selection(
        project,
        {'asset': 'picture-1', 'generation_id': old_import['generation_id'],
         'variant_id': old_import['variant_id']},
        authorize=lambda: None,
    )

    parent = copy.deepcopy(source_document['parent_revision']['payload'])
    shots = copy.deepcopy(source_document['shot_revisions'])
    internals = copy.deepcopy(source_document['internal_timeline_revisions'])
    first_shot = next(row for row in shots if row['shot_id'] == 'shot-1')
    old_internal_id = first_shot['internal_timeline_revision_id']
    first_internal = next(row for row in internals if row['revision_id'] == old_internal_id)
    registry = first_internal['payload']['registry']['assets']
    original_alias_order = list(registry)
    original_opaque = {'keep': True}
    registry['picture-1'].update(
        media_id=old_media['object_id'], type='image',
        generationId=old_media['generation_id'], variantId=old_media['variant_id'],
        opaque={'keep': True},
    )
    registry['picture-1'].pop('object_id', None)
    first_internal['payload']['output'] = {'fps': 24}
    first_internal['payload']['clips'][0]['hold'] = 0.9
    first_internal['payload']['clips'].append({
        'asset': 'picture-1', 'at': 0.9, 'clipType': 'media', 'hold': 0.9,
        'id': 'clip-1b', 'track': 'picture',
    })
    first_shot['payload']['items'] = [
        {'item_id': 'picture-item', 'media_id': old_media['object_id'], 'opaque': {'keep': True}}
    ]
    first_shot['payload']['assets'] = [{
        'asset_id': 'picture-1', 'object_id': old_media['object_id'], 'opaque': {'keep': True},
        'type': 'image', 'media_type': 'image/png',
        'source': {
            'media_id': old_media['object_id'],
            'content_sha256': old_digest.removeprefix('sha256:'),
            'type': 'image/png',
        },
    }]
    first_shot['payload']['opaque_namespace'] = {'media': old_media['object_id']}
    first_shot['payload']['internal_timeline_revision_id'] = old_internal_id

    # The real text-binding API only creates bindings for unplaced catalog
    # shots.  Persist this exact child closure on a private empty timeline so
    # the normal catalog writer can create the authoritative voiceover before
    # the accepted film baseline exists.  This is trusted fixture setup only;
    # the proof below still publishes through the native document route.
    from runtime_protocol.timeline_commit_store import digest

    voiceover_text = b'hello'
    voiceover_media_id = 'sha256:' + hashlib.sha256(voiceover_text).hexdigest()
    voiceover_binding_id = RuntimeService._text_binding_id(
        project, 'shot-1', 'voiceover_script', None
    )
    voiceover_pin = {
        'binding_id': voiceover_binding_id,
        'project_id': project,
        'shot_id': 'shot-1',
        'kind': 'voiceover_script',
        'slot': None,
        'media_id': voiceover_media_id,
        'event_stream_id': voiceover_binding_id + ':shot.text_binding',
        'head': 1,
        'content_hash': voiceover_media_id,
        'mime_type': 'text/plain',
        'byte_size': len(voiceover_text),
        'text': voiceover_text.decode('utf-8'),
    }
    first_shot['payload']['text_bindings'] = [
        *(first_shot['payload'].get('text_bindings') or []), voiceover_pin,
    ]
    first_internal['content_digest'] = digest(first_internal['payload'])
    first_shot['content_digest'] = digest(first_shot['payload'])
    service.create_timeline(project, 'film-bootstrap', idempotency_key='wc6-bootstrap-timeline')
    commits, _ = service._timeline_commits()
    commits.bootstrap(project, 'film-bootstrap', authorize=lambda: None)
    bootstrap = commits.revision_closure(
        project, 'film-bootstrap', commits.head(project, 'film-bootstrap')['revision_id']
    )
    bootstrap['shot_revisions'] = copy.deepcopy(shots)
    bootstrap['internal_timeline_revisions'] = copy.deepcopy(internals)
    with service.store._transaction():
        commits._persist_closure(bootstrap)
    returned_voiceover = service.set_project_shot_text_binding(
        project,
        {
            'shot_id': 'shot-1', 'kind': 'voiceover_script', 'text': voiceover_text,
            'expected_head': 0,
        },
        idempotency_key='wc6-voiceover-binding',
    )['data']
    assert {
        key: returned_voiceover[key]
        for key in ('binding_id', 'project_id', 'shot_id', 'kind', 'slot', 'media_id',
                    'event_stream_id', 'head', 'content_hash', 'mime_type', 'byte_size')
    } == {key: voiceover_pin[key] for key in (
        'binding_id', 'project_id', 'shot_id', 'kind', 'slot', 'media_id',
        'event_stream_id', 'head', 'content_hash', 'mime_type', 'byte_size'
    )}
    assert service.list_project_shot_text_bindings(
        project, shot_id='shot-1', kind='voiceover_script'
    )['items'] == [returned_voiceover]
    assert service.get_project_shot_text_binding(
        project, returned_voiceover['binding_id']
    ) == returned_voiceover
    assert service.cas.read(returned_voiceover['media_id'].removeprefix('sha256:')) == voiceover_text

    baseline_head = service.get_project_timeline(project, 'film')['head_revision_id']
    seeded = seed_trusted_parent(
        service,
        {
            'project_id': project, 'timeline_id': 'film', 'expected_head': baseline_head,
            'parent_revision_id': 'wc6-baseline', 'parent_composition': parent,
            'shot_revisions': shots, 'internal_timeline_revisions': internals,
        },
        idempotency_key='wc6-baseline',
    )
    baseline_publication = seeded['receipt']['receipt_id']
    commits, _ = service._timeline_commits()
    baseline = commits.revision_closure(project, 'film', 'wc6-baseline')
    service.close()

    daemon = real_runtime(realm, authoring_profile='astrid')

    def open_client(endpoint_daemon):
        return AstridClient.open(
            endpoint=endpoint_daemon.endpoint,
            credential=endpoint_daemon.credential_path,
            realm_id=endpoint_daemon.service.realm['id'],
            actor_id='owner', client_name='wc6-native-proof', client_version='1',
            protocol_version='workspace.v1',
        )

    client = open_client(daemon)
    transport = WorkspaceClient(daemon.endpoint, daemon.credential_path)
    authoritative_voiceover = client.shots.show_text_binding(
        project, returned_voiceover['binding_id'], include_text=True
    )
    assert authoritative_voiceover.ok
    assert authoritative_voiceover.data == {
        **returned_voiceover, 'text': voiceover_pin['text']
    }
    script = Path(__file__).parents[2] / 'astrid/packs/rendering/docs/scripts/timeline_document.py'
    spec = importlib.util.spec_from_file_location('timeline_document_wc6_native_test', script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    monkeypatch.setattr(helper.AstridClient, 'open_from_launcher', lambda **_: nullcontext(client))
    monkeypatch.setattr(helper, 'workspace', lambda: transport)

    def run_script(*args):
        monkeypatch.setattr(sys, 'argv', [str(script), *args])
        helper.main()
        capsys.readouterr()

    document = tmp_path / 'timeline.json'
    run_script('checkout', '--project', project, '--timeline', 'film', '--file', str(document))
    checkout = json.loads(document.read_text())
    first_id = first_shot['shot_id']
    checkout_shot = checkout['shots'][first_id]
    checkout_timeline = checkout_shot['internal_timeline']
    checkout_clip = next(row for row in checkout_timeline['clips'] if row['id'] == 'clip-1')
    original_narration = copy.deepcopy(checkout_shot['payload'].get('text_bindings'))
    assert original_narration
    assert next(
        row for row in original_narration if row['kind'] == 'voiceover_script'
    ) == voiceover_pin
    original_metadata = copy.deepcopy(checkout_shot['payload'].get('metadata'))
    original_source_window = {
        key: checkout_clip.get(key) for key in ('from', 'to', 'source_window')
    }
    checkout_clip.update(at=0.125, hold=0.75)
    checkout_timeline['registry']['assets']['picture-1']['local_path'] = new_path.name
    document.write_text(json.dumps(checkout))

    base_bundle = fetch_bundle(
        project, 'film', revision_id='wc6-baseline',
        publication_id=baseline_publication, client=client,
    )
    assert base_bundle['base_parent'] == {
        'revision_id': baseline['parent_revision']['revision_id'],
        'content_digest': baseline['parent_revision']['content_digest'],
        'publication_id': baseline_publication,
    }
    assert checkout['base_parent'] == base_bundle['base_parent']
    assert base_bundle['source_mapping']['_runtime_document'] == baseline
    placement = next(row for row in base_bundle['placements'] if row['shot_id'] == first_id)
    raw_occurrence = next(
        row for row in baseline['parent_revision']['payload']['occurrences']
        if row['occurrence_id'] == placement['occurrence_id']
    )
    raw_shot = next(
        row for row in baseline['shot_revisions']
        if (row['shot_id'], row['revision_id'])
        == (raw_occurrence['shot_id'], raw_occurrence['shot_revision_id'])
    )
    raw_internal = next(
        row for row in baseline['internal_timeline_revisions']
        if row['revision_id'] == raw_shot['internal_timeline_revision_id']
    )
    assert base_bundle['source_mapping']['shots'][first_id] == {
        'shot_id': raw_shot['shot_id'],
        'revision_id': raw_shot['revision_id'],
        'content_digest': raw_shot['content_digest'],
        'internal_timeline_id': raw_internal['timeline_id'],
        'internal_timeline_revision_id': raw_internal['revision_id'],
        'internal_timeline_content_digest': raw_internal['content_digest'],
        'item_id_map': {},
    }
    assert checkout['source_mapping']['shots'][first_id] == base_bundle['source_mapping']['shots'][first_id]
    raw_before = next(row for row in raw_internal['payload']['clips'] if row['id'] == 'clip-1')
    assert raw_before == next(row for row in first_internal['payload']['clips'] if row['id'] == 'clip-1')
    working_before = next(
        row for row in base_bundle['shots'][first_id]['internal_timeline']['clips']
        if row['id'] == 'clip-1'
    )
    assert working_before == raw_before
    disjoint_session = TimelineSession.open(
        project, 'film', client=client, outbox_path=tmp_path / 'disjoint.outbox.json',
        base_revision_id='wc6-baseline', base_publication_id=baseline_publication,
    )
    run_script('publish', '--file', str(document), '--idempotency-key', 'wc6-publish')
    publication_file = document.with_suffix('.publication.json')
    publication_receipt = json.loads(publication_file.read_text())
    publication = publication_receipt['publication']
    assert publication['data']['status'] == 'accepted'
    assert [command['op'] for command in publication['data']['commands']] == [
        'occurrence.swap-media', 'doc.patch',
    ]
    changes = publication['data']['commands'][1]['args']['changes']
    assert [(change['op'], change['path'], change['before'], change['value'])
            for change in changes] == [
        ('set', ['clips', 'id:clip-1', 'at'],
         {'state': 'present', 'value': 0}, 0.125),
        ('set', ['clips', 'id:clip-1', 'hold'],
         {'state': 'present', 'value': working_before['hold']}, 0.75),
    ]
    assert changes[0]['ref'] == changes[1]['ref']
    published_document = publication['data']['canonical_document']
    new_digest = 'sha256:' + hashlib.sha256(new_path.read_bytes()).hexdigest()
    published_internal = next(
        row for row in published_document['internal_timeline_revisions']
        if row['revision_id'] == published_document['shot_revisions'][0]['internal_timeline_revision_id']
    )
    published_clip = next(row for row in published_internal['payload']['clips'] if row['id'] == 'clip-1')
    assert published_clip['at'] == 0.125
    assert published_clip['hold'] == 0.75
    assert {key: published_clip.get(key) for key in ('from', 'to', 'source_window')} == original_source_window
    published_entry = published_internal['payload']['registry']['assets']['picture-1']
    assert published_entry['media_id'] == new_digest
    assert list(published_internal['payload']['registry']['assets']) == original_alias_order
    assert published_entry['opaque'] == original_opaque
    assert [row['id'] for row in published_internal['payload']['clips']] == ['clip-1', 'clip-1b']
    published_shot = next(row for row in published_document['shot_revisions'] if row['shot_id'] == first_id)
    assert published_shot['payload']['items'][0]['media_id'] == new_digest
    assert published_shot['payload']['assets'][0]['source']['media_id'] == new_digest
    assert published_shot['payload']['opaque_namespace']['media'] == old_media['object_id']
    assert published_shot['payload']['text_bindings'] == original_narration
    assert next(
        row for row in published_shot['payload']['text_bindings']
        if row['kind'] == 'voiceover_script'
    ) == voiceover_pin
    assert published_shot['payload']['metadata'] == original_metadata
    second_shot = next(row for row in published_document['shot_revisions'] if row['shot_id'] == 'shot-2')
    baseline_second = next(row for row in baseline['shot_revisions'] if row['shot_id'] == 'shot-2')
    assert second_shot == baseline_second
    records = publication['data']['commands']
    media_record = next(record for record in records if record['op'] == 'occurrence.swap-media')
    assert media_record['args']['old_media']['object_id'] == old_media['object_id']
    assert media_record['args']['media']['object_id'] == new_digest
    history = client.timelines.history(project, 'film', limit=100).data
    assert any(row['receipt']['receipt_id'] == publication['receipt']['receipt_id'] for row in history['items'])

    reloaded = tmp_path / 'reloaded.json'
    run_script('checkout', '--project', project, '--timeline', 'film', '--file', str(reloaded))
    reloaded_document = json.loads(reloaded.read_text())
    assert reloaded_document['base_parent']['revision_id'] == published_document['parent_revision']['revision_id']
    assert reloaded_document['shots'][first_id]['internal_timeline']['registry']['assets']['picture-1']['media_id'] == new_digest
    assert reloaded_document['shots'][first_id]['payload']['text_bindings'] == original_narration
    assert next(
        row for row in reloaded_document['shots'][first_id]['payload']['text_bindings']
        if row['kind'] == 'voiceover_script'
    ) == voiceover_pin

    from runtime_protocol.server import RuntimeHandler

    original_send = RuntimeHandler._send
    lost = {'done': False}
    undo_wire = []

    def lose_undo_ack(handler, status, payload=None, **kwargs):
        if (status == 200 and handler.command == 'POST' and handler.path.endswith('/commit')
                and handler.headers.get('Idempotency-Key') == 'wc6-undo'
                and getattr(handler, '_defer_response', False) and not lost['done']):
            undo_wire.append({
                'key': handler.headers['Idempotency-Key'],
                'body': handler._prefetched_body,
            })
            lost['done'] = True
            # Match the M1 real-HTTP seam: commit_timeline has durably
            # accepted the edit, but no response bytes have been sent.
            raise ConnectionError('WC6 proof dropped undo response after durable acceptance')
        if (handler.command == 'POST' and handler.path.endswith('/commit')
                and handler.headers.get('Idempotency-Key') == 'wc6-undo'
                and getattr(handler, '_defer_response', False)):
            undo_wire.append({
                'key': handler.headers['Idempotency-Key'],
                'body': handler._prefetched_body,
            })
        return original_send(handler, status, payload, **kwargs)

    monkeypatch.setattr(RuntimeHandler, '_send', lose_undo_ack)
    undo_outbox = tmp_path / 'undo.outbox.json'
    undo_session = TimelineSession.open(
        project, 'film', client=client, outbox_path=undo_outbox,
    )
    with pytest.raises(WorkspaceClientError) as transport_error:
        undo_session.undo([publication['receipt']['receipt_id']], idempotency_key='wc6-undo')
    assert transport_error.value.status == 0
    assert transport_error.value.code == 'transport_error'
    flight = json.loads(undo_outbox.read_text())['flight']
    assert flight['key'] == 'wc6-undo'
    assert flight['body'].encode() == undo_wire[0]['body']
    restart_port = daemon.httpd.server_port
    daemon.stop()
    # Keep the stale disjoint session on the same HTTP endpoint across the
    # restart; the M1 real-HTTP proof uses this convention as well.
    restarted = real_runtime(
        realm, port=restart_port,
        authoring_profile='astrid',
    )
    client2 = open_client(restarted)
    retry_session = TimelineSession.open(
        project, 'film', client=client2, outbox_path=undo_outbox,
    )
    undone = retry_session.flush(idempotency_key='wc6-undo')
    assert undone['data']['canonical_document'] == baseline
    assert lost['done'] is True
    assert len(undo_wire) == 2
    assert undo_wire[1] == undo_wire[0]
    undo_receipt = undone['receipt']
    assert undo_receipt['receipt_id'] == undone['data']['accepted_publication_id']
    undo_history = client2.timelines.history(project, 'film', limit=100).data
    assert undo_history['head']['revision_id'] == undone['data']['accepted_revision_id']
    assert undo_history['head']['publication_id'] == undo_receipt['receipt_id']
    assert any(
        row['publication_id'] == undo_receipt['receipt_id']
        and row['receipt']['receipt_id'] == undo_receipt['receipt_id']
        for row in undo_history['items']
    )

    # This stale session is intentionally committed after undo.  It is
    # unrelated to the media occurrence and must rebase without changing the
    # exact undo result.
    disjoint = disjoint_session.commit_many(
        [capture_move(base_bundle, 'parent/clip:music-bed', 1)],
        idempotency_key='wc6-disjoint', via='python',
    )
    assert disjoint['data']['status'] == 'rebased'
    disjoint_document = disjoint['data']['canonical_document']
    assert disjoint_document['parent_revision']['payload']['clips'][0]['at'] == 1

    bad = tmp_path / 'bad.json'
    run_script('checkout', '--project', project, '--timeline', 'film', '--file', str(bad))
    bad_document = json.loads(bad.read_text())
    bad_document['shots'][first_id]['internal_timeline']['registry']['assets']['picture-1']['local_path'] = new_path.name
    bad_document['shots'][first_id]['payload']['opaque_namespace']['media'] = 'changed-opaque-value'
    bad.write_text(json.dumps(bad_document))
    before_bad = client2.timelines.history(project, 'film', limit=100).data
    monkeypatch.setattr(helper.AstridClient, 'open_from_launcher', lambda **_: nullcontext(client2))
    monkeypatch.setattr(helper, 'workspace', lambda: WorkspaceClient(restarted.endpoint, restarted.credential_path))
    with pytest.raises(RuntimeError, match='document publish is held'):
        run_script('publish', '--file', str(bad), '--idempotency-key', 'wc6-bad')
    assert client2.timelines.history(project, 'film', limit=100).data == before_bad

    # A→B→A remains an intervening write: the original publication cannot be
    # undone just because the visible media selection returned to A.
    redo_session = TimelineSession.open(
        project, 'film', client=client2, outbox_path=tmp_path / 'redo.outbox.json',
    )
    # Re-read the exact Runtime-verified inputs after restart; this preserves
    # the F1 media guard while replaying the unchanged command record.
    redo_verified_media = [
        transport.get_project_verified_media(project, media_record['args'][key])
        for key in ('old_media', 'media')
    ]
    redone = redo_session.commit_many(
        records, idempotency_key='wc6-aba-rebind', via='python',
        verified_media=redo_verified_media,
    )
    redone_document = redone['data']['canonical_document']
    assert redone_document['shot_revisions'][0]['payload']['items'][0]['media_id'] == new_digest
    redone_internal = next(
        row for row in redone_document['internal_timeline_revisions']
        if row['revision_id'] == redone_document['shot_revisions'][0]['internal_timeline_revision_id']
    )
    redone_clip = next(row for row in redone_internal['payload']['clips'] if row['id'] == 'clip-1')
    assert (redone_clip['at'], redone_clip['hold']) == (0.125, 0.75)
    aba_before = client2.timelines.history(project, 'film', limit=100).data
    with pytest.raises(CommitConflict) as conflict:
        redo_session.undo([publication['receipt']['receipt_id']], idempotency_key='wc6-aba-undo')
    assert conflict.value.failure['details']['status'] == 'conflict'
    assert client2.timelines.history(project, 'film', limit=100).data == aba_before



def test_inherited_explicit_local_path_remains_an_active_request(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    shot = next(iter(work["shots"].values()))
    for timeline in (shot["base_internal_timeline"], shot["internal_timeline"]):
        timeline["registry"]["assets"]["old-picture"]["local_path"] = path.name
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert len(plan["imports"]) == 1
    assert plan["input_candidate"] == work
    assert plan["base_directory"] == str(tmp_path.resolve())
    assert "local_path" not in next(iter(plan["candidate"]["shots"].values()))[
        "internal_timeline"
    ]["registry"]["assets"]["old-picture"]


def test_local_shot_item_intent_matches_selected_internal_replacement(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    shot = edit(work, path.name)
    shot["payload"]["items"][0]["media_id"] = path.name
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert len(plan["imports"]) == 1
    prepared = next(iter(plan["candidate"]["shots"].values()))
    assert prepared["payload"]["items"][0]["media_id"] == plan["imports"][0]["digest"]


def test_unchanged_historical_and_opaque_paths_survive_without_import(tmp_path):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    for timeline in (shot["base_internal_timeline"], shot["internal_timeline"]):
        timeline["registry"]["assets"]["old-picture"]["file"] = "historical.png"
        timeline["clips"][0]["src"] = "historical.png"
    for payload in (shot["base_payload"], shot["payload"]):
        payload["items"][0]["src"] = "historical.png"
        payload["generation_inputs"]["visual"]["src"] = "historical-input.png"
    shot["payload"]["metadata"]["opaque_source"] = {"path": "arbitrary.png"}
    original = copy.deepcopy(work)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert plan["imports"] == []
    assert plan["candidate"] == original


def test_historical_path_rows_match_by_identity_after_reorder(tmp_path):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    for payload in (shot["base_payload"], shot["payload"]):
        payload["items"][0]["src"] = "historical-picture.png"
        payload["items"][1]["src"] = "historical-audio.wav"
    shot["payload"]["items"].reverse()
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert plan["imports"] == []
    assert plan["candidate"]["shots"][shot["shot_id"]]["payload"]["items"] == shot[
        "payload"
    ]["items"]


@pytest.mark.parametrize("locator_first", [True, False])
def test_duplicate_asset_row_with_one_live_locator_refuses_in_both_orders(tmp_path, locator_first):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    located = {"asset_id": "duplicate", "source": {"file": "new.png"}}
    pathless = {"asset_id": "duplicate"}
    shot["payload"]["assets"] = (
        [located, pathless] if locator_first else [pathless, located]
    )
    with pytest.raises(ValueError, match=r"Ambiguous media locator baseline at .*payload\.assets"):
        plan_authoring_media(work, base_directory=tmp_path)


def test_renamed_or_duplicate_media_row_cannot_borrow_a_historical_baseline(tmp_path):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    for timeline in (shot["base_internal_timeline"], shot["internal_timeline"]):
        timeline["clips"][0]["src"] = "historical.png"
    shot["internal_timeline"]["clips"][0]["id"] = "renamed-picture"
    with pytest.raises(ValueError, match="Unconsumed media locator"):
        plan_authoring_media(work, base_directory=tmp_path)
    shot["internal_timeline"]["clips"][0]["id"] = "picture-1"
    duplicate = copy.deepcopy(shot["internal_timeline"]["clips"][0])
    shot["internal_timeline"]["clips"].append(duplicate)
    with pytest.raises(ValueError, match="Ambiguous media locator baseline"):
        plan_authoring_media(work, base_directory=tmp_path)


@pytest.mark.parametrize("location", [
    "clip_src", "item_local_path", "asset_source_file", "audio_binding_path",
    "registry_source_file", "parent_asset_file", "audio_binding_object_id",
    "asset_object_id", "registry_media_id",
])
def test_changed_unconsumed_live_locators_fail_during_planning(tmp_path, location):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    if location == "clip_src":
        shot["internal_timeline"]["clips"][0]["src"] = "new.png"
    elif location == "item_local_path":
        shot["payload"]["items"][0]["local_path"] = "new.png"
    elif location == "asset_source_file":
        shot["payload"]["assets"] = [{
            "asset_id": "old-picture", "object_id": OLD,
            "source": {"file": "new.png"},
        }]
    elif location == "audio_binding_path":
        shot["payload"]["audio_bindings"][0]["path"] = "new.png"
    elif location == "registry_source_file":
        shot["internal_timeline"]["registry"]["assets"]["old-picture"]["source"] = {
            "file": "new.png"
        }
    elif location == "parent_asset_file":
        work["parent"]["assets"] = [{"asset_id": "direct", "file": "new.png"}]
    elif location == "asset_object_id":
        shot["payload"]["assets"] = [{"asset_id": "old-picture", "object_id": "new.png"}]
    elif location == "registry_media_id":
        shot["internal_timeline"]["registry"]["assets"]["old-picture"]["media_id"] = "new.png"
    else:
        shot["payload"]["audio_bindings"][0]["object_id"] = "new.png"
    original = copy.deepcopy(work)
    with pytest.raises(ValueError, match="Unconsumed media locator"):
        plan_authoring_media(work, base_directory=tmp_path)
    assert work == original


def test_changed_remote_live_locator_refuses_while_opaque_remote_url_survives(tmp_path):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    shot["payload"]["metadata"]["opaque_url"] = "https://example.com/preserved.png"
    shot["internal_timeline"]["clips"][0]["src"] = "https://example.com/new.png"
    with pytest.raises(ValueError, match="Unconsumed media locator"):
        plan_authoring_media(work, base_directory=tmp_path)
    del shot["internal_timeline"]["clips"][0]["src"]
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert plan["imports"] == []
    assert next(iter(plan["candidate"]["shots"].values()))["payload"]["metadata"][
        "opaque_url"
    ] == "https://example.com/preserved.png"


@pytest.mark.parametrize("bad", [False, 0, [], {}])
def test_nonstring_registry_locator_is_malformed_even_when_falsy(tmp_path, bad):
    work = bundle()
    edit(work, picture(tmp_path).name)
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["registry"]["assets"]["old-picture"]["file"] = bad
    with pytest.raises(ValueError, match="Malformed media locator"):
        plan_authoring_media(work, base_directory=tmp_path)


@pytest.mark.parametrize("field", ["asset", "asset_id", "media_id", "object_id"])
@pytest.mark.parametrize("bad", [False, 0, [], {}])
def test_nonstring_clip_selector_has_field_specific_refusal(tmp_path, field, bad):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["clips"][0][field] = bad
    with pytest.raises(ValueError, match=rf"Malformed media selector at .*clips.*\.{field}"):
        plan_authoring_media(work, base_directory=tmp_path)


@pytest.mark.parametrize("parent_registry_location", ["direct", "config"])
def test_effective_parent_alias_needs_no_local_file_or_import(tmp_path, parent_registry_location):
    work = bundle()
    parent = work["parent"]
    if parent_registry_location == "config":
        parent["config"]["registry"] = parent.pop("registry")
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["clips"][0]["asset"] = "direct"
    assert not (tmp_path / "direct").exists()
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert plan["imports"] == []
    assert plan["candidate"] == work


def test_parent_config_clip_uses_effective_parent_registry_alias(tmp_path):
    work = bundle()
    parent = work["parent"]
    parent["registry"]["assets"]["alternate"] = {"media_id": OLD}
    parent["config"]["clips"][0]["asset"] = "alternate"
    assert not (tmp_path / "alternate").exists()
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert plan["imports"] == []
    assert plan["candidate"] == work


def test_explicit_parent_registry_replacement_normalizes_stale_declarations(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    parent = work["parent"]
    parent["config"]["registry"] = copy.deepcopy(parent["registry"])
    direct = parent["registry"]["assets"]["direct"]
    nested = parent["config"]["registry"]["assets"]["direct"]
    direct.update(local_path=path.name, src="https://a.example/stale.png")
    nested.update(local_path=path.name, src="https://b.example/stale.png")
    original = copy.deepcopy(work)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert len(plan["imports"]) == 1
    prepared = plan["candidate"]["parent"]
    assert prepared["registry"] == prepared["config"]["registry"]
    projected = prepared["registry"]["assets"]["direct"]
    assert projected["media_id"] == plan["imports"][0]["digest"]
    assert "local_path" not in projected and "src" not in projected
    assert work == original


def test_persistent_parent_registry_conflict_refuses_after_explicit_projection(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    parent = work["parent"]
    parent["config"]["registry"] = copy.deepcopy(parent["registry"])
    direct = parent["registry"]["assets"]["direct"]
    nested = parent["config"]["registry"]["assets"]["direct"]
    direct.update(local_path=path.name, src="https://a.example/stale.png")
    nested.update(local_path=path.name, src="https://b.example/stale.png")
    parent["config"]["registry"]["assets"]["unmatched"] = {"media_id": OLD}
    original = copy.deepcopy(work)
    with pytest.raises(ValueError, match="conflicts"):
        plan_authoring_media(work, base_directory=tmp_path)
    assert work == original


def test_removed_local_alias_is_not_resurrected_from_baseline(tmp_path):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["registry"]["assets"].pop("old-picture")
    with pytest.raises(ValueError, match="missing registry asset"):
        plan_authoring_media(work, base_directory=tmp_path)


@pytest.mark.parametrize("field, value", [
    ("object_id", "urn:media:new"),
    ("asset_id", "unknown-media-token"),
])
def test_unsupported_secondary_clip_selector_refuses_with_valid_primary(tmp_path, field, value):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    clip = shot["internal_timeline"]["clips"][0]
    assert clip["asset"] == "old-picture"
    clip[field] = value
    assert not (tmp_path / value).exists()
    original = copy.deepcopy(work)
    with pytest.raises(
        ValueError,
        match=rf"Unconsumed media selector at .*clips\['picture-1'\]\.{field}",
    ):
        plan_authoring_media(work, base_directory=tmp_path)
    assert work == original


@pytest.mark.parametrize("bad", [False, 0, [], {}])
def test_nonstring_shot_item_selector_has_field_specific_refusal(tmp_path, bad):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    shot["payload"]["items"][0]["media_id"] = bad
    with pytest.raises(ValueError, match=r"Malformed media selector at .*payload\.items.*\.media_id"):
        plan_authoring_media(work, base_directory=tmp_path)


@pytest.mark.parametrize("clear", [None, ""])
def test_cleared_shot_item_selector_never_requests_local_input(tmp_path, clear, monkeypatch):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    shot["payload"]["items"][0]["media_id"] = clear

    def unexpected_path(*args, **kwargs):
        pytest.fail("a cleared shot-item media_id must not request a local file")

    monkeypatch.setattr("astrid.sdk.authoring_media._path", unexpected_path)
    with pytest.raises(ValueError, match="must be a SHA-256 media identity"):
        plan_authoring_media(work, base_directory=tmp_path)


@pytest.mark.parametrize("clear", [None, ""])
def test_cleared_optional_path_is_not_a_new_import_request(tmp_path, clear):
    work = bundle()
    shot = next(iter(work["shots"].values()))
    shot["internal_timeline"]["registry"]["assets"]["old-picture"]["file"] = clear
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert plan["imports"] == []


def test_equal_changed_sources_choose_one_file_but_conflicts_fail(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    entry = next(iter(work["shots"].values()))["internal_timeline"]["registry"]["assets"][
        "old-picture"
    ]
    entry["file"] = entry["path"] = path.name
    assert len(plan_authoring_media(work, base_directory=tmp_path)["imports"]) == 1
    entry["path"] = "other.png"
    with pytest.raises(ValueError, match="Ambiguous changed registry media locators"):
        plan_authoring_media(work, base_directory=tmp_path)


def test_canonical_file_uri_is_supported_and_malformed_or_remote_forms_fail(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    edit(work, path.as_uri())
    assert len(plan_authoring_media(work, base_directory=tmp_path)["imports"]) == 1
    edit(work, "file://localhost" + path.as_uri().removeprefix("file://"))
    assert len(plan_authoring_media(work, base_directory=tmp_path)["imports"]) == 1
    entry = next(iter(work["shots"].values()))["internal_timeline"]["registry"]["assets"][
        "old-picture"
    ]
    for bad in ("file:relative.png", "file:" + str(path),
                "file://remotehost/tmp/x.png",
                "file:///tmp/has%ZZ", "file:///tmp/x.png?copy=1", "https://example.com/x.png"):
        entry["local_path"] = bad
        with pytest.raises(ValueError):
            plan_authoring_media(work, base_directory=tmp_path)


def test_explicit_local_replacement_strips_stale_remote_descriptor(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    shot = edit(work, path.name)
    entry = shot["internal_timeline"]["registry"]["assets"]["old-picture"]
    entry["src"] = "https://example.com/old-picture.png"
    entry["source"] = {"file": "file:///obsolete/old-picture.png"}
    plan = plan_authoring_media(work, base_directory=tmp_path)
    assert len(plan["imports"]) == 1
    prepared = next(iter(plan["candidate"]["shots"].values()))["internal_timeline"][
        "registry"
    ]["assets"]["old-picture"]
    assert "src" not in prepared and "source" not in prepared


def test_supplied_plan_is_rechecked_before_catalog_read_or_import(tmp_path):
    path = picture(tmp_path)
    work = bundle()
    edit(work, path.name)
    plan = plan_authoring_media(work, base_directory=tmp_path)

    class SpyCatalog(Catalog):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def list_generations(self, project_id, **kwargs):
            self.reads += 1
            return super().list_generations(project_id, **kwargs)

    for mutate in ("projected", "imports", "missing_context"):
        altered = copy.deepcopy(plan)
        if mutate == "projected":
            next(iter(altered["candidate"]["shots"].values()))["payload"]["items"][0][
                "src"
            ] = "not-consumed.png"
        elif mutate == "imports":
            altered["imports"][0]["filename"] = "wrong.png"
        else:
            del altered["input_candidate"]
        catalog = SpyCatalog()
        with pytest.raises(ValueError):
            import_authoring_media(altered, catalog)
        assert catalog.reads == 0
        assert catalog.calls == []


def test_catalog_import_remains_reusable_after_later_parent_cas_conflict(tmp_path):
    # Predicate mapping: the retired whole-composition writer is replaced by
    # one authoring commit_timeline request; the CAS refusal and catalog's
    # idempotent replay/no-second-import guarantees remain exact.
    from astrid.sdk.workspace_client import WorkspaceClientError
    from tests.timeline.test_authoring_bundle import _Writer

    path = picture(tmp_path)
    work = bundle()
    edit(work, path.name)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    catalog = Catalog()
    imported = import_authoring_media(plan, catalog)
    assert len(catalog.calls) == 1

    parent, shots, internals = _closure(shared=False)

    class StaleWriter(_Writer):
        def __init__(self):
            super().__init__(_runtime_document(parent, shots, internals))
            self.requests = []

        def commit_timeline(self, project_id, timeline_id, body, *, idempotency_key):
            self.requests.append((project_id, timeline_id, copy.deepcopy(body), idempotency_key))
            raise WorkspaceClientError(
                409, "conflict", "parent CAS conflict", {"status": "conflict"}
            )

        def get_project_media_selection(self, project_id, selection):
            return {"data": {
                "asset": selection["asset"],
                "generation_id": selection["generation_id"],
                "variant_id": selection["variant_id"],
                "object_id": catalog.calls[0][2]["expected_digest"],
                "catalog_digest": "sha256:" + "c" * 64,
            }}

    writer = StaleWriter()
    with pytest.raises(WorkspaceClientError, match="parent CAS conflict"):
        publish_authoring_candidate(
            imported["candidate"], writer, idempotency_key="parent-conflict"
        )
    assert len(writer.requests) == 1
    project_id, timeline_id, request, key = writer.requests[0]
    assert (project_id, timeline_id, key) == ("project-1", "main", "parent-conflict")
    assert request["api_version"] == 1
    assert request["base_revision_id"] == "parent-1"
    assert request["base_publication_id"] == "publication-1"
    assert request["commands"]
    assert request["label"]
    assert request["steps"]
    assert request["merge"] == "atomic"
    assert "publish_parent_composition" not in dir(writer)
    replay = import_authoring_media(plan, catalog)
    assert replay["imports"][0]["reused"] is True
    assert len(catalog.calls) == 1


@pytest.mark.parametrize("command", ["check", "publish"])
def test_public_document_refuses_unsupported_locator_before_artifacts_or_connection(
    tmp_path, monkeypatch, command
):
    import sys

    script = Path(__file__).parents[2] / "astrid/packs/rendering/docs/scripts/timeline_document.py"
    spec = importlib.util.spec_from_file_location("timeline_document_locator_test", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    work = bundle()
    next(iter(work["shots"].values()))["internal_timeline"]["clips"][0]["src"] = "new.png"
    document = tmp_path / "timeline.json"
    original = json.dumps(work)
    document.write_text(original)
    monkeypatch.setattr(helper, "workspace", lambda: pytest.fail("no Runtime connection expected"))
    monkeypatch.setattr(
        helper.AstridClient, "open_from_launcher",
        lambda **kwargs: pytest.fail("no launcher connection expected"),
    )
    argv = [str(script), command, "--file", str(document)]
    if command == "publish":
        argv.extend(["--idempotency-key", "unsupported-input"])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(ValueError, match="Unconsumed media locator"):
        helper.main()
    assert document.read_text() == original
    for suffix in (".check.json", ".prepared.json", ".imports.json", ".publication.json"):
        assert not document.with_suffix(suffix).exists()
