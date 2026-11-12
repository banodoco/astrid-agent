"""A failed post-install verification names which fixed check failed."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import remote_placement as rp
from astrid.packs.runpod.failure_reason import Reason
from astrid.packs.runpod.failure_receipt import build_receipt, render


def _placement(monkeypatch, exec_result, remaining=600.0):
    placement = object.__new__(rp.RemotePlacement)
    monkeypatch.setattr(placement, "_remaining", lambda: remaining, raising=False)
    monkeypatch.setattr(placement, "_exec", lambda state, command, timeout: exec_result, raising=False)
    return placement


def _state(tmp_path: Path):
    installation = SimpleNamespace(engine="comfyui", platform="linux-x86_64-cuda", root="/workspace/engine",
                                   models_root=None, plan=lambda: object())
    return rp.RemoteRun(run_id="r", remote_root="/workspace/run", local_out=tmp_path, installation=installation)


@pytest.mark.parametrize(("exec_result", "expected"), [
    ((7, "", ""), "status_exit_7"),
    ((0, "null", ""), "status_no_receipt"),
    ((0, json.dumps({"status": "failed", "error_code": "engine_health_failed",
                     "failed_step_id": "start:comfyui"}), ""),
     "status=failed;error_code=engine_health_failed;failed_step_id=start:comfyui"),
    ((0, json.dumps({"status": "failed", "error_code": "prompt text with spaces /home/alice",
                     "failed_step_id": None}), ""),
     "status=failed;error_code=withheld;failed_step_id=withheld"),
])
def test_canonical_verification_records_a_fixed_reason(monkeypatch, tmp_path, exec_result, expected):
    placement = _placement(monkeypatch, exec_result)
    state = _state(tmp_path)
    assert placement._canonical_installed(state) is False
    assert state.install_unverified == expected


def test_exhausted_deadline_is_named(monkeypatch, tmp_path):
    placement = _placement(monkeypatch, (0, "", ""), remaining=0)
    state = _state(tmp_path)
    assert placement._canonical_installed(state) is False
    assert state.install_unverified == "status_deadline_exhausted"


@pytest.mark.parametrize("evidence", ["live", "saved"])
def test_canonical_verification_refuses_stale_receipt(monkeypatch, tmp_path, evidence):
    from dataclasses import asdict

    from astrid.core.generation.engine_contracts import (
        HealthEvidence,
        InstallReceipt,
        IntegrityError,
        StepReceipt,
    )
    from astrid.core.generation.engine_recipe import load_recipe
    from astrid.core.generation.engine_runner import plan_install

    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    plan = plan_install(recipe, root="/workspace/engine", placement="local")
    stamp = "2026-11-01T00:00:00Z"
    receipt = InstallReceipt(
        "astrid.engine-install.v1", "installed-receipt", plan.engine, plan.platform,
        plan.placement, plan.root, "installed", False, plan.recipe_sha256,
        plan.models_lock_sha256, {e.name: "b" * 64 for e in recipe.environments},
        {s.name: s.pin.revision for s in recipe.sources}, (),
        tuple(StepReceipt(s.id, s.kind, "executed", "passed", {}) for s in plan.steps),
        HealthEvidence("healthy", stamp, "a" * 64, True), stamp, stamp, None,
    )
    current = asdict(receipt)
    stale = {**current, "recipe_sha256": "0" * 64}
    live = stale if evidence == "live" else current
    saved = stale if evidence == "saved" else current
    placement = _placement(monkeypatch, (0, json.dumps(live), ""))
    state = _state(tmp_path)
    state.installation.platform = recipe.platform
    state.claim = SimpleNamespace(ops=SimpleNamespace(
        get=lambda remote, local: local.write_text(json.dumps(saved))))

    with pytest.raises(IntegrityError, match="installed but stale"):
        placement._canonical_installed(state)
    assert state.installed is None
    assert not (tmp_path / "installed.json").exists()


def test_install_failure_message_carries_the_reason(monkeypatch, tmp_path):
    placement = _placement(monkeypatch, (0, json.dumps({"status": "failed", "error_code": "engine_health_failed",
                                                        "failed_step_id": "start:comfyui"}), ""))
    monkeypatch.setattr(placement, "_run_job", lambda state, command, what: None, raising=False)
    monkeypatch.setattr(placement, "_engine_installed_healthy",
                        lambda state: placement._canonical_installed(state), raising=False)
    monkeypatch.setattr(rp, "INSTALL_DEADLINE_SECONDS", 600, raising=False)
    state = _state(tmp_path)
    with pytest.raises(rp.RemotePlacementError, match=r"reason: status=failed;error_code=engine_health_failed"):
        placement._install(state)


def test_install_refusal_reaches_receipt_and_cli(monkeypatch, tmp_path):
    placement = _placement(monkeypatch, (7, "", ""))
    monkeypatch.setattr(placement, "_run_job", lambda *args: None)
    with pytest.raises(rp.PlacementReasonError) as raised:
        placement._install(_state(tmp_path))
    receipt = build_receipt({"failure": raised.value, "stage": "claim"}, job=SimpleNamespace(),
                            machine=None, timing=None)
    assert receipt["stage"] == "install"
    assert receipt["cause"]["reason"] == {"stage": "install", "step": "verify", "gate": "install",
                                           "code": "status_exit", "exit_code": 7}
    assert receipt["cause"]["code"] == "status_exit"
    assert "status_exit" in render(receipt)


def test_structured_refusal_requires_reason():
    with pytest.raises((TypeError, ValueError)):
        rp.PlacementReasonError("refused")
    with pytest.raises(ValueError):
        rp.PlacementReasonError("refused", reason=None)


def test_reason_rejects_free_text():
    with pytest.raises(ValueError):
        Reason(stage="install", step="verify", code="private/path")
