"""Focused source-bound tests for the hosted R-24 materializer."""
import argparse
import hashlib
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_journey_provider as provider
from scripts import ci_journey_r24_materializer as materializer
from scripts import ci_journey_r24_push_admission as push_admission
from scripts import ci_journey_r24_task as task_boundary
from scripts import pinokio_linux_mode_actions as mode_actions


def test_materializer_pins_exact_candidate_producer_and_native_engine():
    assert materializer.R24_CANDIDATE == provider.LIVE_CANDIDATE
    assert materializer.R24_CANDIDATE == {
        "app": "bfb15570d72c39fbdc32f6209ecec7c929b71d5c",
        "agent": "69dda026212ca16c37cc8239d06ccd159603aa24",
        "runtime": "2776ba973b6c8f3555b5629b0840d9691a98d3c2",
        "pinokio": "7955800419fc92b11e553d6f7acb7613090617e6",
        "manifest_sha256": "09707100ad238cf206085aa5b267ca4108e482ac50e06068a3475932180b65bd",
    }
    assert provider.LIVE_PRODUCER_REF == "93c21a16dc94269c0c575f1a721e9b01ef1f386b"
    assert materializer.PINOKIOD_REF == "add4a674ad1ba95bb62ec427fce519fe5bf0e9bf"
    assert materializer.PTERM_REF == "5be43718701c448b43daf0ce51d8935f170065bb"
    assert materializer.TASK_DURATION_SECONDS == 300
    assert mode_actions.MODE_PRODUCER_REF == "13cb31104c8aae655d0ee04efdb9537aa111e340"


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
    mode_argv = materializer.route_argv(
        tmp_path / "runner", tmp_path / "run", tmp_path / "candidate.json",
        tmp_path / "config.json", tmp_path / "producer", roots,
        mode="backend", mode_producer_root=tmp_path / "mode-producer",
    )
    assert mode_argv[3:5] == ["--shared-executor", "r25-linux-mode-actions"]
    assert mode_argv[-6:] == [
        "--mode", "backend",
        "--mode-producer-checkout", str(tmp_path / "mode-producer"),
        "--output", str(tmp_path / "run/evidence/r25-linux-mode-actions"),
    ]
    local_argv = materializer.route_argv(
        tmp_path / "runner", tmp_path / "run", tmp_path / "candidate.json",
        tmp_path / "config.json", tmp_path / "producer", roots,
        mode="fully-local", mode_producer_root=tmp_path / "mode-producer",
    )
    assert local_argv[-6:] == [
        "--mode", "fully-local",
        "--mode-producer-checkout", str(tmp_path / "mode-producer"),
        "--output", str(tmp_path / "run/evidence/r25-linux-mode-actions"),
    ]


def test_install_candidate_preserves_the_first_identity_error(tmp_path, monkeypatch):
    monkeypatch.setattr(materializer, "_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        materializer,
        "_installed",
        lambda *args: (_ for _ in ()).throw(
            materializer.MaterializerError("installed current selection mismatch")
        ),
    )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    with pytest.raises(
        materializer.MaterializerError,
        match=(
            "transport completed without a valid installed candidate: "
            "installed current selection mismatch"
        ),
    ):
        materializer._install_candidate(
            node=tmp_path / "node",
            pterm=tmp_path / "pterm",
            resource=tmp_path / "resource",
            manifest=tmp_path / "manifest.json",
            home=tmp_path,
            env={},
            evidence=evidence,
            receipt={},
        )


