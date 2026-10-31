"""Focused composition tests for the Q-owned shared JourneyDriver."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from scripts import journey_release2
from scripts import shared_journey_driver as shared


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_stop_checkout(tmp_path: Path) -> tuple[Path, str, Path]:
    checkout = tmp_path / "pinokio"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    source = checkout / "pinokio" / "stop.js"
    source.parent.mkdir()
    source.write_text(
        "'use strict';\n"
        "const app = require('./lib');\n"
        "module.exports = { run: [app.customStep(app.stopOwnedStart)] };\n",
        encoding="utf-8",
    )
    (source.parent / "lib.js").write_text(
        "'use strict';\n"
        "module.exports = {\n"
        "  customStep(method) { return { method }; },\n"
        "  stopOwnedStart() { throw new Error('must be called by the Pinokio engine'); },\n"
        "};\n",
        encoding="utf-8",
    )
    source.chmod(0o755)
    subprocess.run(
        ["git", "-C", str(checkout), "add", "pinokio/stop.js", "pinokio/lib.js"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Journey Test",
            "-c",
            "user.email=journey@example.invalid",
            "commit",
            "-qm",
            "pin stop action",
        ],
        check=True,
    )
    ref = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    return checkout.resolve(), ref, source.resolve()


_ADOPTION_PROVIDER = r"""
import json
import os
import pathlib
import sys

responses_path, calls_path = map(pathlib.Path, sys.argv[1:])
request = json.loads(sys.stdin.read())
try:
    calls = json.loads(calls_path.read_text())
except FileNotFoundError:
    calls = []
