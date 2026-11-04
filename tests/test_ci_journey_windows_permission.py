"""Replay actual native identities and retain the first failure after selection."""

import base64
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


@pytest.fixture
def native_capture():
    fixture = journey.ROOT / "tests/fixtures/windows-r0-owner-41772014117"
    provenance = json.loads((fixture / "provenance.json").read_text())
    captures = {}
    for name, facts in provenance["files"].items():
        raw = base64.b64decode((fixture / (name + ".b64")).read_bytes())
        assert len(raw) == facts["bytes"]
        assert hashlib.sha256(raw).hexdigest() == facts["sha256"]
        captures[name] = raw
    rows = json.loads(captures["runtime-owner-selection.json"])
    context = json.loads(captures["runtime-owner-selection-context.json"])
    paths = {item["path"]: item for item in context["paths"].values()}

    class CapturedPath(PureWindowsPath):
        def stat(self):
            return SimpleNamespace(st_file_attributes=paths[str(self)]["file_attributes"])

        def is_symlink(self):
            return paths[str(self)]["is_symlink"]

        def is_file(self):
            return paths[str(self)]["is_file"]

        def read_text(self, *, encoding):
            assert encoding == "utf-8"
            return "\n".join(paths[str(self)]["home_lines"])

    native = {int(pid): int(birth) for pid, birth in context["native_creation_filetimes"].items()}
    return SimpleNamespace(version=CapturedPath(context["version_path"]), rows=rows,
                           discovery=context["discovery"], native=native, captures=captures)


def replay_selection(capture, evidence, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "healthy", lambda _: capture.discovery)
    monkeypatch.setattr(journey, "_windows_creation_time", capture.native.__getitem__)

    def census(command, filename, **kwargs):
        assert filename == "runtime-owner-selection.json"
        raw = json.dumps(capture.rows)
        (journey.windows.EVIDENCE / filename).write_text(raw)
        return raw

    monkeypatch.setattr(journey.windows, "powershell", census)
    journey.assert_release0_runtime(evidence, capture.version, evidence)


def test_hosted_native_observations_pass_the_complete_selection_boundary(
        native_capture, tmp_path, monkeypatch):
    c = native_capture
    assert c.native == {6004: 134382394532928411, 7368: 134382394532826626}
    assert c.discovery == {"pid": 6004, "process_birth_id": "windows-creation:134382394532928411"}
    parent, child = [row for row in c.rows if row["ProcessId"] in (7368, 6004)]
    action = ET.fromstring(c.captures["unit-file-before-cleanup.txt"].decode("utf-8")).find(
        ".//{*}Actions/{*}Exec"
    )
    assert parent["ExecutablePath"] == action.find("{*}Command").text
    assert (journey._windows_command_parts(parent["CommandLine"])[1]
            == journey._windows_command_parts(child["CommandLine"])[1]
            == " " + action.find("{*}Arguments").text)
    assert journey._windows_r0_pair(c.rows, c.discovery, c.version, c.native)
    replay_selection(c, tmp_path, monkeypatch)
    context = journey.read_json(tmp_path / "runtime-owner-selection-context.json")
    assert context["native_creation_filetimes"] == {str(k): str(v) for k, v in c.native.items()}


@pytest.mark.parametrize("change", [
    "child-previous-tick", "child-next-tick", "parent-reuse", "missing-child", "missing-parent",
    "stale-census", "extra-owner", "wrong-parent", "wrong-image", "wrong-tail", "query-denied",
])
def test_hosted_capture_rejects_unknown_or_changed_identity(
        native_capture, tmp_path, monkeypatch, change):
    c = native_capture
    child = next(row for row in c.rows if row["ProcessId"] == 6004)
    if change == "child-previous-tick":
        c.native[6004] -= 1
    elif change == "child-next-tick":
        c.native[6004] += 1
    elif change == "parent-reuse":
        c.native[7368] = c.native[6004] + 1
    elif change == "missing-child":
        c.native[6004] = None
    elif change == "missing-parent":
        c.native[7368] = None
    elif change == "stale-census":
        child["Birth"] = str(int(child["Birth"]) - 10)
    elif change == "extra-owner":
        c.rows.append(dict(child, ProcessId=1234))
    elif change == "wrong-parent":
        child["ParentProcessId"] = 1234
    elif change == "wrong-image":
        child["ExecutablePath"] = r"C:\other\pythonw.exe"
    elif change == "wrong-tail":
        child["CommandLine"] += " "
    elif change == "query-denied":
        class Denied(dict):
            def __getitem__(self, pid):
                raise PermissionError(13, "Permission denied")
        c.native = Denied(c.native)
    with pytest.raises(RuntimeError, match=r"Expected exactly the selected Runtime owner 6004"):
        replay_selection(c, tmp_path, monkeypatch)


