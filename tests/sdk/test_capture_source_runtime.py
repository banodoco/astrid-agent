"""Real CAS/public Runtime admission -> claim -> Agent source materialization.

No SQL fixtures, admission doubles, renderer or sockets. Runtime certifies capture
content at admission, so forged captures are refused before any task exists.
"""
import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.execution.generic_host import GenericPackHost, HostError
from astrid.core.rendering.contracts import RenderRequest
from astrid.core.timeline.authoring_bundle import open_authoring_bundle, preview_authoring_candidate
from astrid.sdk.timeline_filmstrip import prepare_filmstrip
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration
CAPABILITY = "rendering.timeline_visualize"
# Runtime's default task capability digest when the body names none.
CAPABILITY_DIGEST = "sha256:" + hashlib.sha256(CAPABILITY.encode()).hexdigest()


class CaptureRuntime:
    """Only adapt client method/response shapes; all custody stays in Runtime."""

    def __init__(self, service, project):
        self.service, self.project_id = service, project
        timelines = service.list_timelines(project)["items"]
        assert len(timelines) == 1
        timeline = service.get_project_timeline(project, timelines[0]["timeline_id"])
        self.timeline_id = timeline["timeline_id"]
        self.parent_revision_id = timeline["head_revision_id"]
        parent = service.get_project_parent_composition_revision(
            project, self.timeline_id, self.parent_revision_id,
        )
        self.occurrences = parent["payload"]["occurrences"]
        occurrence = self.occurrences[0]
        self.shot_id = occurrence["shot_id"]
        self.shot_revision_id = occurrence["shot_revision_id"]
        self.shot_pairs = list(dict.fromkeys(
            (row["shot_id"], row["shot_revision_id"]) for row in self.occurrences
        ))
        self.internal_revision_ids = []
        for shot_id, shot_revision_id in self.shot_pairs:
            shot = service.get_project_shot_revision(project, shot_id, shot_revision_id)
            if shot["internal_timeline_revision_id"] not in self.internal_revision_ids:
                self.internal_revision_ids.append(shot["internal_timeline_revision_id"])
        self.internal_revision_id = self.internal_revision_ids[0]
        self.media = SimpleNamespace(list=self.media_list)
        self._remote = SimpleNamespace(_transport=self)

    def get_project(self, project):
        row = self.service.store.get_project(project)
        return {**row, "project_id": row["id"]}

    def list_timelines(self, project, **_kwargs):
        return [[self.service.get_project_timeline(project, self.timeline_id)], None]

    def media_list(self, project, **kwargs):
        page = self.service.list_project_objects(project, **kwargs)
        return [page["items"], page["next_cursor"]]

    def get_project_parent_composition_revision(self, *args):
        return self.service.get_project_parent_composition_revision(*args)

    def get_project_shot_revision(self, *args):
        return self.service.get_project_shot_revision(*args)

    def get_project_timeline_revision(self, *args):
        return self.service.get_project_timeline_revision(*args)

    def get_object(self, digest):
        return self.service.object(digest)[1]

    def snapshot(self, working_copy=False):
        inputs = {"composed_capture": True, "timeline_ref": self.timeline_id,
                  "revision_id": self.parent_revision_id}
        self.authoring_preview = None
        if working_copy:
            parent = self.get_project_parent_composition_revision(
                self.project_id, self.timeline_id, self.parent_revision_id,
            )
            shots = [self.get_project_shot_revision(self.project_id, shot_id, revision_id)
                     for shot_id, revision_id in self.shot_pairs]
            internals = [self.get_project_timeline_revision(self.project_id, self.timeline_id, revision_id)
                         for revision_id in self.internal_revision_ids]
            bundle = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
            # A real unsaved edit; it must not silently become the saved base.
            bundle["parent"]["config"]["opaque_parent_config"] = {"unsaved": True}
            inputs["authoring_preview"] = preview_authoring_candidate(bundle)
            self.authoring_preview = deepcopy(inputs["authoring_preview"])
        prepared = prepare_filmstrip(inputs, project=self.project_id, client=self)
        return prepared["capture_snapshot"]

    def ref(self, snapshot, *, uncertified=False):
        encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        response = self.service.ingest(self.project_id, encoded, media_type="application/json",
                                       idempotency_key="capture-" + digest.split(":")[1])
        assert response["data"]["object_id"] == digest
        if uncertified:
            return {"digest": digest, "project_id": self.project_id, "timeline_id": self.timeline_id, "source_kind": "cas"}
        authority = snapshot["metadata"]["capture_authority"]
        ref = {"digest": digest, "project_id": self.project_id, "timeline_id": self.timeline_id,
               "revision_id": authority["parent_revision_id"],
               "revision_digest": authority["parent_content_digest"], "source_kind": "revision"}
        marker = snapshot["metadata"].get("working_copy")
        if marker:
            ref.update(source_kind="working_copy", revision_digest=marker["candidate_digest"], working_copy={
                "candidate_digest": marker["candidate_digest"], "publication_digest": marker["publication_digest"],
                "base_revision_id": marker["base_parent"]["revision_id"],
                "base_revision_digest": marker["base_parent"]["content_digest"]})
        return ref

    def request(self, snapshot, *, inline=False, duplicate=None, forward_candidate=True, uncertified=False):
        authority = snapshot["metadata"].get("capture_authority", {})
        context = {"mode": "composed_capture", "project_id": self.project_id,
                   "timeline_id": self.timeline_id,
                   "revision_id": authority.get("parent_revision_id")}
        if inline:
            context["capture_snapshot"] = snapshot
        else:
            context["capture_snapshot_ref"] = self.ref(snapshot, uncertified=uncertified)
        filmstrip = deepcopy(context)
        if duplicate is not None:
            filmstrip["capture_snapshot_ref"] = duplicate
        body = {"project": self.project_id, "capability_id": CAPABILITY,
                "input_object_ids": [item["object_id"] for item in self.media_list(self.project_id)[0]],
                "spec": {"inputs": {"filmstrip_authority": json.dumps(filmstrip)}, "authority_context": context}}
        if forward_candidate and self.authoring_preview is not None:
            body["spec"]["inputs"]["authoring_preview"] = deepcopy(self.authoring_preview)
        return body

    def admit(self, snapshot, **kwargs):
        body = self.request(snapshot, **kwargs)
        task = self.service.create_task(body)["task"]
        claim = self.service.claim_next({"executor_id": "probe", "capability_ids": [CAPABILITY],
                                        "runtime_epoch": self.service.health()["runtime_epoch"]})
        assert claim["task_id"] == task["id"]
        return task

    def materialize(self, task, attempt):
        return GenericPackHost(pack_roots=[], client=self)._materialize_inputs(task["spec"], attempt)