calls.append({
    "stage": request["stage"],
    "credential_inherited": bool(os.environ.get("ASTRID_TEST_SECRET")),
})
calls_path.write_text(json.dumps(calls))
responses = json.loads(responses_path.read_text())
print(json.dumps(responses[request["stage"]]))
"""


def _absent_adoption() -> dict:
    return {
        "status": "absent",
        "pid": None,
        "identity": None,
        "instance_id": None,
        "controller_token_digest": None,
        "start_count": None,
    }


def _running_adoption(
    pid: int,
    *,
    identity: str = "candidate",
    instance_id: str = "one",
    start_count: int = 1,
) -> dict:
    return {
        "status": "running",
        "pid": pid,
        "identity": identity,
        "instance_id": instance_id,
        "controller_token_digest": "a" * 64,
        "start_count": start_count,
    }


def _platform_config(tmp_path: Path) -> tuple[dict, Path]:
    checkout, ref, stop_source = _git_stop_checkout(tmp_path)
    guard = tmp_path / "guard"
    guard.write_text("unchanged", encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    argv = [str(Path(node).resolve()), str(stop_source)]
    control = {
        "argv": argv,
        "cwd": str(tmp_path.resolve()),
        "env": {},
        "expected_exit_code": 23,
        "reason": "busy",
        "stage": "preflight",
    }
    spec = {
        "platform": {"os": "linux", "runner": "local", "hosted": False},
        "identity": "candidate",
        "instance_ids": ["one", "two"],
        "guard_paths": [str(guard.resolve())],
        "stop_controls": {
            "busy": control,
            "unknown": {**control, "reason": "unknown"},
        },
    }
    spec_path = tmp_path / "platform.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    adoption_provider = tmp_path / "adoption-provider"
    adoption_provider.write_text(
        f"#!{Path(sys.executable).resolve()}\n{_ADOPTION_PROVIDER}",
        encoding="utf-8",
    )
    adoption_provider.chmod(0o755)
    adoption_responses = tmp_path / "adoption-responses.json"
    adoption_responses.write_text(
        json.dumps({stage: _absent_adoption() for stage in shared.ADOPTION_STAGES}),
        encoding="utf-8",
    )
    adoption_calls = tmp_path / "adoption-calls.json"
    config = {
        "candidate_checkouts": {
            "pinokio": {
                "path": str(checkout),
                "ref": ref,
                "files": {"pinokio/stop.js": _sha(stop_source)},
            },
        },
        "old_launcher": {
            "checkout": str(checkout),
            "ref": "1" * 40,
            "file": "pinokio/update.js",
            "sha256": "2" * 64,
        },
        "actions": {
            "first": {"steps": [{"kind": "git-shell"}]},
            "retry": {"steps": [{"kind": "custom", "name": "preflightStop"}]},
        },
        "runtime": {"foreign": "replace me"},
        "negative_controls": {"foreign": "replace me"},
        "platform_adapter": {
            "spec": str(spec_path.resolve()),
            "adoption": {
                "command": {
                    "argv": [
                        str(adoption_provider.resolve()),
                        str(adoption_responses.resolve()),
                        str(adoption_calls.resolve()),
                    ],
                    "cwd": str(tmp_path.resolve()),
                    "timeout_seconds": 10,
                },
                "artifacts": {
                    "provider": {
                        "path": str(adoption_provider.resolve()),
                        "sha256": _sha(adoption_provider),
                    }
                },
            },
            "stop_action": {
                "checkout": str(checkout),
                "ref": ref,
                "file": "pinokio/stop.js",
                "sha256": _sha(stop_source),
            },
        },
    }
    return config, spec_path


def test_pinokio_composition_rejects_node_module_load_as_false_stop_execution(tmp_path):
    config, spec_path = _platform_config(tmp_path)
    before = copy.deepcopy(config)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    control = spec["stop_controls"]["busy"]
    direct_load = subprocess.run(
        control["argv"],
        cwd=control["cwd"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert direct_load.returncode == 0
    assert direct_load.stdout == ""
    assert direct_load.stderr == ""
    with pytest.raises(
        shared.JourneyDriverError, match="does not invoke the Pinokio action engine"
    ):
        shared.JourneyDriver.compose_pinokio_backend_config(config)
    assert config == before


def test_pinokio_composition_rejects_foreign_provenance_and_disposable_stop(tmp_path):
    config, spec_path = _platform_config(tmp_path)
    missing_adoption = copy.deepcopy(config)
    del missing_adoption["platform_adapter"]["adoption"]
    with pytest.raises(shared.JourneyDriverError, match="fields must be exactly"):
        shared.JourneyDriver.compose_pinokio_backend_config(missing_adoption)

    arbitrary_path = copy.deepcopy(config)
    arbitrary_path["platform_adapter"]["stop_action"]["file"] = "pinokio/other.js"
    with pytest.raises(shared.JourneyDriverError, match="exact candidate path pinokio/stop.js"):
        shared.JourneyDriver.compose_pinokio_backend_config(arbitrary_path)

    foreign = copy.deepcopy(config)
    foreign["platform_adapter"]["stop_action"]["sha256"] = "f" * 64
    with pytest.raises(shared.JourneyDriverError, match="candidate Pinokio file pins"):
        shared.JourneyDriver.compose_pinokio_backend_config(foreign)

    committed_root = tmp_path / "committed-disposable"
    committed_root.mkdir()
    committed, _ = _platform_config(committed_root)
    checkout = Path(committed["candidate_checkouts"]["pinokio"]["path"])
    source_path = checkout / "pinokio" / "stop.js"
    source_path.write_text(
        "const app = require('./lib');\n"
        "module.exports = { run: [app.customStep(app.stopOwnedStart)] };\n"
        "process.exit(23);\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(checkout), "add", "pinokio/stop.js"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Journey Test",
            "-c",
            "user.email=journey@example.invalid",
            "commit",
            "-qm",
            "replace with disposable stop",
        ],
        check=True,
    )
    committed_ref = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    committed_sha = _sha(source_path)
    committed["candidate_checkouts"]["pinokio"].update(
        ref=committed_ref, files={"pinokio/stop.js": committed_sha}
    )
    committed["platform_adapter"]["stop_action"].update(ref=committed_ref, sha256=committed_sha)
    with pytest.raises(shared.JourneyDriverError, match="obvious disposable"):
        shared.JourneyDriver.compose_pinokio_backend_config(committed)

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    source = config["platform_adapter"]["stop_action"]["checkout"] + "/pinokio/stop.js"
    disposable = [str(Path(sys.executable).resolve()), "-c", "print('stopped')", source]
    spec["stop_controls"]["busy"]["argv"] = disposable
    spec["stop_controls"]["unknown"]["argv"] = disposable
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(shared.JourneyDriverError, match="python -c"):
        shared.JourneyDriver.compose_pinokio_backend_config(config)

    runner = tmp_path / "pinned-action-runner"
    runner.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    runner.chmod(0o755)
    spec["stop_controls"]["busy"]["argv"] = [str(runner.resolve()), "start", source]
    spec["stop_controls"]["unknown"]["argv"] = [str(runner.resolve()), "start", source]
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(shared.JourneyDriverError, match="no locally pinned approved Pinokio"):
        shared.JourneyDriver.compose_pinokio_backend_config(config)


def _set_adoption_responses(config: dict, responses: dict[str, dict]) -> Path:
    argv = config["platform_adapter"]["adoption"]["command"]["argv"]
    response_path = Path(argv[1])
    response_path.write_text(json.dumps(responses), encoding="utf-8")
    return Path(argv[2])


class _FakePinokioBackendExecutor:
    def __init__(self, config, output):
        self.config = copy.deepcopy(config)
        self.output = output
        self.data = {
            "first": {"command": self.config["actions"]["first"]["command"]},
            "retry": {"command": self.config["actions"]["retry"]["command"]},
        }
        self.codes = self.config["test_action_returncodes"]
        self.order = []

    def _run(self, command, *args, **kwargs):
        del args, kwargs
        self.order.append(command["id"])
        return types.SimpleNamespace(returncode=self.codes[command["id"]])

    def execute(self):
        first = self._run(self.data["first"]["command"])
        if first.returncode:
            self._run(self.data["retry"]["command"])
        return {
            "order": self.order,
            "actions": copy.deepcopy(self.config["actions"]),
            "old_launcher": copy.deepcopy(self.config["old_launcher"]),
        }


_FAKE_BACKEND = types.SimpleNamespace(PinokioBackendExecutor=_FakePinokioBackendExecutor)


def _wrapper_config(tmp_path: Path, *, first_code: int, retry_code: int) -> dict:
    config, spec_path = _platform_config(tmp_path)
    config["actions"] = {
        "first": {
            "command": {"id": "first"},
            "steps": [{"kind": "git-shell"}, {"kind": "shell"}],
            "provenance": {"ref": "1" * 40, "sha256": "2" * 64},
        },
        "retry": {
            "command": {"id": "retry"},
            "steps": [
                {"kind": "custom", "bootstrap_action": "preflightStop"},
                {"kind": "git-shell"},
                {"kind": "shell"},
            ],
            "provenance": {"ref": "3" * 40, "sha256": "4" * 64},
        },
    }
    config["test_action_returncodes"] = {
        "first": first_code,
        "retry": retry_code,
    }
    generated = shared.pinokio_platform_adapter.build_executor_commands(spec_path)
    config["runtime"] = generated["runtime"]
    config["negative_controls"] = generated["negative_controls"]
    return config


def test_pinokio_wrapper_adopts_after_successful_first_without_rewriting_actions(
    tmp_path, monkeypatch
):
    config = _wrapper_config(tmp_path, first_code=0, retry_code=0)
    calls_path = _set_adoption_responses(
        config,
        {
            "pre-execution": _absent_adoption(),
            "after-first-action": _running_adoption(101),
        },
    )
    before = copy.deepcopy(config)
    adopted = []
    monkeypatch.setenv("ASTRID_TEST_SECRET", "must-not-cross-adoption-boundary")
    monkeypatch.setattr(
        shared.pinokio_platform_adapter,
        "adopt_process",
        lambda spec, **process: adopted.append((spec, process)) or {"result": "adopted"},
    )

    receipt = shared.JourneyDriver.execute_pinokio_backend(
        config, tmp_path / "output", _FAKE_BACKEND
    )

    assert receipt["order"] == ["first"]
    assert receipt["actions"] == before["actions"]
    assert receipt["old_launcher"] == before["old_launcher"]
    assert config == before
    assert [row["stage"] for row in json.loads(calls_path.read_text())] == [
        "pre-execution",
        "after-first-action",
    ]
    assert not any(row["credential_inherited"] for row in json.loads(calls_path.read_text()))
    assert len(adopted) == 1
    assert adopted[0][0] == json.loads(
        Path(config["platform_adapter"]["spec"]).read_text(encoding="utf-8")
    )
    assert adopted[0][1] == {
        "pid": 101,
        "identity": "candidate",
        "instance_id": "one",
        "controller_token_digest": "a" * 64,
        "start_count": 1,
    }


def test_pinokio_wrapper_adopts_preexisting_and_after_successful_retry_in_order(
    tmp_path, monkeypatch
):
    config = _wrapper_config(tmp_path, first_code=42, retry_code=0)
    calls_path = _set_adoption_responses(
        config,
        {
            "pre-execution": _running_adoption(
                201,
                identity="release-zero",
                instance_id="preexisting",
                start_count=7,
            ),
            "after-retry-action": _running_adoption(202, instance_id="two", start_count=8),
        },
    )
    before = copy.deepcopy(config)
    adopted = []
    monkeypatch.setattr(
        shared.pinokio_platform_adapter,
        "adopt_process",
        lambda spec, **process: adopted.append((spec, process)) or {"result": "adopted"},
    )

    receipt = shared.JourneyDriver.execute_pinokio_backend(
        config, tmp_path / "output", _FAKE_BACKEND
    )

    assert receipt["order"] == ["first", "retry"]
    assert receipt["actions"] == before["actions"]
    assert receipt["old_launcher"] == before["old_launcher"]
    assert config == before
    assert [row["stage"] for row in json.loads(calls_path.read_text())] == [
        "pre-execution",
        "after-retry-action",
    ]
    assert [(row[1]["pid"], row[1]["instance_id"]) for row in adopted] == [
        (201, "preexisting"),
        (202, "two"),
    ]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ({"status": "absent"}, "fields must be exactly"),
        ({**_absent_adoption(), "verdict": "pass"}, "forbidden evidence fields"),
    ],
)
def test_pinokio_wrapper_fails_closed_on_missing_or_foreign_adoption_response(
    tmp_path, response, message
):
    config = _wrapper_config(tmp_path, first_code=0, retry_code=0)
    _set_adoption_responses(config, {"pre-execution": response})

    with pytest.raises(shared.JourneyDriverError, match=message):
        shared.JourneyDriver.execute_pinokio_backend(config, tmp_path / "output", _FAKE_BACKEND)


def test_pinokio_wrapper_requires_running_response_after_successful_action(tmp_path):
    config = _wrapper_config(tmp_path, first_code=0, retry_code=0)
    _set_adoption_responses(
        config,
        {
            "pre-execution": _absent_adoption(),
            "after-first-action": _absent_adoption(),
        },
    )

    with pytest.raises(shared.JourneyDriverError, match="did not find the Runtime"):
        shared.JourneyDriver.execute_pinokio_backend(config, tmp_path / "output", _FAKE_BACKEND)


def test_pinokio_adoption_provider_revalidates_pinned_artifacts_on_every_call(tmp_path):
    config = _wrapper_config(tmp_path, first_code=0, retry_code=0)
    _set_adoption_responses(
        config,
        {
            "pre-execution": _absent_adoption(),
            "after-first-action": _running_adoption(301),
        },
    )
    provider = shared._PinokioAdoptionProvider(config["platform_adapter"]["adoption"])

    assert provider.invoke("pre-execution") is None
    provider_path = Path(config["platform_adapter"]["adoption"]["command"]["argv"][0])
    provider_path.write_text(
        provider_path.read_text(encoding="utf-8") + "\n# changed after first call\n",
        encoding="utf-8",
    )

    with pytest.raises(shared.JourneyDriverError, match="no longer matches its pin"):
        provider.invoke("after-first-action")


def test_m4_5_boundary_preserves_argv_and_exact_twelve_row_matrix(tmp_path):
    adapter = Path(shared.__file__).with_name("installed_browser_adapter.py").resolve()
    rows_before = tuple(copy.deepcopy(journey_release2.RELEASE2_SCENARIOS))
    args = [
        "--evidence-kind",
        "installed-executable",
        "--installed-command",
        "/path/with spaces/installed product",
        "--browser-command",
        "quote' stays one argument",
    ]

    command = shared.JourneyDriver.boundary_command(adapter, args, "hosted-lifecycle")

    assert command == (str(adapter), *args)
    assert command[-1] == "quote' stays one argument"
    assert len(journey_release2.RELEASE2_SCENARIOS) == 12
    assert tuple(journey_release2.RELEASE2_SCENARIOS) == rows_before
    assert len({row["scenario_id"] for row in journey_release2.RELEASE2_SCENARIOS}) == 12

    python_command = shared.JourneyDriver.boundary_command(
        Path(sys.executable).resolve(),
        [str(adapter), "--evidence-kind", "disposable-executable"],
        "disposable-executable",
    )
    assert python_command[1] == str(adapter)
    foreign_python = tmp_path / "python-owned"
    foreign_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    foreign_python.chmod(0o755)
    with pytest.raises(shared.JourneyDriverError, match="exact interpreter"):
        shared.JourneyDriver.boundary_command(
            foreign_python,
            [str(adapter), "--evidence-kind", "disposable-executable"],
            "disposable-executable",
        )
    with pytest.raises(shared.JourneyDriverError, match="outer evidence class"):
        shared.JourneyDriver.boundary_command(
            adapter,
            ["--evidence-kind", "disposable-executable"],
            "hosted-lifecycle",
        )


_PRODUCT_HELPER = r"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys

state_path, definition_path, service_path, principal, mode = sys.argv[1:]
request = json.loads(sys.stdin.read())
try:
    calls = json.loads(pathlib.Path(state_path).read_text())
except FileNotFoundError:
    calls = []
calls.append(request["action"])
if os.environ.get("ASTRID_TEST_SECRET"):
    calls.append("credential-leak")
pathlib.Path(state_path).write_text(json.dumps(calls))
response = {
    "protocol": "astrid-m4-6-product-driver/v1",
    "sequence": request["sequence"],
    "action": request["action"],
    "status": "pass",
}
action = request["action"]
if action == "service-spec":
    response["service_spec"] = {
        "definition_path": definition_path,
        "definition_sha256": __import__("hashlib").sha256(pathlib.Path(definition_path).read_bytes()).hexdigest(),
        "action": [service_path, "--service"],
        "principal": principal,
        "definition_owner": "product-installer",
        "working_directory": str(pathlib.Path(service_path).parent),
    }
    if mode == "promote":
        response["observation"] = {"hosted": True, "verdict": "pass"}
elif action in {"interrupt-controller", "complete-recovery-controller"}:
    pass
elif action == "remove-product":
    response["scenario"] = {
        "name": "keep_data_uninstall",
        "value": {"status": "pass", "action": action},
    }
else:
    response["discovery"] = {"action": action, "process": len(calls)}
    scenario_names = {
        "interruption-recovery-after": "interruption_recovery",
        "failed-candidate-rollback": "failed_candidate_rollback",
        "stop-safety-refused": "stop_safety.refused",
        "stop-safety-unknown": "stop_safety.unknown",
    }
    if action in scenario_names:
        response["scenario"] = {
            "name": scenario_names[action],
            "value": {"status": "pass", "action": action},
        }
print(json.dumps(response))
"""


