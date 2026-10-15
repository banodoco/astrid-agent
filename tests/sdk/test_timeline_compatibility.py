"""H-DATA writer guards. All heads are disposable in-memory protocol fixtures."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import (
    compile_authoring_candidate, open_authoring_bundle, publish_authoring_candidate,
)
from astrid.core.timeline.compatibility import TimelineCompatibilityError, require_writable_parent
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError


def parent(version=1):
    return {"config": {"tracks": [], "app": {"astrid": {"v": version}}},
            "registry": {}, "clips": [], "occurrences": []}


def candidate(version=1):
    payload = parent(version)
    digest = "sha256:" + hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode()).hexdigest()
    return open_authoring_bundle(
        {"project_id": "disposable-project", "timeline_id": "disposable-timeline",
         "revision_id": "disposable-head", "content_digest": digest, "payload": payload},
        shot_revisions=[], internal_timeline_revisions=[],
    )


@pytest.mark.parametrize("version", [True, "1", None, 1.0, [], {}])
def test_version_declaration_requires_one_integer(version):
    with pytest.raises(TimelineCompatibilityError, match="must be an integer"):
        require_writable_parent(parent(version))


@pytest.mark.parametrize("version", [0, 1])
def test_known_versions_compile_without_inserting_feature_lists(version):
    work = candidate(version)
    before = copy.deepcopy(work)
    assert compile_authoring_candidate(work).publication["parent_composition"]["config"]["app"]["astrid"] == {"v": version}
    assert work == before


def test_legacy_missing_version_is_not_rewritten():
    work = candidate()
    del work["parent"]["config"]["app"]["astrid"]["v"]
    del work["base_parent_payload"]["config"]["app"]["astrid"]["v"]
    assert "v" not in compile_authoring_candidate(work).publication["parent_composition"]["config"]["app"]["astrid"]


def test_film_intent_schema_accepts_integer_compatibility_metadata():
    from jsonschema import Draft202012Validator

    path = Path(__file__).parents[2] / "schemas/astrid-intent/astrid-intent.schema.json"
    schema = json.loads(path.read_text())
    validator = Draft202012Validator({"$ref": "#/$defs/filmIntent", "$defs": schema["$defs"]})
    assert not list(validator.iter_errors({"v": 1}))
    assert list(validator.iter_errors({"v": "1"}))


@pytest.mark.parametrize("field", ["parent", "base_parent_payload"])
def test_unknown_feature_version_refuses_core_commit_even_if_candidate_removes_it(field):
    work = candidate()
    work[field]["config"]["app"]["astrid"]["v"] = 2
    writes = []
    writer = SimpleNamespace(publish_parent_composition=lambda *a, **kw: writes.append((a, kw)))
    with pytest.raises(TimelineCompatibilityError, match=r"unknown Astrid feature version app.astrid.v=2"):
        publish_authoring_candidate(work, writer, idempotency_key="disposable-core")
    assert writes == []


class GeneratedFixture:
    def __init__(self, head_version=1, restored_version=1):
        self.revisions = {"disposable-head": parent(head_version), "disposable-old": parent(restored_version)}
        self.reads = []
        self.writes = []
        self.head = "disposable-head"

    def get_project_parent_composition_revision(self, project, timeline, revision):
        self.reads.append(revision)
        return {"revision_id": revision, "payload": copy.deepcopy(self.revisions[revision])}

    def publish_parent_composition(self, *args, **kwargs):
        if args[2]["expected_head"] != self.head:
            raise WorkspaceClientError(409, "head_conflict", "expected head differs from current head")
        self.writes.append(("publish", args, kwargs))
        return {"new_head": "disposable-next"}

    def restore_project_parent_composition_revision(self, *args, **kwargs):
        if kwargs["expected_head"] != self.head:
            raise WorkspaceClientError(409, "head_conflict", "expected head differs from current head")
        self.writes.append(("restore", args, kwargs))
        return {"new_head": "disposable-restored"}


def transport(fake):
    result = object.__new__(WorkspaceClient)
    result._generated = fake
    return result


@pytest.mark.parametrize("route", ["publish", "restore", "replace-media"])
def test_astrid_transport_routes_refuse_writer_lacking_feature_bearing_head(route):
    fake = GeneratedFixture(head_version=2)
    client = transport(fake)
    with pytest.raises(WorkspaceClientError, match=r"app.astrid.v=2"):
        if route == "publish":
            client.publish_parent_composition("p", "t", {
                "expected_head": "disposable-head", "parent_composition": parent(),
            }, idempotency_key="disposable-publish")
        elif route == "restore":
            client.restore_project_parent_composition_revision("p", "t", "disposable-old",
                expected_head="disposable-head", idempotency_key="disposable-restore")
        else:
            client.replace_parent_composition_media("p", "t", occurrence_id="disposable-occurrence",
                clip_id="disposable-clip", source_object_id="sha256:" + "1" * 64,
                expected_head="disposable-head", idempotency_key="disposable-replacement")
    assert fake.reads == ["disposable-head"]
    assert fake.writes == []


def test_supported_head_publish_preserves_cas_and_initial_publish_needs_no_head():
    fake = GeneratedFixture()
    client = transport(fake)
    publication = {"expected_head": "disposable-head", "parent_composition": parent()}
    assert client.publish_parent_composition("p", "t", publication,
        idempotency_key="disposable-supported")["new_head"] == "disposable-next"
    assert fake.reads == ["disposable-head"]
    assert fake.writes[0][1] == ("p", "t", publication)
    publication = {"expected_head": None, "parent_composition": parent()}
    fake.head = None
    client.publish_parent_composition("p", "t", publication, idempotency_key="disposable-initial")
    assert fake.reads == ["disposable-head"]


def test_restore_refuses_unknown_feature_in_source_and_preserves_supported_cas():
    fake = GeneratedFixture(restored_version=2)
    client = transport(fake)
    with pytest.raises(WorkspaceClientError, match=r"app.astrid.v=2"):
        client.restore_project_parent_composition_revision("p", "t", "disposable-old",
            expected_head="disposable-head", idempotency_key="disposable-restore")
    assert fake.writes == []
    fake.revisions["disposable-old"] = parent()
    result = client.restore_project_parent_composition_revision("p", "t", "disposable-old",
        expected_head="disposable-head", idempotency_key="disposable-restore-supported")
    assert result == {"new_head": "disposable-restored"}
    assert fake.writes[0][2] == {"expected_head": "disposable-head", "idempotency_key": "disposable-restore-supported"}


@pytest.mark.parametrize("stale,force", [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize("head_version", [1, 2])
def test_checkout_publish_inspects_feature_bearing_head_even_without_merge(monkeypatch, stale, force, head_version):
    # Existing tiny authoring fixture is synthetic repository test data, never a film.
    path = Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json"
    work = json.loads(path.read_text(encoding="utf-8"))
    # Sprite outline validation is a separate media service concern. Remove
    # only that decorative layer so this fixture's real check stays offline.
    for shot in work["shots"].values():
        shot["internal_timeline"]["clips"] = [
            clip for clip in shot["internal_timeline"]["clips"] if clip.get("clipType") != "am-sprite"
        ]
    head = copy.deepcopy(work)
    current = "disposable-newer" if stale else work["base_parent"]["revision_id"]
    head["base_parent"]["revision_id"] = current
    head["parent"]["config"].setdefault("app", {})["astrid"] = {"v": head_version}
    if head_version == 1:
        # A legacy writer is missing support for the existing namespace format.
        monkeypatch.setattr("astrid.core.timeline.compatibility.SUPPORTED_VERSIONS", frozenset({0}))
    reads = []

    def open_bundle(project, timeline, *, revision_id=None):
        reads.append(revision_id)
        return SimpleNamespace(ok=True, error=None, data={"bundle": copy.deepcopy(head)})

    fake = SimpleNamespace(timelines=SimpleNamespace(
        open_composition=lambda *a: SimpleNamespace(ok=True, data={"summary": {"head_revision_id": current}}),
        open_bundle=open_bundle,
    ))

    def forbidden(**kwargs):
        pytest.fail("unsupported head must fail before any runtime/provider access")

    monkeypatch.setattr("astrid.sdk.autobootstrap.ensure_runtime", forbidden)
    checkout = Checkout(work)
    assert checkout.check().valid
    with pytest.raises(TimelineEditError, match=rf"app.astrid.v={head_version}"):
        checkout.publish("disposable compatibility proof", client=fake, force=force)
    assert reads == [current]


def test_restore_old_writer_refuses_feature_bearing_v1_head(monkeypatch):
    fake = GeneratedFixture()
    monkeypatch.setattr("astrid.core.timeline.compatibility.SUPPORTED_VERSIONS", frozenset({0}))
    with pytest.raises(WorkspaceClientError, match=r"app.astrid.v=1"):
        transport(fake).restore_project_parent_composition_revision("p", "t", "disposable-old",
            expected_head="disposable-head", idempotency_key="disposable-old-writer")
    assert fake.reads == ["disposable-head"]
    assert fake.writes == []


@pytest.mark.parametrize("route", ["publish", "restore"])
def test_null_expected_head_cannot_overwrite_existing_head(route):
    fake = GeneratedFixture(head_version=2)
    client = transport(fake)
    with pytest.raises(WorkspaceClientError, match="expected head differs"):
        if route == "publish":
            client.publish_parent_composition("p", "t", {"expected_head": None, "parent_composition": parent()},
                idempotency_key="disposable-null-head")
        else:
            client.restore_project_parent_composition_revision("p", "t", "disposable-old",
                expected_head=None, idempotency_key="disposable-null-head")
    assert fake.writes == []


def test_restore_refuses_wrong_revision_identity_before_commit():
    fake = GeneratedFixture()
    fake.get_project_parent_composition_revision = lambda *args: {"revision_id": "wrong", "payload": parent()}
    with pytest.raises(WorkspaceClientError, match="no matching identity"):
        transport(fake).restore_project_parent_composition_revision("p", "t", "disposable-old",
            expected_head="disposable-head", idempotency_key="disposable-identity")
    assert fake.writes == []


@pytest.mark.parametrize('route', ['publish', 'restore'])
def test_legacy_payload_cannot_downgrade_a_supported_feature_head(route):
    fake = GeneratedFixture(head_version=1, restored_version=0)
    with pytest.raises(WorkspaceClientError, match='below pinned head'):
        if route == 'publish':
            transport(fake).publish_parent_composition('p', 't', {
                'expected_head_revision_id': 'disposable-head', 'parent_composition': parent(0),
            }, idempotency_key='disposable-downgrade')
        else:
            transport(fake).restore_project_parent_composition_revision('p', 't', 'disposable-old',
                expected_head='disposable-head', idempotency_key='disposable-downgrade')
    assert fake.writes == []


def test_publish_old_writer_refuses_feature_bearing_v1_head(monkeypatch):
    fake = GeneratedFixture()
    monkeypatch.setattr('astrid.core.timeline.compatibility.SUPPORTED_VERSIONS', frozenset({0}))
    with pytest.raises(WorkspaceClientError, match=r'app.astrid.v=1'):
        transport(fake).publish_parent_composition('p', 't', {
            'expected_head': 'disposable-head', 'parent_composition': parent(0),
        }, idempotency_key='disposable-old-writer')
    assert fake.writes == []