@pytest.fixture
def runtime(tmp_path):
    yield from open_source_runtime(tmp_path)


def open_source_runtime(tmp_path, before_provider=lambda runtime: None):
    """Build the source through Runtime media custody and semantic commands.

    Generation selection, CAS custody, commit receipts, and the three-plane
    closure all come from existing Runtime/Agent APIs before the test provider
    and task capability are registered.
    """
    runtime_root = use_runtime()
    if str(runtime_root / "tests") not in sys.path:
        sys.path.insert(0, str(runtime_root / "tests"))
    from runtime_protocol.timeline_commit_port import route_path
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider
    from test_generation_publish_v1_settlement import _fixture, _output, _settle

    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_initialize import capture_initialize
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media

    def effect(project_id):
        return {
            "effect_type": "generation.publish_v1",
            "target_id": project_id,
            "payload": {
                "version": 1,
                "modality": "video",
                "generation_type": "multi_output_render",
                "metadata": {},
                "partial_success_policy": "reject",
                "groups": [{
                    "group_key": "main",
                    "selectors": [{
                        "selector": "picture", "ordinal": 0,
                        "variant_key": "picture", "output_port": "picture",
                    }],
                }],
            },
        }

    service, project_row, generation_task, attempt, generation_effect = _fixture(
        tmp_path, effect_factory=effect, slug="capture-source",
    )
    project = project_row["id"]
    _settle(
        service, attempt,
        [_output(
            b"source picture", output_port="picture", group_key="main",
            variant_key="picture", ordinal=0, name="source.mp4",
            media_type="video/mp4", duration_seconds=1.0,
        )],
        effect=generation_effect,
    )
    managed = service.managed_outputs(generation_task["task"]["id"])[0]
    media = service.get_project_media_selection(
        project,
        {"association_id": managed["association_id"], "object_id": managed["object_id"],
         "version": managed["lifecycle"]["version"]},
        authorize=lambda: None,
    )
    verified_media = service.get_project_verified_media(project, media, authorize=lambda: None)
    service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
    service.create_timeline(project, "main", idempotency_key="capture-source-timeline")

    def read_document():
        timeline = service.get_project_timeline(project, "main")
        parent = service.get_project_parent_composition_revision(
            project, "main", timeline["head_revision_id"],
        )
        shots, internals, seen = [], [], set()
        for occurrence in parent["payload"]["occurrences"]:
            pair = (occurrence["shot_id"], occurrence["shot_revision_id"])
            if pair in seen:
                continue
            seen.add(pair)
            shot = service.get_project_shot_revision(project, *pair)
            shots.append({key: shot[key] for key in (
                "shot_id", "revision_id", "internal_timeline_revision_id",
                "content_digest", "payload",
            )})
            internal = service.get_project_timeline_revision(
                project, "main", shot["internal_timeline_revision_id"],
            )
            internals.append({key: internal[key] for key in (
                "timeline_id", "revision_id", "content_digest", "payload",
            )})
        return {
            "schema_version": 1, "project_id": project, "timeline_id": "main",
            "parent_revision": {key: parent[key] for key in (
                "revision_id", "content_digest", "payload",
            )},
            "shot_revisions": shots,
            "internal_timeline_revisions": internals,
            "dependency_manifest": parent["dependency_manifest"],
        }

    def submit(command, key):
        head = service.get_project_timeline(project, "main")
        return service.commit_timeline(
            project, "main",
            {"api_version": 1, "project_id": project, "timeline_id": "main",
             "base_revision_id": head["head_revision_id"],
             "base_publication_id": head["head_publication_id"], "commands": [command]},
            idempotency_key=key, publisher="capture-source-fixture",
            invocation_path=route_path(project, "main"), authorize=lambda: None,
        )

    bundle = bundle_from_document(read_document())
    initialize = capture_initialize(
        bundle, name="Main", slug="main",
        output={"resolution": "1280x720", "fps": 30, "file": "output.mp4",
                "background": None, "background_scale": None},
    )
    submit(initialize, "capture-source-init")
    bundle = bundle_from_document(read_document())
    parent_track = capture_track_create(
        bundle, "parent", {"id": "picture", "kind": "visual", "role": "output"},
    )
    submit(parent_track, "capture-source-parent-track")
    bundle = bundle_from_document(read_document())
    birth = capture_occurrence_create(
        bundle, "occurrence-1", "shot-1", name="Opening", track="picture", on=0, duration=1,
    )
    submit(birth, "capture-source-birth")
    bundle = bundle_from_document(read_document())
    child_track = capture_track_create(
        bundle, "occurrence:occurrence-1", {"id": "picture", "kind": "visual"},
    )
    submit(child_track, "capture-source-child-track")
    bundle = bundle_from_document(read_document())
    media_row = {
        "id": "picture-1", "clipType": "video", "asset": media["asset"],
        "track": "picture", "at": 0, "hold": 1, "from": 0, "to": 1, "speed": 1,
    }
    media_command = capture_occurrence_add_media(
        bundle, "occurrence:occurrence-1", media_row, media,
        verified_media=[verified_media],
    )
    submit(media_command, "capture-source-media")
    try:
        runtime = CaptureRuntime(service, project)
        before_provider(runtime)
        service.register_capability({"capability_id": CAPABILITY, "definition_digest": CAPABILITY_DIGEST})
        service.register_executor({"executor_id": "probe", "capabilities": [CAPABILITY]})
        yield runtime
    finally:
        service.close()


