"""Canonical CPU-only tests for the selected-source H3 installer wrapper."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from astrid.core.generation import engine_runner as runner
from astrid.core.generation import engine_execution
from astrid.core.generation import engine_local_endpoint as local
from scripts import pod_action_source as source
from tests.core.generation import test_engine_runner as canonical
from tests.core.generation.test_engine_runner import setup as canonical_setup


@pytest.fixture
def install_harness(tmp_path, monkeypatch, canonical_setup):
    plan, context, adapter, deadline = canonical_setup
    root = str(tmp_path / "registered-h3")
    context.root = root
    context.files = {path.replace(canonical.ROOT, root, 1): value
                     for path, value in context.files.items()}
    context.deadline = deadline
    plan = runner.plan_install(context.recipe, root=root, placement="local")
    receipt_path = Path(root, runner._RECEIPT)
    monkeypatch.setattr(local, "_engines_dir", lambda: tmp_path / "engine-registry")
    local.record_engine_root("vibecomfy-h3", root)
    monkeypatch.setenv(local.ENGINE_ROOT_ENV, str(tmp_path / "unused-default-root"))

    write_json = context.write_json_atomic

    def persist(path, value, *, deadline):
        write_json(path, value, deadline=deadline)
        if path == str(receipt_path):
            receipt_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            receipt_path.write_text(runner._json(value), encoding="utf-8")

    monkeypatch.setattr(context, "write_json_atomic", persist)
    context.health_objects["LoadImage"] = {
        "input": {"required": {"image": [["legacy.png"], {"image_upload": True}]}},
        "output": ["IMAGE"],
    }

    def backend(**kwargs):
        assert (kwargs["engine"], kwargs["platform"], kwargs["placement"], kwargs["root"]) == (
            plan.engine, plan.platform, "local", root)
        context.deadline = kwargs["deadline"]
        return context, adapter

    monkeypatch.setattr(engine_execution, "create_local_backend", backend)
    selection = {
        "agent_root": str(tmp_path / "agent"), "runtime_root": str(tmp_path / "runtime"),
        "agent_head": "a" * 40, "runtime_head": "b" * 40,
        "runtime_lock_sha256": "c" * 64,
    }
    identity = {"managed_by": "source", "dirty": False, "agent_head": "a" * 40,
                "runtime_head": "b" * 40}
    monkeypatch.setattr(source, "verify_selection", lambda _selected: (object(), identity))
    args = ["engines", "install", "vibecomfy-h3", "--placement", "local", "--root", root,
            "--deadline", "45"]
    return plan, context, adapter, root, receipt_path, selection, identity, args


def test_install_selected_recovers_legacy_receipt_and_returns_ready_attestation(
        install_harness, monkeypatch, capsys):
    plan, context, adapter, root, receipt_path, selection, identity, args = install_harness
    with monkeypatch.context() as legacy:
        legacy.setattr(canonical.target, "_object_info_sha256", runner._sha)
        runner.install_engine(plan, context=context, adapter=adapter, deadline=context.deadline)
    with pytest.raises(local.EngineNotInstalled):
        local.local_engine_endpoint(root=root)
    source.install_selected(args, selection, identity)
    result = json.loads(capsys.readouterr().out)
    receipt_bytes = receipt_path.read_bytes()

    assert result == {
        "schema": "astrid.pod-h3-install.v1", "engine": "vibecomfy-h3", "root": root,
        "status": "ready", "receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        "receipt_id": json.loads(receipt_bytes)["receipt_id"],
    }
    receipt = json.loads(receipt_bytes)
    assert receipt["status"] == "installed"
    assert all(step["action"] == "skipped" and step["validation"] == "passed"
               for step in receipt["steps"])
    from astrid.packs.h3_av.actions.transform import run as transform
    endpoint = transform._resolve_local_engine()
    assert endpoint.endpoint.state == "ready"
    assert endpoint.endpoint.attestation.install_receipt_sha256 == result["receipt_sha256"]


def test_fresh_canonical_install_refuses_nonprivate_volume_before_any_process(
        tmp_path, monkeypatch, capsys):
    """A mount ignoring mkdir's private mode must fail at the first helper."""
    import os
    from astrid.core.generation import engine_custody

    root = str(tmp_path / "workspace/astrid/runtime/h3/vibecomfy-h3")
    cache = str(tmp_path / "workspace/astrid/engines/vibecomfy-h3/models")
    monkeypatch.setattr(local, "_engines_dir", lambda: tmp_path / "registry")
    local.record_engine_root("vibecomfy-h3", root)
    mkdir = os.mkdir

    def mounted_mkdir(path, mode=0o777, *, dir_fd=None):
        mkdir(path, mode, dir_fd=dir_fd)
        if path in (".astrid-engine", "session"):
            os.chmod(path, 0o777, dir_fd=dir_fd)

    def no_process(*args, **kwargs):
        pytest.fail("unsafe private directory reached a subprocess")

    # Only the filesystem mode behavior is simulated. CLI, backend, install
    # lock, helper dispatch and directory validation are the canonical code.
    monkeypatch.setattr(os, "mkdir", mounted_mkdir)
    monkeypatch.setattr(engine_custody.subprocess, "Popen", no_process)
    args = ["engines", "install", "vibecomfy-h3", "--root", root,
            "--model-cache-root", cache, "--deadline", "15"]
    with pytest.raises(ValueError, match="canonical engine installation failed"):
        source.install_selected(args, {}, {})
    captured = capsys.readouterr()
    check, phase = map(json.loads, captured.err.splitlines())
    assert check == {"schema": "astrid.pod-h3-install-check.v1",
                     "code": "unsafe_path", "step": "helper-identity"}
    assert phase == {"schema": "astrid.pod-h3-install-phase.v1",
                     "phase": "canonical-install", "reason": "check_failed"}
    assert captured.out == "" and root not in captured.err and cache not in captured.err
    assert not Path(root, runner._RECEIPT).exists() and not Path(cache).exists()