@pytest.mark.parametrize("failure_stage", [
    "release0-owner-selection", "release0-preseed-checkpoint", "release0-observe",
    "release0-api-seed",
])
def test_first_failure_stage_and_traceback_survive_both_cleanup_receipts(
        native_capture, tmp_path, monkeypatch, capsys, failure_stage):
    """Selection refusal and later EACCES remain distinct, with no continuation."""
    c = native_capture
    root, data = tmp_path / "program", tmp_path / "data"
    evidence = tmp_path / "evidence" / "release0-to-release1"
    candidate_path = tmp_path / "candidate.json"
    release0_path = tmp_path / "release0-manifest.json"
    old_manifest = {"release": 0, "agent": {"sha": "1" * 40}, "runtime": {"sha": "2" * 40}}
    target = {"release": 1, "agent": {"sha": "3" * 40}, "runtime": {"sha": "4" * 40}}
    journey.write_json(release0_path, old_manifest)
    journey.write_json(candidate_path, {"manifest_sha256": "5" * 64, "artifacts": {}})
    args = SimpleNamespace(job_id="direct-windows", mode="direct", leg="release0-to-release1",
                           shell="powershell.exe", source_override=False,
                           candidate=candidate_path, app_checkout=tmp_path)
    sentinel = SimpleNamespace(pid=4396)
    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path, release0_path, {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **k: (tmp_path / "manifest", target))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *a: target)
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "6" * 40})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *a: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *a: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *a: None)
    monkeypatch.setattr(journey, "wait_ready", lambda *a: None)
    monkeypatch.setattr(journey, "installation", lambda *a: (c.version, data, {}))
    monkeypatch.setattr(jumps, "installed_release", lambda _: 0)
    monkeypatch.setattr(journey, "selected_tuple", lambda *a: old_manifest)
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *a: {"manager": "task-scheduler"})
    actual_selection = journey.assert_release0_runtime
    calls = []
    expected_error = PermissionError(13, "Permission denied")

    def denied_checkpoint(*args, **kwargs):
        calls.append("checkpoint")
        if failure_stage == "release0-preseed-checkpoint":
            raise expected_error

    def denied_verify(*args, **kwargs):
        calls.append("verify")
        if failure_stage == "release0-api-seed":
            return
        raise expected_error

    def denied_seed(*args, **kwargs):
        calls.append("seed")
        raise expected_error

    def selection(*args):
        calls.append("selection")
        # Use the actual boundary and actual hosted identities; no permissive
        # stand-in for selection. Inject a same-microsecond reuse for refusal.
        if failure_stage == "release0-owner-selection":
            c.native[6004] += 1
        monkeypatch.setattr(journey, "assert_release0_runtime", actual_selection)
        replay_selection(c, evidence, monkeypatch)

    monkeypatch.setattr(journey, "assert_release0_runtime", selection)
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", denied_checkpoint)
    monkeypatch.setattr(journey, "verify", denied_verify)
    monkeypatch.setattr(jumps, "direct_workspace", denied_seed)
    error_type = RuntimeError if failure_stage == "release0-owner-selection" else PermissionError
    with pytest.raises(error_type) as raised:
        jumps.execute_release0_to_release1(args, root, data, evidence, sentinel, "birth")
    if error_type is PermissionError:
        assert raised.value is expected_error
    result = journey.read_json(evidence / "journey-result.json")
    assert result["status"] == "fail"
    assert result["failure_stage"] == failure_stage
    assert all(value == "not-run" for value in result["checks"].values())
    assert result["release_transition"] is None
    assert "Traceback (most recent call last):" in result["error_traceback"]
    if error_type is PermissionError:
        assert "PermissionError: [Errno 13] Permission denied" in result["error_traceback"]
        assert any(frame in result["error_traceback"] for frame in (
            "denied_checkpoint", "denied_verify", "denied_seed"))
    assert result["error_traceback"] in capsys.readouterr().err
    assert calls == {
        "release0-owner-selection": ["selection"],
        "release0-preseed-checkpoint": ["selection", "checkpoint"],
        "release0-observe": ["selection", "checkpoint", "verify"],
        "release0-api-seed": ["selection", "checkpoint", "verify", "seed"],
    }[failure_stage]
    # Exercise the real snapshot merge and allowlisted copy in both phases.
    # Native manager/process observation is outside this local replay.
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *a: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *a: None)
    for phase in ("before-cleanup", "after-cleanup"):
        journey.snapshot_failure_evidence(evidence, tmp_path, data, phase)
        staged = journey.read_json(tmp_path / "evidence-upload" / phase / "journey-result.json")
        assert phase in staged["failure_diagnostics"]
        for key in ("failure_stage", "error", "error_traceback", "checks", "status", "release_transition"):
            assert staged[key] == result[key]
