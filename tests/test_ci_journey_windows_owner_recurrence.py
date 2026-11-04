"""Captured R0 selection passes; fresh workspace assertions must use that identity rule."""

import base64
import copy
import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import PureWindowsPath
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


@pytest.fixture
def recurrence():
    fixture = journey.ROOT / "tests/fixtures/windows-r0-owner-41775575452"
    provenance = journey.read_json(fixture / "provenance.json")
    captures = {}
    for name, facts in provenance["files"].items():
        raw = base64.b64decode((fixture / (name + ".b64")).read_bytes())
        assert len(raw) == facts["bytes"]
        assert hashlib.sha256(raw).hexdigest() == facts["sha256"]
        captures[name] = raw
    rows = json.loads(captures["runtime-owner-selection.json"])
    context = json.loads(captures["runtime-owner-selection-context.json"])
    paths = {str(PureWindowsPath(item["path"])): item for item in context["paths"].values()}

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

    return SimpleNamespace(
        rows=rows, context=context, captures=captures,
        version_path=CapturedPath(context["version_path"]), discovery=context["discovery"],
        native={int(pid): int(birth) for pid, birth in context["native_creation_filetimes"].items()},
    )


def test_captured_pair_already_passes_173_without_any_identity_change(recurrence):
    r = recurrence
    owners = [row for row in r.rows if journey._runtime_owner_command(str(row.get("CommandLine") or ""))]
    parent, child = owners
    assert [row["ProcessId"] for row in owners] == [4284, 8384]
    assert child["ParentProcessId"] == 4284
    assert r.discovery == {"pid": 8384, "process_birth_id": "windows-creation:134382415397990742"}
    assert 0 < r.native[4284] < r.native[8384]
    assert r.native[4284] - int(parent["Birth"]) == 6
    assert r.native[8384] - int(child["Birth"]) == 2
    action = ET.fromstring(r.captures["unit-file-before-cleanup.txt"].decode("utf-8")).find(
        ".//{*}Actions/{*}Exec"
    )
    assert parent["ExecutablePath"] == action.find("{*}Command").text
    assert journey._windows_command_parts(parent["CommandLine"])[1] == " " + action.find("{*}Arguments").text
    assert journey._windows_r0_pair(r.rows, r.discovery, r.version_path, r.native)


@pytest.fixture
def workspace_rig(recurrence, tmp_path, monkeypatch):
    r = recurrence
    # Every process, manager and native operation is stubbed. These tests never
    # query or signal host processes; only the actual assertions run.
    rig = SimpleNamespace(
        capture=r, root=tmp_path / "program", data=tmp_path / "data",
        evidence=tmp_path / "evidence/release0-to-release1/release0-api-seed",
        version={"root": str(r.version_path.parent)}, events=[], native_calls=[], live=True,
        after_restart=None, release=0,
    )
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "installation", lambda *_: (r.version_path, rig.data, rig.version))
    monkeypatch.setattr(jumps, "installed_release", lambda _: rig.release)
    monkeypatch.setattr(journey, "healthy", lambda _: r.discovery)
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_: {"manager": "task-scheduler"})
    monkeypatch.setattr(journey, "workspace_root", lambda _: tmp_path / "realm")
    monkeypatch.setattr(journey, "wait_ready", lambda _: None)
    monkeypatch.setattr(jumps, "workspace", lambda *_args, **_kwargs: rig.events.append("workspace") or {"ok": True})
    monkeypatch.setattr(journey, "runtime_processes", lambda _: [4284, 8384] if rig.live else [])

    def native(pid):
        rig.native_calls.append(pid)
        return r.native.get(pid)

    def powershell(command, filename, **kwargs):
        if filename == "runtime-owner-selection.json":
            assert "Get-CimInstance Win32_Process" in command
            raw = json.dumps(r.rows)
            journey.write_json(journey.windows.EVIDENCE / filename, r.rows)
            return raw
        if filename.endswith("-stop-service.log"):
            assert "Stop-ScheduledTask" in command
            rig.events.append("stop")
            rig.live = False
        elif filename.endswith("-start-service.log"):
            assert "Start-ScheduledTask" in command
            rig.events.append("start")
            rig.live = True
            if rig.after_restart:
                rig.after_restart()
        else:
            pytest.fail(f"unexpected external command: {filename}")
        return ""

    monkeypatch.setattr(journey, "_windows_creation_time", native)
    monkeypatch.setattr(journey.windows, "powershell", powershell)
    return rig