class RecordingSession:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def observe(self, scenario, phase, discovery):
        self.calls.append(("observe", scenario, phase, discovery["action"]))
        return {"scenario": scenario, "phase": phase, **discovery}

    def before_external_controller_interruption(self, discovery):
        self.calls.append(("before-interruption", discovery["action"]))
        return "interrupt-token"

    def observe_external_controller_interrupted(self, authorization):
        self.calls.append(("interrupted", authorization))

    def terminate_owned_runtime(self, expected, discovery):
        self.calls.append(("terminate-runtime", expected["scenario"], discovery["action"]))

    def before_external_recovery_controller(self, discovery):
        self.calls.append(("before-recovery", discovery["action"]))
        return "recovery-token"

    def observe_external_recovery_controller_complete(self, authorization):
        self.calls.append(("recovery-complete", authorization))

    def before_product_removal(self, expected, discovery):
        self.calls.append(("before-removal", expected["scenario"], discovery["action"]))
        return "removal-token"

    def observe_product_removal(self, authorization):
        self.calls.append(("removed", authorization))
        return {"removed": True}


class PromotionShapedSession(RecordingSession):
    def observe(self, scenario, phase, discovery):
        value = super().observe(scenario, phase, discovery)
        value["observation"] = {"hosted": True, "verdict": "pass"}
        return value


