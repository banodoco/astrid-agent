"""Focused source-bound tests for the hosted R-24 materializer."""
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_journey_provider as provider
from scripts import ci_journey_r24_materializer as materializer
from scripts import ci_journey_r24_task as task_boundary


def test_materializer_pins_current_tuple_producer_and_native_engine():
    assert materializer.PUBLISHED_CANDIDATE == provider.LIVE_CANDIDATE
    assert materializer.PUBLISHED_CANDIDATE == {
        "app": "ce1502e94b935fe6ef1e2f363fc6b55aade4a204",
        "agent": "2de0726d1eaea7f7bd141251e088aa9fdf224525",
        "runtime": "5453366458f7b4ee874c94d15ae7952e3e82d3c3",
        "pinokio": "3a6a98ae251ca126505245286e2d3e760f4348e9",
        "manifest_sha256": "920477210d01615a6698cda30f248552f13c712734e86ea56f283300cb1a9c87",
    }
    assert provider.LIVE_PRODUCER_REF == "cd86889f4299c7f7aca720f0f20a11be01dacb34"
    assert materializer.PINOKIOD_REF == "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
    assert materializer.PTERM_REF == "5be43718701c448b43daf0ce51d8935f170065bb"


def test_pterm_and_documented_route_arguments_are_exact(tmp_path):
    node = tmp_path / "node"
    pterm = tmp_path / "pterm"
    assert materializer.pterm_argv(node, pterm, "pinokio/install.js") == [
        str(node), str(pterm / "index.js"), "start", "pinokio/install.js",
        "--ref", "pinokio://127.0.0.1:42000/api/astrid",
    ]
    roots = {name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")}
    argv = materializer.route_argv(
        tmp_path / "runner", tmp_path / "run", tmp_path / "candidate.json",
        tmp_path / "config.json", tmp_path / "producer", roots,
    )
    assert argv[3:5] == ["--shared-executor", "r24-live"]
    assert argv[-8:] == [
        "--app-checkout", str(roots["app"]),
        "--agent-checkout", str(roots["agent"]),
        "--runtime-checkout", str(roots["runtime"]),
        "--pinokio-checkout", str(roots["pinokio"]),
    ]


def test_build_config_binds_real_process_and_dependency_hashes(tmp_path, monkeypatch):
    node = tmp_path / "node"
    pterm = tmp_path / "pterm"
    daemon = tmp_path / "daemon"
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    (pterm / "node_modules/unws/src").mkdir(parents=True)
    for path, raw in ((node, b"node"), (pterm / "index.js", b"pterm"),
                      (pterm / "node_modules/unws/src/node.cjs", b"unws")):
        path.write_bytes(raw)
    for path in (daemon, home, scratch):
        path.mkdir()
    monkeypatch.setattr(materializer, "_proc_start_ticks", lambda unused: "4242")
    config = materializer.build_config(
        checkouts={name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")},
        pterm=pterm, pinokiod=daemon, node=node, daemon_pid=123,
        home=home, scratch=scratch,
    )
    assert config["engine"]["pid"] == 123
    assert config["engine"]["start_ticks"] == "4242"
    assert config["node_sha256"] == hashlib.sha256(b"node").hexdigest()
    assert config["pterm_entry_sha256"] == hashlib.sha256(b"pterm").hexdigest()
    assert config["unws_sha256"] == hashlib.sha256(b"unws").hexdigest()


def test_hosted_binding_requires_actions_run_job_and_session(monkeypatch):
    values = {
        "GITHUB_REPOSITORY": "banodoco/astrid-agent",
        "GITHUB_WORKFLOW": "R-24 hosted Linux live materializer",
        "GITHUB_WORKFLOW_REF": "banodoco/astrid-agent/.github/workflows/r24-linux-live.yml@refs/heads/lane",
        "GITHUB_WORKFLOW_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_JOB": "r24-live", "GITHUB_SHA": "a" * 40,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    provider_receipt = {"run_id": "b" * 32, "session": "c" * 32,
                        "request_sha256": "d" * 64, "export": {"files": []}}
    task = {"task": {"task_id": "e" * 32, "run_id": "f" * 32,
                     "state": "running", "execution_binding": {"status": "claimed"}}}
    binding = materializer._hosted_binding(provider_receipt, task)
    assert binding["workflow"]["job"] == "r24-live"
    assert binding["session"] == "c" * 32
    monkeypatch.delenv("GITHUB_JOB")
    with pytest.raises(materializer.MaterializerError, match="Actions binding"):
        materializer._hosted_binding(provider_receipt, task)


def test_workflow_has_dispatch_only_and_separate_exact_artifacts():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/r24-linux-live.yml"
    raw = path.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in raw
    assert "push:" not in raw
    assert "ubuntu-24.04" in raw
    assert "scripts/ci_journey_r24_materializer.py" in raw
    assert "evidence/r24-live/provider-export" in raw
    assert "evidence/r24-live/hosted-binding.json" in raw
    assert "cd86889f4299c7f7aca720f0f20a11be01dacb34" in raw


def test_task_observer_requires_running_claimed_current_session(monkeypatch, tmp_path):
    health = SimpleNamespace(runtime_instance_id="instance", runtime_session_id="session", runtime_epoch=7)
    handshake = SimpleNamespace()
    task = SimpleNamespace(
        task_id="a" * 32, run_id="b" * 32, state="running", version=3,
        capability_id="chiptune.compose", capability_digest="sha256:" + "c" * 64,
        idempotency_key="key", created_at="now", updated_at="now", runtime_epoch=7,
        input_object_ids=[], spec={}, generation_intent=None, project_id="project",
        attempt_id="d" * 32, result=None,
        execution_request={"target": {"kind": "profile", "id": "astrid"}},
        execution_binding={"status": "claimed", "session_id": "session", "runtime_epoch": 7,
                           "executor_id": "astrid-pack-host", "binding_id": "e" * 32},
        storage_estimate=None, required_facts=None, expected_effect=None,
        lease_fence=1, lease_expires_at="later",
    )
    client = SimpleNamespace(get_task=lambda unused: task)
    monkeypatch.setattr(task_boundary, "_client", lambda unused: (
        client, {}, tmp_path, {"runtime_instance_id": "instance"}))
    monkeypatch.setattr(task_boundary, "_health", lambda unused_client, unused_discovery: (health, handshake))
    monkeypatch.setattr(task_boundary, "_task_projection", lambda value: vars(value))
    observed = task_boundary.observe_task(tmp_path, "a" * 32, 1)
    assert observed["task"]["execution_binding"]["status"] == "claimed"
    task.execution_binding["session_id"] = "foreign"
    with pytest.raises(task_boundary.TaskBoundaryError, match="binding mismatch"):
        task_boundary.observe_task(tmp_path, "a" * 32, 1)


def test_task_observer_rejects_terminal_before_provider(monkeypatch, tmp_path):
    task = SimpleNamespace(state="succeeded", version=2, execution_binding=None)
    client = SimpleNamespace(get_task=lambda unused: task)
    health = SimpleNamespace(runtime_instance_id="instance", runtime_session_id="session", runtime_epoch=7)
    monkeypatch.setattr(task_boundary, "_client", lambda unused: (
        client, {}, tmp_path, {"runtime_instance_id": "instance"}))
    monkeypatch.setattr(task_boundary, "_health", lambda unused_client, unused_discovery: (health, None))
    with pytest.raises(task_boundary.TaskBoundaryError, match="terminal before provider"):
        task_boundary.observe_task(tmp_path, "a" * 32, 1)
