"""Producer facts from the exact closure; media admission is isolated here."""
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest

from astrid.core.timeline.authoring_bundle import open_authoring_bundle, preview_authoring_candidate
from astrid.packs.rendering.actions.render import managed_timeline
from astrid.sdk.exceptions import CapabilityValidationError
from astrid.sdk.timeline_filmstrip import prepare_filmstrip
from tests.timeline.test_authoring_bundle import _closure


class ClosureClient:
    def __init__(self):
        self.parent, self.shots, self.internals = _closure(shared=False)
        self.reads = []
        self.project_id = self.parent["project_id"]
        self.timeline_id = self.parent["timeline_id"]

    def get_project(self, _project):
        return {"project_id": self.project_id, "slug": "demo"}

    def list_timelines(self, project, **_kwargs):
        assert project == self.project_id
        return [[{"project_id": project, "timeline_id": self.timeline_id,
                  "slug": "cut", "head_revision_id": "newer-head"}], None]

    def get_project_parent_composition_revision(self, project, timeline, revision):
        self.reads.append(("parent", project, timeline, revision))
        if revision != self.parent["revision_id"]:
            raise KeyError(revision)
        return deepcopy(self.parent)

    def get_project_shot_revision(self, project, shot, revision):
        self.reads.append(("shot", project, shot, revision))
        return deepcopy(next(row for row in self.shots if row["revision_id"] == revision))

    def get_project_timeline_revision(self, project, timeline, revision):
        self.reads.append(("internal", project, timeline, revision))
        return deepcopy(next(row for row in self.internals if row["revision_id"] == revision))


@pytest.fixture
def client(monkeypatch):
    # Exercise the real immutable opener/projector. No Runtime admission or
    # renderer readiness is claimed by this fixture's registry admission seam.
    monkeypatch.setattr(managed_timeline, "_runtime_snapshot_registry",
                        lambda registry, **_kwargs: deepcopy(registry))
    return ClosureClient()


def prepare(client, **inputs):
    return prepare_filmstrip(
        {"composed_capture": True, "timeline_ref": client.timeline_id,
         "revision_id": "parent-1", **inputs}, project="demo", client=client)


def test_historical_capture_keeps_exact_parent_and_child_pins(client):
    result = prepare(client)
    authority = result["capture_snapshot"]["metadata"]["capture_authority"]
    assert result["revision_id"] == authority["parent_revision_id"] == "parent-1"
    assert authority["parent_content_digest"] == client.parent["content_digest"]
    assert result["capture_snapshot"]["metadata"]["host_transition_contract"] == "runtime-parent-closure/v1"
    assert authority["expansion"]["children"] == [{
        "timeline_id": "main", "revision_id": "internal-1", "config_version": 1,
        "config_hash": client.internals[0]["content_digest"],
    }]
    assert authority["expansion"]["shots"][0]["revision_id"] == "shot-rev-1"
    assert client.reads == [
        ("parent", "project-1", "main", "parent-1"),
        ("shot", "project-1", "shot-shared", "shot-rev-1"),
        ("internal", "project-1", "main", "internal-1"),
    ]


def test_saved_capture_keeps_the_entire_managed_expansion(client):
    parent, projected, expansion = managed_timeline._project_exact_parent_head(
        client=client, project_id=client.project_id, timeline_id=client.timeline_id,
        parent_revision_id="parent-1")
    snapshot = prepare(client)["capture_snapshot"]
    assert snapshot["config"] == projected.config
    assert snapshot["registry"] == projected.registry
    assert snapshot["metadata"]["capture_authority"]["expansion"] == {
        **expansion, "parent_content_digest": parent["content_digest"]}
    assert snapshot["metadata"]["host_transition_contract"] == "runtime-parent-closure/v1"
    assert expansion["graph"] and "outputs" in expansion


