import os
import subprocess
from pathlib import Path

import pytest
import yaml

WF = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def load(name):
    data = yaml.safe_load((WF / name).read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # PyYAML reads the key `on` as True
    return data


def test_pull_requests_into_release_mains_run_only_the_cheap_ci_subset():
    wf = load("ci.yml")
    assert wf["on"]["pull_request"] == {"branches": ["release/*/main"]}
    heavy = {"windows-native-and-installer", "darwin-native", "release"}
    for name, job in wf["jobs"].items():
        if name == "landing-reference":
            continue
        if name in heavy:
            assert job["if"] == "github.event_name != 'pull_request'", name
        elif name == "unit":
            assert job["if"] == "always()", name
        else:
            assert "if" not in job, name
    group = wf["jobs"]["unit-shard"]["strategy"]["matrix"]["group"]
    assert "'[1]'" in group and "pull_request" in group
    assert "github.event.pull_request.number" in wf["concurrency"]["group"]


@pytest.mark.parametrize("checks", ["success", "failure", "cancelled", "skipped"])
@pytest.mark.parametrize("shards", ["success", "failure", "cancelled", "skipped"])
def test_unit_aggregate_rejects_any_unsuccessful_dependency(checks, shards):
    job = load("ci.yml")["jobs"]["unit"]
    assert job["needs"] == ["checks", "unit-shard"]
    assert job["env"] == {
        "CHECKS_RESULT": "${{ needs.checks.result }}",
        "SHARDS_RESULT": "${{ needs.unit-shard.result }}",
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", job["steps"][0]["run"]],
        env={**os.environ, "CHECKS_RESULT": checks, "SHARDS_RESULT": shards},
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if checks == shards == "success" else 1), result.stdout + result.stderr


def test_landing_reference_job_runs_only_for_pull_requests_into_release_branches():
    job = load("ci.yml")["jobs"]["landing-reference"]
    assert job["if"] == (
        "${{ github.event_name == 'pull_request' && startsWith(github.base_ref, 'release/') "
        "&& endsWith(github.base_ref, '/main') }}"
    )
    assert job["runs-on"] == "ubuntu-latest"


def test_chat_pull_request_runs_only_the_ubuntu_job():
    wf = load("chat.yml")
    assert wf["on"]["pull_request"] == {"branches": ["release/*/main"]}
    assert wf["jobs"]["windows-launchers"]["if"] == "github.event_name != 'pull_request'"
    assert "if" not in wf["jobs"]["chat"]


def test_journey_branch_is_a_parameter_and_int_r1_stays_the_default():
    text = (WF / "journey.yml").read_text(encoding="utf-8")
    wf = load("journey.yml")
    dispatch = wf["on"]["workflow_dispatch"]["inputs"]
    assert dispatch["branch"]["default"] == "int/r1"
    assert dispatch["candidate"]["required"] is False
    assert wf["on"]["push"]["branches"] == ["int/r1"]
    steps = wf["jobs"]["config"]["steps"]
    checkout = next(s for s in steps if s.get("with", {}).get("path") == "automatic-app")
    assert checkout["with"]["ref"] == "${{ inputs.branch || (github.event_name == 'push' && github.ref_name) || 'int/r1' }}"
    assert 'refs/heads/int/r1' not in text
    assert 'ref: int/r1' not in text
