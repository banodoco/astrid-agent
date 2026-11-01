"""Bindings and evidence boundaries for the Pinokio journey adapter."""
from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import pinokio_journey_adapter as adapter


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    # Real clean checkouts and file digests; their test commits are never evidence.
    refs, paths, entries = {}, {}, {}
    for name in adapter.TUPLE:
        path = tmp_path / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        (path / "source").write_text(name)
        subprocess.run(["git", "-C", str(path), "add", "source"], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "test"], check=True)
        ref = subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()
        refs[name], paths[name] = ref, path
        entries[name] = {"path": str(path), "ref": ref,
                         "files": {"source": hashlib.sha256(name.encode()).hexdigest()}}
    monkeypatch.setattr(adapter, "TUPLE", refs)
    config = {"immutable_tuple": refs.copy(), "candidate_checkouts": entries,
              "source_override_test_mode": False,
              "platform": {"os": "linux", "hosted": False, "evidence_class": "executable-boundary"}}
    template = tmp_path / "template.json"
    template.write_text(json.dumps({"linux": config}))
    binding = tmp_path / "binding.md"
    binding.write_text("test binding")
    monkeypatch.setattr(adapter, "CONFIG_SHA256", hashlib.sha256(template.read_bytes()).hexdigest())
    monkeypatch.setattr(adapter, "BINDING_SHA256", hashlib.sha256(binding.read_bytes()).hexdigest())
    return {"config": config, "template_path": template, "binding_path": binding,
            "os_name": "linux", "checkouts": paths, "output": tmp_path / "out"}


@pytest.mark.parametrize("config", [None, {}])
def test_missing_or_empty_config_rejected(inputs, config):
    inputs["config"] = config
    with pytest.raises(adapter.AdapterError, match="non-empty"):
        adapter.execute("pinokio-backend", **inputs)


def test_tuple_path_and_content_bound_before_executor(inputs, monkeypatch):
    monkeypatch.setattr(adapter.JourneyDriver, "compose_pinokio_backend_config",
                        lambda _: pytest.fail("must reject before execution"))
    bad = copy.deepcopy(inputs)
    bad["config"]["immutable_tuple"]["agent"] = "0" * 40
    with pytest.raises(adapter.AdapterError, match="tuple"):
        adapter.execute("pinokio-backend", **bad)
    bad = copy.deepcopy(inputs)
    bad["checkouts"]["agent"] = inputs["checkouts"]["app"]
    with pytest.raises(adapter.AdapterError, match="path differs"):
        adapter.execute("pinokio-backend", **bad)
    bad = copy.deepcopy(inputs)
    bad["config"]["candidate_checkouts"]["agent"]["files"]["source"] = "0" * 64
    with pytest.raises(adapter.AdapterError, match="file inventory"):
        adapter.execute("pinokio-backend", **bad)
    (inputs["checkouts"]["agent"] / "source").write_text("dirty")
    with pytest.raises(adapter.pinokio_backend_executor.ValidationError, match="mutable"):
        adapter.execute("pinokio-backend", **inputs)


def test_template_and_binding_hashes_required(inputs):
    inputs["binding_path"].write_text("changed")
    with pytest.raises(adapter.AdapterError, match="binding SHA-256"):
        adapter.execute("pinokio-fully-local", **inputs)


def test_backend_uses_existing_composition_and_executor_and_preserves_class(inputs, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "_verify_actions", lambda *_: calls.append("actions"))
    def compose(config):
        calls.append("compose")
        return config
    def execute(config, output, executor):
        calls.append("execute")
        assert executor is adapter.pinokio_backend_executor
        return {"evidence_class": "source_fake"}
    monkeypatch.setattr(adapter.JourneyDriver, "compose_pinokio_backend_config", compose)
    monkeypatch.setattr(adapter.JourneyDriver, "execute_pinokio_backend", execute)
    receipt = adapter.execute("pinokio-backend", **inputs)
    assert calls == ["actions", "compose", "execute"]
    assert receipt["evidence_class"] == "source_fake"
    with pytest.raises(adapter.pinokio_backend_executor.ValidationError):
        adapter.pinokio_backend_executor.admit_hosted_receipt(receipt)


def test_backend_rejects_unbound_and_source_override(inputs, monkeypatch):
    monkeypatch.setattr(adapter.JourneyDriver, "compose_pinokio_backend_config",
                        lambda _: pytest.fail("must reject before execution"))
    inputs["config"]["provider"] = "__M4_4A_PROVIDER__"
    with pytest.raises(adapter.AdapterError, match="unmaterialized"):
        adapter.execute("pinokio-backend", **inputs)
    del inputs["config"]["provider"]
    inputs["config"].pop("source_override_test_mode")
    with pytest.raises(adapter.AdapterError, match="explicitly disable"):
        adapter.execute("pinokio-backend", **inputs)


