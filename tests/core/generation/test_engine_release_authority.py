"""Actual owner protocol tests. Exclusive ingress remains an acceptance gate."""
import copy
from dataclasses import replace
import os
import signal
import subprocess
import sys
import time

import pytest

from astrid.core.generation.engine_contracts import CustodyUnresolved, EndpointOwnershipError
from astrid.core.generation.engine_release_authority import binding_record, digest, process_identity
from tests.core.generation._release_fixtures import (
    binding, claim, issue, ready_owner, runtime, selected, session,  # noqa: F401
)


@pytest.mark.parametrize("field", ["installation", "issuer", "engine", "owner_instance", "launch_sha256",
    "endpoint", "source_revision", "source_content_digest", "config_digest", "receipt_digest"])
def test_issue_compares_actual_owner_and_engine_evidence(ready_owner, field):
    state = ready_owner
    changed = copy.deepcopy(state.engine)
    changed[field] = {} if isinstance(changed[field], dict) else "wrong"
    with pytest.raises(CustodyUnresolved):
        state.owner.request("issue", {"engine": changed, "profile_digest": "sha256:" + "a" * 64}, time.monotonic() + 5)
    # Failed comparisons issue no grant; the exact genuine owner can still issue.
    assert repr(issue(state)) == "<ReleaseOffer private>"
    assert not (state.root / "release-posts").exists()


def test_issue_fails_if_receipt_changed_after_ready_observation(ready_owner):
    state = ready_owner
    (state.root / ".astrid-engine/install.json").write_text("changed after observation")
    with pytest.raises(CustodyUnresolved):
        issue(state)
    assert not (state.root / "release-posts").exists()


def test_claim_is_targeted_single_use_and_blocks_legacy_release(ready_owner):
    state = ready_owner
    offer, target, execution = issue(state), selected(), runtime()
    ticket = offer.delegate(target=target, runtime=execution, deadline=time.monotonic() + 5)
    with pytest.raises(EndpointOwnershipError):
        offer.delegate(target=selected(), runtime=execution, deadline=time.monotonic() + 5)
    credential = {k: ticket[k] for k in ("issuance_id", "secret")}
    payload = {**credential, "target": target, "runtime": execution}
    assert state.owner.request("claim", payload, time.monotonic() + 5) == {"status": "claimed"}
    assert state.owner.request("claim", payload, time.monotonic() + 5) == {"status": "claimed"}
    changed = {**payload, "target": {**target, "nonce": "0" * 32}}
    with pytest.raises(CustodyUnresolved):
        state.owner.request("claim", changed, time.monotonic() + 5)
    with pytest.raises(EndpointOwnershipError):
        state.client.release_owned(deadline=time.monotonic() + 5)
    with pytest.raises(CustodyUnresolved):
        issue(state)
    assert not (state.root / "release-posts").exists()


def test_complete_binding_has_explicit_absence_and_distinct_runtime_namespace(ready_owner):
    state = ready_owner
    claimed = claim(state)
    actual = binding(state)
    record = binding_record(actual)
    assert set(record) == set(actual.__dataclass_fields__)
    assert record["runtime_epoch"] is None
    assert actual.runtime_instance_id != claimed.runtime["instance_id"]
    assert digest(record) != digest({k: v for k, v in record.items() if v is not None})
    # A genuine claim still cannot turn unauthenticated HTTP into exclusive admission.
    with pytest.raises(CustodyUnresolved):
        claimed.bind(actual, 1, runtime=claimed.runtime, admission={"exclusive": True}, deadline=time.monotonic() + 5)
    assert not (state.root / "release-posts").exists()
    with pytest.raises(EndpointOwnershipError):
        claimed.bind(actual, 2, runtime=claimed.runtime, admission={}, deadline=time.monotonic() + 5)


def test_same_pid_wrong_birth_cannot_pin_an_issuer(ready_owner):
    from astrid.core.generation.engine_release_authority import PinnedOwner
    with pytest.raises(EndpointOwnershipError):
        PinnedOwner(ready_owner.client, {**ready_owner.owner.issuer, "birth": "reused"})


@pytest.mark.parametrize("field", [
    "session_id", "runtime_instance_id", "process_birth_id", "endpoint", "source_digest", "config_digest",
    "execution_identity", "runtime_epoch", "launch_generation", "engine_birth_id"])