def call_workspace(rig, action="seed", *, scoped=True):
    kwargs = {"release0_version_path": rig.capture.version_path} if scoped else {}
    return jumps.direct_workspace(rig.root, rig.version, rig.data, rig.evidence, action, {}, **kwargs)


def test_unscoped_workspace_reproduces_the_recorded_failure_before_stop(workspace_rig):
    rig = workspace_rig
    with pytest.raises(RuntimeError, match=r"owner 8384, found \[4284, 8384\]"):
        call_workspace(rig, scoped=False)
    assert rig.events == []
    assert rig.native_calls == []


@pytest.mark.parametrize("action", ["seed", "snapshot"])
def test_scoped_r0_workspace_rechecks_fresh_identities_before_and_after(workspace_rig, action):
    rig = workspace_rig
    assert call_workspace(rig, action) == {"ok": True}
    assert rig.events == ["stop", "workspace", "start"]
    assert rig.native_calls == [4284, 8384, 4284, 8384]
    for phase in ("before", "after"):
        directory = rig.evidence / "runtime-owner-workspace" / f"{action}-{phase}"
        context = journey.read_json(directory / "runtime-owner-selection-context.json")
        assert context["native_creation_filetimes"] == rig.capture.context["native_creation_filetimes"]
        assert context["discovery"] == rig.capture.discovery
        assert json.loads((directory / "runtime-owner-selection.json").read_text()) == rig.capture.rows


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("change", [
    "child_reuse_one_tick", "parent_reuse", "missing_native", "extra_owner", "independent_owner",
    "wrong_image", "changed_tail", "symlink", "reparse", "stale_census", "changed_selected_version",
    "changed_selected_data", "release1",
])
def test_workspace_identity_or_selection_changes_fail_closed(workspace_rig, monkeypatch, phase, change):
    rig = workspace_rig
    r = rig.capture

    def mutate():
        child = next(row for row in r.rows if row["ProcessId"] == 8384)
        if change == "child_reuse_one_tick":
            r.native[8384] += 1
        elif change == "parent_reuse":
            r.native[4284] = r.native[8384] + 1
        elif change == "missing_native":
            r.native[8384] = None
        elif change == "extra_owner":
            r.rows.append(dict(child, ProcessId=9000))
        elif change == "independent_owner":
            child["ParentProcessId"] = 9000
        elif change == "wrong_image":
            child["ExecutablePath"] = r"C:\other\pythonw.exe"
        elif change == "changed_tail":
            child["CommandLine"] += " "
        elif change == "symlink":
            r.context["paths"]["venv"]["is_symlink"] = True
        elif change == "reparse":
            r.context["paths"]["version_dir"]["file_attributes"] |= 0x400
        elif change == "stale_census":
            child["Birth"] = str(int(child["Birth"]) - 10)
        elif change == "changed_selected_version":
            monkeypatch.setattr(journey, "installation", lambda *_: (r.version_path, rig.data, {"root": "other"}))
        elif change == "changed_selected_data":
            monkeypatch.setattr(journey, "installation", lambda *_: (r.version_path, rig.data / "other", rig.version))
        elif change == "release1":
            rig.release = 1

    if phase == "before":
        mutate()
    else:
        rig.after_restart = mutate
    with pytest.raises(RuntimeError, match="owner|selected release-0 version"):
        call_workspace(rig)
    assert rig.events == ([] if phase == "before" else ["stop", "workspace", "start"])