@pytest.mark.parametrize("working_copy", [False, True])
@pytest.mark.parametrize("inline", [False, True])
def test_current_capture_reaches_claim_and_verified_host(runtime, tmp_path, working_copy, inline):
    snapshot = runtime.snapshot(working_copy)
    task = runtime.admit(snapshot, inline=inline)
    values = runtime.materialize(task, tmp_path / "attempt")
    source = values.timeline_source
    binding = source.read()
    assert binding["snapshot"] == snapshot
    assert binding["authority"] == snapshot["metadata"]["capture_authority"]
    assert json.loads(Path(values["timeline"]).read_text()) == snapshot["config"]
    source.validate(values["timeline"], values["assets_registry"])
    source.validate_snapshot(snapshot)
    assert binding["ref"]["source_kind"] == ("working_copy" if working_copy else "revision")
    if working_copy:
        assert task["spec"]["spec"]["inputs"]["authoring_preview"] == runtime.authoring_preview
    else:
        from astrid.packs.rendering.actions.render.managed_timeline import (
            _project_exact_parent_head,
        )

        parent, projected, expansion = _project_exact_parent_head(
            client=runtime, project_id=runtime.project_id, timeline_id=runtime.timeline_id,
            parent_revision_id=runtime.parent_revision_id)
        assert snapshot["config"] == projected.config
        assert snapshot["registry"] == projected.registry
        assert binding["authority"]["expansion"] == {
            **expansion, "parent_content_digest": parent["content_digest"]}


