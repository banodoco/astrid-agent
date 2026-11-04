"""Producer conformance for the versioned CLI/diagnostic boundary bundle."""

from __future__ import annotations

import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from astrid.core.gateway import diagnostics, main
from scripts.reshape.ci_doctor import hermetic_env

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = json.loads((ROOT / "schemas/public-boundary/bundle.json").read_text())


def _artifact(name: str):
    return json.loads((ROOT / BUNDLE["files"][name]).read_text())


FIXTURES = _artifact("diagnostics")
CASES = {case["id"]: case for case in FIXTURES["cases"]}
COMMANDS = {command["commandId"]: command for command in _artifact("commandEffects")["commands"]}
VALIDATOR = Draft202012Validator(_artifact("diagnosticSchema"), format_checker=FormatChecker())


def _check_report(report: dict) -> None:
    VALIDATOR.validate(report)
    assert diagnostics.validate_diagnostic(report) == []
    assert report["timing"]["elapsedMs"] <= report["timing"]["deadlineMs"]
    for captured, limit in (("events", "maxEvents"), ("logBytes", "maxLogBytes"), ("bundleBytes", "maxBundleBytes")):
        assert report["captured"][captured] <= report["limits"][limit]
    for action in report["nextActions"]:
        command = COMMANDS[action["commandId"]]
        assert action["effects"] == command["effects"]
        if command["authorizationRequired"]:
            assert action["authorizationRequired"] is True


def _normalized(report: dict) -> dict:
    # Validate real clock values before comparing stable fixture bytes.
    _check_report(report)
    report = copy.deepcopy(report)
    report["collectedAt"] = FIXTURES["normalizedTimestamp"]
    report["timing"]["elapsedMs"] = 0
    for fact in report["facts"].values():
        if fact["observed"]:
            fact["observedAt"] = FIXTURES["normalizedTimestamp"]
    return report


def _runtime_stub(tmp_path: Path, case: dict) -> tuple[dict[str, str], Path]:
    """Use the real RuntimeCLI transport with a read-only fixture process."""
    tmp_path = tmp_path.resolve()
    fixture = tmp_path / "runtime-replies.json"
    calls = tmp_path / "calls.jsonl"
    stub = tmp_path / "runtime.py"
    stub.write_text(
        "import json, pathlib, sys\n"
        "case = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "args = sys.argv[3:]\n"
        "with pathlib.Path(sys.argv[2]).open('a') as out:\n"
        "    out.write(json.dumps(args) + '\\n')\n"
        "assert args[-3:] == ['--data-root', case['supportRoot'], '--json'], args\n"
        "if args[:2] == ['workspace', 'inspect']:\n"
        "    assert len(args) == 5, args\n"
        "    reply = case['inspect']\n"
        "else:\n"
        "    assert len(args) == 4 and args[0] in ('status', 'doctor'), args\n"
        "    reply = case['observe']\n"
        "print(json.dumps(reply))\n"
        "sys.exit(0 if reply.get('ok', True) else 1)\n"
    )
    support = tmp_path / "home/data"
    case = {**case, "supportRoot": str(support)}
    fixture.write_text(json.dumps(case))
    env = hermetic_env(tmp_path / "home")
    Path(env["HOME"]).mkdir(exist_ok=True)
    env["PYTHONPATH"] = str(ROOT)
    env["ASTRID_RUNTIME_CLI"] = shlex.join([sys.executable, str(stub), str(fixture), str(calls)])
    if case.get("constructorError"):
        # A malformed transport setting exercises the actual gateway's
        # fail-closed construction path, before any observation is possible.
        env["ASTRID_RUNTIME_CLI"] = '"'
    return env, calls


def _gateway_environment(monkeypatch, env):
    for name in list(os.environ):
        if name.startswith(("ASTRID_", "BANODOCO_")) and name.endswith(("_DATA_ROOT", "_SOURCE_MANIFEST")):
            monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def _product_observation_inputs(monkeypatch):
    # Fixture the pack/element inventory inputs, rather than making a healthy
    # projection depend on cold repository scans finishing before C2's bound.
    from astrid.core.element import registry
    from astrid.core.pack import loader

    observations = FIXTURES["productObservations"]
    monkeypatch.setattr(loader, "pack_quarantine_section", lambda: copy.deepcopy(observations["packQuarantine"]))
    monkeypatch.setattr(registry, "element_skip_section", lambda: copy.deepcopy(observations["elementSkips"]))


def test_bundle_members_are_published_as_one_version() -> None:
    assert BUNDLE["producer"] == "astrid-agent"
    assert BUNDLE["version"] == "1.0.0"
    manifest = json.loads((ROOT / "artifacts.json").read_text())["artifacts"]
    assert "schemas/public-boundary/bundle.json" in manifest
    assert set(BUNDLE["files"].values()) <= set(manifest)
    Draft202012Validator.check_schema(VALIDATOR.schema)
    assert diagnostics.COMMAND_EFFECTS == {name: command["effects"] for name, command in COMMANDS.items()}


@pytest.mark.parametrize("command", COMMANDS.values(), ids=lambda command: command["commandId"])
def test_every_suggested_command_conforms_to_schema(command):
    report = copy.deepcopy(CASES["status-unknown-local"]["expected"])
    report["nextActions"] = [{**command, "arguments": [], "executable": True}]
    _check_report(report)
    if command["authorizationRequired"]:
        report["nextActions"][0]["authorizationRequired"] = False
        assert list(VALIDATOR.iter_errors(report))
        assert diagnostics.validate_diagnostic(report)


