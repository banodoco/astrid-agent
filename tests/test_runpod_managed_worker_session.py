from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod.worker_session import (
    ManagedSessionError,
    RunPodManagedVibeComfySessionOwner,
)


def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


class ConfigFactory:
    calls = []

    @classmethod
    def from_dict(cls, values):
        cls.calls.append(dict(values))
        return SimpleNamespace(
            runtime_root=Path(values["runtime_root"]),
            cwd=Path(values["cwd"]),
            port=values["port"],
            warm_policy=values["warm_policy"],
            extra={key: values[key] for key in (
                "base_directory", "models_root", "models_root_normalized",
                "output_directory", "locality", "server_log_path",
            )},
        )


class SessionTransport:
    def __init__(self):
        self.current = None
        self.calls = []
        self.stop_failure = False
        self.stop_confirmed = True
        self.lose_start_response = False
        self.lose_stop_response = False

    def start(self, selection):
        self.calls.append(("session start", selection.session_ref))
        self.current = {
            "account_ref": selection.account_ref,
            "pod_id": selection.pod_id,
            "session_ref": selection.session_ref,
            "runtime_root": selection.runtime_root,
            "config": dict(selection.config),
            "output_root": selection.output_root,
            "url": f"http://127.0.0.1:{selection.port}",
            "daemon": {"pid": 110, "birth_id": "daemon-birth-1"},
            "comfy": {"pid": 120, "birth_id": "comfy-birth-1"},
            "launch_token_digest": "sha256:" + "a" * 64,
            "ownership_verified": True,
            "gpu_qualified": False,
        }
        if self.lose_start_response:
            self.lose_start_response = False
            raise TimeoutError("injected lost start response")

    def status(self, selection):
        self.calls.append(("session status", selection.session_ref))
        return dict(self.current) if self.current is not None else None

    def stop(self, selection, receipt):
        self.calls.append(("session stop", selection.session_ref))
        assert receipt.daemon_pid == self.current["daemon"]["pid"]
        assert receipt.comfy_birth_id == self.current["comfy"]["birth_id"]
        if not self.stop_failure:
            self.current = None
        if self.lose_stop_response:
            self.lose_stop_response = False
            raise TimeoutError("injected lost stop response")

    def confirm_stopped(self, selection, receipt):
        self.calls.append(("confirm stopped", selection.session_ref))
        assert receipt.daemon_birth_id == "daemon-birth-1"
        assert receipt.comfy_birth_id == "comfy-birth-1"
        return self.stop_confirmed


def _selected():
    candidate = "/workspace/releases/comfy-r1"
    runtime_root = candidate + "/runtime"
    comfy = runtime_root + "/ComfyUI"
    model = "/workspace/models"
    output = "/workspace/outputs/session-1"
    vibecomfy = "/workspace/.astrid/prepared/source/vibecomfy"
    config = {
        "runtime_root": runtime_root,
        "cwd": runtime_root,
        "port": 8188,
        "warm_policy": "never",
        "base_directory": comfy,
        "models_root": model,
        "models_root_normalized": model,
        "output_directory": output,
        "locality": "managed_local_server",
        "server_log_path": runtime_root + "/out/sessions/session-1/comfy.log",
    }
    reference = SimpleNamespace(
        effective_target={
            "kind": "runpod", "pod_id": "pod-1", "provider_account_ref": "account-1",
        },
        executable=SimpleNamespace(path=Path(runtime_root + "/venv/bin/python")),
        model_root=Path(model), output_root=Path(output), session_ref="session-1",
        session_config_digest=_digest(config),
    )
    readiness = {
        "status": "cpu_ready", "gpu_qualified": False,
        "effective_paths": {
            "candidate_namespace": candidate,
            "comfy_root": comfy,
            "model_root": model,
            "output_root": output,
            "vibecomfy_root": vibecomfy,
        },
        "python_executable": str(reference.executable.path),
    }
    return reference, readiness, config


