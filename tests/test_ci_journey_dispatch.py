"""C6: exercise the real config, immutable bindings and existing proof refusal."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from scripts import ci_journey as journey
from scripts import ci_journey_dispatch as dispatch
from tests.test_journey_evidence import gate, release1_receipts

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.safe_load((REPO / ".github/workflows/journey.yml").read_text())


@pytest.fixture
def config(tmp_path):
    step = next(step for step in WORKFLOW["jobs"]["config"]["steps"] if step.get("id") == "config")
    program = textwrap.dedent(step["run"].split("python3 - <<'PY'\n")[1].rsplit("\nPY", 1)[0])
    (tmp_path / "driver").symlink_to(REPO, target_is_directory=True)
    output = tmp_path / "outputs"
    candidate = {name: char * 40 for name, char in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    env = dict(os.environ, CANDIDATE=json.dumps(candidate), DRIVER_AGENT_REF="e" * 40,
               WORKFLOW_REPOSITORY="banodoco/astrid-agent", GITHUB_OUTPUT=str(output),
               WORKFLOW_REF="refs/heads/lane/test/KEEPGOING", ROWS="all", PROOF_MODE="proof",
               AGENT_REF="", RUNTIME_REF="", STOPPED_PATH_AGENT_REF=candidate["agent"],
               OLD_LAUNCHER_REF="bbba620e3d89eeff8021417f365e6ea584203105",
               M4_4A_EXECUTOR_CONFIGS='{"linux":{},"macos":{},"windows":{}}',
               DIRECT_ONLY="false", M4_4A_BACKEND_ONLY="false", M4_5_ONLY="false",
               M4_5_ENABLED="false", M4_5_BOUNDARY="", M4_5_BOUNDARY_ARGS="[]",
               M4_4B_ENABLED="false", M4_6_ENABLED="false", M4_6_CANDIDATE="")

    def run(**overrides):
        output.write_text("")
        result = subprocess.run([sys.executable, "-c", program], cwd=tmp_path,
                                env={**env, **overrides}, capture_output=True, text=True)
        values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        return result, values
    return run


@pytest.mark.parametrize("contract", gate.RELEASE1_MATRIX, ids=lambda row: row["job_id"])
def test_exact_row_selects_one_os_and_channel(config, contract):
    result, values = config(ROWS=contract["job_id"])
    assert result.returncode == 0, result.stderr
    assert json.loads(values["matrix"])["include"] == [{**contract, "leg": contract.get("leg", "fresh-install")}]
    assert values["rows"] == contract["job_id"]
    assert values["proof_mode"] == "proof"


@pytest.mark.parametrize("overrides", [
    {"ROWS": "linux"}, {"ROWS": "direct-linux,direct-windows"}, {"ROWS": ""},
    {"ROWS": "unknown"}, {"ROWS": "direct-linux", "M4_4A_BACKEND_ONLY": "true"},
    {"ROWS": "pinokio-backend-linux", "DIRECT_ONLY": "true"},
    {"ROWS": "direct-linux", "M4_4B_ENABLED": "true"},
    {"ROWS": "direct-linux", "PROOF_MODE": "unknown"},
    {"ROWS": "direct-linux", "AGENT_REF": "f" * 40},
])
def test_config_rejects_invalid_or_conflicting_selection(config, overrides):
    result, values = config(**overrides)
    assert result.returncode != 0
    assert "matrix" not in values


def test_default_preserves_nine_rows_and_legacy_channel_filters(config):
    result, values = config()
    assert result.returncode == 0, result.stderr
    assert len(json.loads(values["matrix"])["include"]) == 9
    result, values = config(DIRECT_ONLY="true")
    assert result.returncode == 0, result.stderr
    assert len(json.loads(values["matrix"])["include"]) == 3


def test_diag_binds_requested_refs_and_requires_isolated_branch(config):
    inputs = {"ROWS": "direct-linux", "PROOF_MODE": "diag",
              "CANDIDATE": json.dumps({"app": "a" * 40, "pinokio": "d" * 40}),
              "AGENT_REF": "1" * 40, "RUNTIME_REF": "2" * 40}
    result, values = config(**inputs)
    assert result.returncode == 0, result.stderr
    candidate = json.loads(values["candidate"])
    assert candidate["agent"] == values["stopped_path_agent_ref"] == inputs["AGENT_REF"]
    assert candidate["runtime"] == inputs["RUNTIME_REF"]
    assert candidate["proof_mode"] == "diag"
    for overrides in ({"WORKFLOW_REF": "refs/heads/int/r1"}, {"WORKFLOW_REF": "refs/tags/LOOKAHEAD"},
                      {"ROWS": "all"}, {"AGENT_REF": "int/r1"}, {"RUNTIME_REF": ""},
                      {"PROOF_MODE": "proof"}):
        result, values = config(**{**inputs, **overrides})
        assert result.returncode != 0
        assert "matrix" not in values


def git_repo(path, files):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(data)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.name=C6 test", "-c",
                    "user.email=c6@example.invalid", "commit", "-qm", "fixture"], check=True)
    return path


@pytest.fixture
def sources(tmp_path):
    manifest = {"schema_version": 1, "release": 1, "agent": {"sha": "a" * 40},
                "runtime": {"sha": "b" * 40, "schema_digest": "stale"}, "tools": {"python": "kept"}}
    app = git_repo(tmp_path / "app", {"public/install-manifest.json": json.dumps(manifest),
                                     **{name: name for name in dispatch.ARTIFACTS}})
    agent = git_repo(tmp_path / "agent", {"runtime.lock.json": '{"source_commit":"old"}'})
    runtime = git_repo(tmp_path / "runtime", {
        "contract/manifest.json": '{"openapi":"openapi.json","schemas":["schemas/a.json"]}',
        "contract/openapi.json": '{"openapi":"3.1.0"}', "contract/schemas/a.json": '{"type":"object"}'})
    candidate = {name: dispatch.checkout_sha(path) for name, path in (
        ("app", app), ("agent", agent), ("runtime", runtime))}
    candidate.update(pinokio="d" * 40, proof_mode="diag")
    return candidate, app, agent, runtime


def prepare(sources, **kwargs):
    candidate, app, agent, runtime = sources
    return dispatch.prepare_diagnostic(candidate, app, agent, runtime,
                                       proof_mode=kwargs.get("proof_mode", "diag"),
                                       workflow_ref=kwargs.get("workflow_ref", "refs/heads/test/KEEPGOING"))


def test_manifest_build_binds_exact_refs_digest_bootstraps_and_preserves_inputs(sources):
    candidate, app, agent, runtime = sources
    original = (app / "public/install-manifest.json").read_bytes()
    original_lock = (agent / "runtime.lock.json").read_bytes()
    bound, receipt = prepare(sources)
    manifest_path = app / "public/install-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    assert gate.validate_tuple(bound, app, manifest_path) == manifest
    assert receipt["requested_refs"] == {key: candidate[key] for key in ("app", "agent", "runtime", "pinokio")}
    assert manifest["runtime"]["schema_digest"] == dispatch.runtime_schema_digest(runtime)
    assert manifest["runtime"]["schema_digest"] != "stale"
    assert manifest["tools"] == json.loads(original)["tools"]
    assert manifest["release"] == 1 and manifest["proof_mode"] == "diag"
    assert receipt["qualification"]["eligible"] is False
    assert receipt["original_manifest_sha256"] == hashlib.sha256(original).hexdigest()
    assert bound["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert (agent / "runtime.lock.json").read_bytes() == original_lock
    for name in dispatch.ARTIFACTS:
        assert (app / name).read_text() == name
        assert bound["artifacts"][name] == hashlib.sha256(name.encode()).hexdigest()


@pytest.mark.parametrize("mutation", ["app", "agent", "runtime", "proof", "missing-mode", "branch", "contract-escape"])
def test_manifest_build_refuses_wrong_or_implicit_inputs_before_writing(sources, mutation):
    candidate, app, agent, runtime = sources
    original = (app / "public/install-manifest.json").read_bytes()
    options = {}
    if mutation in {"app", "agent", "runtime"}:
        candidate[mutation] = "f" * 40
    elif mutation == "missing-mode":
        del candidate["proof_mode"]
    elif mutation == "proof":
        options["proof_mode"] = "proof"
    elif mutation == "branch":
        options["workflow_ref"] = "refs/heads/ordinary"
    else:
        (runtime / "contract/manifest.json").write_text('{"openapi":"../outside","schemas":[]}')
    with pytest.raises(ValueError):
        prepare(sources, **options)
    assert (app / "public/install-manifest.json").read_bytes() == original


@pytest.mark.parametrize("outcome", ["pass", "failure"])
def test_execution_labels_success_and_early_failure_receipts_and_staged_copies(sources, tmp_path, monkeypatch, outcome):
    candidate, app, agent, runtime = sources
    bound, _ = prepare(sources)
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(bound))
    args = argparse.Namespace(proof_mode="diag", candidate=path, app_checkout=app, job_id="direct-linux")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/test/KEEPGOING")
    evidence = tmp_path / "run/evidence/release0-to-release1"

    def lifecycle(_args):
        journey.write_json(evidence / "journey-result.json", {"status": "pass" if outcome == "pass" else "fail"})
        for phase in ("before-cleanup", "after-cleanup"):
            journey.retain_named_evidence(evidence, phase)
        if outcome == "failure":
            raise RuntimeError("native prerequisite failed")
        return {"status": "pass"}
    monkeypatch.setattr(journey, "_execute", lifecycle)
    if outcome == "failure":
        with pytest.raises(RuntimeError, match="native prerequisite failed"):
            journey.execute(args)

    else:
        assert journey.execute(args)["diagnostic_status"] == "pass"
    receipts = list((tmp_path / "run").rglob("journey-result.json"))
    assert len(receipts) == 3
    for receipt in receipts:
        value = json.loads(receipt.read_text())
        assert value["proof_mode"] == "diag" and value["status"] == "fail"
        assert value["diagnostic_status"] == ("pass" if outcome == "pass" else "fail")
        assert value["qualification"]["eligible"] is False
        assert value["diagnostic_manifest"] == bound["diagnostic_manifest"]
    assert journey.RECEIPT_MODE.get() is None
    for mode in (None, "proof"):
        args.proof_mode = mode
        with pytest.raises(ValueError, match="explicit --proof-mode diag"):
            journey.execute(args)


def test_actual_runner_refusal_is_labelled_before_snapshot(sources, tmp_path, monkeypatch):
    candidate, app, agent, runtime = sources
    bound, _ = prepare(sources)
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(bound))
    run_root = tmp_path / "run"
    args = argparse.Namespace(proof_mode="diag", candidate=path, app_checkout=app, job_id="direct-linux",
                              mode="direct", leg="release0-to-release1", run_root=run_root,
                              root=run_root / "program", data_root=run_root / "data", shell="sh")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/test/KEEPGOING")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))

    def refuse(_identity):
        raise RuntimeError("runner provenance unavailable")
    monkeypatch.setattr(journey, "assert_runner_identity", refuse)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *args: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *args: None)
    with pytest.raises(RuntimeError, match="runner provenance unavailable"):
        journey.execute(args)
    for receipt in run_root.rglob("journey-result.json"):
        value = json.loads(receipt.read_text())
        assert value["proof_mode"] == "diag" and value["status"] == "fail"
        assert value["qualification"]["eligible"] is False
    assert (run_root / "evidence-upload/before-cleanup/journey-result.json").is_file()


def test_raw_adapter_receipt_is_labelled_before_export(tmp_path):
    evidence = tmp_path / "run/evidence/leg"
    evidence.mkdir(parents=True)
    (evidence / "journey-result.json").write_text('{"status":"pass"}')
    (evidence / "verify-install.json").write_text('{"passed":true}')
    token = journey.RECEIPT_MODE.set({"proof_mode": "diag"})
    try:
        journey.retain_named_evidence(evidence, "before-cleanup")
    finally:
        journey.RECEIPT_MODE.reset(token)
    exported = json.loads((tmp_path / "run/evidence-upload/before-cleanup/journey-result.json").read_text())
    assert exported["proof_mode"] == "diag" and exported["status"] == "fail"
    assert exported["diagnostic_status"] == "pass"
    verifier = json.loads((tmp_path / "run/evidence-upload/before-cleanup/verify-install.json").read_text())
    assert verifier["proof_mode"] == "diag" and verifier["qualification"]["eligible"] is False
    assert verifier["passed"] is True
    assert (evidence / "verify-install.json").read_text() == '{"passed":true}'


def test_explicit_proof_is_labelled_and_does_not_inherit_a_diagnostic_context(tmp_path, monkeypatch):
    args = argparse.Namespace(proof_mode="proof", source_override=False)
    monkeypatch.setattr(journey, "_execute", lambda _args: {"status": "pass"})
    assert journey.execute(args) == {"status": "pass", "proof_mode": "proof"}
    args.proof_mode = None
    assert journey.execute(args) == {"status": "pass"}  # Legacy absence is never relabelled as proof.


def test_existing_aggregate_rejects_diagnostics_even_with_all_nine_successful_lifecycles(tmp_path):
    candidate = release1_receipts(tmp_path)
    assert gate.evaluate_release1_matrix(candidate, tmp_path) == []
    token = journey.RECEIPT_MODE.set({"proof_mode": "diag"})
    try:
        for receipt in tmp_path.rglob("journey-result.json"):
            journey.write_json(receipt, json.loads(receipt.read_text()))
    finally:
        journey.RECEIPT_MODE.reset(token)
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert sum("status 'fail' is not pass" in error for error in errors) == 9
    for receipt in tmp_path.rglob("journey-result.json"):
        assert json.loads(receipt.read_text())["diagnostic_status"] == "pass"


def test_single_proof_row_leaves_eight_missing_rows_red(tmp_path):
    candidate = release1_receipts(tmp_path)
    for receipt in tmp_path.rglob("journey-result.json"):
        if receipt.parent.name != "direct-linux":
            receipt.unlink()
    errors = gate.evaluate_release1_matrix(candidate, tmp_path)
    assert len(errors) == 8 and all("missing" in error for error in errors)


def test_workflow_gate_refuses_partial_and_diagnostic_success():
    guard = WORKFLOW["jobs"]["gate"]["steps"][0]["run"]
    env = dict(os.environ, CONFIG_RESULT="success", JOURNEY_RESULT="success", M4_5_ONLY="false",
               M4_5_ENABLED="false", M4_4B_ENABLED="false", M4_6_ENABLED="false")
    for mode, rows, expected in (("proof", "all", 0), ("proof", "direct-linux", 1), ("diag", "direct-linux", 1)):
        result = subprocess.run(["bash", "-eu", "-c", guard], env={**env, "PROOF_MODE": mode, "ROWS": rows})
        assert result.returncode == expected
