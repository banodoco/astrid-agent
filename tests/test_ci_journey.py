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


def test_direct_journey_workflow_is_dispatchable_and_bound_to_published_tuple():
    workflow = (journey.ROOT / ".github" / "workflows" / "journey-direct.yml").read_text()
    assert "workflow_dispatch: {}" in workflow
    assert "venue/M-QUAL-R-05-1" in workflow
    assert "contents: write" in workflow
    assert "fail-fast: false" in workflow
    for job_id, runner in (
        ("direct-linux", "ubuntu-24.04"),
        ("direct-macos", "macos-14"),
        ("direct-windows", "windows-2022"),
    ):
        assert f"job_id: {job_id}" in workflow
        assert f"runner: {runner}" in workflow
    for sha in (
        "47e5949465cb6f3afdfd1cb8a0e90009fd9d4020",
        "020877f12ea490308a9e5f9564c8f4a4bf77ede1",
        "9d3ccda1e81383082258943207745086da2aabe4",
        "89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4",
        "a63d9bdac6dfcb22e4f4516f2d92f4c880477076512b5115f0cd8f415f34a9ef",
    ):
        assert sha in workflow
    assert "before-cleanup/journey-result.json" in workflow
    assert "after-cleanup/journey-result.json" in workflow
    assert "journey-receipt-${{ matrix.job_id }}" in workflow


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


def test_tst_b1_named_evidence_copy_is_allowlisted_and_bounds_logs(tmp_path):
    evidence = tmp_path / "run" / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)
    (evidence / "journey-result.json").write_text('{"status":"pass"}\n', encoding="utf-8")
    (evidence / "install-bootstrap.log").write_bytes(b"x" * (journey.EVIDENCE_LOG_LIMIT + 17))
    (evidence / "unrelated-large-dump.bin").write_bytes(b"z" * 4096)

    journey.retain_named_evidence(evidence, "before-cleanup")

    retained = evidence.parent / "evidence-upload" / "before-cleanup"
    assert {path.name for path in retained.iterdir()} == {
        "journey-result.json", "install-bootstrap.log",
    }
    assert (retained / "install-bootstrap.log").stat().st_size == journey.EVIDENCE_LOG_LIMIT
    assert (retained / "journey-result.json").read_text(encoding="utf-8") == '{"status":"pass"}\n'


def test_tst_b1_runtime_service_logs_are_named_bounded_and_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    run_root = tmp_path / "run"
    log_dir = run_root / "home" / ".local" / "state" / "astrid-runtime" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "runtime.log").write_bytes(b"r" * (journey.EVIDENCE_LOG_LIMIT + 1))
    (log_dir / "service-worker.log").write_text("worker ready\n", encoding="utf-8")
    (log_dir / "unrelated.log").write_text("not a service log\n", encoding="utf-8")
    evidence = run_root / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)

    journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")
    journey.retain_named_evidence(evidence, "before-cleanup")

    retained = evidence.parent / "evidence-upload" / "before-cleanup"
    assert {path.name for path in retained.iterdir()} == {
        "runtime-service-runtime.log", "runtime-service-service-worker.log",
    }
    assert (retained / "runtime-service-runtime.log").stat().st_size == journey.EVIDENCE_LOG_LIMIT
    assert (retained / "runtime-service-service-worker.log").read_text(encoding="utf-8") == "worker ready\n"


def test_tst_b1_manager_state_capture_keeps_named_unit_and_reports_exceptions(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    data = tmp_path / "run" / "data"
    unit = data.parent / "home" / ".config" / "systemd" / "user" / "astrid.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nExecStart=/owned/runtime\n", encoding="utf-8")
    monkeypatch.setattr(journey.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("systemctl unavailable")))

    journey.capture_manager_state(tmp_path / "evidence", data, "before-cleanup")

    assert "systemctl unavailable" in capsys.readouterr().err
    state = journey.read_json(tmp_path / "evidence" / "manager-state-before-cleanup.json")
    assert state["error"] == "OSError: systemctl unavailable"
    assert (tmp_path / "evidence" / "unit-file-before-cleanup.txt").read_text(encoding="utf-8") == unit.read_text(encoding="utf-8")


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


def _linux_systemd_bridge_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    manager_home = tmp_path / "manager"
    directory = manager_home / ".config" / "systemd" / "user"
    run = tmp_path / "run"
    data = run / "data"
    unit = run / "home" / ".config" / "systemd" / "user" / "astrid.service"
    link = directory / "astrid.service"
    state = {"LoadState": "not-found", "FragmentPath": "", "ActiveState": "inactive"}
    def fake_run(argv, **kwargs):
        if "show-environment" in argv:
            output = f"HOME={manager_home}\n"
        elif "--property=UnitPath" in argv:
            output = f"UnitPath={directory} {tmp_path / 'vendor'}\n"
        elif "show" in argv:
            output = "".join(f"{key}={value}\n" for key, value in state.items())
        elif "daemon-reload" in argv:
            output = ""
        else:
            raise AssertionError(argv)
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    args = argparse.Namespace(job_id="direct-linux", mode="direct", retain_for_next_leg=False)
    return args, data, unit, link, state