def _owner():
    transport = SessionTransport()
    owner = RunPodManagedVibeComfySessionOwner(
        transport, session_config_type=ConfigFactory,
    )
    reference, readiness, config = _selected()
    selection = owner.select(reference, readiness, port=8188)
    return owner, transport, selection, config


def test_selected_config_and_exact_managed_session_resume_stop():
    owner, transport, selection, config = _owner()
    assert selection.config == config
    assert selection.config_digest == _digest(config)
    assert ConfigFactory.calls[-1] == config
    receipt = owner.start(selection)
    assert receipt.account_ref == "account-1"
    assert receipt.pod_id == "pod-1"
    assert receipt.output_root == config["output_directory"]
    assert receipt.daemon_birth_id == "daemon-birth-1"
    assert receipt.comfy_birth_id == "comfy-birth-1"
    assert owner.resume(selection, receipt.as_dict()) == receipt
    assert owner.observe_config(selection, receipt) == {
        "session_ref": "session-1",
        "session_config_digest": selection.config_digest,
        "output_root": selection.output_root,
        "gpu_qualified": False,
    }
    restarted = RunPodManagedVibeComfySessionOwner(
        transport, session_config_type=ConfigFactory,
    )
    assert restarted.resume(selection, receipt.as_dict()) == receipt
    assert restarted.stop(selection, receipt.as_dict()) == {
        "state": "stopped", "session_ref": "session-1",
        "config_digest": selection.config_digest, "gpu_qualified": False,
    }
    assert [name for name, _ in transport.calls].count("session start") == 1
    assert [name for name, _ in transport.calls].count("session stop") == 1


@pytest.mark.parametrize("change", [
    lambda current: current.update(output_root="/workspace/foreign"),
    lambda current: current["config"].update(output_directory="/workspace/foreign"),
    lambda current: current["daemon"].update(birth_id="reused-pid"),
    lambda current: current["comfy"].update(pid=121),
    lambda current: current.update(ownership_verified=False),
    lambda current: current.update(gpu_qualified=True),
])
def test_resume_and_stop_refuse_foreign_or_unproved_session(change):
    owner, transport, selection, _ = _owner()
    receipt = owner.start(selection)
    change(transport.current)
    with pytest.raises(ManagedSessionError):
        owner.resume(selection, receipt)
    with pytest.raises(ManagedSessionError):
        owner.stop(selection, receipt)
    assert not any(name == "session stop" for name, _ in transport.calls)


def test_stop_requires_managed_absence_confirmation():
    owner, transport, selection, _ = _owner()
    receipt = owner.start(selection)
    transport.stop_failure = True
    with pytest.raises(ManagedSessionError, match="confirmed"):
        owner.stop(selection, receipt)


def test_start_and_stop_reconcile_lost_responses_without_duplicate_processes():
    owner, transport, selection, _ = _owner()
    transport.lose_start_response = True
    receipt = owner.start(selection)
    assert receipt.daemon_pid == 110
    assert [name for name, _ in transport.calls].count("session start") == 1

    transport.lose_stop_response = True
    stopped = owner.stop(selection, receipt)
    assert stopped["state"] == "stopped"
    assert [name for name, _ in transport.calls].count("session stop") == 1
    assert owner.stop(selection, receipt) == stopped
    transport.stop_failure = False
    transport.stop_confirmed = False
    with pytest.raises(ManagedSessionError, match="positively confirmed"):
        owner.stop(selection, receipt)


def test_cpu_selection_and_reference_digest_are_exact():
    reference, readiness, _ = _selected()
    owner = RunPodManagedVibeComfySessionOwner(
        SessionTransport(), session_config_type=ConfigFactory,
    )
    readiness["gpu_qualified"] = True
    with pytest.raises(ManagedSessionError, match="CPU"):
        owner.select(reference, readiness, port=8188)
    readiness["gpu_qualified"] = False
    reference.session_config_digest = "sha256:" + "0" * 64
    with pytest.raises(ManagedSessionError, match="SessionConfig"):
        owner.select(reference, readiness, port=8188)
