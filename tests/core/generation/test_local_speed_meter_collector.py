"""Collector invariants; synthetic records never qualify the product."""

import copy
import json

import pytest

from tests.local_speed_meter_run import REPORT_PREFIX, parse_reports, require_junit


def rows(instrumented):
    return [
        {
            "schema": "astrid.local-speed-observation.v1",
            "caller": caller,
            "instrumented": instrumented,
            "caller_seconds": 30.0,
            "standin_execution_seconds": [0.01],
            "attached_http_paths": {"POST /prompt": 1},
            "proof": {
                "warmup_task_id": caller + "1",
                "measured_task_id": caller + "2",
                "verified_states": ["succeeded", "succeeded"],
                "outputs": [
                    {"phase": phase, "bytes": 1, "sha256": "a" * 64, "dimensions": [1, 1]}
                    for phase in ("warmup", "delivered")
                ],
            },
            "observer_gaps": [],
            "process_observations": [{"pid": 1}],
            "output_hash_streams": 2,
            "process_start_counts": {
                "pack_worker": 1,
                "cli": 1 if caller == "fresh_python_cli" else 0,
            },
        }
        for caller in ("resident_sdk", "fresh_python_cli")
    ]


def write(tmp_path, value):
    path = tmp_path / "log.txt"
    path.write_text("".join(REPORT_PREFIX + json.dumps(row) + "\n" for row in value))
    return path


@pytest.mark.parametrize("instrumented", [False, True])
def test_complete_pair_has_one_report_per_actual_caller(tmp_path, instrumented):
    assert len(parse_reports(write(tmp_path, rows(instrumented)), instrumented=instrumented)) == 2


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "mode",
        "output",
        "task",
        "submit",
        "coverage",
        "hash",
        "worker",
        "cli",
    ],
)
def test_incomplete_or_falsely_green_report_is_rejected(tmp_path, mutation):
    value = rows(True)
    if mutation == "missing":
        value.pop()
    elif mutation == "duplicate":
        value[1] = copy.deepcopy(value[0])
    elif mutation == "mode":
        value[0]["instrumented"] = False
    elif mutation == "output":
        value[0]["proof"]["outputs"] = []
    elif mutation == "task":
        value[0]["proof"]["verified_states"] = ["queued", "succeeded"]
    elif mutation == "submit":
        value[0]["attached_http_paths"]["POST /prompt"] = 2
    elif mutation == "coverage":
        value[0]["observer_gaps"] = ["missing worker"]
    elif mutation == "hash":
        value[0]["output_hash_streams"] = 0
    elif mutation == "worker":
        value[0]["process_start_counts"]["pack_worker"] = 0
    elif mutation == "cli":
        value[1]["process_start_counts"]["cli"] = 0
    with pytest.raises(ValueError):
        parse_reports(write(tmp_path, value), instrumented=True)


def test_junit_skips_are_not_passes(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text(
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="1"/></testsuites>'
    )
    with pytest.raises(ValueError):
        require_junit(path)


def test_repeated_samples_require_both_callers_at_each_index(tmp_path):
    value = []
    for sample in range(5):
        for row in rows(False):
            row["sample_index"] = sample
            value.append(row)
    assert len(parse_reports(write(tmp_path, value), instrumented=False, samples=5)) == 10
    value[-1]["sample_index"] = 0
    with pytest.raises(ValueError):
        parse_reports(write(tmp_path, value), instrumented=False, samples=5)


def test_repeated_junit_requires_every_sample(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text(
        '<testsuites><testsuite tests="10" failures="0" errors="0" skipped="0"/></testsuites>'
    )
    assert require_junit(path, samples=5)["tests"] == 10
    with pytest.raises(ValueError):
        require_junit(path, samples=6)


def test_each_phase_gets_a_fresh_short_owned_pytest_base(tmp_path, monkeypatch):
    from pathlib import Path

    from tests.local_speed_meter_run import meter_command

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    first, second = meter_command(tmp_path, "latency"), meter_command(tmp_path, "counters")
    bases = [
        Path(next(value.split("=", 1)[1] for value in command if value.startswith("--basetemp=")))
        for command in (first, second)
    ]
    assert bases[0] != bases[1]
    for base in bases:
        assert base.parent == tmp_path
        assert base.name.startswith("p55-") and len(base.name) <= 16
        assert base.is_dir() and list(base.iterdir()) == []
    assert "--junitxml=" + str(tmp_path / "latency.xml") in first
    assert "--junitxml=" + str(tmp_path / "counters.xml") in second


def test_unknown_phase_does_not_create_or_remove_files(tmp_path, monkeypatch):
    from tests.local_speed_meter_run import meter_command

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    before = set(tmp_path.iterdir())
    with pytest.raises(ValueError, match="unknown meter phase"):
        meter_command(tmp_path, "../other")
    assert set(tmp_path.iterdir()) == before