def test_install_candidate_persists_bounded_identity_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(materializer, "_run", lambda *args, **kwargs: None)
    resource = tmp_path / "resource"
    backend = resource / "app/astrid"
    version = backend / "versions/v1"
    version.mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (backend / "install.json").write_text(
        json.dumps({"owner": "haus.astrid.runtime", "managed_by": "pinokio",
                    "data_root": str(tmp_path / "data")}), encoding="utf-8",
    )
    (backend / "current").write_text("v1\n", encoding="utf-8")
    (version / "version.json").write_text(
        json.dumps({"root": str(tmp_path / "wrong"),
                    "python": str(version / "venv/bin/python"),
                    "profile": str(version / "profile.json"),
                    "identity": {"release": 1, "managed_by": "pinokio"}}),
        encoding="utf-8",
    )
    (version / "manifest.json").write_text("{}", encoding="utf-8")
    manifest = tmp_path / "candidate.json"
    manifest.write_text("{}", encoding="utf-8")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    receipt = {}
    with pytest.raises(materializer.MaterializerError, match="version root mismatch"):
        materializer._install_candidate(
            node=tmp_path / "node", pterm=tmp_path / "pterm", resource=resource,
            manifest=manifest, home=tmp_path, env={}, evidence=evidence, receipt=receipt,
        )
    diagnostic = json.loads((evidence / "installed-identity.json").read_text())
    assert diagnostic["current"]["text"] == "v1\n"
    assert diagnostic["install_json"]["json"]["owner"] == "haus.astrid.runtime"
    assert diagnostic["version_json"]["json"]["root"] == str(tmp_path / "wrong")
    assert diagnostic["installed_manifest"]["sha256"] == diagnostic["candidate_manifest"]["sha256"]
    assert receipt["installed_identity_diagnostic"]["sha256"] == hashlib.sha256(
        (evidence / "installed-identity.json").read_bytes()
    ).hexdigest()


def test_installed_mode_receipt_is_parsed_and_bound_without_deriving_from_route(tmp_path):
    resource = tmp_path / "resource"
    mode_file = resource / "app/install-mode.json"
    mode_file.parent.mkdir(parents=True)
    mode_file.write_text('{"schema_version": 1, "mode": "backend"}\n', encoding="utf-8")
    assert materializer._pinokio_install_mode_receipt(resource, "backend") == {
        "schema_version": 1, "mode": "backend",
    }
    with pytest.raises(materializer.MaterializerError, match="differs from the selected route"):
        materializer._pinokio_install_mode_receipt(resource, "local")


@pytest.mark.parametrize("payload", [
    '{"schema_version": 1, "mode": "backend", "declared": true}',
    '{"schema_version": true, "mode": "backend"}',
    '{"schema_version": 1, "mode": "backend", "mode": "local"}',
    '{"schema_version": 1, "mode": "unknown"}',
])
def test_installed_mode_receipt_rejects_extra_typed_duplicate_and_unknown_fields(tmp_path, payload):
    resource = tmp_path / "resource"
    mode_file = resource / "app/install-mode.json"
    mode_file.parent.mkdir(parents=True)
    mode_file.write_text(payload, encoding="utf-8")
    with pytest.raises(materializer.MaterializerError, match="installed Pinokio mode receipt"):
        materializer._pinokio_install_mode_receipt(resource, "backend")


def test_runner_local_home_reaches_child_outside_pinokio_home(tmp_path):
    run_root = tmp_path / "run"
    pinokio_home = run_root / "pinokio-home"
    pinokio_home.mkdir(parents=True)
    runner_home = materializer._runner_local_home(run_root, pinokio_home)
    env = materializer._engine_environment(pinokio_home, runner_home)
    observed = tmp_path / "child-home.txt"
    receipt = {"commands": []}
    materializer._run(
        [sys.executable, "-c",
         "import os, pathlib; pathlib.Path(os.environ['R24_HOME_PROBE']).write_text(os.environ['HOME'])"],
        log=tmp_path / "child-home.log", receipt=receipt, label="installer-home-child-probe",
        env={**env, "R24_HOME_PROBE": str(observed)}, timeout=30,
    )
    assert observed.read_text() == str(runner_home)
    assert pinokio_home.resolve() not in runner_home.parents
    assert receipt["commands"][-1]["exit"] == 0
    with pytest.raises(materializer.MaterializerError, match="installer-child-failure exited 23"):
        materializer._run(
            [sys.executable, "-c", "raise SystemExit(23)"],
            log=tmp_path / "child-failure.log", receipt=receipt,
            label="installer-child-failure", env=env, timeout=30,
        )
    assert receipt["commands"][-1]["exit"] == 23


