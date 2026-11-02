"""TST-B1: focused custody and result-contract tests for named installed journeys."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import journey_version_jumps as jumps


def app_checkout(tmp_path):
    app = tmp_path / "app"
    (app / "public").mkdir(parents=True)
    shipped = app / "public" / "install-manifest.json"
    shipped.write_bytes(b'{"schema_version":1,"agent":{"sha":"released-a"},"runtime":{"sha":"released-r","schema_digest":"digest"},"tools":{"python":{"version":"3.11.13"}}}\n')
    return app, shipped


def test_tst_b1_production_uses_shipped_manifest_bytes_without_source_override(tmp_path):
    app, shipped = app_checkout(tmp_path)
    original = shipped.read_bytes()
    selected, manifest = journey.selected_manifest(app, source_override=False, agent=None,
                                                   runtime=None, evidence=tmp_path / "evidence")
    assert selected == shipped
    assert selected.read_bytes() == original
    assert manifest["runtime"]["schema_digest"] == "digest"
    assert not (tmp_path / "evidence").exists()


def test_tst_b1_local_sources_require_explicit_test_mode(tmp_path, monkeypatch):
    app, shipped = app_checkout(tmp_path)
    with pytest.raises(ValueError, match="--source-override"):
        journey.selected_manifest(app, source_override=False, agent=tmp_path / "agent",
                                  runtime=tmp_path / "runtime", evidence=tmp_path / "evidence")
    monkeypatch.setattr(journey, "git_source", lambda path: {"sha": path.name * 40, "repo": str(path)})
    selected, manifest = journey.selected_manifest(app, source_override=True,
                                                   agent=tmp_path / "a", runtime=tmp_path / "r",
                                                   evidence=tmp_path / "evidence")
    assert selected.name == "source-override-manifest.json"
    assert manifest["agent"]["repo"] == str(tmp_path / "a")
    assert manifest["runtime"]["repo"] == str(tmp_path / "r")
    assert "schema_digest" not in manifest["runtime"]
    assert shipped.read_bytes().startswith(b'{"schema_version":1')


def test_tst_b1_each_install_update_leg_calls_shared_verifier(tmp_path, monkeypatch):
    commands = []
    def fake_run(argv, log, **kwargs):
        commands.append([str(part) for part in argv])
        Path(argv[argv.index("--output") + 1]).write_text(
            json.dumps({"passed": True, "details": {"origin.imports": {"astrid": "installed"}}})
        )
        return ""
    monkeypatch.setattr(journey, "run", fake_run)
    version = tmp_path / "version.json"
    data = tmp_path / "data"
    journey.verify("install", version, data, tmp_path)
    journey.verify("observe", version, data, tmp_path)
    journey.verify("update", version, data, tmp_path, before=tmp_path / "verify-observe.json")
    assert [argv[argv.index("--mode") + 1] for argv in commands] == ["install", "observe", "update"]
    assert all(argv[1] == str(journey.ROOT / "scripts" / "verify_installed.py") for argv in commands)
    assert commands[-1][-2:] == ["--before", str(tmp_path / "verify-observe.json")]


def test_tst_b1_installed_tuple_rejects_wrong_source(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "manifest.json").write_text(json.dumps({
        "agent": {"sha": "wrong"}, "runtime": {"sha": "right"},
    }), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Installed agent source differs"):
        journey.selected_tuple({"agent": {"sha": "expected"}, "runtime": {"sha": "right"}},
                               {"root": str(installed)})


def _diagnostic_release0_state(tmp_path, monkeypatch, *, write_state=True):
    """Create a retained release-0 state while stubbing only external products."""
    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    version_root = root / "versions" / "release0"
    version_root.mkdir(parents=True)
    data.mkdir(parents=True)
    realm = run_root / "realm"
    realm.mkdir()
    (realm / "realm.sqlite3").write_bytes(b"pre-seed release-0 realm")
    journey.write_json(data / "runtime" / "discovery.json", {"realm_root": str(realm)})
    journey.write_json(root / "install.json", {"data_root": str(data)})
    journey.write_json(run_root / journey.OWNER_FILE, {
        "schema_version": 1, "run_id": "a" * 32,
        "root": str(root), "data_root": str(data),
    })
    (root / "current").write_text("release0\n", encoding="utf-8")
    version_path = version_root / "version.json"
    journey.write_json(version_path, {"root": str(version_root), "python": sys.executable, "env": {}})
    release0_manifest = tmp_path / "release0-manifest.json"
    release0_sources = {"agent": {"sha": "1" * 40}, "runtime": {"sha": "2" * 40}}
    journey.write_json(release0_manifest, {"release": 0, **release0_sources, "tools": {}})
    journey.write_json(version_root / "manifest.json", {"release": 0, **release0_sources, "tools": {}})
    app = tmp_path / "app"
    app.mkdir()
    target_manifest = {
        "release": 1, "agent": {"sha": "3" * 40},
        "runtime": {"sha": "4" * 40, "schema_digest": "f" * 64},
        "tools": {"python": {"version": "3.11.13"}},
    }
    candidate = {
        "app": "5" * 40, "agent": "3" * 40, "runtime": "4" * 40, "pinokio": "6" * 40,
        "manifest_sha256": "7" * 64, "artifacts": {"manifest": "7" * 64},
    }
    candidate_path = tmp_path / "candidate.json"
    journey.write_json(candidate_path, candidate)
    args = argparse.Namespace(
        run_root=run_root, root=root, data_root=data, leg="release0-to-release1", mode="direct",
        shell="sh", job_id=None, source_override=False, app_checkout=app, candidate=candidate_path,
        candidate_agent_checkout=tmp_path / "agent", candidate_runtime_checkout=tmp_path / "runtime",
        pinokio_checkout=tmp_path / "pinokio", release0_fixture=tmp_path / "fixture",
        allow_release0_to_release1=True, diagnostic_start_at="release0-api-seed",
        diagnostic_state=run_root / jumps.DIAGNOSTIC_STATE_NAME,
    )
    fixture_receipt = {"fixture_sums_sha256": "8" * 64, "manifest_sha256": "9" * 64}
    checkouts = {name: {"sha": sha, "repo": str(tmp_path / name)} for name, sha in {
        "app": "5" * 40, "agent": "3" * 40, "runtime": "4" * 40, "pinokio": "6" * 40,
    }.items()}
    monkeypatch.setattr(jumps, "release0_admission", lambda unused: (
        tmp_path / "fixture", release0_manifest, fixture_receipt))
    monkeypatch.setattr(journey, "selected_manifest", lambda *unused, **kwargs: (
        tmp_path / "selected-manifest.json", target_manifest))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *unused: target_manifest)
    monkeypatch.setattr(journey, "candidate_checkouts", lambda unused: checkouts)
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *unused: {"manager": "test"})
    monkeypatch.setattr(journey, "healthy", lambda *unused: {"pid": 42, "runtime_instance_id": "r0"})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *unused: None)
    version_path, _, version = journey.installation(root, data)
    args.candidate_checkouts = checkouts
    if write_state:
        jumps.write_diagnostic_release0_preconditions(
            args, root, data, fixture_receipt=fixture_receipt, release0_manifest=release0_manifest,
            target_manifest=target_manifest, candidate=candidate, version_path=version_path, version=version,
        )
    evidence = run_root / "evidence" / "release0-to-release1-diagnostic-release0-api-seed"
    return args, root, data, evidence, version_path


@pytest.mark.parametrize("case", ["absent", "stale", "wrong-version", "wrong-tuple"])
def test_diagnostic_stage_start_fails_closed_for_invalid_preconditions(tmp_path, monkeypatch, case):
    args, root, data, evidence, version_path = _diagnostic_release0_state(
        tmp_path, monkeypatch, write_state=case != "absent")
    if case == "stale":
        state = journey.read_json(args.diagnostic_state)
        state["run_id"] = "b" * 32
        journey.write_json(args.diagnostic_state, state)
    elif case == "wrong-version":
        version = journey.read_json(version_path)
        version["diagnostic_mutation"] = True
        journey.write_json(version_path, version)
    elif case == "wrong-tuple":
        state = journey.read_json(args.diagnostic_state)
        state["release1_candidate"]["tuple"]["agent"] = "a" * 40
        journey.write_json(args.diagnostic_state, state)
    calls = []
    monkeypatch.setattr(jumps, "direct_workspace", lambda *unused: calls.append(unused))
    with pytest.raises(RuntimeError, match="precondition file is absent|preconditions are stale"):
        jumps.execute_diagnostic_release0_stage(args, root, data, evidence)
    assert calls == []
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["status"] == "fail"
    assert receipt["qualification"] == {
        "eligible": False,
        "reason": "stage-start diagnostics do not execute the complete lifecycle",
    }


def test_diagnostic_stage_runs_only_seed_block_after_validated_checkpoint(tmp_path, monkeypatch):
    args, root, data, evidence, _ = _diagnostic_release0_state(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(journey, "verify", lambda *unused, **kwargs: calls.append("verify"))
    monkeypatch.setattr(jumps, "task_after_update", lambda *unused: "task-1")
    def workspace(_root, _version, _data, _evidence, action, request):
        calls.append(action)
        if action == "seed":
            assert request == {"project": "tst-m4-3b-release0", "generation": "release0",
                               "task_id": None, "create_project": True}
            return {"project": "tst-m4-3b-release0"}
        assert action == "snapshot"
        assert request == {"project": "tst-m4-3b-release0", "task_id": "task-1"}
        return {"snapshot": "ok"}
    monkeypatch.setattr(jumps, "direct_workspace", workspace)
    result = jumps.execute_diagnostic_release0_stage(args, root, data, evidence)
    assert calls == ["verify", "seed", "snapshot"]
    assert result["status"] == "pass"
    assert result["checks"] == {"preconditions": "pass", "seed": "pass", "snapshot": "pass"}
    assert result["evidence_class"] == "diagnostic-only"
    assert result["qualification"]["eligible"] is False


def test_diagnostic_release0_admission_uses_explicit_stage_not_a_job_id(tmp_path, monkeypatch):
    args = argparse.Namespace(
        mode="direct", job_id=None, candidate=tmp_path / "candidate.json",
        allow_release0_to_release1=True, release0_fixture=tmp_path / "fixture",
        source_override=False, diagnostic_start_at="release0-api-seed",
    )
    expected = (tmp_path / "fixture", tmp_path / "manifest.json", {"fixture": "bound"})
    monkeypatch.setattr(jumps, "verified_release0_fixture", lambda fixture: expected)
    assert jumps.release0_admission(args) == expected
    args.diagnostic_start_at = None
    with pytest.raises(ValueError, match="scoped frozen-fixture direct admission"):
        jumps.release0_admission(args)


def test_diagnostic_stage_start_is_explicit_and_cannot_use_a_qualification_job(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject before execution"))
    base = ["--leg", "release0-to-release1", "--mode", "direct", "--app-checkout", str(tmp_path),
            "--run-root", str(tmp_path / "run"), "--root", str(tmp_path / "run" / "program"),
            "--data-root", str(tmp_path / "run" / "data"), "--candidate", str(tmp_path / "candidate.json"),
            "--candidate-agent-checkout", str(tmp_path / "agent"),
            "--candidate-runtime-checkout", str(tmp_path / "runtime"),
            "--pinokio-checkout", str(tmp_path / "pinokio"), "--release0-fixture", str(tmp_path / "fixture"),
            "--allow-release0-to-release1", "--diagnostic-start-at", "release0-api-seed",
            "--diagnostic-state", str(tmp_path / "run" / jumps.DIAGNOSTIC_STATE_NAME)]
    with pytest.raises(SystemExit) as failure:
        journey.main([*base, "--job-id", "direct-linux"])
    assert failure.value.code == 2


def test_default_journey_execution_does_not_enter_diagnostic_stage(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    args = argparse.Namespace(
        run_root=run_root, root=run_root / "program", data_root=run_root / "data",
        leg="fresh-install", shell="sh", mode="direct", source_override=False,
        diagnostic_start_at=None, retain_for_next_leg=False,
    )
    calls = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(journey, "assert_host_available", lambda leg, *unused: calls.append(leg))
    monkeypatch.setattr(journey, "snapshot_failure_evidence", lambda *unused: None)
    monkeypatch.setattr(journey, "_execute_owned", lambda *unused: {"status": "pass"})
    monkeypatch.setattr(jumps, "execute_diagnostic_release0_stage",
                        lambda *unused: pytest.fail("default execution entered diagnostic mode"))
    result = journey.execute(args)
    assert result["status"] == "pass"
    assert result["final_cleanup"] == "pass"
    assert calls == ["fresh-install", "cleanup"]


def test_tst_b1_failure_writes_small_result_with_unrun_checks(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program",
                              data_root=run_root / "data", app_checkout=tmp_path / "missing-app",
                              source_override=False, agent_checkout=None, runtime_checkout=None,
                              leg="fresh-install", shell="sh")
    # The unit check exercises only run custody; local login state is irrelevant.
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    with pytest.raises(FileNotFoundError):
        journey.execute(args)
    result = json.loads((run_root / "evidence" / "fresh-install" / "journey-result.json").read_text())
    assert result["scenario"] == "fresh-install"
    assert result["mode"] == "direct"
    assert result["status"] == "fail"
    assert set(result["checks"].values()) == {"not-run"}
    assert result["installed_tuple"] is None


def test_explicit_pinokio_backend_mode_uses_backend_executor(tmp_path, monkeypatch):
    args = argparse.Namespace(leg="fresh-install", shell="sh", mode="pinokio-backend",
                              source_override=False)
    expected = {"status": "pass", "pinokio_backend": {}, "source_unit_evidence": {}}
    monkeypatch.setattr(journey, "execute_pinokio_backend", lambda *unused: expected)
    result = journey._execute_owned(args, tmp_path / "program", tmp_path / "data",
                                    tmp_path / "evidence", SimpleNamespace(pid=123), "birth")
    assert result["mode"] == "pinokio-backend"
    assert result["status"] == "pass"


def test_journey_workflow_uses_astrid_app_for_all_candidate_checkouts():
    workflow = (journey.ROOT / ".github" / "workflows" / "journey.yml").read_text()
    assert workflow.count("repository: banodoco/astrid-app") == 4
    assert workflow.count("name: Check out frozen App candidate") == 3
    assert "repository: banodoco/reigh-app" not in workflow


def test_tst_b1_isolated_roots_reject_aliases_and_preserve_existing_install(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "install.json"
    marker.write_text('{"owner":"real-install"}\n')
    run_root = tmp_path / "journey"
    with pytest.raises(ValueError, match="run-owned program/data"):
        journey.run_scope(run_root, existing, run_root / "data", "fresh-install")
    assert marker.read_text() == '{"owner":"real-install"}\n'
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks or path aliases"):
        journey.run_scope(alias / "journey", alias / "journey" / "program",
                          alias / "journey" / "data", "fresh-install")
    assert marker.read_text() == '{"owner":"real-install"}\n'
    assert not run_root.exists()
    run_root.mkdir()
    (run_root / "program").symlink_to(existing, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        journey.run_scope(run_root, run_root / "program", run_root / "data", "fresh-install")
    assert marker.read_text() == '{"owner":"real-install"}\n'


def test_tst_b1_preflight_refuses_default_install_before_sentinel_or_bootstrap(tmp_path, monkeypatch):
    default = tmp_path / "real-default"
    default.mkdir()
    marker = default / "install.json"
    marker.write_text('{"owner":"real-install"}\n')
    run_root = tmp_path / "journey"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program",
                              data_root=run_root / "data", app_checkout=tmp_path / "missing-app",
                              source_override=False, agent_checkout=None, runtime_checkout=None,
                              leg="fresh-install", shell="sh")
    monkeypatch.setattr(journey, "program_root", lambda: default)
    monkeypatch.setattr(journey, "sentinel_process", lambda *unused: (_ for _ in ()).throw(
        AssertionError("sentinel must not start")))
    monkeypatch.setattr(journey, "bootstrap", lambda *unused: (_ for _ in ()).throw(
        AssertionError("bootstrap must not run")))
    with pytest.raises(RuntimeError, match="non-journey Astrid login installation"):
        journey.execute(args)
    assert marker.read_text() == '{"owner":"real-install"}\n'
    result = json.loads((run_root / "evidence" / "fresh-install" / "journey-result.json").read_text())
    assert result["status"] == "fail"
    assert set(result["checks"].values()) == {"not-run"}


def test_tst_b1_preflight_refuses_loaded_user_unit_without_unit_file(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey, "program_root", lambda: tmp_path / "missing-default")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.setattr(journey.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="LoadState=loaded\nFragmentPath=/other/astrid.service\n"))
    with pytest.raises(RuntimeError, match="outside this journey is loaded"):
        journey.assert_host_available("fresh-install", tmp_path / "run" / "program",
                                      tmp_path / "run" / "data")


def test_release1_direct_linux_records_failed_manager_prerequisite_before_bootstrap(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program", data_root=run_root / "data",
                              app_checkout=tmp_path, source_override=False, agent_checkout=None,
                              runtime_checkout=None, candidate=tmp_path / "candidate.json",
                              candidate_agent_checkout=tmp_path, candidate_runtime_checkout=tmp_path,
                              pinokio_checkout=tmp_path, job_id="direct-linux", mode="direct",
                              leg="fresh-install", shell="sh", retain_for_next_leg=False)
    monkeypatch.setattr(journey, "runner_identity", lambda: {"runner_image": "ubuntu-24.04", "runner_provenance": {}})
    monkeypatch.setattr(journey, "assert_runner_identity", lambda unused: None)
    monkeypatch.setattr(journey, "candidate_checkouts", lambda unused: {"app": {"sha": "a" * 40}})
    monkeypatch.setattr(journey, "release1_manager_prerequisite", lambda *unused: (_ for _ in ()).throw(
        RuntimeError("Required native service manager systemd --user is unavailable")))
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: (_ for _ in ()).throw(
        AssertionError("host preflight must not run after a missing native manager")))
    with pytest.raises(RuntimeError, match="systemd --user"):
        journey.execute(args)
    receipt = journey.read_json(run_root / "evidence" / "fresh-install" / "journey-result.json")
    assert receipt["status"] == "fail"
    assert receipt["service_manager_prerequisite"] == {
        "required": True, "outcome": "fail", "foreground_fallback": False,
        "error": "RuntimeError: Required native service manager systemd --user is unavailable",
    }


def test_tst_b1_cleanup_accepts_only_exact_uninstalled_selection_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    default = tmp_path / "default" / "astrid"
    default.parent.mkdir()
    monkeypatch.setattr(journey, "program_root", lambda: default)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config"))
    monkeypatch.setattr(journey.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout="LoadState=not-found\nFragmentPath=\n"))
    monkeypatch.setattr(journey, "runtime_processes", lambda *unused: [])
    root, data = tmp_path / "run" / "program", tmp_path / "run" / "data"
    selection = default.parent / "astrid-install.json"
    receipt = {"owner": "haus.astrid.runtime", "program_root": str(root),
               "data_root": str(data), "uninstalled": True}
    journey.write_json(selection, receipt)
    with pytest.raises(RuntimeError, match="non-journey Astrid login installation"):
        journey.assert_host_available("fresh-install", root, data)
    journey.assert_host_available("cleanup", root, data)
    for changed in ({**receipt, "uninstalled": False},
                    {**receipt, "program_root": str(tmp_path / "foreign")},
                    {**receipt, "foreground": {"pid": 123}}):
        journey.write_json(selection, changed)
        with pytest.raises(RuntimeError, match="non-journey Astrid login installation"):
            journey.assert_host_available("cleanup", root, data)


def test_tst_b1_windows_manager_action_must_be_inside_exact_owned_root():
    root = Path(r"C:\runner\tst-b1\program")
    assert journey.windows_task_owned('present\nC:\\runner\\tst-b1\\program\\versions\\v1\\python.exe\n', root)
    assert not journey.windows_task_owned('present\nC:\\runner\\tst-b1\\program-other\\python.exe\n', root)
    assert not journey.windows_task_owned('present\nC:\\Users\\someone\\Astrid\\python.exe\n', root)


@pytest.mark.parametrize("phase,returncode,stderr,absent", [
    ("after-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\n', True),
    ("before-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\n', False),
    ("after-cleanup", 113, "launchctl unavailable\n", False),
    ("after-cleanup", 1, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\n', False),
    ("after-cleanup", 113, 'Could not find service "other" in domain for user gui: 501\n', False),
    ("after-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 502\n', False),
    ("after-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\nextra error\n', False),
])
@pytest.mark.parametrize("unit_exists", [True, False])
def test_darwin_manager_state_classifies_only_expected_cleanup_absence(
        tmp_path, monkeypatch, capsys, phase, returncode, stderr, absent, unit_exists):
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setattr(journey.os, "getuid", lambda: 501)
    calls = []
    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=returncode, stdout="raw stdout\n", stderr=stderr)
    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    data, evidence = tmp_path / "run" / "data", tmp_path / "evidence"
    unit = data.parent / "home" / "Library" / "LaunchAgents" / "haus.astrid.runtime.plist"
    if unit_exists:
        unit.parent.mkdir(parents=True)
        unit.write_bytes(b"raw plist\n")

    journey.capture_manager_state(evidence, data, phase)

    command = ["launchctl", "print", "gui/501/haus.astrid.runtime"]
    assert calls == [(command, {"capture_output": True, "text": True, "timeout": 30, "check": False})]
    manager = journey.read_json(evidence / f"manager-state-{phase}.json")
    assert manager == {"platform": "darwin", "phase": phase, "command": command,
                       "returncode": returncode, "stdout": "raw stdout\n", "stderr": stderr,
                       "unit_file": str(unit) if unit_exists else None,
                       **({"service_state": "absent"} if absent else
                          {"error": f"manager command exited {returncode}"})}
    expected_unit = b"raw plist\n" if unit_exists else f"Unit file unavailable: {unit}\n".encode()
    assert (evidence / f"unit-file-{phase}.txt").read_bytes() == expected_unit
    assert ("Journey manager-state capture" in capsys.readouterr().err) is not absent


def test_tst_b1_final_cleanup_uses_exact_owned_roots_and_rechecks_manager(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    root, data = run_root / "program", run_root / "data"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="fresh-install", shell="sh", retain_for_next_leg=False)
    preflights = []
    monkeypatch.setattr(journey, "assert_host_available", lambda leg, *unused: preflights.append(leg))
    monkeypatch.setattr(journey, "runtime_processes", lambda *unused: [])
    def fake_journey(*unused):
        root.mkdir()
        (root / "current").write_text("v1")
        evidence = run_root / "evidence" / "fresh-install"
        journey.write_json(evidence / "journey-result.json", {"scenario": "fresh-install", "status": "pass"})
        return {"status": "pass"}
    monkeypatch.setattr(journey, "_execute_owned", fake_journey)
    monkeypatch.setattr(journey, "installation", lambda *unused: (root / "versions" / "v1" / "version.json",
                                                                   data, {"python": "python"}))
    calls = []
    def fake_cli(version, argv, evidence, name, **kwargs):
        calls.append(argv)
        (root / "current").unlink()
        return ""
    monkeypatch.setattr(journey, "installed_cli", fake_cli)
    assert journey.execute(args)["status"] == "pass"
    assert calls == [["install", "--uninstall", "--root", str(root), "--data-root", str(data)]]
    assert preflights == ["fresh-install", "cleanup"]
    recorded = journey.read_json(run_root / "evidence" / "fresh-install" / "journey-result.json")
    assert recorded["final_cleanup"] == "pass"


def test_tst_b1_cli_requires_owned_roots_before_any_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "bootstrap", lambda *unused: (_ for _ in ()).throw(
        AssertionError("bootstrap must not run")))
    with pytest.raises(SystemExit) as failure:
        journey.main(["--leg", "fresh-install", "--app-checkout", str(tmp_path)])
    assert failure.value.code == 2


def test_tst_b1_release0_fixture_flags_are_scoped_to_the_direct_release0_leg(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "execute", lambda *unused: pytest.fail("must reject CLI before execution"))
    base = ["--app-checkout", str(tmp_path), "--run-root", str(tmp_path / "run"),
            "--root", str(tmp_path / "run" / "program"), "--data-root", str(tmp_path / "run" / "data")]
    with pytest.raises(SystemExit) as error:
        journey.main(["--leg", "fresh-install", *base, "--release0-fixture", str(tmp_path)])
    assert error.value.code == 2


def test_tst_b1_local_runner_is_explicitly_unqualified(monkeypatch):
    for key in ("ASTRID_CI_EXPECTED_RUNNER", "RUNNER_OS", "ImageOS", "ImageVersion"):
        monkeypatch.delenv(key, raising=False)
    identity = journey.runner_identity()
    assert identity == {"runner_image": "local", "runner_provenance": {
        "observed_os_family": journey.sys.platform}}
    journey.assert_runner_identity(identity)


def test_tst_b1_hosted_runner_label_requires_matching_os_and_raw_image_fields(monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", "ubuntu-24.04")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setenv("ImageOS", "ubuntu24")
    monkeypatch.setenv("ImageVersion", "20261028.1")
    identity = journey.runner_identity()
    assert identity["runner_image"] == "ubuntu-24.04"
    assert identity["runner_provenance"]["ImageVersion"] == "20261028.1"
    journey.assert_runner_identity(identity)
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", "windows-2022")
    with pytest.raises(RuntimeError, match="disagrees with observed"):
        journey.assert_runner_identity(journey.runner_identity())
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", "ubuntu-24.04")
    monkeypatch.setenv("RUNNER_OS", "Windows")
    with pytest.raises(RuntimeError, match="RUNNER_OS"):
        journey.assert_runner_identity(journey.runner_identity())
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.delenv("ImageVersion")
    with pytest.raises(RuntimeError, match="ImageVersion"):
        journey.assert_runner_identity(journey.runner_identity())


@pytest.mark.parametrize("label", ["macos-14", "macos-15"])
def test_tst_b1_macos_hosted_runner_identity(monkeypatch, label):
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", label)
    monkeypatch.setenv("RUNNER_OS", "macOS")
    monkeypatch.setenv("ImageOS", label.replace("-", ""))
    monkeypatch.setenv("ImageVersion", "20261028.1")
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    journey.assert_runner_identity(journey.runner_identity())
    monkeypatch.setattr(journey.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="disagrees with observed"):
        journey.assert_runner_identity(journey.runner_identity())
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    with pytest.raises(RuntimeError, match="RUNNER_OS"):
        journey.assert_runner_identity(journey.runner_identity())
    monkeypatch.setenv("RUNNER_OS", "macOS")
    for field in ("RUNNER_OS", "ImageOS", "ImageVersion"):
        with monkeypatch.context() as missing:
            missing.delenv(field)
            with pytest.raises(RuntimeError, match=field):
                journey.assert_runner_identity(journey.runner_identity())


@pytest.mark.parametrize("job_id,mode,leg", [
    ("direct-macos", "direct", "release0-to-release1"),
    ("pinokio-backend-macos", "pinokio-backend", "fresh-install"),
    ("pinokio-fully-local-macos", "pinokio-fully-local", "fresh-install"),
])
def test_tst_b1_macos15_matrix_job_reaches_candidate_checks(tmp_path, monkeypatch, job_id, mode, leg):
    run_root = tmp_path / "journey"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program",
                              data_root=run_root / "data", leg=leg, shell="sh",
                              job_id=job_id, mode=mode)
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    for key, value in {"ASTRID_CI_EXPECTED_RUNNER": "macos-15", "RUNNER_OS": "macOS",
                       "ImageOS": "macos15", "ImageVersion": "20261028.1"}.items():
        monkeypatch.setenv(key, value)
    def reached_candidate_checks(_args):
        raise RuntimeError("reached candidate checks")
    monkeypatch.setattr(journey, "candidate_checkouts", reached_candidate_checks)
    with pytest.raises(RuntimeError, match="reached candidate checks"):
        journey.execute(args)


def test_tst_b1_runner_mismatch_fails_before_host_or_installer(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program",
                              data_root=run_root / "data", leg="fresh-install", shell="sh")
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", "windows-2022")
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: (_ for _ in ()).throw(
        AssertionError("host preflight must not run")))
    monkeypatch.setattr(journey, "bootstrap", lambda *unused: (_ for _ in ()).throw(
        AssertionError("bootstrap must not run")))
    with pytest.raises(RuntimeError, match="disagrees with observed"):
        journey.execute(args)
    result = journey.read_json(run_root / "evidence" / "fresh-install" / "journey-result.json")
    assert result["status"] == "fail"
    assert result["runner_image"] == "windows-2022"


@pytest.mark.parametrize("system,shell,script", [
    ("linux", "sh", "install"), ("win32", "powershell.exe", "install.ps1"),
])
def test_tst_b1_bootstrap_propagates_run_owned_roots_on_each_os(tmp_path, monkeypatch, system, shell, script):
    app = tmp_path / "app"
    (app / "public").mkdir(parents=True)
    (app / "public" / script).write_text("", encoding="utf-8")
    manifest = app / "public" / "install-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    root, data = tmp_path / "run" / "program", tmp_path / "run" / "data"
    calls = []
    monkeypatch.setattr(journey.sys, "platform", system)
    monkeypatch.setattr(journey, "run", lambda argv, log, **kwargs: calls.append(
        ([str(part) for part in argv], kwargs)))
    journey.bootstrap(app, manifest, shell, "update", tmp_path / "evidence", root, data)
    argv, kwargs = calls[0]
    assert argv[argv.index("--root") + 1] == str(root)
    assert argv[argv.index("--data-root") + 1] == str(data)
    assert argv[argv.index("--manifest") + 1] == str(manifest)
    assert "--update" in argv


@pytest.mark.posix_native
def test_tst_b1_sentinel_survives_then_is_reaped_by_own_context(tmp_path):
    with pytest.raises(RuntimeError, match="stop journey"):
        with journey.sentinel_process(tmp_path) as (process, birth):
            assert birth
            assert journey.sentinel_alive(process, birth)
            raise RuntimeError("stop journey")
    assert process.poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)


def test_tst_b1_sentinel_observation_separates_exit_from_birth_mismatch(monkeypatch):
    class FakeProcess:
        pid = 413
        exit_code = None
        def poll(self):
            return self.exit_code
    process = FakeProcess()
    monkeypatch.setattr(journey, "process_birth", lambda unused: "new-birth")
    mismatch = journey.sentinel_observation(process, "old-birth")
    assert mismatch == {"pid": 413, "exit_code": None,
                        "observed_birth": "new-birth", "alive": False}
    process.exit_code = -15
    exited = journey.sentinel_observation(process, "old-birth")
    assert exited["exit_code"] == -15
    assert exited["observed_birth"] == "new-birth"


def test_tst_b1_provider_credentials_are_removed_during_child_launch(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("FAL_KEY", "secret")
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    with journey.without_provider_credentials():
        assert "OPENAI_API_KEY" not in journey.os.environ
        assert "FAL_KEY" not in journey.os.environ
        assert journey.os.environ["ASTRID_HOST_EXECUTION_PROFILE"] == "installed"
    assert journey.os.environ["OPENAI_API_KEY"] == "secret"


@pytest.mark.posix_native
def test_tst_b1_counts_all_runtime_owners_to_reject_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    data = tmp_path / "data"
    support = str(data / "runtime")
    monkeypatch.setattr(journey.subprocess, "check_output", lambda *args, **kwargs: (
        f"101 python -X utf8 -m runtime_protocol start --support-root {support}\n"
        f"102 python -X utf8 -m runtime_protocol start --support-root /other/runtime\n"
        "103 python -m astrid_runtime up\n"
    ))
    assert journey.runtime_processes(data) == [101, 102]


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_tst_b1_census_finds_installed_python_c_owner_and_foreign_duplicate(
        tmp_path, monkeypatch, platform):
    monkeypatch.setattr(journey.sys, "platform", platform)
    # The actual service prelude is long and may be rendered with newlines by ps.
    installed = ("/selected/py/bin/python -X utf8 -I -c import json,os,sys\n" +
                 "# " + "padding" * 1000 + "\n" +
                 "from astrid_runtime.cli import main\n" +
                 "sys.exit(main(['run', '--profile', 'astrid', '--data-root', '/selected']))")
    foreign = installed.replace("/selected", "/foreign")
    unrelated = "python -c from astrid_runtime.cli import main; main(['status'])"
    if platform == "win32":
        rows = [{"ProcessId": 4123, "CommandLine": installed},
                {"ProcessId": 4124, "CommandLine": foreign},
                {"ProcessId": 4125, "CommandLine": unrelated}]
        monkeypatch.setattr(journey.windows, "powershell", lambda *args, **kwargs: json.dumps(rows))
    else:
        def ps(argv, **kwargs):
            assert argv == ["ps", "-ww", "-axo", "pid=,command="]
            return f" 4123 {installed}\n 4124 {foreign}\n 4125 {unrelated}\n"
        monkeypatch.setattr(journey.subprocess, "check_output", ps)
    assert journey.runtime_processes(tmp_path) == [4123, 4124]
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 4123})
    with pytest.raises(RuntimeError, match=r"found \[4123, 4124\]"):
        journey.assert_one_runtime(tmp_path)


@pytest.mark.parametrize("owners", [[], [4124], [4123, 4124], [4123, 4123]])
def test_tst_b1_rejects_missing_foreign_or_duplicate_runtime_owner(tmp_path, monkeypatch, owners):
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 4123})
    monkeypatch.setattr(journey, "runtime_processes", lambda data: owners)
    with pytest.raises(RuntimeError, match="Expected exactly the selected Runtime owner 4123"):
        journey.assert_one_runtime(tmp_path)
    monkeypatch.setattr(journey, "runtime_processes", lambda data: [4123])
    journey.assert_one_runtime(tmp_path)


def test_failure_retains_install_and_manager_before_cleanup_and_preserves_primary(tmp_path, monkeypatch, capsys):
    run_root = tmp_path / "journey"
    root, data = run_root / "program", run_root / "data"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="fresh-install", shell="sh", retain_for_next_leg=False)
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    real_run = journey.subprocess.run
    manager_state = ["active"]
    def fake_run(argv, *args, **kwargs):
        if argv[0] == "systemctl":
            return SimpleNamespace(returncode=0, stdout=f"ActiveState={manager_state[0]}\n", stderr="")
        return real_run(argv, *args, **kwargs)
    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    log = run_root / "home" / ".local" / "state" / "astrid-runtime" / "logs" / "install.log"
    def failed_journey(*unused):
        root.mkdir()
        (root / "current").write_text("v1")
        log.parent.mkdir(parents=True)
        log.write_text("real installer detail\n")
        evidence = run_root / "evidence" / "fresh-install"
        journey.write_json(evidence / "journey-result.json", {"status": "fail", "error": "ValueError: primary"})
        (evidence / "unrelated-secret.txt").write_text("exclude")
        raise ValueError("primary")
    monkeypatch.setattr(journey, "_execute_owned", failed_journey)
    monkeypatch.setattr(journey, "installation", lambda *unused: (None, data, {"python": "python"}))
    def failed_cleanup(*unused, **kwargs):
        log.unlink()
        manager_state[0] = "inactive"
        raise RuntimeError("cleanup")
    monkeypatch.setattr(journey, "installed_cli", failed_cleanup)
    with pytest.raises(ValueError, match="primary"):
        journey.execute(args)
    output = capsys.readouterr().err
    assert output.index("Journey failed: ValueError: primary") < output.index("Journey cleanup failed: RuntimeError: cleanup")
    receipt = journey.read_json(run_root / "evidence" / "fresh-install" / "journey-result.json")
    assert receipt["error"] == "ValueError: primary"
    assert receipt["cleanup_error"] == "RuntimeError: cleanup"
    assert receipt["final_cleanup"] == "fail"
    before = receipt["failure_diagnostics"]["before-cleanup"]
    assert before["install_log"]["status"] == "captured"
    assert (run_root / before["install_log"]["path"]).read_text() == "real installer detail\n"
    assert before["service_manager"]["status"] == "captured"
    assert before["service_manager"]["observation_status"] == "observed"
    assert journey.read_json(run_root / before["service_manager"]["path"])["stdout"] == "ActiveState=active\n"
    assert not (run_root / "evidence-upload" / "before-cleanup" / "unrelated-secret.txt").exists()


def test_failure_diagnostics_records_unavailable_install_and_manager_error(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program", data_root=run_root / "data",
                              leg="fresh-install", shell="sh")
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: (_ for _ in ()).throw(ValueError("preflight")))
    monkeypatch.setattr(journey.subprocess, "run", lambda *unused, **kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr="manager unavailable"))
    with pytest.raises(ValueError, match="preflight"):
        journey.execute(args)
    receipt = journey.read_json(run_root / "evidence" / "fresh-install" / "journey-result.json")
    before = receipt["failure_diagnostics"]["before-cleanup"]
    assert before["install_log"] == {"status": "unavailable", "path": None}
    assert before["service_manager"]["observation_status"] == "error"
    assert before["service_manager"]["error"] == "manager command exited 1"
    assert journey.read_json(run_root / before["service_manager"]["path"])["stderr"] == "manager unavailable"


@pytest.mark.parametrize("platform,shell", [("win32", "powershell.exe"), ("darwin", "sh")])
def test_release0_failure_retains_raw_bootstrap_and_exit_through_cleanup(tmp_path, monkeypatch, platform, shell):
    from scripts import journey_version_jumps

    run_root = tmp_path / "journey"
    root, data = run_root / "program", run_root / "data"
    evidence = run_root / "evidence" / "release0-to-release1"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="release0-to-release1", shell=shell, retain_for_next_leg=False)
    raw = b"first product error\r\n\xff\x00" + b"x" * (journey.EVIDENCE_LOG_LIMIT + 31)
    monkeypatch.setattr(journey.sys, "platform", platform)
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *unused: None)

    @contextmanager
    def sentinel(*unused):
        yield SimpleNamespace(pid=123), "birth"

    monkeypatch.setattr(journey, "sentinel_process", sentinel)
    monkeypatch.setattr(journey, "sentinel_observation", lambda *unused: {"alive": True})

    def process(argv, **kwargs):
        assert str(argv[0]) == shell
        kwargs["stdout"].buffer.write(raw)
        return SimpleNamespace(returncode=37)

    monkeypatch.setattr(journey.subprocess, "run", process)

    def failed_journey(*unused):
        journey.write_json(evidence / "journey-result.json",
                           {"status": "not-run", "scenario": args.leg})
        root.mkdir()
        (root / "current").write_text("v0")
        journey_version_jumps.bootstrap_release0(tmp_path / "fixture", tmp_path / "manifest",
                                                 args.shell, evidence / "release0", root, data)

    monkeypatch.setattr(journey, "_execute_owned", failed_journey)
    monkeypatch.setattr(journey, "installation", lambda *unused: (None, data, {"python": "python"}))

    def cleanup(*unused, **kwargs):
        shutil.rmtree(evidence / "release0")
        (root / "current").unlink()

    monkeypatch.setattr(journey, "installed_cli", cleanup)
    monkeypatch.setattr(journey, "runtime_processes", lambda *unused: [])
    with pytest.raises(RuntimeError, match=r"release0-bootstrap.log: exit 37"):
        journey.execute(args)

    # The combined workflow uploads its allowlisted receipts from the run root.
    # Keep this source-branch regression bound to that existing harness boundary.
    staged = run_root / "evidence-upload"
    for phase in ("before-cleanup", "after-cleanup"):
        receipt = journey.read_json(staged / phase / "journey-result.json")
        entry = receipt["failure_diagnostics"][phase]["release0_bootstrap"]
        assert receipt["status"] == "fail"
        assert entry == {"status": "captured", "path": (staged / phase / "release0-bootstrap.log").relative_to(run_root).as_posix(),
                         "exit_code": 37, "sha256": hashlib.sha256(raw).hexdigest()}
        assert (run_root / entry["path"]).read_bytes() == raw


def test_cleanup_failure_after_success_still_fails_and_records_diagnostics(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program", data_root=run_root / "data",
                              leg="fresh-install", shell="sh", retain_for_next_leg=False)
    monkeypatch.setattr(journey, "assert_host_available", lambda leg, *unused: None if leg != "cleanup" else
                        (_ for _ in ()).throw(RuntimeError("cleanup only")))
    def successful_journey(*unused):
        journey.write_json(run_root / "evidence" / "fresh-install" / "journey-result.json", {"status": "pass"})
        return {"status": "pass"}
    monkeypatch.setattr(journey, "_execute_owned", successful_journey)
    with pytest.raises(RuntimeError, match="cleanup only"):
        journey.execute(args)
    receipt = journey.read_json(run_root / "evidence" / "fresh-install" / "journey-result.json")
    assert receipt["status"] == "fail"
    assert receipt["cleanup_error"] == "RuntimeError: cleanup only"
    assert "before-cleanup" in receipt["failure_diagnostics"]


def test_sentinel_cleanup_failure_does_not_replace_lifecycle_error(tmp_path, monkeypatch, capsys):
    process = SimpleNamespace(pid=123, poll=lambda: None,
                              terminate=lambda: (_ for _ in ()).throw(RuntimeError("sentinel cleanup")))
    monkeypatch.setattr(journey.subprocess, "Popen", lambda *unused, **kwargs: process)
    monkeypatch.setattr(journey, "process_birth", lambda *unused: "birth")
    monkeypatch.setattr(journey, "sentinel_alive", lambda *unused: True)
    evidence = tmp_path / "evidence" / "fresh-install"
    journey.write_json(evidence / "journey-result.json", {"status": "fail", "error": "ValueError: primary"})
    with pytest.raises(ValueError, match="primary"):
        with journey.sentinel_process(tmp_path, evidence):
            raise ValueError("primary")
    assert "Journey sentinel cleanup failed: RuntimeError: sentinel cleanup" in capsys.readouterr().err
    receipt = journey.read_json(evidence / "journey-result.json")
    assert receipt["error"] == "ValueError: primary"
    assert receipt["sentinel_cleanup_error"] == "RuntimeError: sentinel cleanup"


def test_sentinel_setup_failure_records_failure_diagnostics(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    args = argparse.Namespace(run_root=run_root, root=run_root / "program", data_root=run_root / "data",
                              leg="fresh-install", shell="sh")
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "process_birth", lambda *unused: None)
    with pytest.raises(RuntimeError, match="sentinel process birth"):
        journey.execute(args)
    receipt = journey.read_json(run_root / "evidence" / "fresh-install" / "journey-result.json")
    assert receipt["status"] == "fail"
    assert "sentinel process birth" in receipt["error"]
    assert "before-cleanup" in receipt["failure_diagnostics"]


def test_journey_workflow_direct_only_selects_three_rows_and_keeps_full_gate(tmp_path):
    """Run the checked-in config program and require direct-only to retain the full gate."""
    import subprocess
    import sys
    import textwrap

    workflow = (journey.ROOT / ".github" / "workflows" / "journey.yml").read_text(encoding="utf-8")
    config_step = workflow.split("      - id: config\n", 1)[1]
    program = textwrap.dedent(config_step.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
    (tmp_path / "driver").symlink_to(journey.ROOT, target_is_directory=True)
    output = tmp_path / "output"
    candidate = {key: char * 40 for key, char in zip(("app", "agent", "runtime", "pinokio"), "abcd")}
    env = dict(os.environ, CANDIDATE=json.dumps(candidate), DRIVER_AGENT_REF="e" * 40,
               WORKFLOW_REPOSITORY="banodoco/astrid-agent",
               OLD_LAUNCHER_REF="bbba620e3d89eeff8021417f365e6ea584203105",
               STOPPED_PATH_AGENT_REF=candidate["agent"], M4_4A_EXECUTOR_CONFIGS='{"linux":{},"macos":{},"windows":{}}',
               M4_4A_BACKEND_ONLY="false", M4_5_ENABLED="false", M4_5_ONLY="false",
               M4_5_BOUNDARY="", M4_5_BOUNDARY_ARGS="[]", M4_4B_ENABLED="false",
               M4_6_ENABLED="false", M4_6_CANDIDATE="", GITHUB_OUTPUT=str(output))

    def config(direct_only, **overrides):
        output.write_text("", encoding="utf-8")
        return subprocess.run([sys.executable, "-c", program], cwd=tmp_path,
                              env=dict(env, DIRECT_ONLY=direct_only, **overrides), text=True, capture_output=True)

    def matrix(direct_only, **overrides):
        proc = config(direct_only, **overrides)
        assert proc.returncode == 0, proc.stderr
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return json.loads(values["matrix"])["include"]

    unrestricted = matrix("false")
    assert len(unrestricted) == 9
    assert {row["job_id"] for row in unrestricted} == {
        f"{mode}-{os_name}" for mode in ("direct", "pinokio-backend", "pinokio-fully-local")
        for os_name in ("macos", "linux", "windows")
    }
    expected_direct = {
        ("direct-linux", "direct", "ubuntu-24.04", "release0-to-release1"),
        ("direct-macos", "direct", "macos-15", "release0-to-release1"),
        ("direct-windows", "direct", "windows-2022", "release0-to-release1"),
    }
    assert {(row["job_id"], row["mode"], row["runner"], row["leg"])
            for row in unrestricted if row["mode"] == "direct"} == expected_direct
    gate = workflow.split("      - name: Aggregate the exact Release-1 receipt matrix\n", 1)[1].split("      - name:", 1)[0]
    assert "if: needs.config.outputs.m4_5_only != 'true' && needs.config.outputs.m4_4a_backend_only != 'true'" in gate
    assert "--aggregate-release1" in gate

    rows = matrix("true")
    assert len(rows) == 3
    assert {(row["job_id"], row["mode"], row["runner"], row["leg"]) for row in rows} == expected_direct
    assert workflow.count("      direct_only:\n") == 2
    assert "DIRECT_ONLY: ${{ inputs.direct_only" in config_step

    smoke = matrix("true", MACOS_RUNNER="astrid-mac")
    assert len(smoke) == 1
    assert smoke[0]["job_id"] == "direct-macos" and smoke[0]["runner"] == "astrid-mac"
    assert config("true", MACOS_RUNNER="astrid-mac", M4_4B_ENABLED="true").returncode != 0
    assert config("true", MACOS_RUNNER="astrid-mac", M4_6_ENABLED="true").returncode != 0
    assert config("false", MACOS_RUNNER="astrid-mac").returncode != 0
    assert "needs.config.outputs.self_hosted_smoke != 'true'" in workflow
    assert "name: Self-hosted macOS smoke (not release evidence)" in workflow
    assert config("true", MACOS_RUNNER='["self-hosted", "astrid-mac"]').returncode != 0

    backend_rows = matrix("false", M4_4A_BACKEND_ONLY="true")
    assert {row["job_id"] for row in backend_rows} == {
        "pinokio-backend-linux", "pinokio-backend-macos", "pinokio-backend-windows"
    }
    for override in ({"M4_4A_BACKEND_ONLY": "true"}, {"M4_5_ONLY": "true"}):
        proc = config("true", **override)
        assert proc.returncode != 0
        assert "direct_only cannot be combined" in proc.stderr


def test_windows_preflight_records_query_streams_origin_and_exit(tmp_path, monkeypatch):
    from scripts import ci_windows_journey as windows
    monkeypatch.setattr(windows, "EVIDENCE", tmp_path)
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if len(calls) == 1:
            return SimpleNamespace(returncode=0, stdout='{"PSVersion":"5.1","ModulePath":"scheduler.psd1"}', stderr="")
        return SimpleNamespace(returncode=1, stdout=b'{"status":"error","error":{"message":"access denied"}}', stderr=b"query module error")
    monkeypatch.setattr(windows.subprocess, "run", fake_run)
    monkeypatch.setenv("PSModulePath", "untrusted")
    with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
        windows.scheduled_task_diagnostics()
    record = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
    assert record["query"]["argv"] == calls[1][0]
    assert record["query"]["observation"]["status"] == "error"
    assert record["query"]["stderr"] == "query module error"
    assert record["query"]["exit_code"] == 1 and record["query"]["classification"] == "error"
    assert windows.SCHEDULED_TASK_PREFLIGHT == record["query"]["argv"][-1]
    assert "PSVersion" in record["origin"]["stdout"] and "ModulePath" in record["origin"]["stdout"]
    assert all(not any(key.casefold() == "psmodulepath" for key in call[1]["env"]) for call in calls)
    evidence = tmp_path / "run" / "evidence" / "preflight"
    evidence.mkdir(parents=True)
    shutil.copyfile(tmp_path / "preflight-scheduled-task.txt", evidence / "preflight-scheduled-task.txt")
    journey.retain_named_evidence(evidence, "before-cleanup")
    assert (tmp_path / "run" / "evidence-upload" / "before-cleanup" / "preflight-scheduled-task.txt").is_file()


def test_windows_preflight_absence_and_foreign_owner_rejection(tmp_path, monkeypatch):
    from scripts import ci_windows_journey as windows
    monkeypatch.setattr(windows, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(windows.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=b'{"status":"absent"}' if a[0][-1] == windows.SCHEDULED_TASK_PREFLIGHT else "",
        stderr=b"" if a[0][-1] == windows.SCHEDULED_TASK_PREFLIGHT else ""))
    assert windows.scheduled_task_diagnostics() == {"status": "absent"}
    record = json.loads((windows.EVIDENCE / "preflight-scheduled-task.txt").read_text())
    assert record["query"]["classification"] == "not-found"
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "program_root", lambda: tmp_path / "empty" / "astrid")
    monkeypatch.setattr(journey, "runtime_processes", lambda *a: [])
    monkeypatch.setattr(windows, "scheduled_task_diagnostics", lambda **kwargs: {"status": "present", "tasks": [{"path": "\\", "executables": [r"C:\other\runtime.exe"]}]})
    with pytest.raises(RuntimeError, match="outside this journey"):
        journey.assert_host_available("fresh-install", tmp_path / "program", tmp_path / "data")


def test_linux_manager_boundary_probes_the_isolated_unit_path(tmp_path):
    import subprocess
    workflow = (journey.ROOT / ".github/workflows/journey.yml").read_text()
    block = workflow.split("      - name: Establish Linux user service manager\n", 1)[1].split("      - name: Establish Windows Task Scheduler readiness\n", 1)[0]
    script = "\n".join(line[10:] if line.startswith("          ") else line for line in block.split("        run: |\n", 1)[1].splitlines()) + "\n"
    function = script.split("probe_isolated_unit() {\n", 1)[1].split("\n}\n", 1)[0]
    run_root = tmp_path / "astrid-r1-direct-linux"
    commands = tmp_path / "commands"
    stub = tmp_path / "systemctl"
    stub.write_text('''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$CALLS"
if [[ "$*" == *FragmentPath* ]]; then printf '%s\\n' "$EXPECTED_FRAGMENT"; fi
''')
    stub.chmod(0o755)
    probe = "journey-manager-path-probe-test.service"
    config = run_root / "home/.config"
    probe_dir = config / "systemd/user"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CALLS=str(commands),
               EXPECTED_FRAGMENT=str(probe_dir / probe), run_root=str(run_root), config=str(config),
               probe_dir=str(probe_dir), probe=probe, probe_created="0")
    result = subprocess.run(["bash", "-Eeuo", "pipefail", "-c", function], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not run_root.exists()
    assert f"--user start {probe}" in commands.read_text()
    assert 'Environment="XDG_CONFIG_HOME=%s" "XDG_DATA_HOME=%s"' in script
    assert '"SYSTEMD_UNIT_PATH=%s:"' in script
    assert 'sudo systemctl restart "user@$uid.service"' in script
    assert 'test "$original" = not-found' in script
    env["EXPECTED_FRAGMENT"] = str(tmp_path / "foreign.service")
    failure = subprocess.run(["bash", "-Eeuo", "pipefail", "-c", function], env=env, capture_output=True, text=True)
    assert failure.returncode != 0


def test_windows_preflight_checkpoints_launch_and_unexpected_errors(tmp_path, monkeypatch):
    from scripts import ci_windows_journey as windows
    monkeypatch.setattr(windows, "EVIDENCE", tmp_path)
    calls = []
    def fail(argv, **kwargs):
        # The complete command is already durable even if process launch fails.
        saved = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
        calls.append(saved)
        if len(calls) == 1:
            raise OSError("powershell unavailable")
        raise ValueError("unexpected capture failure")
    monkeypatch.setattr(windows.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
        windows.scheduled_task_diagnostics()
    record = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
    assert calls[0]["query"]["argv"][-1] == windows.SCHEDULED_TASK_PREFLIGHT
    assert "powershell unavailable" in calls[1]["origin"]["error"]
    assert record["query"]["classification"] == "error"
    assert "unexpected capture failure" in record["query"]["process_error"]


def test_windows_preflight_missing_leg_regression(tmp_path, monkeypatch):
    from scripts import ci_windows_journey as windows
    monkeypatch.setattr(windows, "EVIDENCE", tmp_path)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=b'{"status":"absent"}', stderr=b"")
    monkeypatch.setattr(windows.subprocess, "run", run)
    assert windows.scheduled_task_preflight()["status"] == "absent"
    record = json.loads((tmp_path / "preflight-scheduled-task-manager-prerequisite.json").read_text())
    assert record["argv"] == calls[0]
    assert record["exit_code"] == 0
    assert windows.SCHEDULED_TASK_PREFLIGHT == calls[0][-1]


def test_windows_preflight_capture_failure_preserves_traceback_and_partial_streams(tmp_path, monkeypatch):
    import subprocess
    from scripts import ci_windows_journey as windows
    monkeypatch.setattr(windows, "EVIDENCE", tmp_path)
    def fail(argv, **kwargs):
        checkpoint = json.loads((tmp_path / "preflight-scheduled-task-manager-prerequisite.json").read_text())
        assert checkpoint["argv"] == argv and checkpoint["exit_code"] is None
        raise subprocess.TimeoutExpired(argv, 30, output=b"partial out", stderr=b"partial err")
    monkeypatch.setattr(windows.subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="Scheduled Task preflight failed"):
        windows.scheduled_task_preflight()
    record = json.loads((tmp_path / "preflight-scheduled-task-manager-prerequisite.json").read_text())
    assert record["stdout"] == "partial out" and record["stderr"] == "partial err"
    assert record["exit_code"] is None and "Traceback" in record["process_error"]
    assert "TimeoutExpired" in record["process_error"]


def test_windows_diagnostics_timeout_retains_partial_streams_and_traceback(tmp_path, monkeypatch):
    import subprocess
    from scripts import ci_windows_journey as windows
    monkeypatch.setattr(windows, "EVIDENCE", tmp_path)
    def fail(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30, output=b"partial out", stderr=b"partial err")
    monkeypatch.setattr(windows.subprocess, "run", fail)
    with pytest.raises(RuntimeError):
        windows.scheduled_task_diagnostics()
    record = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
    for item in record.values():
        assert item["stdout"] == "partial out" and item["stderr"] == "partial err"
        assert item["exit_code"] is None
    assert "Traceback" in record["origin"]["error"]
    assert "Traceback" in record["query"]["process_error"]


def test_windows_diagnostics_workflow_retains_launch_and_original_command():
    workflow = (journey.ROOT / ".github/workflows/journey.yml").read_text()
    step = workflow.split("      - name: Capture Windows scheduled-task query diagnostics\n", 1)[1].split(
        "      - name: Retain hosted service-manager prerequisite diagnostics\n", 1)[0]
    assert "original_command = $original" in step and "w.scheduled_task_preflight()" in step
    assert "w.scheduled_task_diagnostics(leg=os.environ[" in step
    assert "Get-Content -LiteralPath $stdout" in step and "Get-Content -LiteralPath $stderr" in step
    assert step.index("Save-PreflightLaunch\n          try") < step.index("$process = Start-Process")
    for part in ("$PSVersionTable.PSVersion", "$command.Module.Path", "-RedirectStandardOutput $stdout",
                 "-RedirectStandardError $stderr", "$process.ExitCode", "$_.Exception.ToString()", "finally {"):
        assert part in step
    upload = workflow.split("      - name: Retain hosted service-manager prerequisite diagnostics\n", 1)[1].split("      - name:", 1)[0]
    assert "if: always()" in upload and "journey-manager-${{ matrix.job_id }}" in upload


@pytest.mark.parametrize("module_source, expected", [
    ("raise ImportError('import sentinel')\n", "ImportError: import sentinel"),
    ("def scheduled_task_diagnostics(*, leg):\n    print('preflight leg:', leg, flush=True)\n    print('partial stdout', flush=True)\n    raise RuntimeError('preflight sentinel')\n", "RuntimeError: preflight sentinel"),
])
def test_windows_diagnostics_expression_preserves_python_traceback(tmp_path, module_source, expected):
    import subprocess
    import sys
    workflow = (journey.ROOT / ".github/workflows/journey.yml").read_text()
    code = workflow.split('          $code = "', 1)[1].split('"\n', 1)[0]
    package = tmp_path / "scripts"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "ci_windows_journey.py").write_text(module_source)
    env = dict(os.environ, PYTHONPATH=str(tmp_path), RUNNER_TEMP=str(tmp_path), JOURNEY_LEG="release0-to-release1")
    result = subprocess.run([sys.executable, "-u", "-c", code], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "Traceback" in result.stderr and expected in result.stderr
    if "preflight sentinel" in expected:
        assert "partial stdout" in result.stdout
        assert "preflight leg: release0-to-release1" in result.stdout


@pytest.mark.parametrize("exit_code", [0, 7], ids=["success", "failure"])
def test_seed_diagnostics_survive_cleanup(tmp_path, monkeypatch, exit_code):
    from scripts import journey_version_jumps

    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="release0-to-release1", shell="sh", retain_for_next_leg=False)
    script = tmp_path / "driver" / "scripts" / "journey_workspace.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, pathlib, sys\n"
        "payload = pathlib.Path(sys.argv[sys.argv.index('--request') + 1]).read_bytes()\n"
        "sys.stdout.buffer.write(json.dumps({'payload': json.loads(payload)}).encode() + b'\\n')\n"
        "sys.stderr.buffer.write(b'diagnostic-' + b'x' * 1048600)\n"
        f"sys.exit({exit_code})\n", encoding="utf-8")
    monkeypatch.setattr(journey, "ROOT", script.parent.parent)
    monkeypatch.setattr(journey, "workspace_root", lambda unused: data / "realm")
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    monkeypatch.setattr(journey, "installation", lambda *unused: (None, data, {"python": sys.executable}))
    monkeypatch.setattr(journey, "runtime_processes", lambda *unused: [])
    evidence = run_root / "evidence" / args.leg
    payload = {"project": "seed ü", "generation": "before", "task_id": None}
    def run_owned(*unused):
        root.mkdir()
        (root / "current").write_text("v0")
        journey.write_json(evidence / "journey-result.json", {"status": "pass"})
        result = journey_version_jumps.workspace(
            {"python": sys.executable, "env": {}}, data, evidence / "release0-api-seed", "seed", payload)
        assert result == {"payload": payload}
        return {"status": "pass"}
    monkeypatch.setattr(journey, "_execute_owned", run_owned)
    def cleanup(*unused, **kwargs):
        (root / "current").unlink()
        shutil.rmtree(evidence / "release0-api-seed")
    monkeypatch.setattr(journey, "installed_cli", cleanup)
    if exit_code:
        with pytest.raises(RuntimeError, match="seed.json: exit 7"):
            journey.execute(args)
    else:
        assert journey.execute(args)["status"] == "pass"
    assert not (evidence / "release0-api-seed").exists()
    staged = run_root / "evidence-upload" / "before-cleanup"
    prefix = "runtime-service-service-seed-0001-"
    request = (staged / (prefix + "input.log")).read_bytes()
    assert request == (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    command = json.loads((staged / (prefix + "command.log")).read_bytes())
    assert command[:4] == [sys.executable, "-I", str(script), "seed"]
    assert command[-2:] == ["--request", str(evidence / "release0-api-seed" / "seed-request.json")]
    assert (staged / (prefix + "stdout.log")).read_bytes() == json.dumps({"payload": json.loads(request)}).encode() + b"\n"
    assert (staged / (prefix + "stderr.log")).read_bytes() == b"diagnostic-" + b"x" * 1048600
    assert json.loads((staged / (prefix + "exit.log")).read_bytes()) == {"exit_code": exit_code}


def test_seed_diagnostics_distinguish_repeated_invocations(tmp_path, monkeypatch):
    from scripts import journey_version_jumps

    script = tmp_path / "driver" / "scripts" / "journey_workspace.py"
    script.parent.mkdir(parents=True)
    script.write_text("import pathlib, sys\n"
                      "sys.stdout.buffer.write(pathlib.Path(sys.argv[-1]).read_bytes())\n", encoding="utf-8")
    monkeypatch.setattr(journey, "ROOT", script.parent.parent)
    monkeypatch.setattr(journey, "workspace_root", lambda unused: tmp_path / "realm")
    data = tmp_path / "run" / "data"
    evidence = tmp_path / "run" / "evidence" / "release0-to-release1"
    for generation in ("one", "two"):
        assert journey_version_jumps.workspace(
            {"python": sys.executable, "env": {}}, data, evidence / "seed", "seed",
            {"generation": generation}) == {"generation": generation}
    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = tmp_path / "run" / "evidence-upload" / "before-cleanup"
    for index, generation in enumerate(("one", "two"), 1):
        prefix = f"runtime-service-service-seed-{index:04d}-"
        assert json.loads((staged / (prefix + "input.log")).read_bytes()) == {"generation": generation}
        assert json.loads((staged / (prefix + "stdout.log")).read_bytes()) == {"generation": generation}
        assert json.loads((staged / (prefix + "exit.log")).read_bytes()) == {"exit_code": 0}


@pytest.mark.parametrize("manager", ["systemd-user", "launchd", "task-scheduler"])
def test_release0_direct_workspace_releases_and_restores_owner(tmp_path, monkeypatch, manager):
    from scripts import journey_version_jumps as jumps

    root, data, evidence = tmp_path / "program", tmp_path / "data", tmp_path / "evidence"
    app = tmp_path / "app"
    app.mkdir()
    manifest_path = tmp_path / "manifest.json"
    journey.write_json(manifest_path, {"release": 0})
    candidate_path = tmp_path / "candidate.json"
    candidate = {"manifest_sha256": "digest", "artifacts": {}}
    journey.write_json(candidate_path, candidate)
    manifest = {"release": 1, "agent": {}, "runtime": {}}
    args = SimpleNamespace(job_id="direct", mode="direct", leg="release0-to-release1", shell="sh",
                           app_checkout=app, candidate=candidate_path)
    state = {"running": True, "pid": 10, "release": 0, "local": False, "content": None}
    calls = []

    def ownership(_root, _data):
        assert state["running"] and not state["local"]
        return {"manager": manager, "service_path": str(tmp_path / "service.plist")}

    def one_runtime(_data):
        assert state["running"] and not state["local"]

    def manager_action(operation):
        calls.append(operation)
        if operation == "stop":
            assert state["running"] and not state["local"]
            state["running"] = False
        else:
            assert not state["running"] and not state["local"]
            state["running"] = True
            state["pid"] += 1

    def run(argv, log, **kwargs):
        if manager == "systemd-user":
            assert argv[:2] == ["systemctl", "--user"] and argv[-1] == "astrid.service"
            manager_action(argv[2])
        else:
            assert argv[0] == "launchctl" and argv[1] in ("bootout", "bootstrap")
            manager_action("stop" if argv[1] == "bootout" else "start")
        return ""

    def powershell(code, name, **kwargs):
        assert "-TaskName 'Astrid Runtime'" in code
        manager_action("stop" if "Stop-ScheduledTask" in code else "start")
        return ""

    def workspace(version, _data, _evidence, action, request, *, realm):
        # A second RuntimeService would fail this simulated exclusive lock.
        assert not state["running"] and not state["local"]
        assert realm == tmp_path / "realm"
        state["local"] = True
        try:
            calls.append(action)
            if action == "seed":
                state["content"] = {"project": request["project"], "timeline": "user edit",
                                    "preferences": "keep", "cas_sha256": "content"}
                return {"project": request["project"]}
            assert request["task_id"] == "settled"
            return {**state["content"], "task": "settled", "schema_version": 1,
                    "target_schema_version": 1}
        finally:
            state["local"] = False

    def task(_version, _evidence, project):
        assert state["running"] and state["content"]["project"] == project
        calls.append("task")
        return "settled"

    def bootstrap(*_args):
        assert state["running"] and state["content"] is not None
        state.update(release=1, pid=state["pid"] + 1)

    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path, manifest_path, {}))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest)
    monkeypatch.setattr(journey, "selected_manifest", lambda *_args, **_kw: (candidate_path, manifest))
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "app"})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *_: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_: None)
    monkeypatch.setattr(journey, "wait_ready", lambda _: one_runtime(data))
    monkeypatch.setattr(journey, "installation", lambda *_: (tmp_path / f"v{state['release']}", data,
                                                             {"release": state["release"]}))
    monkeypatch.setattr(jumps, "installed_release", lambda version: version["release"])
    monkeypatch.setattr(journey, "selected_tuple", lambda *_: {"agent": "old", "runtime": "old"})
    monkeypatch.setattr(jumps, "selected_candidate", lambda *_: {"agent": "new", "runtime": "new"})
    monkeypatch.setattr(journey, "assert_direct_service_ownership", ownership)
    monkeypatch.setattr(journey, "assert_one_runtime", one_runtime)
    monkeypatch.setattr(journey, "runtime_processes", lambda _: [state["pid"]] if state["running"] else [])
    monkeypatch.setattr(journey, "workspace_root", lambda _: tmp_path / "realm" if state["running"]
                        else pytest.fail("discovery is unavailable after shutdown"))
    monkeypatch.setattr(journey, "healthy", lambda _: {"pid": state["pid"],
                                                       "runtime_instance_id": str(state["pid"])})
    monkeypatch.setattr(journey, "process_birth", lambda pid: str(pid) if pid == state["pid"] else None)
    monkeypatch.setattr(journey, "run", run)
    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(jumps, "workspace", workspace)
    monkeypatch.setattr(jumps, "task_after_update", task)
    monkeypatch.setattr(journey, "bootstrap", bootstrap)
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions",
                        lambda *_args, **_kwargs: tmp_path / jumps.DIAGNOSTIC_STATE_NAME)

    def verify(mode, _version, _data, directory, **_kwargs):
        if mode == "install":
            journey.write_json(directory / "verify-install.json", {"details": {}})
    monkeypatch.setattr(journey, "verify", verify)
    result = jumps.execute_release0_to_release1(args, root, data, evidence,
                                                SimpleNamespace(pid=1), "birth")
    assert result["status"] == "pass" and result["semantic_data"]["identical"]
    assert calls == ["stop", "seed", "start", "task", "stop", "snapshot", "start",
                     "stop", "snapshot", "start", "task"]
    assert state["running"] and not state["local"]


def test_direct_workspace_restores_service_when_helper_fails(tmp_path, monkeypatch):
    from scripts import journey_version_jumps as jumps

    state = {"running": True}
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda *_: {"manager": "systemd-user"} if state["running"] else pytest.fail("stopped"))
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *_: None)
    monkeypatch.setattr(journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(journey, "runtime_processes", lambda *_: [1] if state["running"] else [])
    monkeypatch.setattr(journey, "workspace_root", lambda *_: tmp_path / "realm")
    monkeypatch.setattr(journey, "run", lambda argv, *_args, **_kw: state.update(running=argv[2] == "start"))
    def fail(*_args, **_kwargs):
        assert not state["running"]
        raise ValueError("workspace failed")
    monkeypatch.setattr(jumps, "workspace", fail)
    with pytest.raises(ValueError, match="workspace failed"):
        jumps.direct_workspace(tmp_path, {}, tmp_path, tmp_path / "evidence", "seed", {})
    assert state["running"]


def test_self_hosted_identity_requires_real_macos_runner(monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", "astrid-mac")
    monkeypatch.setenv("RUNNER_OS", "macOS")
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    monkeypatch.setenv("RUNNER_NAME", "hannah-mac")
    monkeypatch.setenv("ASTRID_RUNTIME_DOOR_PORT", "49152")
    journey.assert_runner_identity(journey.runner_identity())
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    with pytest.raises(RuntimeError, match="self-hosted"):
        journey.assert_runner_identity(journey.runner_identity())
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "self-hosted")
    monkeypatch.setattr(journey.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="self-hosted"):
        journey.assert_runner_identity(journey.runner_identity())


def test_self_hosted_bootstraps_tools_before_pwsh_without_changing_hosted_setup():
    workflow = (journey.ROOT / ".github" / "workflows" / "journey.yml").read_text()
    journey_job = workflow.split("  journey:\n", 1)[1].split("\n  release2:\n", 1)[0]
    bootstrap = journey_job.split("      - name: Bootstrap PowerShell", 1)[1].split("      - name:", 1)[0]
    assert "if: matrix.runner == 'astrid-mac'" in bootstrap
    assert "a1c7e98ac403da4be5ba75c1c5855af272de7787a91d478c4267466fe58fc1af" in bootstrap
    assert "shasum -a 256 -c -" in bootstrap and '>> "$GITHUB_PATH"' in bootstrap
    assert journey_job.index("Bootstrap PowerShell") < journey_job.index("Run the matrix-owned lifecycle driver")
    python_step = journey_job.split("      - name: Select Python 3.11", 1)[1].split("      # The driver", 1)[0]
    assert "if: matrix.runner == 'astrid-mac'" in python_step
    assert "/opt/homebrew/opt/python@3.11/bin/python3.11" in python_step
    assert '"$bin_dir/python"' in python_step and '"$bin_dir/python3"' in python_step
    assert "- uses: actions/setup-python@v5\n        if: matrix.runner != 'astrid-mac'" in journey_job


def _self_hosted_smoke_env(monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    for key, value in {"ASTRID_CI_EXPECTED_RUNNER": "astrid-mac", "RUNNER_OS": "macOS",
                       "RUNNER_ENVIRONMENT": "self-hosted", "RUNNER_NAME": "hannah-mac",
                       "ASTRID_RUNTIME_DOOR_PORT": "49152"}.items():
        monkeypatch.setenv(key, value)


def test_self_hosted_census_counts_only_runner_uid_and_ignores_other_user(monkeypatch, tmp_path):
    _self_hosted_smoke_env(monkeypatch)
    monkeypatch.setattr(journey.os, "getuid", lambda: 503)
    def ps(argv, **kwargs):
        assert argv == ["ps", "-ww", "-axo", "uid=,pid=,command="]
        return ("501 10 python -m runtime_protocol start --root /other\n"
                "503 11 python -m runtime_protocol start --root /ours\n"
                "503 12 python unrelated\n")
    monkeypatch.setattr(journey.subprocess, "check_output", ps)
    assert journey.runtime_processes(tmp_path) == [11]
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 11})
    journey.assert_one_runtime(tmp_path)
    # Cleanup must still see a runner-owned process; it cannot accept a stale owner.
    assert journey.runtime_processes(tmp_path) != []


def test_self_hosted_raced_door_port_cannot_impersonate_owned_runtime(monkeypatch, tmp_path):
    _self_hosted_smoke_env(monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "discovery.json").write_text(json.dumps({"runtime_instance_id": "ours"}))
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, *args): return b'{"status":"ok","runtime_instance_id":"other"}'
    class Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            return Response()
    monkeypatch.setattr(journey.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(RuntimeError, match="does not match selected discovery"):
        journey.healthy(tmp_path)
    assert seen == ["http://127.0.0.1:49152/api/runtime/v1/health"]


def test_self_hosted_requires_nondefault_port(monkeypatch):
    _self_hosted_smoke_env(monkeypatch)
    for port in ("17340", "0", "65536", "nonsense"):
        monkeypatch.setenv("ASTRID_RUNTIME_DOOR_PORT", port)
        with pytest.raises(RuntimeError, match="door port"):
            journey.assert_runner_identity(journey.runner_identity())


def test_self_hosted_installed_generations_must_retain_smoke_port(monkeypatch):
    _self_hosted_smoke_env(monkeypatch)
    assert journey.verify_smoke_door_port({"env": {"ASTRID_RUNTIME_DOOR_PORT": "49152"}}) == "49152"
    with pytest.raises(RuntimeError, match="did not retain"):
        journey.verify_smoke_door_port({"env": {"ASTRID_RUNTIME_DOOR_PORT": "17340"}})


def test_macos_capture_preserves_launchagent_output_log(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    log_dir = tmp_path / "home" / "Library" / "Logs" / "Astrid Runtime"
    log_dir.mkdir(parents=True)
    (log_dir / "runtime-service.log").write_text("actual service failure")
    evidence = tmp_path / "evidence" / "release0-to-release1"
    evidence.mkdir(parents=True)
    journey.capture_runtime_service_logs(evidence, tmp_path, "before-cleanup")
    assert (evidence / "runtime-service-service-launchd.log").read_text() == "actual service failure"
