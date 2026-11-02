"""Claim/receipt/live-composite boundaries, with no provider or GPU access."""
from __future__ import annotations

import json
import socket
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import installed_session as installed
from astrid.packs.runpod.prepared_task import _claim_target
from astrid.packs.runpod.worker_session import ManagedSessionError, ManagedSessionSelection
from astrid.packs.runpod.worker_staging import _SCHEMA, _claim_identity, _public_result
from tests.packs.runpod.test_worker_preparation import _claim_handle


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("installed preparation test attempted network access")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def composite(tmp_path, monkeypatch):
    operation = "a" * 32
    claim_path, journal_path = tmp_path / "claim.json", tmp_path / "cpu.json"
    claim = _claim_handle(
        operation_id=operation, request_name="astrid-product-" + operation,
        name="astrid-product-" + operation, transport="https", https_token_file="/private/agent.token",
        network_volume_datacenter_id="EU-RO-1", volume_mount_path="/workspace",
        target_identity_verified=True,
        ordinary_preparation={"schema": "astrid.runpod.ordinary-preparation.v1", "state": "prepared",
                              "engine": "comfyui", "claim_operation_id": operation,
                              "pod_id": "pod-exact", "provider_account_ref": "runpod-test-account"},
    )
    claim_path.write_text(json.dumps(claim))
    root = "/workspace/engines/vibecomfy-h3"
    config = {"runtime_root": root + "/runtime", "port": 8188,
              "cwd": root + "/runtime/ComfyUI", "warm_policy": "never",
              "base_directory": root + "/runtime/ComfyUI", "models_root": root + "/models",
              "models_root_normalized": root + "/models", "output_directory": root + "/runtime/output",
              "locality": "managed_local_server", "server_log_path": root + "/runtime/out/sessions/vibecomfy-h3/comfy.log"}
    selection = ManagedSessionSelection(
        claim["provider_account_ref"], claim["pod_id"], "vibecomfy-h3", root + "/venv/bin/python",
        root + "/runtime", root + "/vibecomfy", root + "/runtime/ComfyUI", root + "/models",
        root + "/runtime/output", 8188, config, "sha256:" + "b" * 64,
    )
    facts = {
        "status": "cpu_ready", "gpu_qualified": False, "python_executable": selection.python_executable,
        "effective_paths": {"astrid_root": "/workspace/prepared/source/astrid",
                            "candidate_namespace": root,
                            **{name: getattr(selection, name) for name in (
                                "vibecomfy_root", "comfy_root", "model_root", "output_root")}},
        "tree_facts": {"vibecomfy_root": {"tree_digest": "sha256:" + "a" * 64, "file_count": 1}},
    }
    journal = {"schema": _SCHEMA, "identity": _claim_identity(claim_path, selection.account_ref),
               "status": "cpu_ready", "release": {"release_id": "release-1"},
               "plan_digest": "sha256:" + "c" * 64, "cpu_readiness": facts}
    journal_path.write_text(json.dumps(journal))
    staging = _public_result(journal, journal_path)
    value = {
        "runtime_root": selection.runtime_root, "session_ref": selection.session_ref,
        "config": config, "config_digest": selection.config_digest,
        "output_root": selection.output_root, "url": "http://127.0.0.1:8188",
        "daemon": {"pid": 11, "birth_id": "daemon-birth"},
        "comfy": {"pid": 12, "birth_id": "comfy-birth"},
        "launch_token_digest": "sha256:" + "d" * 64, "source_revision": "e" * 40,
        "source_content_digest": "sha256:" + "f" * 64, "gpu_qualified": False,
        "python_executable": selection.python_executable, "vibecomfy_root": selection.vibecomfy_root,
    }
    pod = SimpleNamespace(id=claim["pod_id"], config=SimpleNamespace(transport="https"))
    state = SimpleNamespace(claim_path=claim_path, claim=claim, journal_path=journal_path, journal=journal,
                            staging=staging, selection=selection, value=value, calls=[], mutation=None)

    async def run(program, args, **kwargs):
        state.calls.append((program, json.loads(args[0]), kwargs))
        if state.mutation:
            state.mutation()
        return deepcopy(state.observed)

    preparation = SimpleNamespace(pod=pod, _run_json_program=run)
    state.transport = installed.RunPodInstalledVibeComfySessionTransport(pod, preparation_transport=preparation)
    state.observed = {**value, "account_ref": selection.account_ref, "pod_id": selection.pod_id,
                      "ownership_verified": True}
    state.reference = SimpleNamespace(
        effective_target=_claim_target(claim), session_ref=selection.session_ref,
        executable=SimpleNamespace(path=selection.python_executable), model_root=selection.model_root,
        output_root=selection.output_root, session_config_digest=selection.config_digest,
    )
    monkeypatch.setattr(state.transport, "read_installed", lambda root: deepcopy(state.value))
    state.bind = lambda: state.transport.bind_preparation(
        claim_handle_path=claim_path, reference=state.reference, staging=staging,
    )
    return state