@pytest.mark.parametrize("case", [case for case in FIXTURES["cases"] if case["command"] == "status" or case.get("constructorError")], ids=lambda case: case["id"])
def test_actual_cli_diagnostics_conform_to_bundle(case, tmp_path):
    env, calls = _runtime_stub(tmp_path, case)
    argv = [sys.executable, "-m", "astrid", case["command"], "--diagnostic"]
    if case["mode"] == "shared":
        argv.append("--shared")
    result = subprocess.run(argv, cwd=env["HOME"], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == case["exitCode"], result.stderr or result.stdout
    assert result.stderr == ""
    assert _normalized(json.loads(result.stdout)) == case["expected"]
    expected_calls = [] if case.get("constructorError") else (["workspace", case["command"]] if case["command"] == "status" else ["doctor"])
    observed_calls = [json.loads(line)[0] for line in calls.read_text().splitlines()] if calls.exists() else []
    assert observed_calls == expected_calls


def test_actual_cli_diagnostics_missing_runtime_protocol_stays_in_bundle(tmp_path):
    """A missing Runtime dependency must not escape through exception binding."""
    env = hermetic_env(tmp_path / "home")
    Path(env["HOME"]).mkdir(exist_ok=True)
    env["PYTHONPATH"] = str(ROOT)
    env["ASTRID_RUNTIME_CLI"] = "/definitely/missing/astrid-runtime"
    result = subprocess.run(
        [sys.executable, "-S", "-m", "astrid", "doctor", "--diagnostic", "--shared"],
        cwd=env["HOME"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == CASES["doctor-unknown-shared"]["exitCode"], result.stderr or result.stdout
    assert result.stderr == ""
    assert "UnboundLocalError" not in result.stdout
    assert _normalized(json.loads(result.stdout)) == CASES["doctor-unknown-shared"]["expected"]


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda case: case["id"])
def test_actual_gateway_diagnostics_conform_to_bundle(case, tmp_path, monkeypatch, capsys):
    env, calls = _runtime_stub(tmp_path, case)
    _gateway_environment(monkeypatch, env)
    _product_observation_inputs(monkeypatch)
    argv = [case["command"], "--diagnostic", *(["--shared"] if case["mode"] == "shared" else [])]
    assert main(argv) == case["exitCode"]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert _normalized(json.loads(captured.out)) == case["expected"]
    expected_calls = [] if case.get("constructorError") else (["workspace", case["command"]] if case["command"] == "status" else ["doctor"])
    assert ([json.loads(line)[0] for line in calls.read_text().splitlines()] if calls.exists() else []) == expected_calls


@pytest.mark.parametrize("invalid", FIXTURES["invalid"], ids=lambda invalid: invalid["id"])
def test_diagnostic_rejects_invalid_bundle_fixtures(invalid):
    report = copy.deepcopy(CASES[invalid["base"]]["expected"])
    for edit in invalid.get("edits", [invalid]):
        parent = report
        for key in edit["path"][:-1]:
            parent = parent[key]
        parent[edit["path"][-1]] = edit["value"]
    assert diagnostics.validate_diagnostic(report), invalid["id"]
    with pytest.raises((AssertionError, ValidationError)):
        _check_report(report)


def test_shared_diagnostic_action_uses_redaction_fixture():
    fixture = _artifact("redaction")
    facts = copy.deepcopy(CASES["status-healthy-local"]["expected"]["facts"])
    facts["workspace"]["value"] = fixture["input"]["path"]
    for mode in ("local", "shared"):
        report = diagnostics.build_diagnostic(mode=mode, facts=facts, next_actions=[fixture["action"]])
        _check_report(report)
        assert report["nextActions"] == [fixture["sharedAction" if mode == "shared" else "action"]]
        assert report["facts"]["workspace"]["value"] == ("<redacted>" if mode == "shared" else fixture["input"]["path"])


@pytest.mark.parametrize("command,shared", [("status", False), ("status", True), ("doctor", True)])
def test_gateway_json_redaction_and_observation_effects(command, shared, monkeypatch, capsys, tmp_path):
    fixture = _artifact("redaction")
    case = {"command": command, "inspect": {"ok": True}, "observe": {"ok": True, "compatibility": {"compatible": True}, "details": fixture["input"]}}
    env, calls = _runtime_stub(tmp_path, case)
    # Retain the real parser, gateway and RuntimeCLI JSON decoding, with fixed
    # Runtime/inventory inputs and every permitted Runtime call recorded.
    _gateway_environment(monkeypatch, env)
    _product_observation_inputs(monkeypatch)
    argv = [command, "--json", *(["--shared"] if shared else [])]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    _check_report(payload["diagnostic"])
    observed = payload["runtime"] if command == "status" else payload
    assert observed["details"] == fixture["shared" if shared else "input"]
    assert payload["effects"] == COMMANDS["workspace-status" if command == "status" else "doctor"]["effects"]
    assert payload["authorization_required"] is False
    assert [json.loads(line)[0] for line in calls.read_text().splitlines()] == (["workspace", "status"] if command == "status" else ["doctor"])
