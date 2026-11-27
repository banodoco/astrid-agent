"""Transfer owner regressions; native listener binding remains F's responsibility."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from astrid.core.generation import asset_gaps, gpu_environment as gpu, preflight, settings, targets
from tests.core.generation.environment_states import snapshot
from tests.core.generation.test_gpu_compute_probe import report


@pytest.fixture
def machine(tmp_path, monkeypatch):
    folder = tmp_path / "models"
    folder.mkdir()
    python = tmp_path / "selected-python"
    python.write_bytes(b"test interpreter identity; never executed")
    endpoint = "http://127.0.0.1:8188"
    record = targets.TargetRecord(endpoint=endpoint, root=str(tmp_path), python=str(python),
                                  layout="portable", facts={"installed_by": "astrid"})
    plan = {"entry": "entry", "mode": "mode", "target": endpoint,
            "files": [{"file": "model", "folder": str(folder), "bytes": 8}],
            "present": [], "manual": [], "total_bytes": 8, "partial_bytes": [],
            "space_ok": True, "plan_sha256": "a" * 64}
    placement = {"kind": "attached", "endpoint": endpoint}
    monkeypatch.setattr(targets, "read_target", lambda: record)
    monkeypatch.setattr(settings, "placement_default", lambda: placement)
    monkeypatch.setattr(asset_gaps, "plan_attached_model_download", lambda *a, **k: plan)
    calls = []
    monkeypatch.setattr(asset_gaps, "_hard_links_supported", lambda *a: calls.append("link") or True)
    monkeypatch.setattr(asset_gaps, "_fetch_into_folder", lambda *a, **k: calls.append("fetch") or 0)

    def apply(request=None, digest="a" * 64):
        return asset_gaps.apply_attached_model_download(
            SimpleNamespace(target=SimpleNamespace(url=endpoint, kind="attached")),
            "entry", "mode", plan_sha256=digest, compute_request=request)

    return SimpleNamespace(root=tmp_path, record=record, plan=plan, placement=placement,
                           calls=calls, apply=apply, request={"entry": "entry", "mode": "mode",
                           "device": "cuda:0", "backend": "cuda",
                           "required_operations": ["matmul_fp16"]})


def bind_observation(machine, monkeypatch, *, failed=False, unknown=False, change=None):
    """Exercise E's actual request/receipt classifier via the planned F seam.

    A fake subprocess supplies numerical test evidence, not a physical GPU
    pass. F's actual native identity observer must be proved at integration.
    """
    def inspect(selection):
        assert selection.bound and selection.record == machine.record
        ladder = preflight.new_ladder("compatible")
        for name in ("installed", "reachable", "identified", "compatible"):
            ladder = preflight.with_rung(ladder, name, "observed", observation={})
        return selection, ladder, None, None

    def observe(ladder, selection, *, device, backend, required_operations):
        machine.calls.append("compute")
        assert selection.record.python == machine.record.python
        assert (device, backend, list(required_operations)) == ("cuda:0", "cuda", ["matmul_fp16"])
        ladder["through"] = "computes"
        if unknown:
            return preflight.with_rung(ladder, "computes", "unchecked", reason="interpreter_binding_unverified",
                                       message="Selected interpreter binding unknown.")
        raw = report(required=tuple(required_operations), interpreter=machine.record.python,
                     cuda_visible_devices="3,2", cuda_device_order=None)
        if failed:
            raw.update(ok=False, operations={}, error={"stage": "matmul_fp16", "type": "RuntimeError",
                "message": "CUDA error: no kernel image is available"})

        def run(argv, **kwargs):
            assert argv[0] == machine.record.python
            assert json.loads(argv[-1])["required_operations"] == ["matmul_fp16"]
            assert kwargs["env"]["CUDA_VISIBLE_DEVICES"] == "3,2"
            return subprocess.CompletedProcess(argv, 0, json.dumps(raw), "")

        evidence = gpu.probe_compute(selection.record.python, device=device, backend=backend,
            required_operations=required_operations, environ={"CUDA_VISIBLE_DEVICES": "3,2"}, run=run)
        if change:
            evidence.update(change)
        return preflight.with_rung(ladder, "computes", "failed" if failed else "observed",
                                  reason=evidence["cause"], message=evidence["message"], observation=evidence)

    monkeypatch.setattr(preflight, "inspect_existing_selection", inspect, raising=False)
    monkeypatch.setattr(preflight, "observe_compute_rung", observe, raising=False)


def test_selected_compute_pass_precedes_any_probe_or_fetch(machine, monkeypatch):
    bind_observation(machine, monkeypatch)
    result = machine.apply(machine.request)
    assert machine.calls == ["compute", "link", "fetch"]
    assert len(result["downloaded"]) == 1


@pytest.mark.parametrize(("failed", "unknown", "code", "fragment"), [
    (True, False, "compute_failed", "no usable kernels"),
    (False, True, "compute_unknown", "binding unknown"),
])
def test_failed_or_unknown_compute_prevents_all_writes(machine, monkeypatch, failed, unknown, code, fragment):
    bind_observation(machine, monkeypatch, failed=failed, unknown=unknown)
    before = snapshot(machine.root)
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        machine.apply(machine.request)
    assert caught.value.code == code and fragment in str(caught.value)
    if failed:
        assert "CUDA 12.8 or newer" in str(caught.value)
    assert str(caught.value).endswith("Nothing was downloaded.")
    assert machine.calls == ["compute"] and snapshot(machine.root) == before


@pytest.mark.parametrize("change", [{"device": "cuda:1"}, {"requested_interpreter": "/other/python"},
                                    {"required_operations": ["matmul_fp32"]}, {"ok": False}])
def test_wrong_evidence_cannot_authorize_transfer(machine, monkeypatch, change):
    bind_observation(machine, monkeypatch, change=change)
    before = snapshot(machine.root)
    with pytest.raises(asset_gaps.AttachedDownloadRefused, match="No positive compute receipt") as caught:
        machine.apply(machine.request)
    assert caught.value.code == "compute_unknown"
    assert machine.calls == ["compute"] and snapshot(machine.root) == before


@pytest.mark.parametrize("missing", ["request", "python", "placement", "producer", "workflow"])
def test_managed_route_missing_authority_is_not_legacy_bypass(machine, monkeypatch, missing):
    from dataclasses import replace
    bind_observation(machine, monkeypatch)
    request = machine.request
    if missing == "request":
        request = None
    elif missing == "python":
        monkeypatch.setattr(targets, "read_target", lambda: replace(machine.record, python=None))
    elif missing == "placement":
        machine.placement["endpoint"] = "http://127.0.0.1:8000"
    elif missing == "producer":
        monkeypatch.delattr(preflight, "observe_compute_rung", raising=False)
    elif missing == "workflow":
        request = {**request, "entry": "other"}
    before = snapshot(machine.root)
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        machine.apply(request)
    assert caught.value.code == "compute_unknown"
    assert not machine.calls and snapshot(machine.root) == before


def test_unrelated_person_owned_transfer_keeps_original_behavior(machine, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr(targets, "read_target", lambda: replace(machine.record, facts={}))
    bind_observation(machine, monkeypatch)
    machine.apply()
    assert machine.calls == ["link", "fetch"]


def test_bad_consent_and_no_missing_files_skip_compute(machine, monkeypatch):
    bind_observation(machine, monkeypatch)
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        machine.apply(machine.request, digest="b" * 64)
    assert caught.value.code == "plan_changed"
    machine.plan["files"] = []
    with pytest.raises(asset_gaps.AttachedDownloadRefused) as caught:
        machine.apply(machine.request)
    assert caught.value.code == "nothing_to_download" and not machine.calls