def _native_driver(tmp_path: Path, *, mode: str = "normal"):
    helper = tmp_path / "product-driver"
    helper.write_text(_PRODUCT_HELPER, encoding="utf-8")
    helper.chmod(0o755)
    definition = tmp_path / "astrid.service"
    definition.write_text("exact product service definition\n", encoding="utf-8")
    service = tmp_path / "astrid-runtime"
    service.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    service.chmod(0o755)
    state = tmp_path / "calls.json"
    candidate = {
        "app": "1" * 40,
        "agent": "2" * 40,
        "runtime": "3" * 40,
        "pinokio": "4" * 40,
        "manifest_sha256": "5" * 64,
    }
    context = {
        "slot_id": "direct-linux",
        "contract": {"os": "linux", "mode": "direct"},
        "candidate": candidate,
        "canonical_definition_path": definition,
        "principal": "test-user",
    }
    config = {
        "protocol": shared.PRODUCT_PROTOCOL,
        "candidate": candidate,
        "product_command": {
            "argv": [
                str(helper.resolve()),
                str(state.resolve()),
                str(definition.resolve()),
                str(service.resolve()),
                context["principal"],
                mode,
            ],
            "cwd": str(tmp_path.resolve()),
            "timeout_seconds": 10,
        },
        "artifacts": {
            "product-driver": {"path": str(helper.resolve()), "sha256": _sha(helper)},
            "service-definition": {
                "path": str(definition.resolve()),
                "sha256": _sha(definition),
            },
            "service-executable": {
                "path": str(service.resolve()),
                "sha256": _sha(service),
            },
        },
    }
    config_path = tmp_path / "driver.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return shared.JourneyDriver(config_path.resolve(), context), context, state