@pytest.mark.parametrize("registered", [None, "wrong"])
def test_install_selected_requires_existing_registered_root_before_install(
        install_harness, registered):
    _plan, context, _adapter, root, _receipt, selection, identity, args = install_harness
    if registered is None:
        local._record_path("vibecomfy-h3").unlink()
    else:
        local.record_engine_root("vibecomfy-h3", str(Path(root).parent / "wrong-root"))
    before = list(context.events)

    with pytest.raises(ValueError, match="existing registered root"):
        source.install_selected(args, selection, identity)
    assert context.events == before


@pytest.mark.parametrize("fault", ["receipt-mismatch", "missing-receipt"])
def test_install_selected_rejects_uncommitted_or_missing_canonical_receipt(
        install_harness, monkeypatch, fault):
    _plan, context, _adapter, _root, receipt_path, selection, identity, args = install_harness
    write_json = context.write_json_atomic

    def persist(path, value, *, deadline):
        write_json(path, value, deadline=deadline)
        if path == str(receipt_path):
            if fault == "receipt-mismatch":
                receipt_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                receipt_path.write_text("{}\n", encoding="utf-8")
            else:
                receipt_path.unlink(missing_ok=True)

    monkeypatch.setattr(context, "write_json_atomic", persist)
    with pytest.raises((ValueError, FileNotFoundError)):
        source.install_selected(args, selection, identity)


def test_install_selected_rejects_readiness_mismatch(install_harness, monkeypatch):
    _plan, context, _adapter, _root, _receipt, selection, identity, args = install_harness
    original = engine_execution.create_local_backend
    calls = 0

    def backend(**kwargs):
        nonlocal calls
        calls += 1
        if calls >= 2:
            context.health_healthy = False
        return original(**kwargs)

    # Keep the canonical FakeContext backend while making its later readiness observation fail.
    monkeypatch.setattr(engine_execution, "create_local_backend", backend)
    with pytest.raises(local.EngineNotInstalled):
        source.install_selected(args, selection, identity)


def test_install_selected_rechecks_source_identity_after_install(install_harness, monkeypatch):
    _plan, _context, _adapter, _root, _receipt, selection, identity, args = install_harness
    changed = dict(identity, runtime_head="d" * 40)
    monkeypatch.setattr(source, "verify_selection", lambda _selected: (object(), changed))

    with pytest.raises(ValueError, match="installer source identity changed"):
        source.install_selected(args, selection, identity)



def test_install_integrity_failure_reports_canonical_phase_without_exception_details(
        install_harness, capsys):
    _plan, context, _adapter, _root, receipt_path, selection, identity, args = install_harness
    context.wrong_identity = True
    with pytest.raises(ValueError, match="canonical engine installation failed"):
        source.install_selected(args, selection, identity)
    captured = capsys.readouterr()
    canonical_error, phase = map(json.loads, captured.err.splitlines())
    assert canonical_error == {"schema": "astrid.pod-h3-install-check.v1",
                               "code": "integrity_failed", "step": "helper-identity"}
    assert phase == {"schema": "astrid.pod-h3-install-phase.v1",
                     "phase": "canonical-install", "reason": "check_failed"}
    assert captured.out == "" and not receipt_path.exists()
    assert not [e for e in context.events if e[0] in {"write", "mkdir", "download"}]