def test_linux_systemd_bridge_records_paths_and_removes_only_owned_link(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    evidence = tmp_path / "evidence"
    with journey.linux_direct_unit_bridge(args, data, evidence):
        assert link.is_symlink() and os.readlink(link) == str(unit)
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/bin/true\n")
        state.update(LoadState="loaded", FragmentPath=str(unit), ActiveState="active")
        owner = journey.assert_direct_service_ownership(data.parent / "program", data)
        assert owner["fragment_path"] == str(unit)
        assert owner["load_state"] == "loaded" and owner["active_state"] == "active"
        unit.unlink()
        state.update(LoadState="not-found", FragmentPath="", ActiveState="inactive")
    record = journey.read_json(evidence / "systemd-manager.json")
    assert str(link.parent) in record["unit_path"]
    assert record["owned_unit"] == str(unit)
    assert record["observations"][0]["fragment_path"] == str(unit)
    assert len(record["observations"][0]["unit_sha256"]) == 64
    assert record["cleanup"] == "pass" and record["after_cleanup"]["LoadState"] == "not-found"
    assert not os.path.lexists(link)


def test_linux_systemd_bridge_rejects_existing_or_dangling_unit(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    link.parent.mkdir(parents=True)
    link.symlink_to(tmp_path / "foreign")
    with pytest.raises(RuntimeError, match="already exists"):
        with journey.linux_direct_unit_bridge(args, data, tmp_path / "evidence"):
            pytest.fail("existing units must be refused")
    assert link.is_symlink() and os.readlink(link) == str(tmp_path / "foreign")


def test_linux_systemd_bridge_refuses_to_remove_replaced_link(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="no longer owned"):
        with journey.linux_direct_unit_bridge(args, data, tmp_path / "evidence"):
            link.unlink()
            link.symlink_to(tmp_path / "foreign")
    assert link.is_symlink() and os.readlink(link) == str(tmp_path / "foreign")
    assert journey.read_json(tmp_path / "evidence" / "systemd-manager.json")["cleanup"] == "fail"


def test_linux_systemd_bridge_preserves_link_if_owned_unit_remains(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="remains after uninstall"):
        with journey.linux_direct_unit_bridge(args, data, tmp_path / "evidence"):
            unit.parent.mkdir(parents=True)
            unit.write_text("owned")
    assert link.is_symlink() and unit.is_file()


def test_linux_service_ownership_rejects_foreign_fragment(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    unit.parent.mkdir(parents=True)
    unit.write_text("owned")
    state.update(LoadState="loaded", FragmentPath=str(tmp_path / "foreign"), ActiveState="active")
    with pytest.raises(RuntimeError, match="not owned"):
        journey.assert_direct_service_ownership(data.parent / "program", data)


def test_linux_systemd_bridge_rejects_manager_without_config_unit_path(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    original = journey.subprocess.run
    def no_config_path(argv, **kwargs):
        if "--property=UnitPath" in argv:
            return SimpleNamespace(returncode=0, stdout=f"UnitPath={tmp_path / 'vendor'}\n", stderr="")
        return original(argv, **kwargs)
    monkeypatch.setattr(journey.subprocess, "run", no_config_path)
    with pytest.raises(RuntimeError, match="not a safe UnitPath"):
        with journey.linux_direct_unit_bridge(args, data, tmp_path / "evidence"):
            pytest.fail("must not modify an undiscoverable unit path")
    assert not os.path.lexists(link)


def test_linux_systemd_bridge_rejects_preloaded_unit(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    state.update(LoadState="loaded", FragmentPath=str(tmp_path / "foreign"), ActiveState="inactive")
    with pytest.raises(RuntimeError, match="already loaded"):
        with journey.linux_direct_unit_bridge(args, data, tmp_path / "evidence"):
            pytest.fail("must not replace a loaded unit")
    assert not os.path.lexists(link)


def test_linux_service_ownership_requires_exact_fragment_even_for_symlink(tmp_path, monkeypatch):
    args, data, unit, link, state = _linux_systemd_bridge_fixture(tmp_path, monkeypatch)
    unit.parent.mkdir(parents=True)
    unit.write_text("owned")
    link.parent.mkdir(parents=True)
    link.symlink_to(unit)
    state.update(LoadState="loaded", FragmentPath=str(link), ActiveState="active")
    with pytest.raises(RuntimeError, match="not owned"):
        journey.assert_direct_service_ownership(data.parent / "program", data)


def test_direct_evidence_stages_nested_bootstraps_and_installer_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    run = tmp_path / "run"
    evidence = run / "evidence" / "release0-to-release1"
    for relative in ("release0/release0-bootstrap.log", "release1/update-bootstrap.log"):
        path = evidence / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    log_dir = run / "home" / ".local" / "state" / "astrid-runtime" / "logs"
    log_dir.mkdir(parents=True)
    for name in ("install.log", "runtime-service.log"):
        (log_dir / name).write_text(name)
    journey.capture_runtime_service_logs(evidence, run, "before-cleanup")
    journey.retain_named_evidence(evidence, "before-cleanup")
    retained = evidence.parent / "evidence-upload" / "before-cleanup"
    for relative in ("release0/release0-bootstrap.log", "release1/update-bootstrap.log", "install.log", "runtime-service.log"):
        assert (retained / relative).read_text() == relative.rsplit("/", 1)[-1] or (retained / relative).read_text() == relative


def test_runpod_linux_records_its_own_provenance(monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", "runpod-cpu")
    for name in ("RUNNER_OS", "ImageOS", "ImageVersion"):
        monkeypatch.delenv(name, raising=False)
    identity = journey.runner_identity()
    journey.assert_runner_identity(identity)
    assert identity["runner_provenance"]["venue"] == "runpod-cpu"
    assert "ImageOS" not in identity["runner_provenance"]
