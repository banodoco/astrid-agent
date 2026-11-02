"""TST-B1: focused custody and result-contract tests for named installed journeys."""

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import ci_journey as journey


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
    assert not (run_root / "evidence" / "evidence-upload" / "before-cleanup" / "unrelated-secret.txt").exists()


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
        return SimpleNamespace(returncode=1, stdout="partial query output", stderr="query module error")
    monkeypatch.setattr(windows.subprocess, "run", fake_run)
    monkeypatch.setenv("PSModulePath", "untrusted")
    with pytest.raises(RuntimeError, match="query exit 1"):
        windows.scheduled_task_preflight()
    record = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
    assert record["query"] == {"argv": calls[1][0], "stdout": "partial query output",
                               "stderr": "query module error", "exit_code": 1, "classification": "error"}
    assert windows.SCHEDULED_TASK_QUERY in record["query"]["argv"][-1]
    assert "PSVersion" in record["origin"]["stdout"] and "ModulePath" in record["origin"]["stdout"]
    assert all(not any(key.casefold() == "psmodulepath" for key in call[1]["env"]) for call in calls)
    assert "CmdletizationQuery_NotFound_TaskName," in windows.SCHEDULED_TASK_QUERY
    assert "else { throw }" in windows.SCHEDULED_TASK_QUERY
    journey.retain_named_evidence(tmp_path, "before-cleanup")
    assert (tmp_path.parent / "evidence-upload" / "before-cleanup" / "preflight-scheduled-task.txt").is_file()


def test_windows_preflight_absence_and_foreign_owner_rejection(tmp_path, monkeypatch):
    from scripts import ci_windows_journey as windows
    monkeypatch.setattr(windows, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(windows.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    assert windows.scheduled_task_preflight() == ""
    record = json.loads((windows.EVIDENCE / "preflight-scheduled-task.txt").read_text())
    assert record["query"]["classification"] == "not-found"
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "program_root", lambda: tmp_path / "empty" / "astrid")
    monkeypatch.setattr(journey, "runtime_processes", lambda *a: [])
    monkeypatch.setattr(windows, "scheduled_task_preflight", lambda: 'present\nC:\\other\\runtime.exe\n')
    with pytest.raises(RuntimeError, match="outside this journey"):
        journey.assert_host_available("fresh-install", tmp_path / "program", tmp_path / "data")


def test_linux_manager_boundary_probes_the_isolated_unit_path(tmp_path):
    import subprocess
    script = (journey.ROOT / ".github/workflows/journey-linux-manager.sh").read_text()
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
    with pytest.raises(RuntimeError, match="origin exit None"):
        windows.scheduled_task_preflight()
    record = json.loads((tmp_path / "preflight-scheduled-task.txt").read_text())
    assert calls[0]["query"]["argv"][-1].endswith(windows.SCHEDULED_TASK_QUERY)
    assert "powershell unavailable" in calls[1]["origin"]["error"]
    assert record["query"]["classification"] == "error"
    assert "unexpected capture failure" in record["query"]["error"]
