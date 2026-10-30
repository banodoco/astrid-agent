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
