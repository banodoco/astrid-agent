"""Original supervisor control through a pinned fake channel; no processes/GPU."""
import asyncio
import hashlib
import json
import socket
from contextlib import contextmanager
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_custody as custody
from astrid.packs.runpod import installed_session, worker_owner, worker_preparation
from astrid.packs.runpod.worker_owner import RunPodWorkerOwnerError, _stop_original_installed_session
from tests.packs.runpod.test_worker_release import release_case as release_case
from tests.test_runpod_managed_worker_session import _owner


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setattr(socket.socket, "connect", lambda *args: pytest.fail("network forbidden"))


@pytest.fixture
def supervisor(monkeypatch):
    owner, transport, selection, _ = _owner()
    receipt = owner.start(selection)
    token = "fake-original-owner"
    selection = replace(selection, session_ref="vibecomfy-h3")
    receipt = replace(receipt, session_ref="vibecomfy-h3",
                      launch_token_digest="sha256:" + hashlib.sha256(token.encode()).hexdigest())
    transport.current.update(session_ref=selection.session_ref, config_digest=selection.config_digest,
                             launch_token_digest=receipt.launch_token_digest)
    state = SimpleNamespace(selection=selection, receipt=receipt, marker=None, sent=[], connects=[],
                            births={receipt.daemon_pid: receipt.daemon_birth_id, receipt.comfy_pid: receipt.comfy_birth_id},
                            missing=False, mutation=None, response=None, lose_reply=False)
    constructor = custody.LinuxEngineServiceClient

    class Channel:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def client(**kwargs):
        instance = constructor(**kwargs)
        state.identity = instance.identity
        if state.marker is None and state.births:
            state.marker = {"identity": instance.identity, "owner_id": token}

        def connect(fd, deadline, *, start):
            state.connects.append(start)
            assert start is False, "release cannot create or adopt an installation owner"
            if state.mutation:
                state.mutation()
            return None if state.missing else Channel()
        instance._connect = connect
        return instance

    def read(self, root, path, deadline):
        if state.marker is None:
            raise FileNotFoundError()
        return json.dumps(state.marker).encode()

    def birth(pid):
        if pid not in state.births:
            raise FileNotFoundError()
        return state.births[pid]

    @contextmanager
    def directory(path, *, create):
        assert not create
        yield 11

    def send(channel, request, deadline):
        assert request["identity"] == state.identity and request["operation"] == "stop"
        assert request["payload"] == {}
        state.sent.append(request)

    def receive(channel, deadline):
        state.births.clear()
        state.marker = None
        if state.lose_reply:
            state.lose_reply = False
            raise TimeoutError("stop completed but reply lost")
        return state.response or {"identity": state.identity, "result": {"stopped": True}}

    monkeypatch.setattr(custody, "LinuxEngineServiceClient", client)
    monkeypatch.setattr(custody, "_vibe_birth", birth)
    monkeypatch.setattr(custody, "_directory", directory)
    monkeypatch.setattr(custody, "_send", send)
    monkeypatch.setattr(custody, "_receive", receive)
    monkeypatch.setattr(installed_session.LocalInstalledVibeComfySessionTransport, "_read", read)
    monkeypatch.setattr(installed_session.LocalInstalledVibeComfySessionTransport, "status", lambda self, selected: dict(transport.current))
    return state


def stop(c):
    return _stop_original_installed_session(asdict(c.selection), c.receipt.as_dict())


def test_original_supervisor_stops_and_confirms_both_receipt_processes(supervisor):
    c = supervisor
    result = stop(c)
    assert result["supervisor_stop"] == "original_installation_owner"
    assert result["daemon_birth_id"] == c.receipt.daemon_birth_id
    assert result["comfy_birth_id"] == c.receipt.comfy_birth_id
    assert c.connects == [False] and len(c.sent) == 1
    assert not c.births and c.marker is None


def test_lost_stop_reply_recovers_positive_absence_without_repeating_control(supervisor):
    c = supervisor
    c.lose_reply = True
    with pytest.raises(TimeoutError):
        stop(c)
    assert stop(c)["state"] == "stopped"
    assert c.connects == [False] and len(c.sent) == 1


@pytest.mark.parametrize("mutation", ["birth", "marker", "missing"])
def test_changed_or_missing_supervisor_cannot_receive_stop(supervisor, mutation):
    c = supervisor
    def change():
        if mutation == "birth":
            c.births[c.receipt.daemon_pid] = "foreign-birth"
        elif mutation == "marker":
            c.marker = {**c.marker, "owner_id": "another-broker"}
        else:
            c.missing = True
    c.mutation = change
    with pytest.raises(RunPodWorkerOwnerError):
        stop(c)
    assert c.sent == []


@pytest.mark.parametrize("response", [
    {"identity": {}, "result": {"stopped": True}},
    {"identity": {}, "result": {"stopped": False}},
    {"error": "uncertain"},
])
def test_unbound_supervisor_response_cannot_become_quiescence(supervisor, response):
    supervisor.response = response
    with pytest.raises(RunPodWorkerOwnerError, match="did not confirm"):
        stop(supervisor)


def test_original_stop_handoff_and_restart_checkpoint(release_case, monkeypatch):
    c = release_case
    owner, transport, selected, _ = _owner()
    receipt = owner.start(selected)
    selected = replace(selected, session_ref="vibecomfy-h3", account_ref="runpod-test-account", pod_id="pod-exact")
    receipt = replace(receipt, session_ref="vibecomfy-h3", account_ref="runpod-test-account", pod_id="pod-exact")
    record = {**c.record, "managed_session": {
        "schema": "astrid.runpod.managed-session.v1", "claim_operation_id": c.record["operation_id"],
        "session_ref": selected.session_ref, "config_digest": selected.config_digest,
        "output_root": selected.output_root, "selection": asdict(selected), "receipt": receipt.as_dict(),
    }}
    worker_preparation.write_target_record(c.path, record)
    c.reference.source_checkout = "/workspace/prepared/astrid"
    remote_calls = []

    async def run(program, args, **kwargs):
        remote_calls.append((program, args, kwargs))
        assert json.loads(c.path.read_text())["release_progress"]["credential_remove"] == {"removed": True}
        assert json.loads(args[1]) == asdict(selected) and json.loads(args[2]) == receipt.as_dict()
        return {"state": "stopped", "session_ref": selected.session_ref, "config_digest": selected.config_digest,
                "gpu_qualified": False, "supervisor_stop": "original_installation_owner",
                **{key: getattr(receipt, key) for key in ("daemon_pid", "daemon_birth_id", "comfy_pid", "comfy_birth_id")}}

    preparer = c.fresh()
    preparer.session_owner = owner
    preparer.preparation_transport = SimpleNamespace(_run_json_program=run)
    result = asyncio.run(preparer.release(c.reference, runtime_owner=c.runtime))
    assert result["state"] == "cleanup_complete"
    assert len(remote_calls) == 1
    assert remote_calls[0][2] == {"python": selected.python_executable, "timeout_seconds": 45}
    assert result["release_progress"]["session_stop"] == result["release_quiescence"]["session_stop"]
    assert result["cleanup"]["absence"]["account_list_absent"] is True
    assert asyncio.run(preparer.release(c.reference, runtime_owner=c.runtime, resume=True)) == result
    assert len(remote_calls) == 1