def test_pterm_transport_success_is_not_install_success(monkeypatch, tmp_path):
    calls = []

    def completed(argv, **kwargs):
        calls.append((argv, kwargs))

    monkeypatch.setattr(materializer, "_run", completed)
    resource = tmp_path / "pinokio-home/api/astrid"
    manifest = tmp_path / "install-manifest.json"
    manifest.write_text("{}")
    with pytest.raises(materializer.MaterializerError, match="transport completed"):
        materializer._install_candidate(
            node=tmp_path / "node", pterm=tmp_path / "pterm", resource=resource,
            manifest=manifest, home=tmp_path / "pinokio-home",
            env={"HOME": str(tmp_path / "installer-home")},
            evidence=tmp_path / "evidence", receipt={"commands": []},
        )
    assert calls[0][0] == materializer.pterm_argv(tmp_path / "node", tmp_path / "pterm",
                                                   "pinokio/install.js")
    assert calls[0][1]["label"] == "pinokio-install"


def test_build_config_binds_real_process_and_dependency_hashes(tmp_path, monkeypatch):
    node = tmp_path / "node"
    pterm = tmp_path / "pterm"
    daemon = tmp_path / "daemon"
    home = tmp_path / "pinokio-home"
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
    execution_binding = {"status": "claimed", "binding_id": "1" * 32,
                         "session_id": "runtime-session", "runtime_epoch": 7,
                         "executor_id": "astrid-pack-host", "target_kind": "profile",
                         "target_id": "astrid"}
    client_source = {"path": "/installed/generated.py", "sha256": "9" * 64,
                     "expected_path": "/checkout/generated.py"}
    task = {"authenticated": True, "installed_client": client_source,
            "runtime": {"instance_id": "instance",
                        "session_id": "runtime-session", "epoch": 7},
            "task": {"task_id": "e" * 32, "run_id": "f" * 32,
                     "state": "running", "capability_id": "chiptune.compose",
                     "execution_binding": execution_binding}}
    active = {"task_id": "e" * 32, "run_id": "f" * 32,
              "capability": "chiptune.compose", "status": "running",
              "binding_id": "1" * 32, "execution_session_id": "runtime-session",
              "runtime_epoch": 7, "executor_id": "astrid-pack-host",
              "target_kind": "profile", "target_id": "astrid", "binding_status": "claimed"}
    protected = {"active_tasks": [active]}
    busy = {"runtime": {"runtime_instance_id": "instance",
                        "runtime_session_id": "runtime-session", "runtime_epoch": 7},
            "protected_before": protected, "protected_after": protected,
            "busy": {"blockers": [
                {"task_id": "e" * 32, "state": "running", "blocker_kind": "running_task"},
                {"task_id": "e" * 32, "state": "running",
                 "blocker_kind": "claimed_or_stale_binding"},
            ]}}
    busy_sha = hashlib.sha256(materializer._canonical(busy)).hexdigest()
    provider_receipt = {"run_id": "b" * 32, "session": "c" * 32,
                        "request_sha256": "d" * 64,
                        "runtime_execution_sources": {"agent": {"agent.py": "1" * 64},
                                                      "runtime": {"runtime.py": "2" * 64}},
                        "export": {"files": [{"path": "observation-busy.json",
                                                "sha256": busy_sha}]}}
    settled = {"authenticated": True, "installed_client": client_source,
               "runtime": task["runtime"],
               "task": {"task_id": "e" * 32, "run_id": "f" * 32,
                        "state": "cancelled"}}
    binding = materializer._hosted_binding(
        provider_receipt, task, busy, settled,
        task_sha="1" * 64, busy_sha=busy_sha, settled_sha="2" * 64,
    )
    assert binding["workflow"]["job"] == "r24-live"
    assert binding["session"] == "c" * 32
    assert binding["busy_observation_sha256"] == busy_sha
    monkeypatch.delenv("GITHUB_JOB")
    with pytest.raises(materializer.MaterializerError, match="Actions binding"):
        materializer._hosted_binding(
            provider_receipt, task, busy, settled,
            task_sha="1" * 64, busy_sha=busy_sha, settled_sha="2" * 64,
        )


