"""Native retained closure -> Runtime capture -> host inputs -> normal Root lifetime.

No renderer or successful capture is supplied by these read-only source tests.
"""
from __future__ import annotations

import hashlib
import io
import json
import wave
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.execution.generic_host import HostError
from astrid.core.rendering.verified_source import PreparedTimelineInputs, inherited_source
from astrid.core.timeline.authoring_bundle import open_authoring_bundle, preview_authoring_candidate
from astrid.packs.rendering.actions.timeline_visualize.provenance import snapshot_label
from astrid.sdk.timeline_filmstrip import prepare_filmstrip
from tests.helpers.runtime import use_runtime
from tests.sdk.test_capture_source_runtime import CAPABILITY, CAPABILITY_DIGEST, CaptureRuntime
from tests.timeline.test_authoring_bundle import AUDIO, OLD, _closure, _replace_values

pytestmark = pytest.mark.integration


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@pytest.fixture
def native_runtime(tmp_path):
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from runtime_protocol.timeline_commit_validation import digest

    root = tmp_path / "realm"
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
    try:
        project = service.create_project({"slug": "native-source", "name": "Native source"})["id"]
        service.create_timeline(project, "main", idempotency_key="timeline")
        picture, audio = io.BytesIO(), io.BytesIO()
        Image.new("RGB", (16, 16), (240, 240, 240)).save(picture, format="PNG")
        with wave.open(audio, "wb") as stream:
            stream.setparams((1, 2, 44100, 0, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 44100)
        replacements = {}
        for old, data, media_type in ((OLD, picture.getvalue(), "image/png"),
                                      (AUDIO, audio.getvalue(), "audio/wav")):
            replacements[old] = service.ingest(project, data, media_type=media_type,
                idempotency_key="media-" + old[7:])["data"]["object_id"]
        parent, shots, internals = (_replace_values(part, replacements) for part in _closure(shared=True))
        for row in (parent, *shots, *internals):
            row["project_id"] = project
        shots[0]["payload"]["text_bindings"] = []
        # Select renderable fields before retention; never repair an admitted snapshot.
        parent["payload"]["config"].pop("opaque_parent_config")
        for config in (parent["payload"]["config"], internals[0]["payload"]):
            for entry in (parent["payload"]["registry"] if config is parent["payload"]["config"]
                          else config["registry"])["assets"].values():
                entry["content_sha256"] = entry["media_id"]
            for track in config["tracks"]:
                track["label"] = track["id"]
                track.pop("opaque_track", None)
            for clip in config["clips"]:
                clip.pop("opaque_clip", None)
                clip.pop("rect", None)
        parent["payload"]["config"]["output"] = {"fps": 30, "resolution": "200x100", "file": "source.mp4"}
        for clip in internals[0]["payload"]["clips"]:
            clip.setdefault("app", {})["astrid"] = {"cut": "c08", "layer": clip["id"]}
        for row in (parent, *shots, *internals):
            row["content_digest"] = digest(row["payload"])
        # Same closure shape as the native retained-fixture setup; no history/head writes.
        parent = {key: parent[key] for key in ("revision_id", "payload", "content_digest")}
        shots = [{key: row[key] for key in ("shot_id", "revision_id", "payload", "content_digest",
                                           "internal_timeline_revision_id")} for row in shots]
        internals = [{key: row[key] for key in ("timeline_id", "revision_id", "payload", "content_digest")}
                     for row in internals]
        document = {"schema_version": 1, "project_id": project, "timeline_id": "main",
                    "parent_revision": parent, "shot_revisions": shots, "internal_timeline_revisions": internals,
                    "dependency_manifest": {
                        "shots": [{key: row[key] for key in ("shot_id", "revision_id", "content_digest",
                                                            "internal_timeline_revision_id")} for row in shots],
                        "internal_timelines": [{key: row[key] for key in ("timeline_id", "revision_id", "content_digest")}
                                               for row in internals],
                        "media": sorted([{"media_id": pin, "content_digest": pin} for pin in replacements.values()],
                                        key=lambda row: row["media_id"]), "documents": []}}
        commits = service._timeline_commits()[0]
        initial = commits.head(project, "main")
        # The existing inactive native fixture seam persists retained inputs only.
        with service.store._mutex, service.store._transaction():
            commits._validate_closure(document)
            commits._persist_closure(document)
        assert commits.head(project, "main") == initial
        runtime = CaptureRuntime(service, project)
        runtime.authoring_preview = None
        runtime.document = document
        runtime.commits = commits
        runtime.restore = lambda revision, key: restore(runtime, revision, key)
        accepted = runtime.restore(document["parent_revision"], "restore-r1")
        runtime.r1 = accepted["data"]["accepted_revision_id"]
        assert canonical(accepted["data"]["canonical_document"]) == canonical(document)
        service.register_capability({"capability_id": CAPABILITY, "definition_digest": CAPABILITY_DIGEST})
        service.register_executor({"executor_id": "probe", "capabilities": [CAPABILITY]})
        yield runtime
    finally:
        service.close()


def restore(runtime, revision, key):
    head = runtime.commits.head(runtime.project_id, "main")
    return runtime.service.commit_timeline(runtime.project_id, "main", {
        "api_version": 1, "project_id": runtime.project_id, "timeline_id": "main",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [{"schema_version": 1, "op": "timeline.restore", "version": "1", "select": None,
                      "args": {"revision_id": revision["revision_id"], "content_digest": revision["content_digest"]}}],
    }, idempotency_key=key, publisher="fixture-owner-test",
        invocation_path=f"/v1/projects/{runtime.project_id}/timelines/main/commit",
        authorize=runtime.service._assert_mutation_admitted)


def snapshot(runtime, *, candidate=False):
    inputs = {"composed_capture": True, "timeline_ref": "main", "revision_id": runtime.r1}
    if candidate:
        doc = runtime.document
        parent = runtime.get_project_parent_composition_revision(runtime.project_id, "main", runtime.r1)
        shots = [runtime.get_project_shot_revision(runtime.project_id, row["shot_id"], row["revision_id"])
                 for row in doc["shot_revisions"]]
        internals = [runtime.get_project_timeline_revision(runtime.project_id, "main", row["revision_id"])
                     for row in doc["internal_timeline_revisions"]]
        bundle = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
        bundle["parent"]["config"]["output"]["file"] = "unsaved.mp4"
        runtime.authoring_preview = preview_authoring_candidate(bundle)
        inputs["authoring_preview"] = runtime.authoring_preview
    return prepare_filmstrip(inputs, project=runtime.project_id, client=runtime)["capture_snapshot"]


@pytest.mark.parametrize("candidate", [False, True])
def test_runtime_capture_ref_reaches_existing_host_source(native_runtime, tmp_path, candidate):
    runtime = native_runtime
    original = snapshot(runtime, candidate=candidate)
    raw = canonical(original)
    task = runtime.admit(original, inline=True)
    ref = task["spec"]["spec"]["authority_context"]["capture_snapshot_ref"]
    assert runtime.service.object(ref["digest"])[1] == raw
    values = runtime.materialize(task, tmp_path / "attempt")
    source = values.timeline_source
    assert source is not None
    assert canonical(source.read()["snapshot"]) == raw
    assert canonical(json.loads(Path(values["timeline"]).read_bytes())) == canonical(original["config"])
    source.validate(values["timeline"], values["assets_registry"])
    label = snapshot_label(original, source=source)
    assert label["status"] == ("candidate" if candidate else "committed")
    assert label["base_revision_id"] == (runtime.r1 if candidate else None)
    assert label["actual_revision_id"] == (None if candidate else runtime.r1)
    assert snapshot_label(original)["status"] == "uncertified"
    if candidate:
        assert task["spec"]["spec"]["inputs"]["authoring_preview"] == runtime.authoring_preview
        assert original["config"]["output"]["file"] == "unsaved.mp4"


@pytest.mark.parametrize("changed", ["config", "registry", "expansion", "missing-media"])
def test_forged_capture_refuses_before_any_writes(native_runtime, changed):
    from runtime_protocol.errors import RuntimeErrorBase

    runtime = native_runtime
    value = snapshot(runtime)
    if changed == "config":
        value["config"]["clips"][0]["hold"] = 99
    elif changed == "registry":
        value["registry"]["forged"] = True
    elif changed == "expansion":
        value["metadata"]["capture_authority"]["expansion"]["shots"][0]["content_digest"] = "sha256:" + "0" * 64
    else:
        next(iter(value["registry"]["assets"].values()))["media_id"] = "sha256:" + "0" * 64
    request = runtime.request(value, inline=True)
    before_sql = list(runtime.service.store.conn.iterdump())
    root = Path(runtime.service.store.root)
    before = {str(path.relative_to(root)): path.read_bytes() for directory in ("cas", "staging")
              for path in (root / directory).rglob("*") if path.is_file()}
    with pytest.raises(RuntimeErrorBase):
        runtime.service.create_task(request)
    assert list(runtime.service.store.conn.iterdump()) == before_sql
    assert {str(path.relative_to(root)): path.read_bytes() for directory in ("cas", "staging")
            for path in (root / directory).rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("changed", ["config", "metadata", "registry", "source", "ref", "prepared"])
def test_replacement_refuses_before_label(native_runtime, tmp_path, monkeypatch, changed):
    runtime = native_runtime
    original = snapshot(runtime)
    task = runtime.admit(original, inline=True)
    values = runtime.materialize(task, tmp_path / "attempt")
    source = values.timeline_source
    value = deepcopy(original)
    if changed in {"config", "metadata", "registry"}:
        value[changed]["forged"] = True
    elif changed == "source":
        source = source.read()["ref"]
    elif changed == "ref":
        forged = deepcopy(task["spec"])
        forged["spec"]["authority_context"]["capture_snapshot_ref"]["revision_id"] = "R2"
        with pytest.raises(HostError):
            runtime.materialize({"spec": forged}, tmp_path / "forged-attempt")
        return
    else:
        env = {}
        PreparedTimelineInputs.handoff_source(values, env)
        monkeypatch.setenv("ASTRID_VERIFIED_TIMELINE_SOURCE", env["ASTRID_VERIFIED_TIMELINE_SOURCE"])
        Path(values["timeline"]).write_text("{}")
        with pytest.raises(ValueError, match="prepared timeline bytes differ"):
            inherited_source(values["timeline"], values["assets_registry"])
        return
    with pytest.raises(ValueError):
        snapshot_label(value, source=source)


@pytest.mark.parametrize("candidate", [False, True])
def test_cas_and_unclaimed_inputs_stay_uncertified(native_runtime, tmp_path, candidate):
    runtime = native_runtime
    value = snapshot(runtime, candidate=candidate)
    task = runtime.admit(value, forward_candidate=False, uncertified=True)
    values = runtime.materialize(task, tmp_path / "attempt")
    assert values.timeline_source is None
    label = snapshot_label(value, source=values.timeline_source)
    assert label["status"] == "uncertified" and label["actual_revision_id"] is None
    if candidate:
        assert label["base_revision_id"] == runtime.r1
        assert value["metadata"]["working_copy"]["candidate_parent_revision_id"] in label["label"]


@pytest.mark.parametrize("candidate", [False, True])
def test_retained_source_reaches_exact_final_root_inside_normal_lifetime(
    native_runtime, tmp_path, monkeypatch, candidate,
):
    import os
    import sys
    import uuid
    from urllib.request import urlopen

    from astrid.core.rendering.profile import resolve_render_profile
    from astrid.core.rendering.remotion_runtime import prepare_remotion_validation
    from astrid.core.rendering import scene_answers
    from astrid.core.timeline import Timeline
    from astrid.core.timeline.duration import timeline_duration_frames
    from astrid.packs.rendering.rendering.renderers.remotion import lock, run

    project = Path(os.environ["ASTRID_REMOTION_PROJECT_DIR"])
    runtime = native_runtime
    original = snapshot(runtime, candidate=candidate)
    original_bytes = canonical(original)
    task = runtime.admit(original, inline=True)
    ref = task["spec"]["spec"]["authority_context"]["capture_snapshot_ref"]
    # Advance the actual accepted head after retaining R1/the unsaved candidate.
    head = runtime.commits.head(runtime.project_id, "main")
    newer = runtime.service.commit_timeline(runtime.project_id, "main", {
        "api_version": 1, "project_id": runtime.project_id, "timeline_id": "main",
        "base_revision_id": head["revision_id"], "base_publication_id": head["publication_id"],
        "commands": [{"schema_version": 1, "op": "track.create", "version": "1", "select": "parent",
                      "args": {"row": {"id": "new-head-track", "kind": "visual"}}}],
    }, idempotency_key="advance-r2", publisher="fixture-owner-test",
        invocation_path=f"/v1/projects/{runtime.project_id}/timelines/main/commit",
        authorize=runtime.service._assert_mutation_admitted)
    r2 = newer["data"]["accepted_revision_id"]
    assert r2 != runtime.r1
    values = runtime.materialize(task, tmp_path / "attempt")
    env = {}
    PreparedTimelineInputs.handoff_source(values, env)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    timeline, registry = Path(values["timeline"]), Path(values["assets_registry"])
    source = inherited_source(timeline, registry)
    assert source.read()["ref"] == ref
    profile = resolve_render_profile(original["config"], original["registry"])
    basis = {"width": profile.width, "height": profile.height,
             "fps": profile.fps_rational[0] / profile.fps_rational[1]}
    extent = timeline_duration_frames(original["config"], basis["fps"])
    addresses = original["metadata"]["scene_clips"]
    expected_theme = run._resolved_theme_for_render(timeline, None)
    expected = Timeline.from_config(original["config"]).for_render(default_theme=expected_theme["id"]).to_json_data()
    # Qualify the real host environment through its normal owned launch path.
    environment = dict(os.environ)
    validation = prepare_remotion_validation(
        artifact_directory=tmp_path / "runtime-admission", host_environment=environment,
        python_executable=sys.executable, launch_id=uuid.uuid4().hex, contexts={"host": environment},
    )
    for key, value in validation.items():
        monkeypatch.setenv(key, value)
    with lock.remotion_render_lock(), run._prepared_remotion_root(
        timeline, registry, tmp_path / "unused.mp4", provenance_out_path=tmp_path / "provenance.json",
        project_dir=project, composition_id=run.DEFAULT_COMPOSITION_ID, theme_path=None, min_free_gb=None,
        materialized_root=Path(values["materialized_root"]), materialized_objects=values["materialized_objects"],
        frame_numbers=(0, 30, extent), frame_output_dir=tmp_path / "uncaptured-frames",
    ) as prepared:
        props, props_path = prepared[:2]
        source.validate(timeline, registry)
        source.validate_snapshot(original)
        assert canonical(source.read()["snapshot"]) == original_bytes
        assert canonical(props["timeline"]) == canonical(expected)
        assert props["theme"] == expected_theme
        assert json.loads(props_path.read_bytes()) == props
        actual_profile = resolve_render_profile(props["timeline"], props["assets"], theme=props["theme"])
        assert (actual_profile.width, actual_profile.height, actual_profile.fps_rational) == (
            profile.width, profile.height, profile.fps_rational)
        assert set(addresses) == {clip["id"] for clip in props["timeline"]["clips"]}
        for key, asset in props["assets"]["assets"].items():
            original_asset = original["registry"]["assets"][key]
            assert {name: value for name, value in asset.items() if name != "file"} == original_asset
            with urlopen(asset["file"], timeout=5) as response:
                data = response.read()
            pin = original_asset["content_sha256"]
            assert hashlib.sha256(data).hexdigest() == pin.removeprefix("sha256:")
        answers = scene_answers.host_frames_at(props, basis, (0, 30, extent))
        assert [answer["frame"] for answer in answers] == [0, 30, extent]
        # Existing media facts establish neither opaque coverage nor media alpha.
        assert answers[0]["coverage"] == "UNKNOWN"
        assert answers[0]["layers"][0]["clipId"] == "occ-1:picture-1"
        assert addresses["occ-1:picture-1"]["address"] == "c08.picture-1"
        assert addresses["occ-2:picture-1"]["occurrence_id"] == "occ-2"
        assert answers[1]["layers"][0]["clipId"] == "occ-2:picture-1"
        assert answers[-1]["layers"] == []
        label = snapshot_label(original, source=source)
        assert label["actual_revision_id"] == (None if candidate else runtime.r1)
        assert label["base_revision_id"] == (runtime.r1 if candidate else None)
        assert r2 not in label["label"]
        assert runtime.commits.head(runtime.project_id, "main")["revision_id"] == r2
    assert not props_path.exists() and not prepared[6].exists()
    assert not list((tmp_path / "uncaptured-frames").glob("*.png"))
    assert runtime.service.object(ref["digest"])[1] == original_bytes


@pytest.mark.parametrize("legacy", [False, True])
def test_recorded_snapshot_and_bundle_bytes_are_never_recertified(tmp_path, legacy):
    from astrid.packs.rendering.actions.timeline_visualize.inspection_contract import (
        inspect_filmstrip,
    )
    from tests.packs.rendering.test_timeline_offline_inspection import make_bundle

    manifest, _ = make_bundle(tmp_path, legacy=legacy)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert inspect_filmstrip(manifest)["ok"]
    assert inspect_filmstrip(manifest, section="placements")["ok"]
    assert {path: path.read_bytes() for path in before} == before
