"""CLI -> generated COMMIT -> real semantic provider, with synthetic acceptance.

No installed Runtime, credentials, realm, public film or publication is touched.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from test_timeline_commit import bundle, restore_fixture, synthetic_scene_answers  # noqa: F401
from test_workspace_commit_adapter import _client

from astrid.packs.timeline import cli_history
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import journal, restore_commit, saved_publish
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import RuntimeCommitService
from astrid.sdk.timeline.revisions import Revision, resolve_revision_record

pytestmark = pytest.mark.usefixtures("synthetic_scene_answers")


@pytest.fixture
def journey(bundle, monkeypatch, tmp_path):  # noqa: F811 - imported pytest fixture
    original_body, port, closures = restore_fixture(bundle)
    target = closures["opaque-selected"]["parent_revision"]
    rows = [dict(revision_id="rev-0", publication_id="pub-rev-0", is_current_head=True,
                 previous_revision_id="opaque-selected", previous_publication_id="pub-selected"),
            dict(revision_id="opaque-selected", publication_id="pub-selected", is_current_head=False,
                 previous_revision_id=None, previous_publication_id=None,
                 content_digest=target["content_digest"])]
    port.revisions = lambda opts: copy.deepcopy(rows)
    calls, publications = [], []
    service = RuntimeCommitService()
    accept = port.accept
    def accept_image(prepared):
        accepted = accept(prepared)
        publications.append(dict(revision_id=accepted["stored_result"]["accepted_revision_id"],
            publication_id=accepted["stored_result"]["accepted_publication_id"],
            previous_revision_id=prepared["expected_head_revision_id"],
            previous_publication_id=prepared["restore"]["expected_publication_id"],
            restored_from_revision_id=prepared["restore"]["revision_id"],
            message=prepared["label"], provenance=accepted["stored_result"]["provenance"]))
        port.current = accepted["stored_result"]["accepted_revision_id"]
        return accepted
    port.accept = accept_image
    def transport(method, path, headers, payload):
        body = json.loads(payload)
        calls.append((method, path, copy.deepcopy(headers), body, payload))
        port.body = copy.deepcopy(body)
        result = service.commit(body, port)
        return 200, {}, json.dumps(result).encode()
    workspace = _client(transport)
    workspace.get_project_parent_composition_revision = lambda p, t, rid: copy.deepcopy(closures[rid]["parent_revision"])
    def child(kind, rid):
        return copy.deepcopy(next(row for closure in closures.values() for row in closure[kind]
                                  if row["revision_id"] == rid))
    def shot(p, s, rid):
        row = child("shot_revisions", rid)
        internal = child("internal_timeline_revisions", row["internal_timeline_revision_id"])
        row["internal_timeline_id"] = internal["timeline_id"]
        return row
    workspace.get_project_shot_revision = shot
    workspace.get_project_timeline_revision = lambda p, t, rid: child("internal_timeline_revisions", rid)
    class Timelines:
        def resolve_scope(self, p, t):
            assert (p, t) == ("project-name", "timeline-name")
            return DomainResult.success(dict(project_id="p", timeline_id="t"))
        def head_canonical(self, p, t):
            assert (p, t) == ("p", "t")
            return DomainResult.success(port.head())
        def list_parent_composition_revisions(self, p, t, **opts):
            assert (p, t) == ("p", "t")
            return DomainResult.success((copy.deepcopy(rows), None))
        def restore_parent_composition_revision(self, *a, **kw):
            pytest.fail("CLI bypassed semantic COMMIT")
    client = SimpleNamespace(timelines=Timelines(), _remote=SimpleNamespace(_transport=workspace))
    monkeypatch.setattr(journal, "_drafts_root", lambda: tmp_path)
    monkeypatch.setattr(restore_commit, "_authenticate", lambda w: (
        dict(realm_id="synthetic-realm", principal="fixture-publisher"), "synthetic-session"))
    parsed = SimpleNamespace(client=client, project="project-name", ref="timeline-name",
        from_ref="timeline-name@prev", idempotency_key="restore-fixture-key", message="Restore selected cut", json=True)
    return SimpleNamespace(parsed=parsed, port=port, closures=closures, rows=rows, calls=calls,
                           publications=publications, original_body=original_body)


@pytest.mark.parametrize("source", ["timeline-name@prev", "opaque-selected", "opaque"])
def test_cli_named_restore_commit_exact_pins_ancestry_receipt_and_replay(journey, capsys, monkeypatch, source):
    j = journey
    j.parsed.from_ref = source
    original = copy.deepcopy(j.closures)
    assert cli_history.run_restore(j.parsed) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] and first["idempotency_key"] == "restore-fixture-key"
    method, path, headers, body, payload = j.calls[0]
    assert (method, path) == ("POST", "/astrid/v1/projects/p/timelines/t/commit")
    assert headers["Idempotency-Key"] == body["request_id"] == "restore-fixture-key"
    assert headers["X-Astrid-Session"] == body["session_id"] == "synthetic-session"
    assert body["base_revision_id"] == "rev-0" and body["base_publication_id"] == "pub-rev-0"
    assert body["label"] == "Restore selected cut" and body["source"] == {"via": "cli"}
    assert body["scope"] == dict(target="parent", complete=["clips", "tracks"], projection_version=1)
    assert body["change"]["commands"] == [dict(op="timeline.restore", select=None, resolved=[], version=None,
        args=dict(revision_id="opaque-selected", publication_id="pub-selected",
                  content_digest=original["opaque-selected"]["parent_revision"]["content_digest"]))]
    assert j.port.accepts[0]["restore"] == dict(body["change"]["commands"][0]["args"],
                                                expected_publication_id="pub-rev-0")
    assert j.publications == [dict(revision_id="accepted-fixture", publication_id="publication-fixture",
        previous_revision_id="rev-0", previous_publication_id="pub-rev-0",
        restored_from_revision_id="opaque-selected", message=body["label"],
        provenance=dict(publisher="fixture-publisher", source=dict(via="cli")))]
    assert first["data"] == j.port.saved["result"]
    assert first["data"]["runtime_receipt"]["publisher"] == "fixture-publisher"
    assert {rid: j.closures[rid] for rid in original} == original
    # Recovery must not re-resolve @prev, refresh the head or change session identity.
    monkeypatch.setattr(j.parsed.client.timelines, "head_canonical", lambda *a: pytest.fail("replay read head"))
    monkeypatch.setattr(j.parsed.client.timelines, "list_parent_composition_revisions", lambda *a, **k: pytest.fail("replay reselected history"))
    monkeypatch.setattr(restore_commit, "_authenticate", lambda w: (
        dict(realm_id="synthetic-realm", principal="fixture-publisher"), "new-session"))
    assert cli_history.run_restore(j.parsed) == 0
    assert json.loads(capsys.readouterr().out) == first
    assert j.calls[1][4] == payload and len(j.port.accepts) == len(j.publications) == 1
    log = journal.journal_path("p", "t")
    assert not journal.verify(log)
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records[0]["evidence"][0]["body"] == body
    assert records[-1]["evidence"][0]["result"] == first["data"]
    # Normal saved-draft readers still accept all journal rows.
    checkout = SimpleNamespace(bundle=dict(project_id="p", timeline_id="t"), path="/tmp/main.json")
    assert saved_publish.read_rows(checkout, log.read_text()) == []


def test_cli_restore_lost_response_reuses_journaled_envelope(journey, capsys):
    j = journey
    accept = j.port.accept
    def lost(prepared):
        accept(prepared)
        raise TimeoutError("synthetic accepted response lost")
    j.port.accept = lost
    assert cli_history.run_restore(j.parsed) != 0
    failure = json.loads(capsys.readouterr().out)
    assert not failure["ok"] and failure["idempotency_key"] == j.parsed.idempotency_key
    j.port.accept = accept
    assert cli_history.run_restore(j.parsed) == 0
    assert json.loads(capsys.readouterr().out)["data"] == j.port.saved["result"]
    assert j.calls[0][4] == j.calls[1][4] and len(j.port.accepts) == 1


@pytest.mark.parametrize("damage", ["stale-head", "stale-publication", "wrong-project", "wrong-timeline",
                                    "unknown-publication", "digest-mismatch"])
def test_cli_restore_provider_refusals_preserve_public_state(journey, capsys, damage):
    j = journey
    if damage.startswith("stale"):
        head = j.port.head
        j.port.head = lambda: {**head(), ("revision_id" if damage == "stale-head" else "publication_id"): "later"}
        # Caller captures original; provider sees the changed Runtime head.
        j.parsed.client.timelines.head_canonical = lambda *a: DomainResult.success(
            dict(revision_id="rev-0", publication_id="pub-rev-0", updated_at="fixture"))
    elif damage.startswith("wrong"):
        field = "project_id" if damage == "wrong-project" else "timeline_id"
        j.closures["opaque-selected"]["parent_revision"][field] = "other"
    elif damage == "unknown-publication":
        j.port.revisions = lambda opts: []
    else:
        j.rows[1]["content_digest"] = "sha256:" + "0" * 64
    before = copy.deepcopy(j.closures)
    assert cli_history.run_restore(j.parsed) != 0
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"]
    receipt = result["error"]["details"]["receipt"]
    assert receipt["status"] == ("conflict" if damage.startswith("stale") else "refused")
    assert receipt["accepted_revision_id"] is receipt["runtime_receipt"] is None
    assert not j.port.accepts and not j.publications and j.closures == before


@pytest.mark.parametrize("source", ["unknown-revision", "other-timeline@prev"])
def test_cli_restore_unknown_or_cross_timeline_selection_never_dispatches(journey, source):
    journey.parsed.from_ref = source
    with pytest.raises(TimelineEditError):
        cli_history.run_restore(journey.parsed)
    assert not journey.calls and not journey.port.accepts


def test_restore_positional_selection_keeps_exact_republication_event():
    digest = "sha256:" + "1" * 64
    older = Revision("same", "cli", None, None, None, publication_id="older", content_digest=digest)
    head = Revision("same", "cli", None, "same", None, publication_id="head",
                    previous_publication_id="older", is_current_head=True, content_digest=digest)
    store = SimpleNamespace(list=lambda *a, **kw: [head, older])
    assert resolve_revision_record("p", "t", "prev", store=store) == older
    assert resolve_revision_record("p", "t", "same", store=store) == head


def test_restore_key_cannot_change_intent_or_origin(journey, capsys, monkeypatch):
    cli_history.run_restore(journey.parsed)
    capsys.readouterr()
    journey.parsed.from_ref = "opaque-selected"
    with pytest.raises(TimelineEditError, match="different scope, origin or intent"):
        cli_history.run_restore(journey.parsed)
    journey.parsed.from_ref = "timeline-name@prev"
    monkeypatch.setattr(restore_commit, "_authenticate", lambda w: (
        dict(realm_id="other-realm", principal="fixture-publisher"), "synthetic-session"))
    with pytest.raises(TimelineEditError, match="different scope, origin or intent"):
        cli_history.run_restore(journey.parsed)
    assert len(journey.calls) == len(journey.port.accepts) == 1


def test_restore_unverified_response_retains_key_and_pending_request(journey, capsys, monkeypatch):
    journey.parsed.idempotency_key = None
    verify = restore_commit._verify_result
    monkeypatch.setattr(restore_commit, "_verify_result", lambda *a: (_ for _ in ()).throw(
        TimelineEditError("unverified COMMIT receipt")))
    assert cli_history.run_restore(journey.parsed) != 0
    failure = json.loads(capsys.readouterr().out)
    key = failure["idempotency_key"]
    assert key and not failure["ok"] and len(journey.port.accepts) == 1
    log = journal.journal_path("p", "t")
    assert len(log.read_text().splitlines()) == 1  # only the original pending envelope
    journey.parsed.idempotency_key = key
    monkeypatch.setattr(restore_commit, "_verify_result", verify)
    assert cli_history.run_restore(journey.parsed) == 0
    assert json.loads(capsys.readouterr().out)["data"] == journey.port.saved["result"]
    assert len(journey.port.accepts) == 1