def test_native_driver_uses_live_action_sequence_and_native_session(tmp_path):
    driver, context, state = _native_driver(tmp_path)
    spec = driver.service_spec(context)
    session = RecordingSession()

    values = driver.run(context, session)

    assert spec.definition_owner == "product-installer"
    assert set(values) == {
        "interruption_recovery",
        "failed_candidate_rollback",
        "keep_data_uninstall",
        "stop_safety",
    }
    assert set(values["stop_safety"]["cases"]) == {"refused", "unknown"}
    assert json.loads(state.read_text()) == [
        "service-spec",
        "interruption-recovery-before",
        "interruption-controller",
        "interrupt-controller",
        "interruption-recovery-after",
        "recovery-controller",
        "complete-recovery-controller",
        "failed-candidate-rollback",
        "stop-safety-refused-before",
        "stop-safety-refused",
        "stop-safety-unknown-before",
        "stop-safety-unknown",
        "keep-data-uninstall-before",
        "remove-product",
    ]
    assert ("interrupted", "interrupt-token") in session.calls
    assert ("recovery-complete", "recovery-token") in session.calls
    assert ("removed", "removal-token") in session.calls


def test_native_driver_rejects_serialized_observation_promotion(tmp_path):
    driver, context, _ = _native_driver(tmp_path, mode="promote")
    with pytest.raises(shared.JourneyDriverError, match="response fields"):
        driver.service_spec(context)


