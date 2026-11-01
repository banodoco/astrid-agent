from __future__ import annotations

import copy
import importlib.util
import json
import shutil
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
from tests.timeline.test_authoring_bundle import AUDIO, OLD, _closure


def bundle():
    parent, shots, internals = _closure(shared=False)
    return open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=internals
    )


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
    import sys
    from contextlib import nullcontext
    from types import SimpleNamespace

    script = Path(__file__).parents[2] / "astrid/packs/rendering/docs/scripts/timeline_document.py"
    spec = importlib.util.spec_from_file_location("timeline_document_test", script)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    path = picture(tmp_path)
    work = bundle()
    edit(work, path.name)
    document = tmp_path / "timeline.json"
    document.write_text(json.dumps(work))
    shown = SimpleNamespace(ok=True, data={"summary": {"head_revision_id": "advanced-head"}})
    client = SimpleNamespace(timelines=SimpleNamespace(open_composition=lambda *args: shown))
    class StaleWorkspace(Catalog):
        def publish_parent_composition(self, *args, **kwargs):
            raise RuntimeError("parent composition head is stale")

    stale_workspace = StaleWorkspace()
    monkeypatch.setattr(
        helper.AstridClient, "open_from_launcher", lambda **kwargs: nullcontext(client)
    )
    monkeypatch.setattr(
        helper, "workspace", lambda: stale_workspace
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(script), "publish", "--file", str(document), "--idempotency-key", "stale-edit"],
    )
    with pytest.raises(RuntimeError, match="parent composition head is stale"):
        helper.main()
    assert document.read_text() == json.dumps(work)
    assert document.with_suffix(".check.json").exists()
    assert document.with_suffix(".imports.json").exists()
    assert document.with_suffix(".prepared.json").exists()
    assert not document.with_suffix(".publication.json").exists()
    assert len(stale_workspace.calls) == 1


@pytest.mark.integration  # a real Runtime from a second checkout
def test_real_runtime_import_and_parent_publication_roundtrip(tmp_path):
    from runtime_protocol import service as service_module
    from runtime_protocol import store as store_module

    root = tmp_path / "isolated-realm"
    store_module.RealmStore.initialize(root).close()
    service = service_module.RuntimeService(root)
    try:
        project = service.create_project(
            {"slug": "checkin-test", "name": "Checkin test"}, idempotency_key="project"
        )
        project_id = project["id"]
        service.create_timeline(project_id, "main", idempotency_key="timeline")
        parent, shots, internals = _closure(shared=False)
        # Real Runtime accepts text only through its registered binding owner.
        # Publish the fixture's voiceover text with that authoritative descriptor.
        service.create_project_shot(
            project_id,
            {"shot_id": shots[0]["shot_id"], "name": "Opening"},
            idempotency_key="shot",
        )
        text_binding = service.set_project_shot_text_binding(
            project_id,
            {
                "shot_id": shots[0]["shot_id"],
                "kind": "voiceover_script",
                "text": "hello",
                "expected_head": 0,
            },
            idempotency_key="voiceover-binding",
        )
        shots[0]["payload"]["text_bindings"] = [
            {**text_binding["data"], "text": "hello"}
        ]
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
        service.publish_parent_composition(project_id, "main", publication, idempotency_key="seed")
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

            def publish_parent_composition(self, project_id, timeline_id, payload, **kwargs):
                return service.publish_parent_composition(
                    project_id, timeline_id, payload, **kwargs
                )["data"]

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
    path = picture(tmp_path)
    work = bundle()
    edit(work, path.name)
    plan = plan_authoring_media(work, base_directory=tmp_path)
    catalog = Catalog()
    imported = import_authoring_media(plan, catalog)
    assert len(catalog.calls) == 1

    class StaleWriter:
        def publish_parent_composition(self, *args, **kwargs):
            raise RuntimeError("parent CAS conflict")

    with pytest.raises(RuntimeError, match="parent CAS conflict"):
        publish_authoring_candidate(
            imported["candidate"], StaleWriter(), idempotency_key="parent-conflict"
        )
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
