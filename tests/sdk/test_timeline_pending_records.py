"""Durable SDK records for refused timeline edits."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline.commit_session import Timeline
from astrid.sdk.timeline.outbox import Outbox
from astrid.sdk.timeline.sync import TimelineSession


def _session(path, *, explicit=False):
    transport = SimpleNamespace(_last_handshake={"realm_id": "realm", "actor_id": "actor"})
    timelines = SimpleNamespace(
        open_composition=lambda *a, **kw: DomainResult.success({
            "native_inspection": {"project_id": "project", "timeline_id": "timeline"},
            "summary": {"head_revision_id": "r2"}}),
        history=lambda *a, **kw: DomainResult.success({
            "head": {"revision_id": "r2", "publication_id": "p2"}}))
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=timelines)
    identity = {"realm_id": "realm", "actor_id": "actor"}
    session = TimelineSession(client, Outbox(Path(path)), identity, "project", "timeline")
    session._explicit_outbox = explicit
    return session


def _scope():
    return {"realm_id": "realm", "actor_id": "actor", "project_id": "project",
            "timeline_id": "timeline"}


def _timeline(session):
    timeline = Timeline.__new__(Timeline)
    timeline.session = session
    return timeline


def _initialize_outbox(session):
    session.outbox.path.parent.mkdir(parents=True, exist_ok=True)
    session.outbox.write({"scope": _scope(), "pending": [], "flight": None,
                          "pending_changes": {}})


def test_pending_drop_keeps_raw_record_and_tombstone(tmp_path):
    session = _session(tmp_path / "outbox.json")
    record = {"id": "pc_0123456789ab", "state": "pending", "rev": 2,
              "key": "recut", "key_generated": False, "label": "Recut", "origin": "list",
              "parts": [{"base": {"revision_id": "r1", "publication_id": "p1"},
                         "commands": [{"op": "clip.move", "select": "clip:a", "args": {"on": 3}}],
                         "commit_options": {"merge": "fields"}, "rationale": None,
                         "media_input": None, "imports": [], "evaluation": None}],
              "part": 0, "failure": {"details": {"status": "conflict"}},
              "clash": {"receipts": []},
              "head": {"revision_id": "r2", "publication_id": "p2"}, "diff": None,
              "attempts": 1, "receipts": [], "closed_by": None, "versions": [],
              "created_at": "2026-10-29T00:00:00Z", "updated_at": "2026-10-29T00:00:00Z",
              "dropped_reason": None}
    session.outbox.write({"scope": _scope(), "base": record["parts"][0]["base"],
                          "pending": [], "flight": None, "pending_changes": {record["id"]: record}})
    timeline = _timeline(session)

    assert timeline.pending_list() == [record]
    assert timeline.pending_show("recut") == record
    dropped = timeline.pending_drop(record["id"], reason="keep for later")

    assert dropped["state"] == "dropped"
    assert dropped["rev"] == 3
    assert dropped["dropped_reason"] == "keep for later"
    assert dropped["parts"] == record["parts"]
    assert timeline.pending_list() == []
    assert timeline.pending_list(all=True) == [dropped]
    assert timeline.pending_show(record["id"]) == dropped


def test_actors_all_lists_only_matching_scope(tmp_path):
    own = _session(tmp_path / "timeline-outbox" / "self.json")
    _initialize_outbox(own)
    timeline = _timeline(own)
    own_record = {"id": "pc_self", "state": "pending", "created_at": "2026-10-30T00:00:00Z"}
    peer_record = {"id": "pc_peer", "state": "pending", "created_at": "2026-10-30T00:00:01Z"}
    other_project = {"id": "pc_other", "state": "pending", "created_at": "2026-10-30T00:00:02Z"}
    own.outbox.write({"scope": _scope(), "pending_changes": {own_record["id"]: own_record}})
    Outbox(tmp_path / "timeline-outbox" / "peer.json").write({
        "scope": {**_scope(), "actor_id": "peer"},
        "pending_changes": {peer_record["id"]: peer_record},
    })
    Outbox(tmp_path / "timeline-outbox" / "other-project.json").write({
        "scope": {**_scope(), "actor_id": "peer", "project_id": "elsewhere"},
        "pending_changes": {other_project["id"]: other_project},
    })

    assert [row["id"] for row in timeline.pending_list(actors="all")] == ["pc_self", "pc_peer"]
    assert [row["id"] for row in timeline.pending_list()] == ["pc_self"]


def test_pending_show_resolves_peer_id_and_key_without_mutating_peer(tmp_path, monkeypatch):
    from astrid.sdk.timeline import commit_session

    own = _session(tmp_path / "timeline-outbox" / "self.json")
    _initialize_outbox(own)
    timeline = _timeline(own)
    record = {"id": "pc_peer", "state": "pending", "key": "peer-key",
              "failure": {"details": {"status": "conflict"}}}
    peer_outbox = Outbox(tmp_path / "timeline-outbox" / "peer.json")
    peer_outbox.path.parent.mkdir(parents=True, exist_ok=True)
    peer_outbox.write({"scope": {**_scope(), "actor_id": "peer"},
                       "pending_changes": {record["id"]: record}})

    def enrich_peer(*_args):
        raise AssertionError("reading another actor's record must not enrich or write it")

    monkeypatch.setattr(commit_session, "_pending_clash", enrich_peer)
    before = peer_outbox.read()
    assert timeline.pending_show(record["id"]) == record
    assert timeline.pending_show("peer-key") == record
    assert peer_outbox.read() == before


def test_pending_show_keeps_own_conflict_enrichment(tmp_path, monkeypatch):
    from astrid.sdk.timeline import commit_session

    session = _session(tmp_path / "outbox.json")
    record = {"id": "pc_self", "state": "pending", "key": "own-key",
              "failure": {"details": {"status": "conflict"}}}
    session.outbox.write({"scope": _scope(), "pending": [], "flight": None,
                          "pending_changes": {record["id"]: record}})
    timeline = _timeline(session)
    enriched = {**record, "clash": {"receipts": []}}
    calls = []

    def enrich_own(saved_session, saved_record):
        calls.append((saved_session, saved_record))
        return enriched

    monkeypatch.setattr(commit_session, "_pending_clash", enrich_own)
    assert timeline.pending_show(record["id"]) == enriched
    assert calls == [(session, record)]


def test_pending_show_rejects_ambiguous_visible_key(tmp_path):
    from astrid.sdk.timeline._common import TimelineEditError

    timeline = _timeline(_session(tmp_path / "timeline-outbox" / "self.json"))
    _initialize_outbox(timeline.session)
    for actor in ("peer-a", "peer-b"):
        record = {"id": f"pc_{actor}", "state": "pending", "key": "same-key"}
        Outbox(tmp_path / "timeline-outbox" / f"{actor}.json").write({
            "scope": {**_scope(), "actor_id": actor},
            "pending_changes": {record["id"]: record},
        })

    with pytest.raises(TimelineEditError, match="ambiguous"):
        timeline.pending_show("same-key")


def test_pending_show_keeps_explicit_outbox_isolated(tmp_path):
    from astrid.sdk.timeline._common import TimelineEditError

    path = tmp_path / "timeline-outbox" / "custom.json"
    timeline = _timeline(_session(path, explicit=True))
    _initialize_outbox(timeline.session)
    record = {"id": "pc_peer", "state": "pending", "key": "peer-key"}
    Outbox(path.parent / "peer.json").write({
        "scope": {**_scope(), "actor_id": "peer"},
        "pending_changes": {record["id"]: record},
    })

    with pytest.raises(TimelineEditError, match="no pending change"):
        timeline.pending_show(record["id"])