def test_verified_prepared_ordinary_attachment_accepts_and_restart_reobserves(composite):
    c = composite
    c.bind()
    assert c.transport.status(c.selection) == c.observed
    first = c.transport
    c.transport = installed.RunPodInstalledVibeComfySessionTransport(
        first.pod, preparation_transport=first.preparation_transport,
    )
    c.transport.read_installed = first.read_installed
    c.bind()
    assert c.transport.status(c.selection) == c.observed
    assert len(c.calls) == 2
    program, request, kwargs = c.calls[0]
    assert request["selection"] == asdict(c.selection)
    assert kwargs == {"python": c.selection.python_executable, "timeout_seconds": 20}
    assert "LocalInstalledVibeComfySessionTransport" in program
    assert "launch_token" not in json.dumps(request)


@pytest.mark.parametrize("change", [
    {"state": "cleanup_pending"}, {"pod_id": "foreign"}, {"provider_account_ref": "foreign"},
    {"worker_image": "foreign"}, {"network_volume_id": "foreign"}, {"volume_mount_path": "/foreign"},
    {"operation_id": "foreign"}, {"request_name": "another-broker"},
    {"release_authorized": False}, {"acquisition_mode": "attach"}, {"target_identity_verified": False},
    {"ordinary_preparation": {}},
])
def test_changed_or_foreign_journal_cannot_authorize_attachment(composite, change):
    c = composite
    c.bind()
    c.claim_path.write_text(json.dumps({**c.claim, **change}))
    with pytest.raises(ManagedSessionError):
        c.transport.status(c.selection)
    assert not c.calls


@pytest.mark.parametrize("change", [
    {"status": "preparing"}, {"identity": {}}, {"plan_digest": "sha256:" + "0" * 64},
    {"cpu_readiness": {}},
])
def test_changed_preparation_receipt_cannot_authorize_attachment(composite, change):
    c = composite
    c.bind()
    c.journal_path.write_text(json.dumps({**c.journal, **change}))
    with pytest.raises(ManagedSessionError):
        c.transport.status(c.selection)
    assert not c.calls


@pytest.mark.parametrize("change", [
    {"ownership_verified": False}, {"account_ref": "foreign"}, {"pod_id": "foreign"},
    {"daemon": {"pid": 11, "birth_id": "reused-pid"}},
    {"comfy": {"pid": 12, "birth_id": "reused-pid"}},
    {"source_content_digest": "sha256:" + "0" * 64}, {"source_revision": "0" * 40},
    {"config_digest": "sha256:" + "0" * 64}, {"url": "http://127.0.0.1:9999"},
    {"launch_token_digest": "sha256:" + "0" * 64},
])
def test_live_composite_must_match_every_installed_witness(composite, change):
    c = composite
    c.bind()
    c.observed.update(change)
    with pytest.raises(ManagedSessionError, match="live composite differs"):
        c.transport.status(c.selection)


def test_receipt_and_registry_alone_never_authorize_live_attachment(composite):
    with pytest.raises(ManagedSessionError, match="verified journal claim and preparation receipt"):
        composite.transport.status(composite.selection)
    assert not composite.calls


def test_failed_live_observation_and_racing_custody_change_fail_closed(composite):
    c = composite
    c.bind()
    c.mutation = lambda: c.claim_path.write_text(json.dumps({**c.claim, "state": "cleanup_pending"}))
    with pytest.raises(ManagedSessionError, match="custody changed"):
        c.transport.status(c.selection)


def test_unavailable_live_verifier_refuses_without_exposing_remote_error(composite):
    c = composite
    c.bind()
    async def unavailable(*args, **kwargs):
        raise TimeoutError("private remote details")
    c.transport.preparation_transport._run_json_program = unavailable
    with pytest.raises(ManagedSessionError, match="could not be verified") as error:
        c.transport.status(c.selection)
    assert "private remote details" not in str(error.value)


