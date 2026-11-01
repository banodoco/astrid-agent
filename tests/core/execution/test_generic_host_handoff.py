from __future__ import annotations

import json
import hashlib
import copy
import os
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from astrid_runtime_client.generated import Health

from astrid.core.execution import generic_host


@pytest.fixture(autouse=True)
def _release_handoff_journal_locks(monkeypatch):
    """Close the process-lifetime flock fd each preparation keeps on its journal.

    A real host owns that lock until the process exits; these tests build many
    preparations inside one pytest process, so each must be released at teardown.
    """
    acquired = []
    original = generic_host.LocalExecutionPreparation._acquire_handoff_journal

    def tracked(self, scope):
        acquired.append(self)
        return original(self, scope)

    monkeypatch.setattr(generic_host.LocalExecutionPreparation, "_acquire_handoff_journal", tracked)
    yield
    for preparation in acquired:
        descriptor, preparation._handoff_lock_fd = preparation._handoff_lock_fd, None
        if descriptor is not None:
            os.close(descriptor)


def _host(client):
    from astrid_runtime_client.contract_metadata import PROTOCOL

    def health(**changes):
        values = {"status": "ok", "protocol": PROTOCOL, "schema_digest": "sha256:" + "c" * 64,
                  "runtime_epoch": 7, "runtime_session_id": "runtime-session",
                  "runtime_instance_id": "runtime-A"}
        values.update(changes)
        return Health(**values)

    class RuntimeClient:
        endpoint = "http://127.0.0.1:8765"
        schema_digest = "sha256:" + "c" * 64
        def __init__(self, wrapped):
            self.wrapped = wrapped
        def health(self):
            return health()
        def register_executor(self, *args, **kwargs):
            operation = getattr(self.wrapped, "register_executor", None)
            return operation(*args, **kwargs) if callable(operation) else {"accepted": True}
        def register_capability(self, *args, **kwargs):
            operation = getattr(self.wrapped, "register_capability", None)
            return operation(*args, **kwargs) if callable(operation) else {"accepted": True}
        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    runtime_client = RuntimeClient(client)
    host = generic_host.GenericPackHost(pack_roots=[], client=client)
    host.capabilities = {"ready": object()}
    host.discover = lambda: None
    host._preflight = lambda *a, **k: [type("Ready", (), {"id": "ready", "ready": True, "matrix": {}})()]
    host.execution_policy = type("Policy", (), {"assert_budget_available": lambda self: None})()
    host.client = runtime_client
    host.source_epoch = "source-epoch"
    host.capabilities = {"ready": SimpleNamespace(capability_digest="sha256:" + "d" * 64,
                                                    source_digest="e" * 64,
                                                    dependency_digest="f" * 64)}
    host._registered_state = {"ready": {"capability_digest": "sha256:" + "d" * 64,
                                         "source_digest": "e" * 64,
                                         "dependency_digest": "f" * 64}}
    host._registered_runtime_state = {"protocol": PROTOCOL, "schema_digest": runtime_client.schema_digest,
                                      "runtime_epoch": 7, "runtime_session_id": "runtime-session",
                                      "runtime_instance_id": "runtime-A", "coordinator_epoch": "runtime-A",
                                      "source_epoch": host.source_epoch}
    host._registration_refresh_deadline = float("inf")
    return host


def test_review_session_reads_do_not_require_o_nofollow(monkeypatch, tmp_path):
    from types import SimpleNamespace

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    path = attempt / generic_host._REVIEW_SESSION_NAME
    nonce = "a" * 64
    row = {
        "schema": "astrid.human-review-session/v2",
        "receiver_url": "http://127.0.0.1:8123/?token=" + "b" * 32,
        "process_pid": 123,
        "process_birth_id": "receiver-birth",
        "launch_nonce": nonce,
    }
    path.write_text(json.dumps(row))
    path.chmod(0o600)
    process = SimpleNamespace(_astrid_review_launch_nonce=nonce)
    monkeypatch.setattr(generic_host, "_process_birth_identity", lambda pid: "receiver-birth")
    monkeypatch.delattr(generic_host.os, "O_NOFOLLOW")

    snapshot = generic_host._observe_owned_review_session(process, attempt)
    generic_host._retire_owned_review_session(attempt, snapshot)
    assert snapshot is not None
    assert not path.exists()


@pytest.mark.parametrize("is_posix", [True, False])
def test_handoff_journal_keeps_file_fsync_and_only_posix_directory_fsync(monkeypatch, tmp_path, is_posix):
    import stat

    journal = tmp_path / "host-handoff-state.json"
    preparation = generic_host.LocalExecutionPreparation.__new__(generic_host.LocalExecutionPreparation)
    preparation._handoff_journal_ready = True
    preparation._handoff_journal_path = journal
    calls = []
    original_fsync = os.fsync

    def fsync(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        calls.append(is_directory)
        if not is_posix and is_directory:
            raise OSError("Windows refuses directory fsync")
        return original_fsync(fd)

    monkeypatch.setattr(generic_host, "_is_posix", lambda: is_posix, raising=False)
    monkeypatch.setattr(generic_host.os, "fsync", fsync)
    request = {"binding": {"original_owner_epoch": "epoch", "handoff_id": "handoff"}, "command": "pause"}
    reply = {"host": {"pid": 123, "birth_id": "host-birth"}, "request_digest": "digest"}

    assert preparation._persist_handoff_reply(request, reply)
    assert calls == ([False, True] if is_posix else [False])
    assert json.loads(journal.read_text())["reply"] == reply


def _actor(pid=10):
    return {"pid": pid, "uid": 501, "birth_id": f"birth-{pid}",
            "audit_token_sha256": "sha256:" + "a" * 64,
            "audit_token_pidversion": pid}


def _role(role, generation=1):
    return {"version": "runtime.role-custody-reference/v1", "scope_root": "/tmp/custody",
            "role": role, "generation": generation, "target": _actor()}


def _binding(**overrides):
    roles = {role: _role(role) for role in generic_host._HANDOFF_ROLE_NAMES}
    value = {
        "operation_id": "operation", "channel_id": "channel", "handoff_id": "handoff-1",
        "nonce_digest": "sha256:" + "1" * 64, "deadline_unix_ms": int(time.time() * 1000) + 60_000,
        "workspace_uuid": "workspace", "profile_binding_digest": "sha256:" + "2" * 64,
        "launch_evidence_digest": "sha256:" + "3" * 64,
        "activation_record_digest": "sha256:" + "4" * 64,
        "executor_incarnation": "executor-incarnation", "custody_scope": "/tmp/custody",
        "original_owner_epoch": "runtime-A", "new_owner_epoch": "runtime-B", "new_owner": _actor(11),
        "credential_generation_digest": "sha256:" + "5" * 64,
        "original_roles": roles, "intent_digest": "sha256:" + "0" * 64,
        "source_owner": _actor(), "source_owner_epoch": "runtime-A",
        "source_relay_reference": roles["relay"], "current_roles": roles,
    }
    value.update(overrides)
    value["intent_digest"] = generic_host._handoff_digest(
        {"version": generic_host._HANDOFF_VERSION,
         "binding": {k: v for k, v in value.items() if k != "intent_digest"}}
    )
    return value


def _request(command="handoff_prepare", binding=None, payload=None):
    return {"version": generic_host._HANDOFF_VERSION, "command": command,
            "binding": binding or _binding(), "payload": payload or {}}


def _preparation(host, tmp_path, *, real_retained_binding=False):
    scope = tmp_path / "custody"
    scope.mkdir(mode=0o700)
    prep = generic_host.LocalExecutionPreparation(operation_id="operation", channel_id="channel")
    prep._request = {"operation_id": "operation", "channel_id": "channel",
                     "owner_epoch": "runtime-A", "custody_scope": str(scope),
                     "profile": {"workspace_uuid": "workspace", "support_root": str(tmp_path)}}
    prep._activation_grant = {"version": generic_host._ACTIVATION_VERSION,
                              "operation_id": "operation", "channel_id": "channel",
                              "executor_incarnation": "executor-incarnation",
                              "evidence_digest": "sha256:" + "3" * 64}
    if not real_retained_binding:
        # Synthetic fixtures opt out explicitly. Producer-shaped lifecycle
        # fixtures keep the retained preparation validator intact.
        prep._validate_retained_binding = lambda _binding: True
    prep.attach_claim_host(host)
    return prep


def _pause_before_test_rebind(host, prep, binding):
    paused = prep.dispatch(_request("handoff_prepare", binding=binding))
    assert paused["status"] == "ok"
    assert paused["phase"] == "host_paused"
    snapshot = host.handoff_quiescence()
    assert snapshot["claim_gate_closed"] is True
    assert snapshot["observation_status"] == "known"
    assert all(snapshot[key] == 0 for key in (
        "claim_rpc_in_flight", "active_attempts", "pending_settlements",
        "registration_rpc_in_flight",
    ))
    return paused


def test_pause_serializes_with_real_claim_rpc_and_settlement():
    entered, release = threading.Event(), threading.Event()

    class Client:
        def claim_next(self, **_kwargs):
            entered.set()
            assert release.wait(2)
            return None

    host = _host(Client())
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host.claim_once)
        assert entered.wait(2)
        disposition, observed = host.pause_claim_admission()
        assert disposition == "active_work"
        assert observed["claim_rpc_in_flight"] == 1
        assert observed["claim_gate_closed"] is False
        release.set()
        assert future.result(timeout=2) is None

    settlement_entered, settlement_release = threading.Event(), threading.Event()
    def settlement():
        settlement_entered.set()
        assert settlement_release.wait(2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host._handoff_settlement_call, settlement)
        assert settlement_entered.wait(2)
        disposition, observed = host.pause_claim_admission()
        assert disposition == "active_work"
        assert observed["pending_settlements"] == 1
        settlement_release.set()
        future.result(timeout=2)
    disposition, observed = host.pause_claim_admission()
    assert disposition == "paused"
    assert observed["claim_gate_closed"] is True
    assert all(observed[key] == 0 for key in (
        "claim_rpc_in_flight", "active_attempts", "pending_settlements", "registration_rpc_in_flight"))