@pytest.mark.parametrize("working_copy", [False, True])
@pytest.mark.parametrize("inline", [False, True])
def test_saved_projection_and_unsaved_candidate_reach_real_claim_and_host(runtime, tmp_path, monkeypatch, working_copy, inline):
    snapshot = runtime.snapshot(working_copy)
    task = runtime.admit(snapshot, inline=inline)
    values = runtime.materialize(task, tmp_path / "attempt")
    binding = values.timeline_source.read()
    assert binding["snapshot"] == snapshot
    assert json.loads(Path(values["timeline"]).read_text()) == snapshot["config"]
    assert binding["authority"] == snapshot["metadata"]["capture_authority"]
    assert binding["authority"]["expansion"]["shots"][0]["content_digest"]
    assert binding["authority"]["expansion"]["children"][0]["config_hash"]
    assert binding["authority"]["expansion"]["shots"][0]["content_digest"] == runtime.get_project_shot_revision(
        runtime.project_id, runtime.shot_id, runtime.shot_revision_id)["content_digest"]
    assert binding["authority"]["expansion"]["children"][0]["config_hash"] == runtime.get_project_timeline_revision(
        runtime.project_id, runtime.timeline_id, runtime.internal_revision_id)["content_digest"]
    assert binding["ref"]["source_kind"] == ("working_copy" if working_copy else "revision")
    if working_copy:
        marker = snapshot["metadata"]["working_copy"]
        assert snapshot["config"]["opaque_parent_config"] == {"unsaved": True}
        assert binding["ref"]["revision_digest"] == marker["candidate_digest"]
        assert binding["ref"]["working_copy"]["base_revision_digest"] == marker["base_parent"]["content_digest"]
        assert task["spec"]["spec"]["inputs"]["authoring_preview"] == runtime.authoring_preview
        from runtime_protocol.errors import NotFoundError
        with pytest.raises(NotFoundError):
            runtime.get_project_parent_composition_revision(
                runtime.project_id, runtime.timeline_id, marker["candidate_parent_revision_id"])
    else:
        assert "authoring_preview" not in task["spec"]["spec"]["inputs"]
    from astrid.core.rendering.verified_source import SOURCE_ENV
    monkeypatch.setenv(SOURCE_ENV, values.timeline_source._anchor)
    parsed = RenderRequest(schema_version=1, timeline_path=values["timeline"],
                           assets_registry_path=values["assets_registry"], output_name="probe.mp4")
    assert parsed.verified_source.read() == binding