def test_each_binding_field_participates_in_canonical_identity(ready_owner, field):
    actual = binding(ready_owner)
    changed = replace(actual, **{field: 9 if field == "runtime_epoch" else "different"})
    assert changed.identity_key != actual.identity_key
    assert digest(binding_record(changed)) != digest(binding_record(actual))


def test_handoff_credentials_cannot_follow_an_exited_process(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        selected_peer = process_identity(child.pid)
        assert selected_peer["pid"] != os.getpid()
        child.terminate()
        child.wait(timeout=5)
        with pytest.raises(CustodyUnresolved):
            process_identity(selected_peer["pid"])
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_enrolled_dead_engine_cannot_be_replaced_or_reissued(ready_owner):
    """The original native handle remains in custody after engine death."""
    state = ready_owner
    claimed = claim(state)
    original = state.engine["engine"]
    assert process_identity(original["pid"]) == original
    os.kill(original["pid"], signal.SIGKILL)
    deadline = time.monotonic() + 5
    while True:
        try:
            process_identity(original["pid"])
        except CustodyUnresolved:
            break
        assert time.monotonic() < deadline
        time.sleep(0.01)
    launches = state.started.read_bytes()
    with pytest.raises(CustodyUnresolved):
        state.client.call("start", state.payload, deadline=time.monotonic() + 5)
    with pytest.raises(CustodyUnresolved):
        claimed.bind(binding(state), 1, runtime=claimed.runtime,
                     admission={"mode": "host_serial"}, deadline=time.monotonic() + 5)
    with pytest.raises(CustodyUnresolved):
        issue(state)
    assert state.started.read_bytes() == launches
    assert not (state.root / "release-posts").exists()


def test_original_issuer_does_not_follow_same_installation_successor(ready_owner):
    state = ready_owner
    offer = issue(state)
    original = state.engine["issuer"]
    state.client.stop_owned(deadline=time.monotonic() + 10)
    state.client.call("start", state.payload, deadline=time.monotonic() + 10)
    import json
    marker = json.loads((state.registry / "launch.json").read_bytes())
    successor = process_identity(marker["pid"])
    assert successor != original
    with pytest.raises(CustodyUnresolved, match="original release issuer exited"):
        offer.delegate(target=selected(), runtime=runtime(), deadline=time.monotonic() + 5)
    assert process_identity(marker["comfy_pid"])["birth"] == marker["comfy_process_start_identity"]
    assert not (state.root / "release-posts").exists()


def test_full_readiness_issues_at_original_live_supervisor(tmp_path_factory, tmp_path, monkeypatch):
    """Reuse the labelled readiness fixture, with real pinned VibeComfy checks.

    Models, installed receipt and GPU probe are the existing synthetic CPU
    fixture. The producer runs from the verified retained tree, and the owner,
    connected peer, births, source verification and private publication are real.
    An independent fixture lifetime keeps this grant from affecting other tests.
    """
    from types import SimpleNamespace
    from tests.core.generation import test_engine_readiness as producer
    fixture = producer.installation.__wrapped__(tmp_path_factory, SimpleNamespace(param=producer.ENGINE))
    installed = next(fixture)
    try:
        receipt = installed["root"] / ".astrid-engine/install.json"
        receipt.chmod(0o600)
        state = tmp_path / "private"
        state.mkdir(mode=0o700)
        harness = producer._HARNESS.replace(
            "result=engine_readiness.run_readiness(", "result,offer=engine_readiness.prepare_ready_engine_release(")
        harness = harness.replace("out={'ok':True,", "out={'ok':True,'offer':repr(offer),")
        monkeypatch.setattr(producer, "_HARNESS", harness)
        before = producer._snapshot(installed)
        result = producer._harness(installed, state)
        assert result["ok"] is True, result
        assert result["offer"] == "<ReleaseOffer private>"
        producer._assert_engine_retained(installed, before)
        with pytest.raises(EndpointOwnershipError):
            installed["client"].release_owned(deadline=time.monotonic() + 5)
        profile_bytes = (state / "engine-readiness.json").read_bytes()
        assert b'"issuance_id"' not in profile_bytes and b'"secret"' not in profile_bytes
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