def test_native_driver_rechecks_pinned_helper_before_every_action(tmp_path):
    driver, context, _ = _native_driver(tmp_path)
    helper = tmp_path / "product-driver"
    helper.write_text(_PRODUCT_HELPER + "\n# changed after pinning\n", encoding="utf-8")
    with pytest.raises(shared.JourneyDriverError, match="pinned artifact"):
        driver.service_spec(context)


def test_native_driver_does_not_inherit_unregistered_environment(tmp_path, monkeypatch):
    driver, context, state = _native_driver(tmp_path)
    monkeypatch.setenv("ASTRID_TEST_SECRET", "must-not-cross-product-boundary")

    driver.service_spec(context)

    assert json.loads(state.read_text()) == ["service-spec"]


def test_native_driver_never_serializes_native_session_observations(tmp_path):
    driver, context, _ = _native_driver(tmp_path)
    driver.service_spec(context)
    values = driver.run(context, PromotionShapedSession())
    assert set(values) == {
        "interruption_recovery",
        "failed_candidate_rollback",
        "keep_data_uninstall",
        "stop_safety",
    }


def test_factory_constructs_a_concrete_driver(tmp_path):
    driver, context, _ = _native_driver(tmp_path)
    config_path = tmp_path / "driver.json"
    factory = shared.journey_driver_factory(config_path)
    constructed = factory(context)
    assert isinstance(driver, shared.JourneyDriver)
    assert isinstance(constructed, shared.JourneyDriver)