@pytest.mark.parametrize("field,value", [
    ("project_id", "foreign"), ("timeline_id", "foreign"),
    ("revision_id", "newer-head"), ("content_digest", None),
])
def test_saved_closure_scope_and_revision_mismatch_refuse_without_head_fallback(client, field, value):
    client.parent[field] = value
    with pytest.raises(CapabilityValidationError, match="cannot resolve admitted saved revision"):
        prepare(client)
    assert client.reads == [("parent", "project-1", "main", "parent-1")]


def test_caller_labels_and_flags_do_not_replace_opened_source(client):
    ordinary = prepare(client)
    supplied = prepare(client, canonical_source={"source_kind": "revision", "revision_id": "forged"},
                       capture_authority={"parent_revision_id": "forged"},
                       snapshot_label="canonical", transition_policy="incoming")
    assert supplied["capture_snapshot"] == ordinary["capture_snapshot"]
    assert supplied["capture_identity"] == ordinary["capture_identity"]


def candidate(client):
    return preview_authoring_candidate(open_authoring_bundle(
        client.parent, shot_revisions=client.shots, internal_timeline_revisions=client.internals))


def test_candidate_capture_preserves_compiled_digest_and_unpublished_identity(client):
    preview = candidate(client)
    result = prepare(client, authoring_preview=preview)
    authority = result["capture_snapshot"]["metadata"]["capture_authority"]
    assert result["capture_snapshot"]["metadata"]["host_transition_contract"] == "runtime-parent-closure/v1"
    publication = preview["publication"]
    assert authority["parent_revision_id"] == publication["parent_revision_id"]
    assert authority["parent_content_digest"] == publication["content_digest"]
    assert authority["expansion"]["working_copy"]["candidate_digest"] == preview["candidate_digest"]
    assert result["capture_snapshot"]["metadata"]["working_copy"]["base_parent"] == preview["head"]
    assert all(read[0] != "parent" for read in client.reads)


@pytest.mark.parametrize("working_copy", [False, True], ids=["saved-parent", "typed-working-copy"])
def test_exact_capture_producer_mints_binding_transition_contract(client, tmp_path, working_copy):
    import json

    from astrid.core.rendering.verified_source import mint_prepared_source

    preview = candidate(client) if working_copy else None
    snapshot = prepare(client, **({"authoring_preview": preview} if preview else {}))["capture_snapshot"]
    authority = snapshot["metadata"]["capture_authority"]
    expansion = authority["expansion"]
    assert snapshot["metadata"]["host_transition_contract"] == "runtime-parent-closure/v1"
    assert isinstance(expansion["occurrences"], list)
    assert isinstance(expansion["children"], list)

    attempt = tmp_path / "attempt"
    inputs = attempt / "inputs"
    inputs.mkdir(parents=True)
    timeline_path = inputs / "timeline.json"
    assets_path = inputs / "registry.json"
    timeline_path.write_text(json.dumps(snapshot["config"]), encoding="utf-8")
    assets_path.write_text(json.dumps(snapshot["registry"]), encoding="utf-8")
    marker = snapshot["metadata"].get("working_copy")
    if working_copy:
        assert isinstance(marker, Mapping)
        revision_digest = marker["candidate_digest"]
        ref = {
            "project_id": authority["project_id"],
            "timeline_id": authority["timeline_id"],
            "revision_id": authority["parent_revision_id"],
            "source_kind": "working_copy",
            "revision_digest": revision_digest,
            "working_copy": {
                "candidate_digest": marker["candidate_digest"],
                "publication_digest": marker["publication_digest"],
                "base_revision_id": marker["base_parent"]["revision_id"],
                "base_revision_digest": marker["base_parent"]["content_digest"],
            },
        }
    else:
        revision_digest = authority["parent_content_digest"]
        ref = {
            "project_id": authority["project_id"],
            "timeline_id": authority["timeline_id"],
            "revision_id": authority["parent_revision_id"],
            "source_kind": "revision",
            "revision_digest": revision_digest,
        }
    source = mint_prepared_source(
        ref=ref, authority=authority, snapshot=snapshot,
        timeline_path=timeline_path, assets_path=assets_path, attempt=attempt,
    )
    assert source.read()["transition_contract"] == "runtime-parent-closure/v1"