def test_active_attempt_cannot_acknowledge_pause(tmp_path):
    heartbeat_entered, heartbeat_release = threading.Event(), threading.Event()

    class Client:
        def claim_next(self, **_kwargs):
            return {"task_id": "task", "lease_id": "lease", "attempt_id": "attempt", "fence": 1}
        def heartbeat(self, *_args, **_kwargs):
            heartbeat_entered.set()
            assert heartbeat_release.wait(2)
        def task(self, _task_id):
            return {"task": {"id": "task", "spec": {}}}

    host = _host(Client())
    host.run_task = lambda *_args, **_kwargs: {"status": "done"}
    prep = _preparation(host, tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(host.claim_once)
        assert heartbeat_entered.wait(2)
        reply = prep.dispatch(_request())
        assert reply["status"] == "active_work"
        assert reply["error_code"] == "active_work"
        assert reply["quiescence"]["active_attempts"] == 1
        assert reply["quiescence"]["claim_rpc_in_flight"] == 0
        assert not (tmp_path / "custody" / "host-handoff-state.json").exists()
        heartbeat_release.set()
        future.result(timeout=2)
    assert host.handoff_quiescence()["active_attempts"] == 0


def test_rebind_refuses_registration_without_durable_runtime_fence(tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    paused = prep.dispatch(_request(binding=binding))
    assert paused["status"] == "ok" and paused["phase"] == "host_paused"
    journal = json.loads((tmp_path / "custody" / "host-handoff-state.json").read_text())
    assert journal["request_digest"] == paused["request_digest"]
    assert journal["reply"] == paused
    assert journal["writer_owner_epoch"] == "runtime-A"
    before_client = host.client
    prep._handoff_phase = "adopt_prepared"
    reply = prep.dispatch(_request("handoff_commit", binding=binding, payload={
        "new_runtime": {}, "task_fence_digest": "sha256:" + "6" * 64,
        "relay_reference": _role("relay", 2)}))
    assert reply["status"] == "unresolved"
    assert reply["error_code"] == "fence_unresolved"
    assert host.client is before_client
    assert host.handoff_quiescence()["claim_gate_closed"] is True


def test_resume_finalize_replay_and_changed_binding(tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    assert prep.dispatch(_request(binding=binding))["phase"] == "host_paused"
    request = _request("resume_commit", binding=binding, payload={
        "new_runtime": {}, "task_fence_digest": "sha256:" + "6" * 64,
        "registered_state_digest": "sha256:" + "7" * 64})
    prep._handoff_phase = "resume_armed"
    first = prep.dispatch(request)
    assert prep.dispatch(request) == first
    assert first["status"] == "unresolved" and first["error_code"] == "fence_unresolved"
    changed = _binding(handoff_id="different")
    conflict = prep.dispatch(_request("handoff_prepare", changed))
    assert conflict["status"] == "conflict"
    assert conflict["error_code"] == "binding_conflict"
    assert host.handoff_quiescence()["claim_gate_closed"] is True


def test_unknown_control_and_cleanup_leave_claims_closed(tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    assert prep.dispatch(_request(binding=binding))["status"] == "ok"
    with pytest.raises(generic_host.HostError):
        prep.dispatch(_request("not-a-command", binding=binding))
    host._handoff_observation_known = False
    reply = prep.dispatch(_request("handoff_report", binding=binding))
    assert reply["status"] == "ok"
    assert reply["quiescence"]["observation_status"] == "unknown"
    assert reply["quiescence"]["claim_gate_closed"] is None
    abort = prep.dispatch(_request("handoff_abort", binding=binding,
                                   payload={"reason_code": "operator_cancelled"}))
    assert abort["status"] == "unresolved"
    assert abort["error_code"] == "custody_unresolved"
    assert host._handoff_claim_gate_closed is True


@pytest.mark.parametrize("command,payload,error", [
    ("handoff_export_sealed", {"export_metadata": {}, "seal_record": {}, "sealed_record_digest": "sha256:" + "8" * 64}, "fence_unresolved"),
    ("handoff_adopt", {"export_digest": "sha256:" + "8" * 64, "sealed_record_digest": "sha256:" + "9" * 64,
                        "task_fence_digest": "sha256:" + "a" * 64, "relay_transfer_ack": {},
                        "successor_authentication_digest": "sha256:" + "b" * 64}, "identity_unresolved"),
    ("resume_prepare", {"new_runtime": {}, "task_fence_digest": "sha256:" + "a" * 64,
                         "registered_state_digest": "sha256:" + "b" * 64}, "fence_unresolved"),
    ("handoff_finalize", {"registered_state_digest": "sha256:" + "a" * 64,
                          "task_fence_digest": "sha256:" + "b" * 64}, "fence_unresolved"),
])
def test_missing_runtime_fence_or_successor_authentication_refuses_resume(command, payload, error, tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    assert prep.dispatch(_request(binding=binding))["phase"] == "host_paused"
    phases = {"handoff_export_sealed": "host_paused", "handoff_adopt": "export_sealed",
              "resume_prepare": "rebind_committed", "handoff_finalize": "resumed"}
    prep._handoff_phase = phases[command]
    reply = prep.dispatch(_request(command, binding=binding, payload=payload))
    assert reply["status"] == "unresolved"
    assert reply["error_code"] == error
    assert host._handoff_claim_gate_closed is True


def test_duplicate_keys_and_unknown_quiescence_cannot_ack_pause(tmp_path):
    left, right = socket.socketpair()
    try:
        right.sendall(b'{"command":"handoff_prepare","command":"handoff_abort"}\n')
        with pytest.raises(generic_host.HostError):
            generic_host._read_activation_frame(left)
    finally:
        left.close()
        right.close()

    host = _host(client=object())
    prep = _preparation(host, tmp_path)
    host._handoff_observation_known = False
    reply = prep.dispatch(_request())
    assert reply["status"] == "unresolved"
    assert reply["error_code"] == "quiescence_unresolved"
    assert host._handoff_claim_gate_closed is True
    assert reply["quiescence"]["active_attempts"] is None


def test_expired_pause_preserves_cached_success_and_closed_gate(tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    request = _request(binding=binding)
    reply = prep.dispatch(request)
    assert prep.dispatch(request) == reply  # exact replay before expiry is read-only
    deadline = request["binding"]["deadline_unix_ms"]
    before_client = host.client
    monkeypatch.setattr(generic_host.time, "time", lambda: deadline / 1000 + 60)
    expired_pause = prep.dispatch(request)
    assert expired_pause["status"] == "unresolved"
    assert expired_pause["error_code"] == "deadline_expired"
    assert prep._handoff_replies[reply["request_digest"]] == reply
    assert prep._handoff_phase == "host_paused"
    assert host._handoff_claim_gate_closed is True
    durable = json.loads((tmp_path / "custody" / "host-handoff-state.json").read_text())
    assert durable["reply"] == reply
    # Direct dispatch has no independent requester authentication proof, so
    # even a retained pause receipt cannot be replayed after expiry here.
    blocked = prep.dispatch(_request("handoff_adopt", binding=request["binding"], payload={
        "export_digest": "sha256:" + "8" * 64, "sealed_record_digest": "sha256:" + "9" * 64,
        "task_fence_digest": "sha256:" + "a" * 64, "relay_transfer_ack": {},
        "successor_authentication_digest": "sha256:" + "b" * 64}))
    assert blocked["status"] == "unresolved"
    assert blocked["error_code"] == "deadline_expired"
    assert host._handoff_claim_gate_closed is True
    assert host.client is before_client
    assert (tmp_path / "custody" / "host-handoff-state.json").exists()


def test_expired_unseen_binding_does_not_pin_and_valid_retry_succeeds(tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    expired = _binding(**{**binding, "deadline_unix_ms": int(time.time() * 1000) - 60_000})
    refusal = prep.dispatch(_request(binding=expired))
    assert refusal["status"] == "unresolved"
    assert refusal["error_code"] == "deadline_expired"
    assert prep._handoff_binding is None
    assert prep._handoff_binding_digest is None
    assert prep._handoff_replies == {}
    assert prep._handoff_phase == "owned"
    assert host.handoff_quiescence()["claim_gate_closed"] is False
    assert not (tmp_path / "custody" / "host-handoff-state.json").exists()

    valid = _binding(**{**binding, "handoff_id": "valid-after-expiry"})
    accepted = prep.dispatch(_request(binding=valid))
    assert accepted["status"] == "ok"
    assert prep._handoff_binding_digest == generic_host._handoff_digest(valid)
    assert host.handoff_quiescence()["claim_gate_closed"] is True
    assert (tmp_path / "custody" / "host-handoff-state.json").exists()


def test_excessively_future_binding_does_not_pin(tmp_path):
    host = _host(client=object())
    prep = _preparation(host, tmp_path)
    future = _binding(deadline_unix_ms=int(time.time() * 1000) + 86_460_000)
    with pytest.raises(generic_host.HostError, match="deadline exceeds the local horizon"):
        prep.dispatch(_request(binding=future))
    assert prep._handoff_binding is None
    assert prep._handoff_binding_digest is None
    assert prep._handoff_replies == {}
    assert prep._handoff_phase == "owned"
    assert host.handoff_quiescence()["claim_gate_closed"] is False
    assert not (tmp_path / "custody" / "host-handoff-state.json").exists()


def test_changed_binding_conflicts_even_after_expiry(tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    original_request = _request(binding=binding)
    success = prep.dispatch(original_request)
    deadline = binding["deadline_unix_ms"]
    monkeypatch.setattr(generic_host.time, "time", lambda: deadline / 1000 + 60)
    changed = _binding(handoff_id="changed-after-expiry", deadline_unix_ms=deadline - 1)
    conflict = prep.dispatch(_request(binding=changed))
    assert conflict["status"] == "conflict"
    assert conflict["error_code"] == "binding_conflict"
    assert prep._handoff_binding_digest == generic_host._handoff_digest(binding)
    assert prep._handoff_replies[success["request_digest"]] == success
    assert prep._handoff_phase == "host_paused"
    assert host.handoff_quiescence()["claim_gate_closed"] is True


def _adoption_fixture(tmp_path, *, handoff_id="handoff-1", adopt=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    tmp_path.chmod(0o700)
    support_root_info = tmp_path.stat()
    assert support_root_info.st_uid == os.getuid()
    assert stat.S_IMODE(support_root_info.st_mode) == 0o700
    # Positive lifecycle fixture uses the real retained-binding/role compiler;
    # CPU actor providers are scoped only to its initial native-shaped pause.
    with pytest.MonkeyPatch.context() as actor_patch:
        host, prep, binding, roles, _write = _native_two_role_pause_fixture(
            tmp_path, actor_patch, generations={role: 1 for role in generic_host._HANDOFF_ROLE_NAMES})
        scope = tmp_path / "custody"
        profile = prep._request["profile"]
        commit = tmp_path / "credential.commit"
        commit.write_bytes(b"accepted credential generation")
        commit.chmod(0o600)
        prep._activation_grant["credential_file"] = str(tmp_path / "credential")
        binding = _binding(**{**binding, "handoff_id": handoff_id,
            "credential_generation_digest": "sha256:" + hashlib.sha256(commit.read_bytes()).hexdigest()})
        assert prep._validate_retained_binding(binding)
        expected = generic_host._handoff_registered_state(host, prep._activation_grant)
        assert expected is not None
        fence = {"version": "runtime.local-execution-claim-fence/v1", "state": "held",
                 "workspace_uuid": binding["workspace_uuid"],
                 "executor_incarnation": binding["executor_incarnation"],
                 "credential_generation_digest": binding["credential_generation_digest"],
                 "operation_id": binding["operation_id"], "handoff_id": binding["handoff_id"],
                 "intent_digest": binding["intent_digest"], "source_owner_epoch": binding["source_owner_epoch"],
                 "target_owner_epoch": binding["new_owner_epoch"], "fence_generation": 4,
                 "release_ack_digest": None}
        fence_path = tmp_path / "local-execution-claim-fence.json"
        fence_path.write_text(json.dumps(fence, sort_keys=True, separators=(",", ":")))
        fence_path.chmod(0o600)
        fence_digest = generic_host._handoff_digest(fence)
        paused = prep.dispatch(_request(binding=binding))
        assert paused["status"] == "ok"
        assert paused["registered_state"] == expected
        assert paused["custody_capabilities"] == roles
        assert json.loads((scope / "host-handoff-state.json").read_text())["reply"] == paused

    prep._retained_relay_actor = type("Relay", (), {"verify": lambda _self: binding["source_relay_reference"]["target"]})()
    pause_request = _request(binding=binding)
    pause_digest = generic_host._handoff_digest(pause_request)
    pause_entry = {"request_digest": pause_digest, "binding_digest": generic_host._handoff_digest(binding),
                   "reply": paused}
    metadata = {"version": "runtime.local-execution-handoff-export/v1",
                "handoff_id": binding["handoff_id"], "intent_digest": binding["intent_digest"],
                "nonce_digest": binding["nonce_digest"], "host_pause_ack_digest": generic_host._handoff_digest(paused),
                "task_fence_digest": fence_digest,
                "credential_generation_digest": binding["credential_generation_digest"],
                "source_owner_epoch": binding["source_owner_epoch"],
                "source_relay_reference": binding["source_relay_reference"],
                "descriptor_identity_digest": "sha256:" + "a" * 64,
                "launch_evidence_digest": binding["launch_evidence_digest"]}
    seal = {"version": "runtime.local-execution-handoff-seal/v1",
            "handoff_id": binding["handoff_id"], "intent_digest": binding["intent_digest"],
            "nonce_digest": binding["nonce_digest"], "export_digest": generic_host._handoff_digest(metadata),
            "host_pause_ack_digest": generic_host._handoff_digest(paused), "task_fence_digest": fence_digest,
            "credential_generation_digest": binding["credential_generation_digest"],
            "source_owner_epoch": binding["source_owner_epoch"], "target_owner_epoch": binding["new_owner_epoch"],
            "source_relay_reference": binding["source_relay_reference"], "successor_incarnation": binding["new_owner"]}
    sealed = {"export_metadata": metadata, "seal_record": seal,
              "sealed_record_digest": generic_host._handoff_digest(seal)}
    export_request = _request("handoff_export_sealed", binding=binding, payload=sealed)
    pending_export = {"request_digest": generic_host._handoff_digest(export_request),
                      "binding_digest": generic_host._handoff_digest(binding), "reply": None}
    entries = {f"{binding['handoff_id']}:handoff_prepare": pause_entry,
               f"{binding['handoff_id']}:handoff_export_sealed": pending_export}
    journal_base = {"version": generic_host._HANDOFF_VERSION,
                    "writer_owner_epoch": binding["source_owner_epoch"], "writer_generation": 1,
                    "phase": "host_paused", "binding_digest": generic_host._handoff_digest(binding),
                    "binding": binding, "entries": entries}
    runtime_journal = {**copy.deepcopy(journal_base), "writer_incarnation": binding["source_owner"]}
    relay_journal = {**copy.deepcopy(journal_base), "writer_incarnation": binding["source_relay_reference"]["target"],
                     "writer_owner_epoch": binding["original_owner_epoch"],
                     "writer_generation": binding["source_relay_reference"]["generation"]}
    for filename, journal in (("runtime-handoff-state.json", runtime_journal),
                              ("relay-handoff-state.json", relay_journal)):
        path = scope / filename
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
    exported = prep.dispatch(export_request)
    assert exported["status"] == "ok"
    assert exported["registered_state"] == expected
    assert prep._handoff_registered_state == expected

    transition_id = "relay-transfer:" + generic_host._handoff_digest({
        "handoff_id": binding["handoff_id"], "intent_digest": binding["intent_digest"],
        "source_relay_reference": binding["source_relay_reference"],
    })
    relay_reference = {**binding["source_relay_reference"], "generation": 2}
    ack = {"version": "runtime.role-custody-designation/v1", "transition_id": transition_id,
           "role": "relay", "generation": 2, "owner_epoch": binding["new_owner_epoch"],
           "actor": binding["new_owner"], "target": binding["source_relay_reference"]["target"]}
    transfer_intent = {"transition_id": transition_id, "binding_digest": generic_host._handoff_digest(binding),
                       "from_writer": binding["source_owner"], "from_epoch": binding["source_owner_epoch"],
                       "from_generation": 1, "to_writer": binding["new_owner"],
                       "to_epoch": binding["new_owner_epoch"],
                       "source_relay_reference": binding["source_relay_reference"]}
    current_roles = dict(binding["current_roles"])
    current_roles["relay"] = relay_reference
    transfer = {"state": "committed", "intent": transfer_intent, "relay_ack": ack}
    ownership = {"owner": binding["new_owner"], "owner_epoch": binding["new_owner_epoch"],
                 "relay_reference": relay_reference, "current_roles": current_roles,
                 "previous_projection_digest": "sha256:" + "b" * 64}
    runtime_journal.update({"phase": "export_sealed", "writer_incarnation": binding["new_owner"],
                            "writer_owner_epoch": binding["new_owner_epoch"], "writer_generation": 2,
                            "sealed_export": sealed, "writer_transfer": transfer, "ownership": ownership})
    runtime_journal["entries"][f"{binding['handoff_id']}:handoff_export_sealed"] = {
        **pending_export, "reply": exported}

    adopt_payload = {"export_digest": generic_host._handoff_digest(metadata),
                     "sealed_record_digest": sealed["sealed_record_digest"],
                     "task_fence_digest": fence_digest, "relay_transfer_ack": ack,
                     "successor_authentication_digest": generic_host._handoff_digest({
                         "version": "runtime.local-execution-successor-auth/v1",
                         "binding_digest": generic_host._handoff_digest(binding),
                         "successor_peer": binding["new_owner"],
                         "relay_peer": binding["source_relay_reference"]["target"],
                     })}
    adopt_request = _request("handoff_adopt", binding=binding, payload=adopt_payload)
    pending_adopt = {"request_digest": generic_host._handoff_digest(adopt_request),
                     "binding_digest": generic_host._handoff_digest(binding), "reply": None}
    relay_journal.update({"phase": "export_sealed", "writer_generation": 2})
    relay_journal["entries"][f"{binding['handoff_id']}:handoff_export_sealed"] = {
        **pending_export, "reply": exported}
    relay_journal["entries"][f"{binding['handoff_id']}:handoff_adopt"] = pending_adopt

    for role, reference in current_roles.items():
        actor = binding["new_owner"] if role == "relay" else binding["source_owner"]
        transitions = []
        if role == "relay":
            transitions = [{"transition_id": transition_id,
                            "intent": {"transition_id": transition_id, "from_generation": 1,
                                       "requester": binding["source_owner"], "next_actor": binding["new_owner"],
                                       "owner_epoch": binding["new_owner_epoch"]},
                            "ack": ack}]
        designation = {"version": "runtime.role-custody-designation/v1", "role": role,
                       "generation": reference["generation"],
                       "owner_epoch": binding["new_owner_epoch"] if role == "relay" else binding["original_owner_epoch"],
                       "state": "active", "actor": actor,
                       "target": {**reference["target"], "audit_token_words": [1, 2, 3, 4]},
                       "transitions": transitions}
        designation["digest"] = generic_host._handoff_digest(designation)
        designation_path = scope / f"designation-{role}.json"
        designation_path.write_text(json.dumps(designation, sort_keys=True, separators=(",", ":")))
        designation_path.chmod(0o600)
    for filename, journal in (("runtime-handoff-state.json", runtime_journal),
                              ("relay-handoff-state.json", relay_journal)):
        path = scope / filename
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
    assert f"{binding['handoff_id']}:handoff_adopt" not in runtime_journal["entries"]
    assert relay_journal["entries"][f"{binding['handoff_id']}:handoff_adopt"]["reply"] is None
    adopted = prep.dispatch(adopt_request) if adopt else None
    if adopt:
        assert adopted["status"] == "ok"
        assert adopted["phase"] == "adopt_prepared"
        assert adopted["registered_state"] == expected
        runtime_journal["phase"] = "adopt_prepared"
        runtime_journal["entries"][f"{binding['handoff_id']}:handoff_adopt"] = {
            **pending_adopt, "reply": adopted}
        relay_journal["phase"] = "adopt_prepared"
        relay_journal["entries"][f"{binding['handoff_id']}:handoff_adopt"] = {
            **pending_adopt, "reply": adopted}
        for name, journal in (("runtime-handoff-state.json", runtime_journal),
                              ("relay-handoff-state.json", relay_journal)):
            path = scope / name
            path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
            path.chmod(0o600)
        host_record = json.loads((scope / "host-handoff-state.json").read_text())
        assert host_record["command"] == "handoff_adopt"
        assert host_record["reply"] == adopted
    return {"host": host, "prep": prep, "binding": binding, "expected": expected,
            "fence": fence, "fence_digest": fence_digest, "scope": scope,
            "runtime_journal": runtime_journal, "relay_journal": relay_journal,
            "adopt_request": adopt_request, "adopted": adopted}


def test_pause_and_export_preserve_actual_registered_state(tmp_path):
    fixture = _adoption_fixture(tmp_path)
    assert fixture["adopted"]["registered_state"] == fixture["expected"]


def _resumed_fixture(tmp_path, *, resume=True):
    fixture = _adoption_fixture(tmp_path)
    host, prep, binding = fixture["host"], fixture["prep"], fixture["binding"]
    host.capabilities = {"ready": SimpleNamespace(
        id="ready", capability_digest="sha256:" + "d" * 64,
        source_digest="e" * 64, dependency_digest="f" * 64,
        matrix={}, ready=True, preflight={}, resource_keys=(), estimated_scratch_bytes=0,
        estimated_output_bytes=0, manifest=lambda: {"id": "ready", "ready": True},
    )}
    live = host.client.health()
    host.client.health = lambda: Health(status=live.status, protocol=live.protocol,
                                        schema_digest=live.schema_digest, runtime_epoch=8,
                                        runtime_session_id="runtime-session-B",
                                        runtime_instance_id="runtime-B")
    runtime = generic_host._handoff_live_runtime_binding(host)
    assert runtime is not None
    relay_reference = prep._handoff_current_roles["relay"]
    rebind_request = _request("handoff_commit", binding=binding, payload={
        "new_runtime": runtime, "task_fence_digest": fixture["fence_digest"],
        "relay_reference": relay_reference,
    })
    rebind = prep.dispatch(rebind_request)
    assert rebind["status"] == "ok"
    assert rebind["phase"] == "rebind_committed"
    assert host._registered_runtime_state["runtime_instance_id"] == "runtime-B"
    # Runtime and Relay durably acknowledge Astrid's rebind response before
    # either resume command is produced.
    for journal in (fixture["runtime_journal"], fixture["relay_journal"]):
        journal["phase"] = "rebind_committed"
        journal["entries"][f"{binding['handoff_id']}:handoff_commit"] = {
            "request_digest": generic_host._handoff_digest(rebind_request),
            "binding_digest": generic_host._handoff_digest(binding), "reply": rebind}
    for name, journal in (("runtime-handoff-state.json", fixture["runtime_journal"]),
                          ("relay-handoff-state.json", fixture["relay_journal"])):
        path = fixture["scope"] / name
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
    if not resume:
        return fixture
    payload = {"new_runtime": runtime, "task_fence_digest": fixture["fence_digest"],
               "registered_state_digest": prep._handoff_registered_state_digest}
    arm_request = _request("resume_prepare", binding=binding, payload=payload)
    armed = prep.dispatch(arm_request)
    assert armed["status"] == "ok"
    for journal, name in ((fixture["runtime_journal"], "runtime-handoff-state.json"),
                          (fixture["relay_journal"], "relay-handoff-state.json")):
        journal["phase"] = "resume_armed"
        journal["entries"][f"{binding['handoff_id']}:resume_prepare"] = {
            "request_digest": generic_host._handoff_digest(arm_request),
            "binding_digest": generic_host._handoff_digest(binding), "reply": armed}
        path = fixture["scope"] / name
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
    commit_request = _request("resume_commit", binding=binding, payload=payload)
    committed = prep.dispatch(commit_request)
    assert committed["status"] == "ok"
    for journal, name in ((fixture["runtime_journal"], "runtime-handoff-state.json"),
                          (fixture["relay_journal"], "relay-handoff-state.json")):
        journal["phase"] = "resumed"
        journal["entries"][f"{binding['handoff_id']}:resume_commit"] = {
            "request_digest": generic_host._handoff_digest(commit_request),
            "binding_digest": generic_host._handoff_digest(binding), "reply": committed}
        path = fixture["scope"] / name
        path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
    return fixture


def test_successor_evidence_rejects_forged_stale_or_changed_binding(tmp_path):
    cases = ("forged_authentication", "changed_retained_binding", "stale_fence_generation",
             "wrong_successor", "stale_relay_designation")
    for index, case in enumerate(cases):
        fixture = _adoption_fixture(tmp_path / str(index), adopt=False)
        prep, binding = fixture["prep"], fixture["binding"]
        request = fixture["adopt_request"]
        if case == "forged_authentication":
            request["payload"]["successor_authentication_digest"] = "sha256:" + "8" * 64
            relay = fixture["relay_journal"]
            relay["entries"][f"{binding['handoff_id']}:handoff_adopt"]["request_digest"] = (
                generic_host._handoff_digest(request))
            path = fixture["scope"] / "relay-handoff-state.json"
            path.write_text(json.dumps(relay, sort_keys=True, separators=(",", ":")))
            path.chmod(0o600)
        elif case == "changed_retained_binding":
            prep._request["profile"]["workspace_uuid"] = "different-workspace"
        elif case == "stale_fence_generation":
            changed = dict(fixture["fence"], fence_generation=fixture["fence"]["fence_generation"] + 1)
            (fixture["scope"].parent / "local-execution-claim-fence.json").write_text(
                json.dumps(changed, sort_keys=True, separators=(",", ":")))
            (fixture["scope"].parent / "local-execution-claim-fence.json").chmod(0o600)
        elif case == "wrong_successor":
            journal = fixture["runtime_journal"]
            journal["writer_incarnation"] = _actor(12)
            path = fixture["scope"] / "runtime-handoff-state.json"
            path.write_text(json.dumps(journal, sort_keys=True, separators=(",", ":")))
            path.chmod(0o600)
        else:
            path = fixture["scope"] / "designation-relay.json"
            designation = json.loads(path.read_text())
            designation["actor"] = _actor(12)
            designation["digest"] = generic_host._handoff_digest(
                {key: value for key, value in designation.items() if key != "digest"})
            path.write_text(json.dumps(designation, sort_keys=True, separators=(",", ":")))
            path.chmod(0o600)
        refused = prep.dispatch(request)
        assert refused["status"] == "unresolved", case
        assert refused["error_code"] == "identity_unresolved", case
        assert prep._handoff_phase == "export_sealed", case
        assert fixture["host"].handoff_quiescence()["claim_gate_closed"] is True


def test_rebind_registers_once_while_claim_gate_remains_closed(tmp_path, monkeypatch):
    calls = []
    class Runtime:
        def register_executor(self, executor_id, **kwargs):
            calls.append({"executor_id": executor_id, "in_flight": host._handoff_registration_rpc_in_flight,
                          "gate_closed": host._handoff_claim_gate_closed, "kwargs": kwargs})
            return {"registered": True}
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    host.client.wrapped = Runtime()
    record = SimpleNamespace(id="ready", capability_digest="sha256:" + "d" * 64,
                             source_digest="e" * 64, dependency_digest="f" * 64,
                             matrix={}, ready=True, preflight={}, resource_keys=(), estimated_scratch_bytes=0,
                             estimated_output_bytes=0, manifest=lambda: {"id": "ready", "ready": True})
    host.capabilities = {"ready": record}
    monkeypatch.setattr(generic_host, "_registration_verified_facts", lambda: [])
    _pause_before_test_rebind(host, prep, binding)
    task_fence_digest = "sha256:" + "6" * 64
    relay = {**binding["current_roles"]["relay"], "generation": binding["current_roles"]["relay"]["generation"] + 1}
    prep._handoff_phase = "adopt_prepared"
    prep._handoff_binding_digest = generic_host._handoff_digest(binding)
    prep._handoff_sealed_export = {"seal_record": {"task_fence_digest": task_fence_digest}}
    prep._handoff_current_roles = dict(binding["current_roles"])
    prep._handoff_current_roles["relay"] = relay
    prep._verify_finalization_context = lambda _request, **_kwargs: {"registered_state": None}
    publications = []
    finish_registration = host._handoff_end_registration
    def observe_published_registration():
        publications.append((host._handoff_registration_rpc_in_flight,
                             dict(host._registered_state), dict(host._registered_runtime_state)))
        finish_registration()
    monkeypatch.setattr(host, "_handoff_end_registration", observe_published_registration)
    live = host.client.health()
    host.client.health = lambda: Health(status=live.status, protocol=live.protocol,
                                        schema_digest=live.schema_digest, runtime_epoch=8,
                                        runtime_session_id="runtime-session-B",
                                        runtime_instance_id="runtime-B")
    new_runtime = generic_host._handoff_live_runtime_binding(host)
    request = _request("handoff_commit", binding=binding, payload={
        "new_runtime": new_runtime, "task_fence_digest": task_fence_digest,
        "relay_reference": relay,
    })
    committed = prep.dispatch(request)
    assert committed["status"] == "ok"
    assert len(calls) == 1
    assert calls[0]["in_flight"] == 1
    assert calls[0]["gate_closed"] is True
    assert calls[0]["kwargs"]["source_epoch"] == host.source_epoch
    assert publications[0][0] == 1
    assert publications[0][1] == host._registered_state
    assert publications[0][2]["runtime_instance_id"] == "runtime-B"
    assert host.handoff_quiescence()["claim_gate_closed"] is True
    assert host.handoff_quiescence()["registration_rpc_in_flight"] == 0


def test_resume_rechecks_fence_registration_and_current_roles(tmp_path):
    for index, drift in enumerate(("fence", "registration", "roles")):
        fixture = _resumed_fixture(tmp_path / f"resume-{index}", resume=False)
        host, prep, binding = fixture["host"], fixture["prep"], fixture["binding"]
        if drift == "fence":
            path = fixture["scope"].parent / "local-execution-claim-fence.json"
            changed = dict(fixture["fence"], fence_generation=fixture["fence"]["fence_generation"] + 1)
            path.write_text(json.dumps(changed, sort_keys=True, separators=(",", ":")))
            path.chmod(0o600)
        elif drift == "registration":
            host.capabilities["ready"].source_digest = "0" * 64
        else:
            path = fixture["scope"] / "designation-relay.json"
            designation = json.loads(path.read_text())
            designation["actor"] = _actor(12)
            designation["digest"] = generic_host._handoff_digest(
                {key: value for key, value in designation.items() if key != "digest"})
            path.write_text(json.dumps(designation, sort_keys=True, separators=(",", ":")))
            path.chmod(0o600)
        payload = {"new_runtime": prep._handoff_runtime_binding,
                   "task_fence_digest": fixture["fence_digest"],
                   "registered_state_digest": prep._handoff_registered_state_digest}
        request = _request("resume_prepare", binding=binding, payload=payload)
        assert generic_host._handoff_digest(request) not in prep._handoff_replies
        reply = prep.dispatch(request)
        assert reply["status"] == "unresolved", drift
        assert reply["error_code"] == "fence_unresolved", drift
        assert prep._handoff_phase == "rebind_committed", drift
        assert host.handoff_quiescence()["claim_gate_closed"] is True

    def finalization_case(root, *, drift=None, block_health=False, fail_persist=False):
        fixture = _resumed_fixture(root)
        host, prep, binding = fixture["host"], fixture["prep"], fixture["binding"]
        state_digest = prep._handoff_registered_state_digest
        task_digest = fixture["fence_digest"]
        if fail_persist:
            prep._persist_handoff_reply = lambda _request, _reply: False
        entered, release = threading.Event(), threading.Event()
        original_health = host.client.health
        if block_health:
            def blocked_health():
                entered.set()
                assert release.wait(2)
                return original_health()
            host.client.health = blocked_health
        request = _request("handoff_finalize", binding=binding, payload={
            "registered_state_digest": state_digest, "task_fence_digest": task_digest,
        })
        if block_health:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(prep.dispatch, request)
                assert entered.wait(2)
                # A concurrent observer can take the claim condition during
                # Runtime health; the external RPC is outside that boundary.
                with host._handoff_condition:
                    assert host._handoff_claim_gate_closed is True
                if drift == "client":
                    host.client = _host(client=object()).client
                elif drift == "registration":
                    host._registered_state["ready"]["source_digest"] = "0" * 64
                elif drift == "binding":
                    prep._handoff_binding_digest = "sha256:" + "9" * 64
                release.set()
                reply = future.result(timeout=3)
        else:
            reply = prep.dispatch(request)
        return host, prep, reply

    host, _prep, finalized = finalization_case(tmp_path / "finalized", block_health=True)
    assert finalized["status"] == "ok"
    assert finalized["phase"] == "finalized"
    assert host.handoff_quiescence()["claim_gate_closed"] is False
    for index, drift in enumerate(("client", "registration", "binding")):
        host, _prep, refused = finalization_case(tmp_path / f"drift-{index}", drift=drift,
                                                 block_health=True)
        assert refused["status"] == "unresolved"
        assert host.handoff_quiescence()["claim_gate_closed"] is True
    host, prep, refused = finalization_case(tmp_path / "persist-failure", fail_persist=True)
    assert refused["status"] == "unresolved"
    assert refused["registered_state"] is None
    assert prep._handoff_finalize_persistence_failed is True
    assert host.handoff_quiescence()["claim_gate_closed"] is True


def test_committed_handoff_replay_does_not_repeat_registration(tmp_path, monkeypatch):
    host, prep, binding, _refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    _pause_before_test_rebind(host, prep, binding)
    task_fence_digest = "sha256:" + "6" * 64
    relay = {**binding["current_roles"]["relay"], "generation": binding["current_roles"]["relay"]["generation"] + 1}
    prep._handoff_phase = "adopt_prepared"
    prep._handoff_binding_digest = generic_host._handoff_digest(binding)
    prep._handoff_sealed_export = {"seal_record": {"task_fence_digest": task_fence_digest}}
    prep._handoff_current_roles = dict(binding["current_roles"])
    prep._handoff_current_roles["relay"] = relay
    prep._verify_finalization_context = lambda _request, **_kwargs: {"registered_state": None}
    live = host.client.health()
    host.client.health = lambda: Health(status=live.status, protocol=live.protocol,
                                        schema_digest=live.schema_digest, runtime_epoch=8,
                                        runtime_session_id="runtime-session-B",
                                        runtime_instance_id="runtime-B")
    new_runtime = generic_host._handoff_live_runtime_binding(host)
    calls = []
    def register(*, deliberate=False):
        host._handoff_begin_registration()
        try:
            calls.append(deliberate)
            host._registered_runtime_state.update({
                "runtime_epoch": 8, "runtime_session_id": "runtime-session-B",
                "runtime_instance_id": "runtime-B", "coordinator_epoch": "runtime-B",
            })
        finally:
            host._handoff_end_registration()
    host.register = register
    request = _request("handoff_commit", binding=binding, payload={
        "new_runtime": new_runtime, "task_fence_digest": task_fence_digest,
        "relay_reference": relay,
    })
    first = prep.dispatch(request)
    assert first["status"] == "ok"
    assert prep.dispatch(request) == first
    assert calls == [True]
    assert host.handoff_quiescence()["claim_gate_closed"] is True

    # A new process cannot infer success from an old host journal. The
    # retained outcome stays fail-closed until Runtime reconciles it.
    os.close(prep._handoff_lock_fd)
    prep._handoff_lock_fd = None
    restarted = generic_host.LocalExecutionPreparation(operation_id="operation", channel_id="channel")
    restarted._request = {"custody_scope": str(tmp_path / "custody")}
    restarted.attach_claim_host(host)
    assert host.handoff_quiescence()["claim_gate_closed"] is None
    assert host._handoff_observation_known is False


# Native-shaped ownership evidence, CPU actor providers only: no kernel/native
# signaling qualification. The graph exposes exactly its two delegated roles;
# host/relay evidence is read independently from protected designation files.
def _native_two_role_pause_fixture(tmp_path, monkeypatch, *, generations=None):
    from astrid.core.execution import custody_broker
    tmp_path.chmod(0o700)
    host = _host(object())
    prep = _preparation(host, tmp_path, real_retained_binding=True)
    scope = tmp_path / "custody"
    host_actor = {**_actor(os.getpid()), "uid": os.getuid(),
                  "birth_id": generic_host.process_birth_identity()}
    source_actor, relay_actor = _actor(20), _actor(21)
    targets = {"relay": relay_actor, "host": host_actor,
               "engine": _actor(22), "engine_listener": _actor(23)}
    generations = generations or {"relay": 7, "host": 4, "engine": 2, "engine_listener": 3}
    refs = {role: {**_role(role, generations[role]), "scope_root": str(scope), "target": target}
            for role, target in targets.items()}
    def write_designation(role, **changes):
        record = {"version": "runtime.role-custody-designation/v1", "role": role,
                  "generation": generations[role], "owner_epoch": "runtime-A", "state": "active",
                  "actor": source_actor if role == "relay" else relay_actor if role == "host" else host_actor,
                  "target": targets[role], "transitions": []}
        record.update(changes)
        record["digest"] = generic_host._handoff_digest(record)
        path = scope / f"designation-{role}.json"
        path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
        path.chmod(0o600)
        return path
    for role in refs: write_designation(role)
    class TwoRoleGraph:
        def known_custody_capabilities(self):
            return {role: custody_broker.RoleCustodyAuthority(scope, role).reference()
                    for role in ("engine", "engine_listener")}
        def abort(self):
            # Synthetic exit results test only the unchanged cleanup shape.
            return {role: {"generation": ref["generation"], "target": ref["target"],
                           "exit_code": 0, "proof_kind": "retained-child-exit"}
                    for role, ref in self.known_custody_capabilities().items()}
    prep.graph = TwoRoleGraph()
    profile = {"workspace_uuid": "workspace", "support_root": str(tmp_path),
               "profile_digest": "sha256:" + "2" * 64}
    prep._request.update({"profile": profile, "runtime_owner": {
        **{key: source_actor[key] for key in ("pid", "uid", "birth_id")},
        "runtime_instance_id": "runtime-A", "coordinator_epoch": "runtime-A"}})
    prep._activation_grant["host"] = {key: host_actor[key] for key in ("pid", "birth_id")}
    prep._retained_relay_actor = SimpleNamespace(verify=lambda: dict(relay_actor))
    monkeypatch.setattr(custody_broker.AuthenticatedCleanupActor, "current",
                        classmethod(lambda cls: SimpleNamespace(verify=lambda: dict(host_actor))))
    binding = _binding(custody_scope=str(scope), original_roles=refs, current_roles=refs,
                       source_relay_reference=refs["relay"], source_owner=source_actor,
                       profile_binding_digest=generic_host._handoff_digest(profile))
    assert prep._validate_retained_binding(binding)
    assert set(prep.graph.known_custody_capabilities()) == {"engine", "engine_listener"}
    return host, prep, binding, refs, write_designation


def test_native_graph_pause_publishes_complete_validated_roles(tmp_path, monkeypatch):
    host, prep, binding, refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    request = _request(binding=binding)
    reply = prep.dispatch(request)
    assert reply["status"] == "ok"
    assert reply["phase"] == "host_paused"
    assert reply["custody_capabilities"] == refs
    assert reply["custody_capabilities"]["host"]["generation"] == 4
    assert reply["custody_capabilities"]["relay"]["generation"] == 7
    assert set(prep.known_custody_capabilities()) == {"engine", "engine_listener"}
    persisted = json.loads((tmp_path / "custody" / "host-handoff-state.json").read_text())
    assert persisted["reply"] == reply
    assert persisted["request_digest"] == generic_host._handoff_digest(request)
    assert prep.dispatch(request) == reply
    assert host.handoff_quiescence()["claim_gate_closed"] is True


@pytest.mark.parametrize("role", ["host", "relay"])
@pytest.mark.parametrize("drift", ["missing", "generation", "target", "actor", "epoch"])
def test_native_graph_pause_refuses_missing_or_stale_retained_roles(tmp_path, monkeypatch, role, drift):
    host, prep, binding, refs, write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    if drift == "missing": (tmp_path / "custody" / f"designation-{role}.json").unlink()
    elif drift == "generation": write(role, generation=refs[role]["generation"] + 1)
    elif drift == "target": write(role, target=_actor(99))
    elif drift == "actor": write(role, actor=_actor(99))
    else: write(role, owner_epoch="stale-runtime")
    reply = prep.dispatch(_request(binding=binding))
    assert reply["status"] == "unresolved"
    assert reply["error_code"] == "custody_unresolved"
    assert prep._handoff_phase == "owned"
    assert prep._handoff_current_roles is None
    with host._handoff_condition:
        assert host._handoff_claim_gate_closed is True
        assert host._handoff_observation_known is False
    snapshot = host.handoff_quiescence()
    for observed in (snapshot, reply["quiescence"]):
        assert observed["observation_status"] == "unknown"
        assert observed["claim_gate_closed"] is None
        assert all(observed[key] is None for key in (
            "claim_rpc_in_flight", "active_attempts", "pending_settlements", "registration_rpc_in_flight"))
    assert not (tmp_path / "custody" / "host-handoff-state.json").exists()


@pytest.mark.parametrize("role", ["engine", "engine_listener"])
def test_native_graph_pause_refuses_inconsistent_delegated_role(tmp_path, monkeypatch, role):
    host, prep, binding, refs, write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    write(role, generation=refs[role]["generation"] + 1)
    reply = prep.dispatch(_request(binding=binding))
    assert reply["status"] == "unresolved"
    assert reply["error_code"] == "custody_unresolved"
    with host._handoff_condition:
        assert host._handoff_claim_gate_closed is True
        assert host._handoff_observation_known is False
    snapshot = host.handoff_quiescence()
    for observed in (snapshot, reply["quiescence"]):
        assert observed["observation_status"] == "unknown"
        assert observed["claim_gate_closed"] is None
        assert all(observed[key] is None for key in (
            "claim_rpc_in_flight", "active_attempts", "pending_settlements", "registration_rpc_in_flight"))
    assert not (tmp_path / "custody" / "host-handoff-state.json").exists()


def test_native_graph_pause_persistence_failure_keeps_gate_closed(tmp_path, monkeypatch):
    host, prep, binding, refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    candidates = []
    def failed_persist(request, reply):
        candidates.append(copy.deepcopy(reply))
        return False
    prep._persist_handoff_reply = failed_persist
    reply = prep.dispatch(_request(binding=binding))
    assert candidates[0]["custody_capabilities"] == refs
    assert reply["status"] == "unresolved"
    assert reply["error_code"] == "custody_unresolved"
    assert prep._handoff_phase == "owned"
    assert prep._handoff_current_roles is None
    with host._handoff_condition:
        assert host._handoff_claim_gate_closed is True
        assert host._handoff_observation_known is False
    snapshot = host.handoff_quiescence()
    for observed in (snapshot, reply["quiescence"]):
        assert observed["observation_status"] == "unknown"
        assert observed["claim_gate_closed"] is None
        assert all(observed[key] is None for key in (
            "claim_rpc_in_flight", "active_attempts", "pending_settlements", "registration_rpc_in_flight"))
    assert host.handoff_quiescence()["observation_status"] == "unknown"


def test_native_graph_cleanup_retains_two_role_contract(tmp_path, monkeypatch):
    _host_owner, prep, binding, refs, _write = _native_two_role_pause_fixture(tmp_path, monkeypatch)
    assert prep.dispatch(_request(binding=binding))["custody_capabilities"] == refs
    graph_refs = prep.known_custody_capabilities()
    assert set(graph_refs) == {"engine", "engine_listener"}
    profile = prep._request["profile"]
    request = {"version": generic_host._LOCAL_PREPARATION_VERSION, "command": "abort_local_execution",
               "operation_id": prep.operation_id, "channel_id": prep.channel_id, "owner_epoch": "runtime-A",
               "profile_digest": profile["profile_digest"], "profile_binding_digest": generic_host._handoff_digest(profile),
               "custody_scope": str(tmp_path / "custody"), "custody_capabilities": graph_refs}
    reply = prep.dispatch(request)
    assert reply["status"] == "cleaned"
    assert reply["custody_capabilities"] == graph_refs
    assert set(reply["cleanup"]) == {"engine", "engine_listener"}
    assert prep._handoff_current_roles == refs
    assert prep.dispatch(request) == reply


# Auth/custody evidence providers remain the existing explicitly synthetic CPU
# fixture. Registration, typed Health, local drift, verifier and persister are real.
def _successor_health_before_adoption(host):
    previous = host.client.health()
    successor = Health(status=previous.status, protocol=previous.protocol,
                       schema_digest=previous.schema_digest, runtime_epoch=8,
                       runtime_session_id="runtime-session-B", runtime_instance_id="runtime-B")
    health_calls = []
    def health():
        health_calls.append(successor.runtime_instance_id)
        return successor
    host.client.health = health
    assert host.client.health().runtime_instance_id == "runtime-B"
    return health_calls


def test_adopt_sealed_registration_after_health_moves_to_successor(tmp_path):
    fixture = _adoption_fixture(tmp_path, adopt=False)
    host, prep, binding = fixture["host"], fixture["prep"], fixture["binding"]
    health_calls = _successor_health_before_adoption(host)
    assert generic_host._handoff_registered_state(host, prep._activation_grant) is None
    before_adopt_calls = len(health_calls)
    registrations = []
    class RecordingRuntime:
        def register_executor(self, executor_id, **kwargs):
            registrations.append({"kwargs": kwargs, "gate_closed": host._handoff_claim_gate_closed,
                                  "in_flight": host._handoff_registration_rpc_in_flight})
            return {"registered": True}
    host.client.wrapped = RecordingRuntime()
    host.capabilities = {"ready": SimpleNamespace(
        id="ready", capability_digest="sha256:" + "d" * 64,
        source_digest="e" * 64, dependency_digest="f" * 64,
        matrix={}, ready=True, preflight={}, resource_keys=(), estimated_scratch_bytes=0,
        estimated_output_bytes=0, manifest=lambda: {"id": "ready", "ready": True})}
    adopted = prep.dispatch(fixture["adopt_request"])
    assert adopted["status"] == "ok" and adopted["phase"] == "adopt_prepared"
    assert adopted["registered_state"] == fixture["expected"]
    assert adopted["registered_state"]["runtime"]["runtime_instance_id"] == "runtime-A"
    assert adopted["quiescence"]["claim_gate_closed"] is True
    assert len(health_calls) == before_adopt_calls  # No live-A validation at verifier or publisher.
    assert registrations == [] and host._registered_runtime_state["runtime_instance_id"] == "runtime-A"
    assert json.loads((fixture["scope"] / "host-handoff-state.json").read_text())["reply"] == adopted
    for name, journal in (("runtime", fixture["runtime_journal"]), ("relay", fixture["relay_journal"])):
        journal["phase"] = "adopt_prepared"
        journal["entries"][f"{binding['handoff_id']}:handoff_adopt"] = {
            "request_digest": generic_host._handoff_digest(fixture["adopt_request"]),
            "binding_digest": generic_host._handoff_digest(binding), "reply": adopted}
        (fixture["scope"] / (name + "-handoff-state.json")).write_text(
            json.dumps(journal, sort_keys=True, separators=(",", ":")))
    runtime_b = generic_host._handoff_live_runtime_binding(host)
    committed = prep.dispatch(_request("handoff_commit", binding=binding, payload={
        "new_runtime": runtime_b, "task_fence_digest": fixture["fence_digest"],
        "relay_reference": prep._handoff_current_roles["relay"]}))
    assert committed["status"] == "ok" and committed["phase"] == "rebind_committed"
    assert committed["registered_state"]["runtime"] == runtime_b
    assert host._registered_runtime_state["runtime_instance_id"] == "runtime-B"
    assert len(registrations) == 1 and registrations[0]["gate_closed"] is True
    assert registrations[0]["in_flight"] == 1 and registrations[0]["kwargs"]["runtime_epoch"] == 8
    assert host.handoff_quiescence()["claim_gate_closed"] is True


@pytest.mark.parametrize("drift", ["registration", "source", "capability", "runtime", "sealed", "endpoint"])
def test_adopt_refuses_local_or_sealed_registration_drift(tmp_path, drift):
    fixture = _adoption_fixture(tmp_path, adopt=False)
    host, prep = fixture["host"], fixture["prep"]
    _successor_health_before_adoption(host)
    before = (fixture["scope"] / "host-handoff-state.json").read_bytes()
    if drift == "registration":
        host._registered_state["ready"]["source_digest"] = "1" * 64
    elif drift == "source":
        host.source_epoch = "changed-source"
    elif drift == "capability":
        host.capabilities["ready"].source_digest = "1" * 64
    elif drift == "runtime":
        host._registered_runtime_state["runtime_session_id"] = "changed-session"
    elif drift == "sealed":
        path = fixture["scope"] / "host-handoff-state.json"
        record = json.loads(path.read_text())
        record["reply"]["registered_state"]["source_epoch"] = "changed-source"
        path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")))
        before = path.read_bytes()
    else:
        host.client.endpoint = "http://127.0.0.1:9999"
    reply = prep.dispatch(fixture["adopt_request"])
    assert reply["status"] == "unresolved" and reply["error_code"] == "identity_unresolved"
    assert prep._handoff_phase == "export_sealed"
    assert host.handoff_quiescence()["claim_gate_closed"] is True
    assert (fixture["scope"] / "host-handoff-state.json").read_bytes() == before


@pytest.mark.parametrize("drift", ["source", "registration", "client", "activation"])
def test_adopt_rechecks_local_registration_before_publication(tmp_path, monkeypatch, drift):
    fixture = _adoption_fixture(tmp_path, adopt=False)
    host, prep = fixture["host"], fixture["prep"]
    _successor_health_before_adoption(host)
    before = (fixture["scope"] / "host-handoff-state.json").read_bytes()
    original = prep._verify_adoption_evidence
    verified = []
    def verify_then_drift(request):
        evidence = original(request)
        assert evidence is not None
        verified.append(True)
        if drift == "source": host.source_epoch = "changed-after-verification"
        elif drift == "registration": host._registered_state["ready"]["source_digest"] = "1" * 64
        elif drift == "client":
            host.client = SimpleNamespace(endpoint=host.client.endpoint, schema_digest=host.client.schema_digest)
        else: prep._activation_grant["executor_incarnation"] = "different-incarnation"
        return evidence
    monkeypatch.setattr(prep, "_verify_adoption_evidence", verify_then_drift)
    reply = prep.dispatch(fixture["adopt_request"])
    assert verified == [True]
    assert reply["status"] == "unresolved" and reply["error_code"] == "registration_unresolved"
    assert prep._handoff_phase == "export_sealed"
    assert host.handoff_quiescence()["claim_gate_closed"] is True
    assert (fixture["scope"] / "host-handoff-state.json").read_bytes() == before