def test_missing_candidate_explicit_cas_preview_stays_uncertified(runtime, tmp_path):
    snapshot = runtime.snapshot(working_copy=True)
    task = runtime.admit(snapshot, forward_candidate=False, uncertified=True)
    values = runtime.materialize(task, tmp_path / "attempt")
    assert values.timeline_source is None
    assert json.loads(Path(values["timeline"]).read_text()) == snapshot["config"]
    assert "authoring_preview" not in task["spec"]["spec"]["inputs"]


@pytest.mark.parametrize("inline", [False, True])
def test_missing_candidate_working_copy_is_admitted_uncertified(runtime, inline):
    snapshot = runtime.snapshot(working_copy=True)
    task = runtime.service.create_task(runtime.request(snapshot, inline=inline, forward_candidate=False))["task"]
    claim = runtime.service.claim_next({"executor_id": "probe", "capability_ids": [CAPABILITY],
                                        "runtime_epoch": runtime.service.health()["runtime_epoch"]})
    assert claim["task_id"] == task["id"]
    assert "authoring_preview" not in task["spec"]["spec"]["inputs"]


@pytest.mark.parametrize("inline", [False, True])
def test_missing_candidate_certificate_without_authority_refuses_before_admission_writes(runtime, inline):
    from runtime_protocol.errors import RuntimeErrorBase

    snapshot = runtime.snapshot(working_copy=True)
    # The claimed candidate marker contradicts the capture's own authority.
    marker = snapshot["metadata"]["working_copy"]
    snapshot["metadata"]["working_copy"] = dict(marker, candidate_digest="sha256:" + "0" * 64)
    body = runtime.request(snapshot, inline=inline, forward_candidate=False)
    connection = runtime.service.store.conn
    before = list(connection.iterdump())
    root = Path(runtime.service.store.root)
    files = {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
             for directory in ("cas", "staging") for path in (root / directory).rglob("*")}
    with pytest.raises(RuntimeErrorBase):
        runtime.service.create_task(body)
    assert list(connection.iterdump()) == before
    assert {str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
            for directory in ("cas", "staging") for path in (root / directory).rglob("*")} == files


@pytest.mark.parametrize("field", ["config", "registry", "shot_digest"])
@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("working_copy", [False, True])
def test_arbitrary_capture_cannot_acquire_canonical_source(runtime, tmp_path, field, inline, working_copy):
    from runtime_protocol.errors import RuntimeErrorBase

    snapshot = runtime.snapshot(working_copy)
    original = deepcopy(snapshot)
    if field == "config":
        snapshot["config"]["clips"][0]["hold"] = 123
    elif field == "registry":
        # Same real project-owned media/dependency pins; only its meaning changes.
        entry = next(iter(snapshot["registry"]["assets"].values()))
        entry["opaque_asset"] = "not from the immutable source"
    else:
        snapshot["metadata"]["capture_authority"]["expansion"]["shots"][0]["content_digest"] = "sha256:" + "0" * 64
    try:
        task = runtime.admit(snapshot, inline=inline)
        values = runtime.materialize(task, tmp_path / "attempt")
    except (RuntimeErrorBase, HostError):
        return
    # Runtime must verify the exact projection/candidate, or leave it native.
    assert snapshot != original
    assert values.timeline_source is None


def test_replaced_duplicate_capture_ref_is_refused_at_admission(runtime):
    from runtime_protocol.errors import ValidationError

    snapshot = runtime.snapshot()
    replacement = deepcopy(snapshot)
    replacement["config"]["clips"][0]["hold"] = 123
    with pytest.raises(ValidationError, match="differs from admitted authority context"):
        runtime.admit(snapshot, duplicate=runtime.ref(replacement))


def test_native_compiled_capture_does_not_acquire_canonical_source(runtime, tmp_path):
    snapshot = runtime.snapshot()
    # Neither selector remains; canonical_timeline alone would make Runtime publish a revision ref.
    snapshot["metadata"].pop("capture_authority")
    snapshot["metadata"].pop("canonical_timeline", None)
    # Ordinary compiled capture remains accepted, without a canonical selector.
    task = runtime.admit(snapshot, inline=True)
    values = runtime.materialize(task, tmp_path / "attempt")
    assert values.timeline_source is None