@pytest.mark.parametrize("change", ["missing", "foreign", "extra"])
def test_hosted_binding_rejects_busy_census_not_exact_task(monkeypatch, change):
    values = {
        "GITHUB_REPOSITORY": "banodoco/astrid-agent", "GITHUB_WORKFLOW": "workflow",
        "GITHUB_WORKFLOW_REF": "workflow-ref", "GITHUB_WORKFLOW_SHA": "a" * 40,
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_JOB": "r24-live",
        "GITHUB_SHA": "a" * 40,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    client_source = {"path": "/installed/generated.py", "sha256": "9" * 64,
                     "expected_path": "/checkout/generated.py"}
    task = {"authenticated": True, "installed_client": client_source,
            "runtime": {"instance_id": "instance", "session_id": "session", "epoch": 3},
            "task": {"task_id": "1" * 32, "run_id": "2" * 32, "state": "running",
                     "capability_id": "chiptune.compose", "execution_binding": {
                         "binding_id": "3" * 32, "executor_id": "executor",
                         "status": "claimed", "session_id": "session", "runtime_epoch": 3,
                         "target_kind": "profile", "target_id": "astrid"}}}
    row = {"task_id": "1" * 32, "run_id": "2" * 32, "capability": "chiptune.compose",
           "status": "running", "binding_id": "3" * 32,
           "execution_session_id": "session", "runtime_epoch": 3,
           "executor_id": "executor", "target_kind": "profile", "target_id": "astrid",
           "binding_status": "claimed"}
    rows = [] if change == "missing" else [dict(row)]
    if change == "foreign":
        rows[0]["task_id"] = "4" * 32
    if change == "extra":
        rows.append({**row, "task_id": "5" * 32})
    protected = {"active_tasks": rows}
    busy = {"runtime": {"runtime_instance_id": "instance",
                        "runtime_session_id": "session", "runtime_epoch": 3},
            "protected_before": protected, "protected_after": protected,
            "busy": {"blockers": [{"task_id": "1" * 32, "state": "running",
                                      "blocker_kind": "running_task"}]}}
    digest = hashlib.sha256(materializer._canonical(busy)).hexdigest()
    receipt = {"run_id": "6" * 32, "session": "7" * 32, "request_sha256": "8" * 64,
               "runtime_execution_sources": {"agent": {"agent.py": "1" * 64},
                                             "runtime": {"runtime.py": "2" * 64}},
               "export": {"files": [{"path": "observation-busy.json", "sha256": digest}]}}
    settled = {"authenticated": True, "installed_client": client_source,
               "runtime": task["runtime"],
               "task": {"task_id": "1" * 32, "run_id": "2" * 32,
                        "state": "cancelled"}}
    with pytest.raises(materializer.MaterializerError, match="busy census"):
        materializer._hosted_binding(
            receipt, task, busy, settled,
            task_sha="1" * 64, busy_sha=digest, settled_sha="2" * 64,
        )


def test_workflow_has_registered_reusable_route_and_separate_exact_artifacts():
    root = Path(__file__).resolve().parents[1]
    raw = (root / ".github/workflows/r24-linux-live.yml").read_text(encoding="utf-8")
    registered = (root / ".github/workflows/windows-journey.yml").read_text(encoding="utf-8")
    assert "workflow_call:" in raw
    assert "workflow_dispatch:" in raw
    assert "push:" in raw
    assert "venue/M-PIN-R-24-PUSH-ROUTE-R1-E53B11D1" in raw
    assert "ubuntu-24.04" in raw
    assert "scripts/ci_journey_r24_push_admission.py" in raw
    assert "scripts/ci_journey_r24_materializer.py" in raw
    assert "evidence/r24-live/provider-export" in raw
    assert "evidence/r24-live/hosted-binding.json" in raw
    assert "evidence/materializer/installed-identity.json" in raw
    assert "93c21a16dc94269c0c575f1a721e9b01ef1f386b" in raw
    assert "13cb31104c8aae655d0ee04efdb9537aa111e340" in raw
    assert "          - fully-local" in raw
    assert "ref: bfb15570d72c39fbdc32f6209ecec7c929b71d5c" in raw
    assert "ref: 69dda026212ca16c37cc8239d06ccd159603aa24" in raw
    assert "ref: 7955800419fc92b11e553d6f7acb7613090617e6" in raw
    assert "r25-linux-mode-actions" in raw
    assert mode_actions.MODE_PRODUCER_REF in raw
    assert "REQUESTED_MODE" in raw
    assert "runner_ref:" in registered
    assert "producer_ref:" in registered
    assert "confirmation:" in registered
    assert "inputs.confirmation != 'R24_LIVE_PIN_MANIFEST_CANDIDATE'" in registered
    assert "uses: ./.github/workflows/r24-linux-live.yml" in registered


def _push_admission_environment(**changes):
    sha = "a" * 40
    values = {
        "GITHUB_REPOSITORY": push_admission.REPOSITORY,
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_REF": push_admission.VENUE_REF,
        "GITHUB_SHA": sha,
        "GITHUB_WORKFLOW_SHA": sha,
        "GITHUB_WORKFLOW_REF": (
            f"{push_admission.REPOSITORY}/{push_admission.WORKFLOW_PATH}@"
            f"{push_admission.VENUE_REF}"
        ),
    }
    values.update(changes)
    return values


def test_push_admission_accepts_only_the_exact_dedicated_venue_head():
    environment = _push_admission_environment()
    admitted = push_admission.validate_admission(
        environment, runner_ref="a" * 40,
        producer_ref=push_admission.PRODUCER_REF,
        confirmation=push_admission.CONFIRMATION,
    )
    assert admitted == {
        "schema": "astrid.r24.push-route-request.v1",
        "event": "push",
        "ref": push_admission.VENUE_REF,
        "workflow_ref": environment["GITHUB_WORKFLOW_REF"],
        "workflow_sha": "a" * 40,
        "runner_ref": "a" * 40,
        "producer_ref": push_admission.PRODUCER_REF,
        "confirmation": push_admission.CONFIRMATION,
    }


@pytest.mark.parametrize(
    ("environment", "runner_ref", "producer_ref", "confirmation", "message"),
    [
        (_push_admission_environment(GITHUB_EVENT_NAME="schedule"), "a" * 40,
         push_admission.PRODUCER_REF, push_admission.CONFIRMATION, "GitHub event"),
        (_push_admission_environment(GITHUB_REF="refs/heads/main"), "a" * 40,
         push_admission.PRODUCER_REF, push_admission.CONFIRMATION, "GITHUB_REF"),
        (_push_admission_environment(GITHUB_WORKFLOW_SHA="b" * 40), "a" * 40,
         push_admission.PRODUCER_REF, push_admission.CONFIRMATION, "GITHUB_WORKFLOW_SHA"),
        (_push_admission_environment(GITHUB_WORKFLOW_REF="foreign/workflow@refs/heads/main"), "a" * 40,
         push_admission.PRODUCER_REF, push_admission.CONFIRMATION, "GITHUB_WORKFLOW_REF"),
        (_push_admission_environment(), "b" * 40,
         push_admission.PRODUCER_REF, push_admission.CONFIRMATION, "runner_ref/GITHUB_SHA"),
        (_push_admission_environment(), "a" * 40, "b" * 40,
         push_admission.CONFIRMATION, "producer_ref"),
    ],
)
def test_push_admission_rejects_wrong_event_ref_or_identity(
        environment, runner_ref, producer_ref, confirmation, message):
    with pytest.raises(push_admission.AdmissionError, match=message):
        push_admission.validate_admission(
            environment, runner_ref=runner_ref, producer_ref=producer_ref,
            confirmation=confirmation,
        )


def test_dispatch_admission_keeps_existing_exact_head_compatibility():
    environment = _push_admission_environment(
        GITHUB_EVENT_NAME="workflow_dispatch",
        GITHUB_REF="refs/heads/lane/M-PIN/R-24-MATERIALIZER-HOME-FIX-R1",
        GITHUB_WORKFLOW_REF=(
            "banodoco/astrid-agent/.github/workflows/windows-journey.yml@"
            "refs/heads/lane/M-PIN/R-24-MATERIALIZER-HOME-FIX-R1"
        ),
    )
    admitted = push_admission.validate_admission(
        environment, runner_ref="a" * 40,
        producer_ref=push_admission.PRODUCER_REF,
        confirmation=push_admission.CONFIRMATION,
    )
    assert admitted["event"] == "workflow_dispatch"


def test_run_records_timeout_as_exact_first_command_outcome(tmp_path):
    receipt = {"commands": []}
    with pytest.raises(materializer.MaterializerError, match="timed out"):
        materializer._run(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            log=tmp_path / "timeout.log", receipt=receipt, label="bounded-command",
            timeout=1,
        )
    assert receipt["commands"][0]["argv"][0] == sys.executable
    assert receipt["commands"][0]["exit"] is None
    assert receipt["commands"][0]["timed_out"] is True
    assert receipt["commands"][0]["timeout_seconds"] == 1


def test_pinokiod_port_preflight_rejects_existing_listener():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        with pytest.raises(materializer.MaterializerError, match="unavailable"):
            materializer._require_port_free(port)


@pytest.mark.parametrize("value", [
    "http://127.0.0.1:17340@foreign.example",
    "https://127.0.0.1:17340",
    "http://127.0.0.1:17340/path",
])
def test_runtime_endpoint_rejects_nonexact_loopback(value):
    with pytest.raises(materializer.MaterializerError, match="endpoint mismatch"):
        materializer._loopback_endpoint(value)
    with pytest.raises(task_boundary.TaskBoundaryError, match="discovery mismatch"):
        task_boundary._loopback_endpoint(value)
    assert materializer._loopback_endpoint("http://127.0.0.1:17340") == (
        "http://127.0.0.1:17340")


class _RunningProcess:
    returncode = None

    def poll(self):
        return None


class _HealthResponse:
    status = 200

    def __init__(self, value):
        self._payload = json.dumps(value).encode("ascii")

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def read(self):
        return self._payload


def test_wait_runtime_polls_until_discovery_appears_and_verifies_health(
        tmp_path, monkeypatch):
    discovery = tmp_path / "runtime/discovery.json"
    value = {"endpoint": "http://127.0.0.1:17340",
             "runtime_instance_id": "instance-1"}
    clock = [0]
    sleeps = []

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:17340/v1/health"
            assert timeout == 3
            return _HealthResponse({"status": "ok", "runtime_instance_id": "instance-1"})

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        if not discovery.exists():
            discovery.parent.mkdir(parents=True)
            discovery.write_text(json.dumps(value), encoding="ascii")

    monkeypatch.setattr(materializer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(materializer.time, "sleep", sleep)
    monkeypatch.setattr(materializer.urllib.request, "build_opener", lambda *unused: Opener())

    assert materializer._wait_runtime(tmp_path, _RunningProcess(), timeout=5) == value
    assert sleeps == [2]


def test_wait_runtime_times_out_for_persistently_absent_discovery(tmp_path, monkeypatch):
    clock = [0]
    monkeypatch.setattr(materializer.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(materializer.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    with pytest.raises(materializer.MaterializerError, match="readiness timeout"):
        materializer._wait_runtime(tmp_path, _RunningProcess(), timeout=5)
    assert clock[0] == 6


def test_wait_runtime_rejects_discovery_symlink_without_polling(tmp_path, monkeypatch):
    discovery = tmp_path / "runtime/discovery.json"
    discovery.parent.mkdir()
    target = tmp_path / "foreign-discovery.json"
    target.write_text("{}", encoding="ascii")
    discovery.symlink_to(target)
    monkeypatch.setattr(materializer.time, "sleep", lambda unused: pytest.fail("must not poll"))

    with pytest.raises(materializer.MaterializerError, match="unsafe symlink"):
        materializer._wait_runtime(tmp_path, _RunningProcess(), timeout=5)


def test_wait_runtime_rejects_invalid_discovery_identity_without_polling(tmp_path, monkeypatch):
    discovery = tmp_path / "runtime/discovery.json"
    discovery.parent.mkdir()
    discovery.write_text(json.dumps({"endpoint": "https://127.0.0.1:17340",
                                     "runtime_instance_id": "instance-1"}),
                         encoding="ascii")
    monkeypatch.setattr(materializer.time, "sleep", lambda unused: pytest.fail("must not poll"))

    with pytest.raises(materializer.MaterializerError, match="endpoint mismatch"):
        materializer._wait_runtime(tmp_path, _RunningProcess(), timeout=5)


def test_installed_candidate_selection_is_manifest_and_root_bound(tmp_path):
    backend = tmp_path / "backend"
    version_root = backend / "versions/v1"
    data = tmp_path / "data"
    python = version_root / "bin/python"
    profile = version_root / "profile.json"
    python.parent.mkdir(parents=True)
    data.mkdir()
    real_python = python.with_name("python3")
    real_python.write_bytes(b"python")
    python.symlink_to(real_python.name)
    profile.write_text("{}")
    manifest = tmp_path / "candidate.json"
    manifest.write_text('{"candidate": true}')
    (backend / "current").write_text("v1")
    (backend / "install.json").write_text(json.dumps({
        "owner": "haus.astrid.runtime", "managed_by": "pinokio",
        "data_root": str(data.resolve()),
    }))
    (version_root / "manifest.json").write_bytes(manifest.read_bytes())
    (version_root / "version.json").write_text(json.dumps({
        "root": str(version_root.resolve()), "python": str(python),
        "profile": str(profile.resolve()),
        "identity": {"release": 1, "managed_by": "pinokio"}, "env": {},
    }))
    selected, selected_data = materializer._installed(backend, manifest)
    assert selected["python"] == str(python)
    assert python.is_symlink()
    assert selected_data == data.resolve()
    (version_root / "manifest.json").write_text('{"candidate": false}')
    with pytest.raises(materializer.MaterializerError, match="candidate identity"):
        materializer._installed(backend, manifest)


def test_installed_agent_client_source_must_match_checkout(tmp_path):
    version_root = tmp_path / "version"
    observed = version_root / "site/astrid_runtime_client/generated.py"
    expected = tmp_path / "checkout/generated.py"
    observed.parent.mkdir(parents=True)
    expected.parent.mkdir(parents=True)
    observed.write_bytes(b"generated-client")
    expected.write_bytes(b"generated-client")
    source = task_boundary._source_binding(version_root, expected, observed)
    assert source["sha256"] == hashlib.sha256(b"generated-client").hexdigest()
    expected.write_bytes(b"different")
    with pytest.raises(task_boundary.TaskBoundaryError, match="source mismatch"):
        task_boundary._source_binding(version_root, expected, observed)


def test_materializer_records_exact_first_stop_before_sources(tmp_path):
    args = argparse.Namespace(
        run_root=tmp_path / "run", confirmation="wrong",
        runner=tmp_path / "runner", runner_ref="a" * 40,
        producer=tmp_path / "producer", producer_ref=provider.LIVE_PRODUCER_REF,
        app=tmp_path / "app", agent=tmp_path / "agent",
        runtime=tmp_path / "runtime", pinokio=tmp_path / "pinokio",
        pterm=tmp_path / "pterm", pinokiod=tmp_path / "pinokiod",
    )
    assert materializer.execute(args) == 1
    receipt = json.loads((args.run_root / "evidence/materializer/r24-materializer.json").read_bytes())
    assert receipt["status"] == "blocked"
    assert receipt["first_stop"] == "preflight"
    assert receipt["commands"] == []


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
    monkeypatch.setattr(task_boundary, "_client", lambda unused, unused_source: (
        client, {}, tmp_path, {"runtime_instance_id": "instance"}, {}))
    monkeypatch.setattr(task_boundary, "_health", lambda unused_client, unused_discovery: (health, handshake))
    monkeypatch.setattr(task_boundary, "_task_projection", lambda value: vars(value))
    observed = task_boundary.observe_task(tmp_path, tmp_path / "client.py", "a" * 32, 1)
    assert observed["task"]["execution_binding"]["status"] == "claimed"
    task.execution_binding["session_id"] = "foreign"
    with pytest.raises(task_boundary.TaskBoundaryError, match="binding mismatch"):
        task_boundary.observe_task(tmp_path, tmp_path / "client.py", "a" * 32, 1)
    task.execution_binding["session_id"] = "session"
    task.task_id = "f" * 32
    with pytest.raises(task_boundary.TaskBoundaryError, match="binding mismatch"):
        task_boundary.observe_task(tmp_path, tmp_path / "client.py", "a" * 32, 1)


def test_task_observer_rejects_terminal_before_provider(monkeypatch, tmp_path):
    task = SimpleNamespace(state="succeeded", version=2, execution_binding=None)
    client = SimpleNamespace(get_task=lambda unused: task)
    health = SimpleNamespace(runtime_instance_id="instance", runtime_session_id="session", runtime_epoch=7)
    monkeypatch.setattr(task_boundary, "_client", lambda unused, unused_source: (
        client, {}, tmp_path, {"runtime_instance_id": "instance"}, {}))
    monkeypatch.setattr(task_boundary, "_health", lambda unused_client, unused_discovery: (health, None))
    with pytest.raises(task_boundary.TaskBoundaryError, match="terminal before provider"):
        task_boundary.observe_task(tmp_path, tmp_path / "client.py", "a" * 32, 1)


@pytest.mark.parametrize("status,code", [(404, "foreign"), (409, "not_found")])
def test_worker_start_accepts_only_exact_generation_not_found(monkeypatch, tmp_path, status, code):
    from astrid_runtime_client.generated import ApiError

    client = SimpleNamespace(
        get_local_worker_generation=lambda: (_ for _ in ()).throw(ApiError(status, code, "missing")),
        start_local_worker=lambda *unused: pytest.fail("must not start for an inexact error"),
    )
    monkeypatch.setattr(task_boundary, "_client", lambda unused, unused_source: (
        client, {}, tmp_path, {"active_realm": "realm"}, {}))
    monkeypatch.setattr(task_boundary, "_health", lambda unused_client, unused_discovery: (None, None))
    with pytest.raises(ApiError):
        task_boundary.ensure_worker(tmp_path, tmp_path / "client.py")


def test_worker_start_is_observed_with_get_start_get(monkeypatch, tmp_path):
    from astrid_runtime_client.generated import ApiError

    calls = []
    generation = {"state": "active", "profile_id": "astrid", "workspace_uuid": "realm",
                  "executor_incarnation": "incarnation"}

    def get_generation():
        calls.append("get")
        if calls == ["get"]:
            raise ApiError(404, "not_found", "missing")
        return generation

    def start(profile, realm):
        calls.append(("start", profile, realm))
        return {"state": "active"}

    health = SimpleNamespace(runtime_instance_id="instance", runtime_session_id="session", runtime_epoch=7)
    handshake = SimpleNamespace(realm_id="realm")
    client = SimpleNamespace(get_local_worker_generation=get_generation, start_local_worker=start)
    monkeypatch.setattr(task_boundary, "_client", lambda unused, unused_source: (
        client, {}, tmp_path, {"active_realm": "realm"}, {}))
    monkeypatch.setattr(task_boundary, "_health", lambda unused_client, unused_discovery: (health, handshake))
    result = task_boundary.ensure_worker(tmp_path, tmp_path / "client.py")
    assert calls == ["get", ("start", "astrid", "realm"), "get"]
    assert result["started"] is True
    assert result["generation"] == generation