def test_unverified_preparation_cannot_even_bind(composite):
    c = composite
    c.staging["plan_digest"] = "sha256:" + "0" * 64
    with pytest.raises(ManagedSessionError, match="do not verify"):
        c.bind()
    assert c.transport._prepared is None


def test_installed_checkout_can_differ_from_the_receipt_bound_staging_location(composite):
    from astrid.packs.runpod.worker_session import RunPodManagedVibeComfySessionOwner
    from tests.test_runpod_managed_worker_session import ConfigFactory

    c = composite
    c.staging["cpu_readiness"]["effective_paths"]["vibecomfy_root"] = "/workspace/prepared/source/vibecomfy"
    c.journal_path.write_text(json.dumps(c.journal))
    c.bind()
    owner = RunPodManagedVibeComfySessionOwner(c.transport, session_config_type=ConfigFactory)
    selected = owner.select(c.reference, c.staging["cpu_readiness"], port=8188)
    assert selected == c.selection
    assert owner.start(selected).comfy_birth_id == "comfy-birth"
    assert c.calls[0][1]["staged_vibecomfy_root"] == "/workspace/prepared/source/vibecomfy"
    assert c.calls[0][1]["vibecomfy_tree"] == c.staging["cpu_readiness"]["tree_facts"]["vibecomfy_root"]


@pytest.mark.parametrize("failure", [None, "installed", "staged", "during_observation"])
def test_target_program_compares_actual_staged_and_installed_source_bytes(
    composite, tmp_path, monkeypatch, capsys, failure,
):
    from dataclasses import replace
    from astrid.packs.runpod.worker_staging import _tree_inventory
    import sys

    staged, selected = tmp_path / "staged", tmp_path / "installed"
    for root in (staged, selected):
        root.mkdir()
        (root / "source.py").write_text("# verified source\n")
    digest, count = _tree_inventory(staged)
    if failure in {"installed", "staged"}:
        ((selected if failure == "installed" else staged) / "source.py").write_text("# changed source\n")
    calls = []
    class LocalVerifier:
        def __init__(self, **kwargs):
            pass
        def status(self, selection):
            calls.append(selection)
            if failure == "during_observation":
                (selected / "source.py").write_text("# changed while observing\n")
            return composite.observed
    monkeypatch.setattr(installed, "LocalInstalledVibeComfySessionTransport", LocalVerifier)
    request = {"selection": asdict(replace(composite.selection, vibecomfy_root=str(selected))),
               "platform": installed.PLATFORM, "astrid_root": "/fixture/astrid",
               "staged_vibecomfy_root": str(staged), "vibecomfy_tree": {"tree_digest": digest, "file_count": count}}
    monkeypatch.setattr(sys, "argv", ["observation", json.dumps(request)])
    monkeypatch.setattr(sys, "path", list(sys.path))
    if failure is None:
        exec(installed._PREPARED_INSTALLED_OBSERVATION, {})
        assert len(calls) == 1
        assert json.loads(capsys.readouterr().out) == composite.observed
    else:
        with pytest.raises(RuntimeError, match="sources differ"):
            exec(installed._PREPARED_INSTALLED_OBSERVATION, {})
        assert len(calls) == (1 if failure == "during_observation" else 0)


def test_target_observation_program_calls_local_live_verifier_only(composite, monkeypatch, capsys):
    calls = []
    class LocalVerifier:
        def __init__(self, **kwargs):
            pass
        def status(self, selection):
            calls.append(selection)
            return composite.observed
    monkeypatch.setattr(installed, "LocalInstalledVibeComfySessionTransport", LocalVerifier)
    from astrid.packs.runpod import worker_staging
    from pathlib import Path
    tree = composite.staging["cpu_readiness"]["tree_facts"]["vibecomfy_root"]
    monkeypatch.setattr(worker_staging, "_tree_inventory", lambda root: (tree["tree_digest"], tree["file_count"]))
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: path)
    request = {"selection": asdict(composite.selection), "platform": installed.PLATFORM,
               "astrid_root": "/fixture/astrid", "staged_vibecomfy_root": "/fixture/staged-vibecomfy",
               "vibecomfy_tree": tree}
    import sys
    monkeypatch.setattr(sys, "argv", ["observation", json.dumps(request)])
    monkeypatch.setattr(sys, "path", list(sys.path))
    exec(installed._PREPARED_INSTALLED_OBSERVATION, {})
    assert calls == [composite.selection]
    assert json.loads(capsys.readouterr().out) == composite.observed