@pytest.mark.parametrize("scope", ["project_id", "timeline_id"])
def test_candidate_from_another_selected_scope_is_refused(client, scope):
    preview = candidate(client)
    setattr(client, scope, "foreign")
    with pytest.raises(CapabilityValidationError, match="different project or timeline"):
        prepare(client, authoring_preview=preview)


def test_forged_candidate_digest_is_not_certified_by_capture_labels(client):
    preview = candidate(client)
    preview["candidate_digest"] = "sha256:" + "f" * 64
    with pytest.raises(CapabilityValidationError, match="does not match its frozen candidate"):
        prepare(client, authoring_preview=preview, capture_authority={"canonical": True})


@pytest.mark.parametrize("working_copy", [False, True], ids=["saved-ref", "candidate-ref"])
def test_sdk_capture_reference_admission_keeps_exact_source_and_dependencies(tmp_path, working_copy):
    """Real SDK enrichment/publication and host handoff through accepted Runtime closure."""
    import hashlib
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace

    from astrid.core.execution.generic_host import GenericPackHost
    from astrid.sdk.invocation import _invoke_internal_result
    from tests.sdk.test_capture_source_runtime import open_source_runtime

    @contextmanager
    def source_runtime():
        source = open_source_runtime(tmp_path)
        try:
            yield next(source)
        finally:
            next(source, None)

    with source_runtime() as runtime:
        # Adapt the existing accepted Runtime task resource to the generated
        # SDK task seam; Runtime still owns publication, validation and claim.
        runtime.created = []

        class Tasks:
            def create(inner, **request):
                response = runtime.service.create_task({
                    "project": request["project_id"],
                    "capability_id": request["capability"],
                    "input_object_ids": request["input_manifest"],
                    "spec": request["spec"],
                    **({"idempotency_key": request["idempotency_key"]}
                       if request.get("idempotency_key") else {}),
                })
                task_id = response["task"]["id"]
                claim = runtime.service.claim_next({
                    "executor_id": "probe",
                    "capability_ids": [request["capability"]],
                    "runtime_epoch": runtime.service.health()["runtime_epoch"],
                })
                assert claim["task_id"] == task_id
                task = response["task"]
                admitted = {
                    **task,
                    # The generated SDK task result exposes the admitted
                    # capability spec directly; Runtime stores it under its
                    # task envelope. Keep this adapter local to the case.
                    "spec": task["spec"]["spec"],
                    "input_manifest": list(task["spec"]["input_object_ids"]),
                }
                runtime.created.append(admitted)
                return SimpleNamespace(ok=True, data=admitted, error=None)

        runtime.tasks = Tasks()
        runtime.snapshot(working_copy)
        inputs = {"view": "filmstrip", "timeline_ref": runtime.timeline_id,
                  "composed_capture": True, "revision_id": runtime.parent_revision_id}
        if working_copy:
            inputs["authoring_preview"] = runtime.authoring_preview
        result = _invoke_internal_result(
            "rendering.timeline_visualize", kind="executor", project=runtime.project_id,
            client=runtime, inputs=inputs,
        )
        assert runtime.created, result.error
        admitted = runtime.created[-1]
        context = admitted["spec"]["authority_context"]
        assert "authority_object" not in context and "capture_snapshot" not in context
        ref = context["capture_snapshot_ref"]
        assert ref["source_kind"] == ("working_copy" if working_copy else "revision")
        raw = runtime.get_object(ref["digest"])
        assert ref["digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
        snapshot = json.loads(raw)
        authority = snapshot["metadata"]["capture_authority"]
        assert authority["expansion"]["shots"][0]["content_digest"]
        assert authority["expansion"]["children"][0]["config_hash"]
        if working_copy:
            assert ref["working_copy"]["candidate_digest"] == snapshot["metadata"]["working_copy"]["candidate_digest"]
            assert admitted["spec"]["inputs"]["authoring_preview"] == inputs["authoring_preview"]
            assert admitted["spec"]["inputs"]["authoring_preview"]["candidate"] == inputs["authoring_preview"]["candidate"]
        else:
            assert "authoring_preview" not in admitted["spec"]["inputs"]
            assert ref["revision_id"] == runtime.parent_revision_id
            parent = runtime.get_project_parent_composition_revision(
                runtime.project_id, runtime.timeline_id, runtime.parent_revision_id
            )
            assert ref["revision_digest"] == parent["content_digest"]
        assert ref["digest"] in admitted["input_manifest"]
        host = GenericPackHost(
            pack_roots=[tmp_path],
            client=SimpleNamespace(get_object=lambda key: runtime.get_object("sha256:" + key)),
        )
        values = host._materialize_inputs(
            {"spec": admitted["spec"], "input_object_ids": admitted["input_manifest"]},
            tmp_path / "attempt",
        )
        assert values.timeline_source.read()["ref"] == ref
        assert values.timeline_source.read()["authority"] == authority
        assert values.timeline_source.read()["snapshot"] == snapshot
        assert values.timeline_source.read()["transition_contract"] == "runtime-parent-closure/v1"


@pytest.mark.parametrize('working_copy', [False, True])
@pytest.mark.parametrize('include_media', [False, True])
def test_sdk_capture_reaches_runtime_before_any_snapshot_write(client, monkeypatch, working_copy, include_media):
    import json

    from astrid.sdk.invocation import _invoke_internal_result
    from tests.sdk.test_visualize_authority_objects import Runtime

    runtime = Runtime()
    preview = candidate(client) if working_copy else None
    prepared = prepare(client, **({'authoring_preview': preview} if working_copy else {}))
    if include_media:
        # include_media changes the mode but keeps the same capture authority.
        from tests.sdk.test_visualize_authority_objects import ASSET_DIGEST
        prepared.update(mode='filmstrip', video_digest=ASSET_DIGEST, video_object_id=ASSET_DIGEST)
    monkeypatch.setattr('astrid.sdk.timeline_filmstrip.prepare_filmstrip', lambda *args, **kwargs: deepcopy(prepared))
    before = deepcopy(runtime.objects)
    inputs = {'view': 'filmstrip', 'composed_capture': True}
    if preview is not None:
        inputs['authoring_preview'] = preview
    result = _invoke_internal_result('rendering.timeline_visualize', kind='executor', project='demo',
                                    client=runtime, inputs=inputs)
    assert runtime.created, result.error
    spec = runtime.created[-1]['spec']
    snapshot = spec['authority_context']['capture_snapshot']
    assert snapshot['config'] == prepared['capture_snapshot']['config']
    assert snapshot['registry'] == prepared['capture_snapshot']['registry']
    assert snapshot['metadata']['capture_authority'] == prepared['capture_snapshot']['metadata']['capture_authority']
    assert json.loads(spec['inputs']['filmstrip_authority'])['capture_snapshot'] == snapshot
    assert runtime.objects == before
    if working_copy:
        assert spec['inputs']['authoring_preview'] == preview
        assert snapshot['metadata']['working_copy']['candidate_digest'] == preview['candidate_digest']
        assert snapshot['metadata']['working_copy']['base_parent'] == preview['head']
    else:
        assert 'authoring_preview' not in spec['inputs']


def test_cas_capture_metadata_cannot_mint_source_or_activate_canonical_compiler(tmp_path):
    import hashlib
    import json

    from astrid.core.execution.generic_host import GenericPackHost
    from tests.sdk.test_visualize_authority_objects import Runtime, _snapshot

    runtime = Runtime()
    snapshot = _snapshot()
    snapshot['metadata'].update(capture_authority={'parent_revision_id': 'claimed'},
                                canonical_timeline={'parent_revision_id': 'claimed'},
                                working_copy={'candidate_digest': 'claimed', 'label': 'canonical'})
    raw = json.dumps(snapshot).encode()
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    runtime.objects[digest] = raw
    context = {'mode': 'composed_capture', 'capture_snapshot_ref': {'digest': digest, 'source_kind': 'cas'}}
    from types import SimpleNamespace

    host = GenericPackHost(pack_roots=[], client=SimpleNamespace(get_object=lambda key: runtime.objects['sha256:' + key]))
    values = host._materialize_inputs({'spec': {'authority_context': context,
                                     'inputs': {'filmstrip_authority': json.dumps(context)}},
                                     'input_object_ids': list(runtime.objects)}, tmp_path / 'attempt')
    assert values.timeline_source is None
    assert json.loads(Path(values['timeline']).read_text()) == snapshot['config']


def test_candidate_task_data_is_frozen_before_preflight_reads(client, monkeypatch):
    from astrid.sdk.invocation import _invoke_internal_result
    from tests.sdk.test_visualize_authority_objects import Runtime

    runtime = Runtime()
    preview = candidate(client)
    expected = deepcopy(preview)
    reader = client.get_project_shot_revision

    def read(*args):
        preview['candidate']['parent']['config']['opaque_parent_config'] = {'later': True}
        return reader(*args)

    for name in ('get_project', 'list_timelines', 'get_project_parent_composition_revision',
                 'get_project_shot_revision', 'get_project_timeline_revision'):
        setattr(runtime._remote._transport, name, getattr(client, name))
    runtime._remote._transport.get_project_shot_revision = read
    result = _invoke_internal_result('rendering.timeline_visualize', kind='executor', project='demo',
                                    client=runtime, inputs={'view': 'filmstrip', 'composed_capture': True,
                                    'timeline_ref': 'main', 'revision_id': 'parent-1', 'authoring_preview': preview})
    assert runtime.created, result.error
    assert preview != expected
    assert runtime.created[-1]['spec']['inputs']['authoring_preview'] == expected


@pytest.mark.parametrize('preview', [True, 'canonical', {'candidate_digest': 'sha256:' + 'f' * 64}])
def test_candidate_shape_or_label_cannot_replace_authored_data(client, preview):
    from astrid.sdk.invocation import _invoke_internal_result
    from tests.sdk.test_visualize_authority_objects import Runtime

    runtime = Runtime()
    for name in ('get_project', 'list_timelines', 'get_project_parent_composition_revision',
                 'get_project_shot_revision', 'get_project_timeline_revision'):
        setattr(runtime._remote._transport, name, getattr(client, name))
    before = deepcopy(runtime.objects)
    result = _invoke_internal_result('rendering.timeline_visualize', kind='executor', project='demo',
                                    client=runtime, inputs={'view': 'filmstrip', 'composed_capture': True,
                                    'timeline_ref': 'main', 'revision_id': 'parent-1', 'authoring_preview': preview})
    assert not result.ok
    assert runtime.created == [] and runtime.objects == before


@pytest.mark.parametrize('key', ['verified_source', 'timeline_source', 'ASTRID_VERIFIED_TIMELINE_SOURCE'])
def test_sdk_capture_reserved_source_ports_are_refused(client, key):
    with pytest.raises(ValueError, match='host-owned'):
        prepare(client, **{key: {'source_kind': 'revision', 'revision_id': 'forged'}})


@pytest.mark.parametrize('key', ['verified_source', 'timeline_source', 'ASTRID_VERIFIED_TIMELINE_SOURCE'])
def test_sdk_capture_invocation_refuses_source_before_admission(key):
    from astrid.sdk.invocation import _invoke_internal_result
    from tests.sdk.test_visualize_authority_objects import Runtime

    runtime = Runtime()
    result = _invoke_internal_result('rendering.timeline_visualize', kind='executor', project='demo',
                                     client=runtime, inputs={'view': 'filmstrip', key: {'canonical': True}})
    assert not result.ok and 'host-owned' in result.error['message']
    assert runtime.created == []
