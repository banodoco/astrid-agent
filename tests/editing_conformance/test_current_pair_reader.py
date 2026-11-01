"""Current checkout authority from authentic Runtime history, without sockets."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import create_runtime_commit_service
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration

PROJECT = "7b6f2bae8ffe42bf925216c38249ed79"
REVISION = "a3e671256f1c42b8837d3e118c97b21b"
PUBLICATION = "txn-11c90075be6c4c8bb9e2627366f0fe4f"
ARCHIVE_HASH = "2915bd4ac0d46954d19c30f87a517b82074218699978e650fab03fcba81b72d3"
AUTHENTIC_ARCHIVE = Path(
    os.environ.get(
        "ASTRID_AUTHENTIC_LEGACY_ARCHIVE",
        "/tmp/claude-0/-home-user-repo/7dc61be1-7066-52e0-a5d0-94f90e61430a/"
        "scratchpad/orch/design/g5-sdk-authentic-capture-20261023-b6069d1d.tar.gz",
    )
)


class ServiceTransport:
    """Adapt only generated-client page/auth arguments to the real public service."""

    def __init__(self, service):
        self.service = service

    def __getattr__(self, name):
        return getattr(self.service, name)

    def list_timelines(self, *args, **kwargs):
        page = self.service.list_timelines(*args, **kwargs)
        return page["items"], page["next_cursor"]

    def list_timeline_commits(self, *args, **kwargs):
        return self.service.list_timeline_commits(*args, **kwargs, authorize=lambda: None)

    def commit_timeline(self, project, timeline, body, *, idempotency_key):
        from runtime_protocol.timeline_commit_port import route_path

        return self.service.commit_timeline(
            project, timeline, body, idempotency_key=idempotency_key,
            publisher="reader-test", invocation_path=route_path(project, timeline), authorize=lambda: None,
        )


@pytest.fixture
def reader(tmp_path, monkeypatch):
    runtime = use_runtime()
    monkeypatch.syspath_prepend(str(runtime / "tests"))
    from precut_realm_fixture import FIXTURE_BACKUP_KEY, _extract_archive
    from runtime_protocol.backup import restore_backup, verify_backup
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_baseline import verify_baseline

    archive = AUTHENTIC_ARCHIVE
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == ARCHIVE_HASH
    backup = _extract_archive(tmp_path / "extract", archive)
    verify_backup(backup, key=FIXTURE_BACKUP_KEY)
    restore_backup(backup, tmp_path / "realm", key=FIXTURE_BACKUP_KEY)
    service = RuntimeService(tmp_path / "realm")
    service.register_timeline_commit_provider(create_runtime_commit_service())
    facts = verify_baseline(service.store.conn, PROJECT, "film", read_object=service.cas.read)
    assert facts["head"] == {"revision_id": REVISION, "publication_id": PUBLICATION}
    client = SimpleNamespace(timelines=RemoteTimelines(ServiceTransport(service)))
    try:
        yield service, client
    finally:
        service.close()


def test_current_fetch_retains_authenticated_pair_and_canonical_source(reader):
    service, client = reader
    raw = client.timelines.open_bundle(PROJECT, "film")
    assert raw.ok, raw.as_dict()
    assert "publication_id" not in raw.data["bundle"]["base_parent"]
    bundle = store.fetch_bundle(PROJECT, "film", client=client)
    assert bundle["base_parent"]["revision_id"] == REVISION
    assert bundle["base_parent"]["publication_id"] == PUBLICATION
    assert bundle["source_mapping"]["_runtime_document"] == service._timeline_commits()[0].revision_closure(
        PROJECT, "film", REVISION)


def test_explicit_historical_read_does_not_acquire_current_publication(reader):
    _, client = reader
    raw = client.timelines.open_bundle(PROJECT, "film", revision_id=REVISION)
    assert raw.ok, raw.as_dict()
    assert store.fetch_bundle(PROJECT, "film", revision_id=REVISION, client=client) == raw.data["bundle"]


def test_explicit_wrong_publication_refuses(reader):
    _, client = reader
    with pytest.raises(TimelineEditError, match="original publication is unavailable"):
        store.fetch_bundle(PROJECT, "film", revision_id=REVISION, publication_id="unavailable", client=client)


def test_reset_reads_reused_revision_under_new_publication(reader, tmp_path):
    from astrid.sdk.timeline.checkout import Checkout

    service, client = reader
    original = store.fetch_bundle(PROJECT, "film", client=client)
    draft = Checkout(copy.deepcopy(original), path=tmp_path / "draft.json")
    draft.save(quiet=True)
    draft.bundle["parent"]["clips"][0]["at"] = 42
    draft.save(quiet=True)
    command = {"schema_version": 1, "op": "timeline.restore", "version": "1", "select": None,
               "args": {"revision_id": REVISION, "content_digest": original["base_parent"]["content_digest"]}}
    body = {"api_version": 1, "project_id": PROJECT, "timeline_id": "film",
            "base_revision_id": REVISION, "base_publication_id": PUBLICATION,
            "commands": [command], "label": "Restore reader source", "merge": "fields", "reads": [],
            "steps": [{"id": "restore", "label": "Restore reader source", "intent": "", "tags": [],
                       "command_ordinals": [0], "patch": [], "reads": []}],
            "dry_run": False, "source": {"via": "python"},
            "rationale": "Qualify exact current-pair reading", "group_id": "reader-restore"}
    restored = client.timelines.commit(PROJECT, "film", body, idempotency_key="reader-restore")
    assert restored.ok, restored.as_dict()
    packet = restored.as_dict()
    publication = packet["receipt"]["receipt_id"]
    assert publication != PUBLICATION
    assert packet["data"]["accepted_revision_id"] == REVISION
    assert packet["data"]["canonical_document"] == original["source_mapping"]["_runtime_document"]
    draft._reset_base(client=client)
    reloaded = Checkout.load(draft.path)
    assert reloaded.base_revision == REVISION
    assert reloaded.bundle["base_parent"]["publication_id"] == publication
    expected = copy.deepcopy(original)
    expected["base_parent"]["publication_id"] = publication
    assert reloaded.document() == Checkout(expected).document()
    current = store.fetch_bundle(PROJECT, "film", client=client)
    assert current["source_mapping"]["_runtime_document"] == packet["data"]["canonical_document"]
    historical = store.fetch_bundle(PROJECT, "film", revision_id=REVISION,
                                    publication_id=PUBLICATION, client=client)
    assert historical == original
    head = client.timelines.history(PROJECT, "film", limit=200)
    assert head.ok, head.as_dict()
    assert head.data["head"] == {"revision_id": REVISION, "publication_id": publication}
    assert {row["publication_id"] for row in head.data["items"] if row["revision_id"] == REVISION} >= {
        PUBLICATION, publication}
    assert service.get_project_timeline(PROJECT, "film")["head_publication_id"] == publication
    for operation in (reloaded.undo, reloaded.redo):
        with pytest.raises(TimelineEditError, match="no unsent draft edit"):
            operation()


@pytest.mark.parametrize("damage", ["missing-entry", "wrong-revision", "wrong-receipt"])
def test_current_fetch_requires_exact_publication_history_entry(reader, monkeypatch, damage):
    _, client = reader
    original = client.timelines.history

    def damaged(*args, **kwargs):
        from astrid.sdk.contracts import DomainResult

        result = original(*args, **kwargs)
        assert result.ok, result.as_dict()
        data = copy.deepcopy(result.data)
        if damage == "missing-entry":
            data["items"] = []
            data["next_cursor"] = None
        else:
            entry = next((row for row in data["items"] if row["publication_id"] == PUBLICATION), None)
            if entry is not None:
                if damage == "wrong-revision":
                    entry["revision_id"] = "wrong-revision"
                else:
                    entry["receipt"]["receipt_id"] = "wrong-publication"
        return DomainResult.success(data)

    monkeypatch.setattr(client.timelines, "history", damaged)
    with pytest.raises(TimelineEditError):
        store.fetch_bundle(PROJECT, "film", client=client)


@pytest.mark.parametrize("damage", ["missing-head", "missing-publication", "changed-head", "history-error"])
def test_current_fetch_fails_closed_without_head_proof(reader, monkeypatch, damage):
    _, client = reader
    original = client.timelines.history

    def damaged(*args, **kwargs):
        from astrid.sdk.contracts import DomainResult, ErrorObject

        result = original(*args, **kwargs)
        assert result.ok, result.as_dict()
        data = copy.deepcopy(result.data)
        if damage == "history-error":
            return DomainResult.failure(ErrorObject("forbidden", "history denied", {}))
        if damage == "missing-head":
            data.pop("head")
        elif damage == "missing-publication":
            data["head"].pop("publication_id")
        else:
            data["head"]["revision_id"] = "head-moved"
        return DomainResult.success(data)

    monkeypatch.setattr(client.timelines, "history", damaged)
    with pytest.raises(TimelineEditError):
        store.fetch_bundle(PROJECT, "film", client=client)


@pytest.mark.parametrize("damage", ["project_id", "timeline_id", "revision_id", "content_digest", "internal-owner"])
def test_current_fetch_preserves_exact_scope_digest_and_owner_guards(reader, monkeypatch, damage):
    _, client = reader
    transport = client.timelines._client
    method = "get_project_timeline_revision" if damage == "internal-owner" else "get_project_parent_composition_revision"
    original = getattr(transport, method)

    def damaged(*args, **kwargs):
        row = copy.deepcopy(original(*args, **kwargs))
        row["timeline_id" if damage == "internal-owner" else damage] = "foreign"
        return row

    monkeypatch.setattr(transport, method, damaged)
    with pytest.raises(TimelineEditError):
        store.fetch_bundle(PROJECT, "film", client=client)