@pytest.mark.parametrize("diagnostic", [
    {"code": "integrity_failed", "step": "source:comfyui", "recovery_handle": "/private/input/token-marker"},
    {"code": "/private/input/token-marker", "step": "/private/input/token-marker"},
    {"code": ["token-marker"], "step": ["token-marker"]},
])
def test_install_error_projection_omits_handles_and_untrusted_details(
        install_harness, monkeypatch, capsys, diagnostic):
    from astrid.core import engine_cli
    import sys
    _plan, _context, _adapter, _root, _receipt, selection, identity, args = install_harness
    def fail(_args):
        print(json.dumps({"error": diagnostic}), file=sys.stderr)
        return 1
    monkeypatch.setattr(engine_cli, "main", fail)
    with pytest.raises(ValueError, match="canonical engine installation failed"):
        source.install_selected(args, selection, identity)
    captured = capsys.readouterr()
    check, phase = map(json.loads, captured.err.splitlines())
    assert check == {"schema": "astrid.pod-h3-install-check.v1",
                     "code": "integrity_failed" if isinstance(diagnostic["code"], str) and diagnostic["code"] == "integrity_failed" else "engine_error",
                     "step": "source:comfyui" if diagnostic["step"] == "source:comfyui" else None}
    assert phase["phase"] == "canonical-install" and captured.out == ""
    assert "token-marker" not in captured.err and "/private/input" not in captured.err


@pytest.mark.parametrize("state", ["installed", "failed", "missing", "mismatch", "malformed", "symlink", "oversize"])
def test_receipt_only_diagnostic_is_private_and_cannot_start_or_repair(
        install_harness, monkeypatch, state):
    plan, context, adapter, root, receipt_path, selection, _identity, _args = install_harness
    runner.install_engine(plan, context=context, adapter=adapter, deadline=context.deadline)
    raw = json.loads(receipt_path.read_bytes())
    raw["receipt_id"] = "private-token-marker"
    receipt_path.write_text(json.dumps(raw))
    if state == "failed":
        raw.update(status="failed", error_code="integrity_failed", failed_step_id="helper-identity")
        receipt_path.write_text(json.dumps(raw))
    elif state == "missing":
        receipt_path.unlink()
    elif state == "mismatch":
        raw["recipe_sha256"] = "e" * 64
        raw["receipt_id"] = "private-token-marker"
        receipt_path.write_text(json.dumps(raw))
    elif state == "malformed":
        receipt_path.write_text('{"private-path":"/private/input","private-token":"private-token-marker"')
    elif state == "symlink":
        target = receipt_path.parent / "private-token-marker"
        receipt_path.rename(target)
        receipt_path.symlink_to(target)
    elif state == "oversize":
        receipt_path.write_bytes(b"x" * (1024 * 1024 + 1))
    monkeypatch.setattr(source, "DIAGNOSTIC_ENGINE_ROOT", root)
    def forbidden(*_args, **_kwargs):
        pytest.fail("receipt diagnostic invoked execution/status/readiness")
    monkeypatch.setattr(engine_execution, "create_local_backend", forbidden)
    monkeypatch.setattr(runner, "engine_status", forbidden)
    monkeypatch.setattr(runner, "install_engine", forbidden)
    monkeypatch.setattr(runner._Runner, "helper", forbidden)
    monkeypatch.setattr(local, "local_engine_endpoint", forbidden)
    before_events = list(context.events)
    before = {p: p.read_bytes() for p in receipt_path.parent.iterdir() if p.is_file()}
    result = source.diagnose_selected(selection)
    assert context.events == before_events
    assert before == {p: p.read_bytes() for p in receipt_path.parent.iterdir() if p.is_file()}
    assert result["readiness"] == "not_checked"
    assert result["registered_root_matches"] is True
    assert result["source"] == {k: selection[k] for k in ("agent_head", "runtime_head", "runtime_lock_sha256")}
    assert result["recipe_sha256"] == plan.recipe_sha256
    assert result["models_lock_sha256"] == plan.models_lock_sha256
    assert len(result["expected_helper_sha256"]) == 64
    expected = {"installed": "valid", "failed": "valid", "missing": "missing", "mismatch": "invalid",
                "malformed": "unreadable", "symlink": "unsafe", "oversize": "unsafe"}
    assert result["receipt_state"] == expected[state]
    if state == "failed":
        assert result["receipt_status"] == "failed"
        assert result["error_code"] == "integrity_failed"
        assert result["failed_step_ids"] == ["helper-identity"]
    elif state == "installed":
        assert result["receipt_status"] == "installed"
        assert result["failed_step_ids"] == []
    elif state == "mismatch":
        assert result["receipt_identity_matches"]["recipe_sha256"] is False
    text = json.dumps(result)
    assert root not in text and "/private/input" not in text and "private-token-marker" not in text
    assert selection["agent_root"] not in text and selection["runtime_root"] not in text


def test_receipt_diagnostic_refuses_unregistered_root_before_reading(install_harness, monkeypatch):
    _plan, context, _adapter, root, _receipt, selection, _identity, _args = install_harness
    local._record_path("vibecomfy-h3").unlink()
    monkeypatch.setattr(source, "DIAGNOSTIC_ENGINE_ROOT", root)
    from astrid.core.generation._engine_target_files import _TargetFiles
    monkeypatch.setattr(_TargetFiles, "file_fact", lambda *_a, **_kw: pytest.fail("unregistered receipt read"))
    before = list(context.events)
    result = source.diagnose_selected(selection)
    assert result["registered_root_matches"] is False and result["receipt_state"] == "not_read"
    assert context.events == before