def test_action_arrays_are_compared_to_immutable_builder(inputs, monkeypatch):
    from scripts import ci_journey

    monkeypatch.setattr(ci_journey, "validate_old_launcher_provenance", lambda *_: {})
    monkeypatch.setattr(ci_journey, "build_pinokio_action",
                        lambda _path, ref, **_kw: {"run": [{"ref": ref}]})
    old = adapter.pinokio_backend_executor.APPROVED_OLD_LAUNCHER_REF
    config = inputs["config"]
    config["old_launcher"] = {"ref": old, "sha256": adapter.pinokio_backend_executor.APPROVED_OLD_UPDATE_SHA256,
                              "file": "pinokio/update.js", "checkout": str(inputs["checkouts"]["pinokio"])}
    config["actions"] = {"first": {"steps": [{"ref": old}]},
                         "retry": {"steps": [{"ref": adapter.TUPLE["pinokio"]}]}}
    adapter._verify_actions(config, inputs["checkouts"]["pinokio"])
    config["actions"]["retry"]["steps"] = []
    with pytest.raises(adapter.AdapterError, match="retry action"):
        adapter._verify_actions(config, inputs["checkouts"]["pinokio"])


def test_local_routes_to_executor_and_keeps_its_original_candidate(inputs, monkeypatch, tmp_path):
    checkout, state = tmp_path / "editor", tmp_path / "state"
    checkout.mkdir()
    state.mkdir()
    raw = {"evidence_class": "local_process", "candidate": {"base": "provisional"},
           "hosted_linux_receipt_required": True}
    calls = []
    def run_execution(**kwargs):
        calls.append(kwargs)
        return raw
    monkeypatch.setattr(adapter.fully_local_executor, "run_execution", run_execution)
    monkeypatch.setattr(adapter.fully_local_executor, "validate_execution_receipt", lambda value: None)
    result = adapter.execute("pinokio-fully-local", local_checkout=checkout, state_root=state, **inputs)
    assert calls == [{"checkout": checkout, "state_root": state, "output": inputs["output"]}]
    assert result["executor_receipt"] is raw
    assert result["executor_receipt"]["candidate"] == {"base": "provisional"}
    assert result["tuple_execution_proven"] is False and result["hosted_lifecycle"] is False
    assert result["evidence_class"] == "local_process"
    raw["evidence_class"] = "source_fake"
    with pytest.raises(adapter.AdapterError, match="cannot produce hosted"):
        adapter.execute("pinokio-fully-local", local_checkout=checkout, state_root=state, **inputs)


def test_local_rejects_candidate_as_mutable_and_unknown_mode(inputs, tmp_path):
    with pytest.raises(adapter.AdapterError, match="disjoint"):
        adapter.execute("pinokio-fully-local", local_checkout=inputs["checkouts"]["app"],
                        state_root=tmp_path / "state", **inputs)
    with pytest.raises(adapter.AdapterError, match="unsupported"):
        adapter.execute("direct", **inputs)


def test_hosted_backend_must_pass_existing_admission(inputs, monkeypatch):
    inputs["config"]["platform"].update(hosted=True, **{"evidence_class": "hosted-lifecycle"})
    monkeypatch.setattr(adapter, "_verify_actions", lambda *_: None)
    monkeypatch.setattr(adapter.JourneyDriver, "compose_pinokio_backend_config", lambda config: config)
    monkeypatch.setattr(adapter.JourneyDriver, "execute_pinokio_backend",
                        lambda *_: {"evidence_class": "source_fake"})
    with pytest.raises(adapter.pinokio_backend_executor.ValidationError, match="cannot satisfy hosted"):
        adapter.execute("pinokio-backend", **inputs)


def test_local_adapter_runs_real_disposable_executor(inputs, tmp_path):
    from tests.test_fully_local_executor import _disposable_fixture, _port_free

    if not (_port_free(17340) and _port_free(5180)):
        pytest.skip("local smoke ports are occupied")
    checkout, state, baseline, _ = _disposable_fixture(tmp_path)
    result = adapter.execute("pinokio-fully-local", local_checkout=checkout, state_root=state, **inputs)
    raw = json.loads(inputs["output"].read_text())
    assert result["executor_receipt"] == raw
    assert raw["candidate"] == adapter.fully_local_executor.PINNED_TUPLE
    assert raw["checkout"]["after"]["head"] == baseline
    assert result["evidence_class"] == "local_process"
    assert result["tuple_execution_proven"] is False and result["hosted_lifecycle"] is False
