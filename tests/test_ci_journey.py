"""TST-B1: focused custody and result-contract tests for named installed journeys."""

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey
from scripts import journey_matrix, journey_release2
from scripts import journey_matrix as matrix


def test_macos_fault_inventory_preserves_existing_journeys():
    rows = journey_matrix.RELEASE1_MATRIX
    ids = [row["job_id"] for row in rows]
    assert len(ids) == len(set(ids))
    assert {row["job_id"] for row in rows if row.get("leg") == "release0-to-release1"} == {
        "direct-macos", "direct-linux", "direct-windows"
    }
    assert {(row["mode"], row["os"]) for row in rows if row["mode"].startswith("pinokio-")} == {
        (mode, os_name) for mode in ("pinokio-backend", "pinokio-fully-local")
        for os_name in ("macos", "linux", "windows")
    }
    assert journey_matrix.M4_5_MATRIX == journey_release2.RELEASE2_SCENARIOS
    assert journey_matrix.M4_5_MATRIX


@pytest.mark.parametrize(
    "leg, steps, evidence",
    (
        ("interrupted-update", ("baseline", "interrupt", "recover", "verify"),
         {"interruption_and_same_operation_terminal_journal", "installed_identity_after_recovery"}),
        ("rollback", ("baseline", "fail_candidate", "verify"),
         {"authenticated_selected_release0_predecessor_identity",
          "candidate_failure_and_rollback_transaction_journal", "restored_predecessor_identity"}),
    ),
)
def test_macos_fault_inventory_binds_shared_helper_and_preservation(leg, steps, evidence):
    job_id = f"direct-macos-{leg}"
    row = journey_matrix.release1_job(job_id)
    assert row["scenario_id"] == job_id
    assert (row["mode"], row["os"], row["runner"], row["shell"], row["manager"]) == (
        "direct", "macos", "macos-14", "sh", "launchd"
    )
    assert row["leg"] == leg
    assert row["scenario"] == leg
    assert row["executor"] == "failure_mode_executor.execute_installed_fault"
    assert row["steps"] == steps
    assert evidence | {
        "release0_fixture_and_installed_identity", "populated_data_before_and_after",
        "sentinel_identity_and_survival", "launchd_ownership_health_and_one_runtime",
        "per_step_command_and_inner_exit", "scenario_tuple_runner_and_evidence_file_hashes",
    } <= set(row["required_evidence"])


@pytest.mark.parametrize(
    ("leg", "steps", "specific_evidence"),
    (
        ("interrupted-update", ("baseline", "interrupt", "recover", "verify"),
         {"interruption_and_same_operation_terminal_journal", "installed_identity_after_recovery"}),
        ("rollback", ("baseline", "fail_candidate", "verify"),
         {"authenticated_selected_release0_predecessor_identity",
          "candidate_failure_and_rollback_transaction_journal", "restored_predecessor_identity"}),
    ),
)
def test_windows_direct_fault_inventory(leg, steps, specific_evidence):
    job_id = f"direct-windows-{leg}"
    rows = [row for row in matrix.RELEASE1_MATRIX if row["job_id"] == job_id]
    assert len(rows) == 1
    row = matrix.validate_job(job_id, "direct")
    assert {key: row[key] for key in ("scenario_id", "os", "runner", "shell", "manager", "leg", "scenario")} == {
        "scenario_id": job_id, "os": "windows", "runner": "windows-2022",
        "shell": "powershell.exe", "manager": "task-scheduler", "leg": leg, "scenario": leg,
    }
    assert row["executor"] == "failure_mode_executor.execute_installed_fault"
    assert row["steps"] == steps
    assert set(row["required_evidence"]) >= specific_evidence | {
        "release0_fixture_and_installed_identity", "populated_data_before_and_after",
        "sentinel_identity_and_survival", "task_scheduler_ownership_health_and_one_runtime",
        "keep_data_and_task_scheduler_process_cleanup", "per_step_command_and_inner_exit",
        "scenario_tuple_runner_and_evidence_file_hashes",
    }


def test_existing_release1_and_release2_inventory_remains_available():
    for os_name in ("macos", "linux", "windows"):
        assert matrix.release1_job(f"direct-{os_name}")["leg"] == "release0-to-release1"
        for mode in ("pinokio-backend", "pinokio-fully-local"):
            assert matrix.release1_job(f"{mode}-{os_name}")["mode"] == mode
        assert {row["mode"] for row in matrix.M4_5_MATRIX if row["os"] == os_name} == {
            "direct", "pinokio-backend", "pinokio-fully-local",
        }


def test_installed_app_update_guard_inventory_has_one_linux_row():
    row = journey_matrix.APP_UPDATE_GUARD_SCENARIO

    assert row["scenario_id"] == "installed-app-update-guard-linux"
    assert (row["os"], row["runner"], row["shell"], row["evidence_class"]) == (
        "linux", "ubuntu-24.04", "sh", "installed_app"
    )
    assert row["phases"] == (
        {"save_state": "unresolved", "expected_update_post_count": 0},
        {"save_state": "settled", "expected_update_post_count": 1},
    )


def test_existing_release1_and_release2_inventory_remains_present():
    original_ids = {
        f"{mode}-{os_name}"
        for mode in ("direct", "pinokio-backend", "pinokio-fully-local")
        for os_name in ("macos", "linux", "windows")
    }
    jobs = [row for row in journey_matrix.RELEASE1_MATRIX if row["job_id"] in original_ids]
    assert {(row["mode"], row["os"]) for row in jobs} == {
        (mode, os_name)
        for mode in ("direct", "pinokio-backend", "pinokio-fully-local")
        for os_name in ("macos", "linux", "windows")
    }
    assert len(jobs) == 9
    assert all(row["leg"] == "release0-to-release1" for row in jobs if row["mode"] == "direct")
    release2 = journey_matrix.M4_5_MATRIX
    assert len(release2) == 12
    assert len({row["scenario_id"] for row in release2}) == 12
    assert all(row["scenario"] == "release1-to-release2" for row in release2)


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
    assert workflow.count("repository: banodoco/astrid-app") == 3
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