def test_restart_replacement_pair_is_accepted_only_with_its_new_exact_discovery(workspace_rig):
    rig = workspace_rig
    r = rig.capture

    def replacement():
        for row in r.rows:
            if row["ProcessId"] in (4284, 8384):
                old_pid = row["ProcessId"]
                row["ProcessId"] += 1000
                row["Birth"] = str(int(row["Birth"]) + 1000000)
                r.native[row["ProcessId"]] = r.native.pop(old_pid) + 1000000
                if old_pid == 8384:
                    row["ParentProcessId"] += 1000
        r.discovery.update(pid=9384, process_birth_id=f"windows-creation:{r.native[9384]}")

    rig.after_restart = replacement
    assert call_workspace(rig) == {"ok": True}
    assert rig.native_calls == [4284, 8384, 5284, 9384]


def test_workspace_captures_survive_cleanup_without_overwriting_initial_selection(workspace_rig, monkeypatch):
    rig = workspace_rig
    evidence = rig.evidence.parent
    initial = copy.deepcopy(rig.capture.context)
    journey.write_json(evidence / "runtime-owner-selection-context.json", initial)
    call_workspace(rig, "seed")
    call_workspace(rig, "snapshot")
    monkeypatch.setattr(journey, "capture_manager_state", lambda *_: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *_: None)
    for phase in ("before-cleanup", "after-cleanup"):
        journey.snapshot_failure_evidence(evidence, evidence.parent.parent, rig.data, phase)
        staged = evidence.parent.parent / "evidence-upload" / phase
        assert journey.read_json(staged / "runtime-owner-selection-context.json") == initial
        records = journey.read_json(staged / "runtime-owner-workspace.json")
        assert set(records) == {"seed-before", "seed-after", "snapshot-before", "snapshot-after"}
        for observation in records.values():
            assert observation["census"] == rig.capture.rows
            assert observation["context"]["discovery"] == rig.capture.discovery
            assert observation["context"]["native_creation_filetimes"] == initial["native_creation_filetimes"]


def test_authentic_leg_uses_live_r0_without_workspace_restart(workspace_rig, monkeypatch):
    rig = workspace_rig
    candidate_path = rig.root.parent / "candidate.json"
    app = rig.root.parent / "app"
    app.mkdir()
    candidate = {"manifest_sha256": "a" * 64, "artifacts": {}}
    journey.write_json(candidate_path, candidate)
    target = {"release": 1, "agent": {"sha": "b" * 40}, "runtime": {"sha": "c" * 40}}
    args = SimpleNamespace(
        job_id="direct-windows", mode="direct", leg="release0-to-release1", shell="powershell.exe",
        candidate=candidate_path, app_checkout=app, release0_fixture=jumps.RELEASE0_FIXTURE,
        allow_release0_to_release1=True, source_override=False,
    )
    monkeypatch.setattr(journey, "runner_identity", lambda: {})
    monkeypatch.setattr(journey, "selected_manifest", lambda *_args, **_kwargs: (candidate_path, target))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: target)
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "d" * 40})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *_: rig.events.append("bootstrap-r0"))
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda _: None)
    monkeypatch.setattr(journey, "selected_tuple", lambda *_: {})
    # Checkpoint and verifier behavior are outside this identity regression.
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(journey, "verify", lambda *_: None)
    monkeypatch.setattr(jumps, "task_after_update", lambda *_: "task-1")
    def live(_root, _data, _evidence, action, request):
        rig.events.append(action)
        return {"project": request["project"]} if action == "seed" else {}
    monkeypatch.setattr(jumps, "live_workspace", live)
    monkeypatch.setattr(journey, "process_birth", lambda _: "old-birth")

    def update_boundary(*_args, **_kwargs):
        raise RuntimeError("reached update boundary without executing installer")

    monkeypatch.setattr(journey, "bootstrap", update_boundary)
    with pytest.raises(RuntimeError, match="reached update boundary"):
        jumps.execute_release0_to_release1(
            args, rig.root, rig.data, rig.evidence.parent, SimpleNamespace(pid=9000), "sentinel-birth")
    assert rig.events == ["bootstrap-r0", "seed", "snapshot"]
    assert rig.native_calls == [4284, 8384]  # original selected R0 owner
    receipt = journey.read_json(rig.evidence.parent / "journey-result.json")
    assert receipt["release_transition"] is None
    assert receipt["status"] == "fail"
    assert all(value == "not-run" for value in receipt["checks"].values())
