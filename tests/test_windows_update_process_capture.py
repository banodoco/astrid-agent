import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from scripts import ci_journey
from scripts import windows_update_process_capture as capture


STAMP = "2026-11-05T05:00:00.0000000Z"
LATER = "2026-11-05T05:00:01.0000000Z"


def row(pid, parent, created=STAMP, **extra):
    return {"pid": pid, "ppid": parent, "created": created, **extra}


def test_census_projects_fields_and_bounds_output(monkeypatch):
    rows = [row(10, 1, command="secret", path="private"), row(11, 10, LATER)] + [row(12, 10)] * 4095
    value = {"processes": rows, "owners": [11, 11, 99] + [99] * 63, "port_status": "observed", "secret": "private"}
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))
    monkeypatch.setattr(capture.subprocess, "run", run)
    observed = capture.observe()
    assert observed["status"] == "observed" and observed["process_limit_reached"]
    assert observed["door_limit_reached"] and observed["door_owners"] == [11, 99]
    assert len(observed["processes"]) == 4096
    assert "private" not in json.dumps(observed) and "secret" not in json.dumps(observed)
    script = calls[0][0][-1]
    assert "CommandLine" not in script and "ExecutablePath" not in script
    assert "-LocalPort 17340 -State Listen" in script and calls[0][1]["timeout"] == 15


def test_failure_census_tracks_birth_and_surviving_descendants_and_stages(tmp_path):
    evidence = tmp_path / "run" / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)
    before = {"status": "observed", "processes": [row(10, 1), row(11, 10, LATER), row(12, 11, LATER), row(20, 1)],
              "door_status": "observed", "door_owners": [20], "process_limit_reached": False, "door_limit_reached": False}
    after = {**before, "processes": [row(10, 1, LATER), row(11, 10, LATER), row(12, 11, LATER), row(13, 12, LATER), row(20, 1)], "door_owners": [11]}
    capture.capture_failure(evidence, 10, before, after)
    ci_journey.retain_named_evidence(evidence, "before-cleanup")
    staged = tmp_path / "run" / "evidence-upload" / "before-cleanup" / capture.NAME
    record = json.loads(staged.read_text())
    assert [x["pid"] for x in record["baseline_tree"]] == [10, 11, 12]
    assert [x["pid"] for x in record["surviving_baseline"]] == [11, 12]
    assert [x["pid"] for x in record["failure_tree"]] == [11, 12, 13]
    assert record["after"]["door_owners"] == [11]
    assert "not a dedicated pack-host port" in record["limitation"]
    assert capture.NAME in (Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text()


def test_unavailable_and_invalid_census_does_not_expose_error(monkeypatch):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired("private command", 15, output="secret")
    monkeypatch.setattr(capture.subprocess, "run", timed_out)
    observed = capture.observe()
    assert observed["status"] == "unavailable" and observed["door_status"] == "unavailable"
    assert "private" not in json.dumps(observed) and "secret" not in json.dumps(observed)
    value = {"processes": [row(1, 0, "private"), row(True, 0), row(2, 0)], "owners": ["private", True], "port_status": "observed"}
    monkeypatch.setattr(capture.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(value)))
    observed = capture.observe()
    assert observed["processes"] == [row(2, 0)] and observed["door_owners"] == []


def test_marker_is_only_a_bounded_pid_hint(tmp_path):
    marker = tmp_path / "runtime" / "generic-host.ready.json"
    marker.parent.mkdir()
    marker.write_text(json.dumps({"pid": 23, "path": "private", "token": "secret"}))
    assert capture.host_pid(tmp_path) == 23
    marker.write_text('{"pid": true}')
    assert capture.host_pid(tmp_path) is None
    marker.write_text(' ' * 8193 + '{"pid": 23}')
    assert capture.host_pid(tmp_path) is None
    assert capture.host_pid(tmp_path / "missing") is None
