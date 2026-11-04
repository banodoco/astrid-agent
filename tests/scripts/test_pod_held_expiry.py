"""Admission boundaries use only synthetic local custody and stub transports."""
import copy
import json
from types import SimpleNamespace

import pytest

from tests.scripts.test_pod_session import held_list_renewal, ps


@pytest.mark.parametrize("offset,allowed", [(-0.001, True), (0, False), (0.001, False), (-3601, False)])
def test_renewal_at_admission_boundary(held_list_renewal, monkeypatch, offset, allowed):
    session, doc, _progress, _events, _publish, messages = held_list_renewal
    before = session._held_path().read_bytes()
    monkeypatch.setattr(ps.time, "time", lambda: doc["expires_at"] + offset)
    assert session.renew_held() == (0 if allowed else 1), messages
    after = session._read_held()
    if allowed:
        assert after["expires_at"] == doc["expires_at"] + offset + 3600
        assert dict(after, expires_at=doc["expires_at"]) == doc
    else:
        assert session._held_path().read_bytes() == before
    assert session.budget.count == 0


@pytest.mark.parametrize("offset", [0, 0.001, -3601])
@pytest.mark.parametrize("operation", ["held", "starting", "exited"])
def test_stale_resume_never_contacts_transport_or_reconciles(held_list_renewal, monkeypatch, offset, operation):
    session, doc, _progress, _events, publish, messages = held_list_renewal
    if operation != "held":
        doc["status"] = "in_use"
        doc["operation"].update(state=operation, exit_code=None if operation == "starting" else 0)
    publish()
    before = session._held_path().read_bytes()
    monkeypatch.setattr(ps.time, "time", lambda: doc["expires_at"] + offset)
    # Fixture CLI/transport collaborators fail the test if invoked.
    assert session.run_held(command="printf forbidden") == 1, messages
    assert session._held_path().read_bytes() == before
    assert session.renew_held() == 1
    assert session._held_path().read_bytes() == before


@pytest.mark.parametrize("offset,allowed", [(-0.001, True), (0, False), (0.001, False), (-3601, False)])
def test_each_reservation_rechecks_admission_before_charge(held_list_renewal, monkeypatch, offset, allowed):
    session, doc, _progress, _events, _publish, _messages = held_list_renewal
    session._held_doc = copy.deepcopy(doc)
    session._held_doc["status"] = "in_use"
    session.budget.count = doc["charged_jobs"]
    before = session._held_path().read_bytes()
    monkeypatch.setattr(ps.time, "time", lambda: doc["expires_at"] + offset)
    if allowed:
        identity = session._reserve_held_job("synthetic boundary")
        durable = session._read_held()
        assert durable["operation"]["id"] == identity
        assert durable["operation"]["state"] == "starting"
        assert durable["charged_jobs"] == session.budget.count == doc["charged_jobs"] + 1
    else:
        with pytest.raises(ps.SessionError, match="admission expired or stale"):
            session._reserve_held_job("synthetic boundary")
        assert session._held_path().read_bytes() == before
        assert session.budget.count == doc["charged_jobs"]


@pytest.mark.parametrize("terminal_at_expiry", [False, True])
def test_wait_is_bounded_by_admission_and_keeps_uncertain_identity(held_list_renewal, monkeypatch, terminal_at_expiry):
    session, doc, _progress, _events, _publish, _messages = held_list_renewal
    clock = {"wall": 100.0, "mono": 50.0}
    monkeypatch.setattr(ps.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(ps.time, "monotonic", lambda: clock["mono"])
    doc.update(status="in_use", expires_at=100.25)
    doc["operation"].update(state="starting", exit_code=None)
    session._write_held(doc)
    session._held_doc = copy.deepcopy(doc)
    session.budget.count = doc["charged_jobs"]
    before = session._held_path().read_bytes()
    polls, sleeps = [], []
    def advance(seconds):
        sleeps.append(seconds)
        clock["wall"] += seconds
        clock["mono"] += seconds
    def poll(identity, *, root):
        polls.append(identity)
        if terminal_at_expiry:
            advance(0.25)
        return {"id": identity, "dir": root + "/" + identity,
                "state": "exited" if terminal_at_expiry else "running",
                "exit_code": 0 if terminal_at_expiry else None}
    session.transport = SimpleNamespace(deadline=None, detached_poll=poll)
    session.sleep = advance
    with pytest.raises(ps.SessionError, match="admission expired or stale"):
        session.wait_job({"id": doc["operation"]["id"]}, 600)
    assert polls == [doc["operation"]["id"]]
    assert sleeps == [0.25]
    assert session.transport.deadline == 50.25
    assert session._held_path().read_bytes() == before
    assert session.budget.count == doc["charged_jobs"]
    with pytest.raises(ps.SessionError, match="admission expired or stale"):
        session._reconcile_held_operation()
    assert len(polls) == 1


@pytest.mark.parametrize("phase", ["validation", "publication"])
def test_renewal_cannot_extend_admission_that_expires_during_validation_or_commit(held_list_renewal, monkeypatch, phase):
    session, doc, _progress, _events, _publish, messages = held_list_renewal
    clock = {"wall": doc["expires_at"] - 0.1}
    monkeypatch.setattr(ps.time, "time", lambda: clock["wall"])
    before = session._held_path().read_bytes()
    if phase == "validation":
        verify = session._verify_renewal_progress
        def expire(*args):
            verify(*args)
            clock["wall"] = doc["expires_at"]
        monkeypatch.setattr(session, "_verify_renewal_progress", expire)
    else:
        write = session._write_held
        def expire(value):
            write(value)
            if value["expires_at"] != doc["expires_at"]:
                clock["wall"] = doc["expires_at"]
        monkeypatch.setattr(session, "_write_held", expire)
    assert session.renew_held() == 1, messages
    assert session._held_path().read_bytes() == before


def test_expiry_during_completed_progress_commit_restores_in_use_custody(held_list_renewal, monkeypatch):
    session, doc, _progress, _events, _publish, _messages = held_list_renewal
    clock = {"wall": doc["expires_at"] - 0.1}
    monkeypatch.setattr(ps.time, "time", lambda: clock["wall"])
    session._held_doc = copy.deepcopy(doc)
    session._held_doc["status"] = "in_use"
    session._write_held(session._held_doc)
    session.budget.count = doc["charged_jobs"]
    write = session._write_held
    def expire(value):
        write(value)
        if value["status"] == "held":
            clock["wall"] = doc["expires_at"]
    monkeypatch.setattr(session, "_write_held", expire)
    with pytest.raises(ps.SessionError, match="admission expired or stale"):
        session._finish_held_progress()
    durable = session._read_held()
    assert durable["status"] == "in_use"
    assert durable["charged_jobs"] == doc["charged_jobs"]
    assert durable["operation"] == doc["operation"]
    assert durable["expires_at"] == doc["expires_at"]
