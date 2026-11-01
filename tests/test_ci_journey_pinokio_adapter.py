"""Journey-driver dispatch and fail-closed M-PIN adapter bindings."""
from __future__ import annotations

import argparse
import copy
import json
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import pinokio_journey_adapter as adapter
from tests.test_pinokio_journey_adapter import inputs  # synthetic immutable inputs


@pytest.fixture
def driver_args(inputs, tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "journey_os_label", lambda: "linux")
    paths = inputs["checkouts"]
    config = inputs["config"]
    config["platform"].update(runner="local", shell="sh")
    old = adapter.pinokio_backend_executor.APPROVED_OLD_LAUNCHER_REF
    config["old_launcher"] = {"ref": old}
    config["actions"] = {"first": {"steps": [{"ref": old}]},
                         "retry": {"steps": [{"ref": adapter.TUPLE["pinokio"]}]}}
    config_path = tmp_path / "materialized.json"
    config_path.write_text(json.dumps(config))
    args = argparse.Namespace(
        leg="fresh-install", shell="sh", mode="pinokio-backend", source_override=False,
        pinokio_backend_config=config_path, pinokio_config_template=inputs["template_path"],
        pinokio_config_binding=inputs["binding_path"],
        app_checkout=paths["app"], candidate_agent_checkout=paths["agent"],
        candidate_runtime_checkout=paths["runtime"], pinokio_checkout=paths["pinokio"],
        candidate_checkouts={name: {"sha": ref} for name, ref in adapter.TUPLE.items()},
        old_launcher_ref=old, pinokio_local_checkout=tmp_path / "local-checkout",
        pinokio_local_state_root=tmp_path / "local-state")
    monkeypatch.setattr(journey, "runner_identity", lambda: {"runner_image": "local"})
    monkeypatch.setattr(journey, "validate_old_launcher_provenance", lambda *_: {})
    monkeypatch.setattr(journey, "build_pinokio_action", lambda _path, ref, **_: {"run": [{"ref": ref}]})
    return args


def test_backend_invokes_adapter_with_pinned_inputs(driver_args, inputs, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "_verify_actions", lambda *_: calls.append("actions"))
    monkeypatch.setattr(adapter.JourneyDriver, "compose_pinokio_backend_config", lambda config: config)
    def execute(config, output, executor):
        calls.append("executor")
        assert config == json.loads(driver_args.pinokio_backend_config.read_text())
        assert executor is adapter.pinokio_backend_executor
        (output / "pinokio-backend").mkdir(parents=True)
        (output / "pinokio-backend" / "events.jsonl").write_text("sidecar")
        return {"pinokio_backend": {}, "evidence_class": "executable-boundary"}
    monkeypatch.setattr(adapter.JourneyDriver, "execute_pinokio_backend", execute)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    result = journey.execute_pinokio_backend(driver_args, evidence)
    assert calls == ["actions", "executor"]
    assert result["evidence_class"] == "executable-boundary"
    assert (evidence / "pinokio-backend" / "events.jsonl").read_text() == "sidecar"


def test_fully_local_invokes_adapter_and_preserves_envelope(driver_args, tmp_path, monkeypatch):
    driver_args.mode = "pinokio-fully-local"
    raw = {"evidence_class": "local_process", "candidate": {"base": "provisional"}}
    calls = []
    monkeypatch.setattr(adapter.fully_local_executor, "run_execution", lambda **kwargs: calls.append(kwargs) or raw)
    monkeypatch.setattr(adapter.fully_local_executor, "validate_execution_receipt", lambda receipt: None)
    evidence = tmp_path / "evidence"
    result = journey._execute_owned(driver_args, tmp_path / "program", tmp_path / "data",
                                    evidence, SimpleNamespace(pid=123), "birth")
    assert calls == [{"checkout": driver_args.pinokio_local_checkout,
                      "state_root": driver_args.pinokio_local_state_root,
                      "output": evidence / "pinokio-local-raw.json"}]
    assert result["executor_receipt"] == raw
    assert result["bound_input_tuple"] == adapter.TUPLE
    assert result["tuple_execution_proven"] is False
    assert result["hosted_lifecycle"] is False and result["evidence_class"] == "local_process"
    assert result["status"] == "exercised"
    assert json.loads((evidence / "journey-result.json").read_text())["tuple_execution_proven"] is False


@pytest.mark.parametrize("mode", ["pinokio-backend", "pinokio-fully-local"])
@pytest.mark.parametrize("mismatch", ["tuple", "template", "binding", "inventory", "override"])
def test_driver_rejects_mismatched_inputs_before_executor(driver_args, inputs, tmp_path, monkeypatch, mode, mismatch):
    driver_args.mode = mode
    monkeypatch.setattr(adapter.JourneyDriver, "execute_pinokio_backend", lambda *_: pytest.fail("executed"))
    monkeypatch.setattr(adapter.fully_local_executor, "run_execution", lambda **_: pytest.fail("executed"))
    config = copy.deepcopy(json.loads(driver_args.pinokio_backend_config.read_text()))
    if mismatch == "tuple":
        config["immutable_tuple"]["agent"] = "0" * 40
    elif mismatch == "inventory":
        config["candidate_checkouts"]["agent"]["files"]["source"] = "0" * 64
    elif mismatch in {"template", "binding"}:
        path = driver_args.pinokio_config_template if mismatch == "template" else driver_args.pinokio_config_binding
        path.write_bytes(path.read_bytes() + b"tampered")
    else:
        driver_args.source_override = True
    driver_args.pinokio_backend_config.write_text(json.dumps(config))
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with pytest.raises(ValueError):
        if mode == "pinokio-backend":
            journey.execute_pinokio_backend(driver_args, evidence)
        else:
            journey.execute_pinokio_fully_local(driver_args, evidence)


def test_direct_selection_does_not_call_adapter(tmp_path, monkeypatch):
    args = argparse.Namespace(leg="fresh-install", shell="sh", mode="direct", source_override=False,
                              app_checkout=tmp_path, agent_checkout=None, runtime_checkout=None)
    monkeypatch.setattr(adapter, "execute", lambda *a, **kw: pytest.fail("direct selected Pinokio adapter"))
    def direct_manifest(*args, **kwargs):
        raise RuntimeError("direct path selected")
    monkeypatch.setattr(journey, "selected_manifest", direct_manifest)
    with pytest.raises(RuntimeError, match="direct path selected"):
        journey._execute_owned(args, tmp_path / "program", tmp_path / "data", tmp_path / "evidence",
                               SimpleNamespace(pid=123), "birth")
