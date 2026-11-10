"""TST-B1: focused custody and result-contract tests for named installed journeys."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
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


def test_direct_windows_failure_snapshot_prints_recursive_logs_and_matching_processes(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    evidence = tmp_path / "evidence" / "direct-windows"
    evidence.mkdir(parents=True)
    run_root = tmp_path / "astrid-r1-direct-windows"
    journey.write_json(evidence / "journey-result.json", {
        "job_id": "direct-windows", "mode": "direct", "status": "fail",
        "scenario": "fresh-install",
    })
    calls = []

    def capture(code, name, *, show, timeout):
        calls.append((code, name, show, timeout))
        return "captured log tail and process row\n"

    monkeypatch.setattr(journey.windows, "powershell", capture)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    monkeypatch.setattr(journey, "retain_named_evidence", lambda *unused: None)
    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "before-cleanup")

    output = capsys.readouterr().out
    assert "captured log tail and process row" in output
    assert len(calls) == 1
    code, name, show, timeout = calls[0]
    assert str(run_root / "localappdata" / "Astrid Runtime" / "logs") in code
    assert "-Filter '*.log' -File -Recurse -Force" in code
    assert "Get-Content -LiteralPath $log.FullName -Tail 120" in code
    assert "CommandLine -like '*astrid-r1-direct-windows*'" in code
    assert "Select-Object ProcessId, ParentProcessId, CommandLine" in code
    assert (name, show, timeout) == ("direct-windows-failure-diagnostics.txt", False, 60)

    journey.write_json(evidence / "journey-result.json", {
        "job_id": "direct-windows", "mode": "direct", "status": "pass",
        "scenario": "fresh-install",
    })
    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "before-cleanup")
    assert len(calls) == 1


def test_task_reports_sdk_error_code_before_empty_task_refs(tmp_path, monkeypatch):
    evidence = tmp_path / "release1-working-task"
    evidence.mkdir()
    monkeypatch.setattr(journey, "install_journey_packs", lambda run_cli, **kwargs: None)
    monkeypatch.setattr(
        journey, "installed_cli_with_capture",
        lambda *args, **kwargs: json.dumps({"ok": False, "error": {"code": "not_found"}}),
    )

    with pytest.raises(RuntimeError, match="invocation failed: not_found"):
        journey.task({}, evidence, "fresh-project", create_project=False)


def test_journey_packs_install_from_the_pinned_source_through_packs_add():
    assert journey.JOURNEY_PACKS == {"chiptune": (
        "https://github.com/banodoco/poms_packs", "2379ab54aca522019ff4f869cfdf9375bdb56dfc")}
    calls = []

    def run_cli(argv, name):
        calls.append(argv)
        return json.dumps({"packs": [{"id": "other"}]})

    journey.install_journey_packs(run_cli)
    assert calls == [
        ["packs", "list", "--json"],
        ["packs", "add", "https://github.com/banodoco/poms_packs", "--revision",
         "2379ab54aca522019ff4f869cfdf9375bdb56dfc", "--pack-id", "chiptune",
         "--subpath", "chiptune", "--yes"],
    ]
    calls.clear()
    journey.install_journey_packs(lambda argv, name: (calls.append(argv), json.dumps(
        {"packs": [{"id": "chiptune"}]}))[1])
    assert calls == [["packs", "list", "--json"]]


def test_journey_packs_run_through_the_console_entry_point(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(journey, "run", lambda argv, log, **kw: seen.append(argv) or "")
    version = {"python": "py", "env": {}}
    journey.installed_cli(version, ["packs", "list", "--json"], tmp_path, "journey-packs-list.json",
                          console=True)
    journey.installed_cli(version, ["tasks", "show"], tmp_path, "task.json")
    assert seen[0] == ["py", "-I", "-c", journey.CONSOLE_ENTRY, "packs", "list", "--json"]
    assert "from astrid.console import main" in journey.CONSOLE_ENTRY
    assert seen[1] == ["py", "-I", "-m", "astrid", "tasks", "show"]


# astrid/console.py at R9 Agent 709492fa5, verbatim: no `packs` branch.
R9_CONSOLE = '''"""Installed console dispatch, with a lightweight engine route."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    rest = list(sys.argv[1:] if argv is None else argv)
    if rest and rest[0] == "h3":
        from .packs.h3_av.cli import main as h3_main

        return h3_main(rest[1:])

    if rest and rest[0] == "generate":
        from .core.gateway import main as gateway_main

        return gateway_main(rest)
    if rest and rest[0] == "engines":
        from .core.engine_cli import main as engines_main

        return engines_main(rest[1:])
    if rest and rest[0] == "action":
        from .core.action_cli import main as action_main

        return action_main(rest[1:])
    from .omp_agent import main as omp_main

    return omp_main(rest)
'''


def test_console_packs_route_detection_covers_old_releases():
    # R0 fixture Agent 3204bb00: no astrid/console.py at all.
    assert journey.console_routes_packs(None) is False
    assert journey.console_routes_packs("") is False
    # R9: a console module without a packs branch.
    assert journey.console_routes_packs(R9_CONSOLE) is False
    # Current release: the console routes packs.
    current = (journey.ROOT / "astrid" / "console.py").read_text(encoding="utf-8")
    assert journey.console_routes_packs(current) is True


def test_installed_routes_packs_reads_the_installed_console(tmp_path, monkeypatch):
    origins = {"value": ""}
    monkeypatch.setattr(journey, "_installed_probe", lambda *a, **k: origins["value"])
    assert journey.installed_routes_packs({}, tmp_path) is False
    console = tmp_path / "console.py"
    console.write_text(R9_CONSOLE, encoding="utf-8")
    origins["value"] = str(console)
    assert journey.installed_routes_packs({}, tmp_path) is False
    console.write_text('if rest and rest[0] == "packs":\n    pass\n', encoding="utf-8")
    assert journey.installed_routes_packs({}, tmp_path) is True


def test_journey_packs_skip_without_a_packs_route_and_wait_for_registration():
    calls = []
    journey.install_journey_packs(lambda argv, name: calls.append(argv), routes_packs=lambda: False,
                                  wait_registered=calls.append)
    assert calls == []

    def run_cli(argv, name):
        calls.append(argv)
        return json.dumps({"packs": []})

    journey.install_journey_packs(run_cli, routes_packs=lambda: True, wait_registered=calls.append)
    assert [call[:2] if isinstance(call, list) else call for call in calls] == [
        ["packs", "list"], ["packs", "add"], "chiptune"]
    calls.clear()
    journey.install_journey_packs(lambda argv, name: (calls.append(argv), json.dumps(
        {"packs": [{"id": "chiptune"}]}))[1], routes_packs=lambda: True, wait_registered=calls.append)
    assert calls == [["packs", "list", "--json"]]


def test_wait_pack_registered_polls_the_refresh_outbox(tmp_path, monkeypatch):
    answers = iter(["pending", "pending", "registered"])
    seen = []
    monkeypatch.setattr(journey, "_installed_probe",
                        lambda version, code, evidence, name: seen.append(name) or next(answers))
    journey.wait_pack_registered({}, tmp_path, "chiptune", timeout=60, interval=0)
    assert seen == ["journey-pack-refresh.log"] * 3
    assert "refresh_pending" in journey.PACK_REFRESH_PROBE
    monkeypatch.setattr(journey, "_installed_probe", lambda *a, **k: "pending")
    with pytest.raises(RuntimeError, match="did not register chiptune .*still pending"):
        journey.wait_pack_registered({}, tmp_path, "chiptune", timeout=0, interval=0)


def test_invocation_outcome_decodes_omp_invoke_content_text():
    failed = {"content": [{"type": "text", "text": json.dumps(
        {"ok": False, "error": {"code": "capability_not_found", "message": "chiptune.sfx"}, "result": {}})}],
        "details": {"astridTaskRefs": []}}
    assert journey.invocation_outcome(failed) == (
        False, {"code": "capability_not_found", "message": "chiptune.sfx"})
    assert journey._invocation_failure(journey.invocation_outcome(failed)[1]) == (
        "capability_not_found: chiptune.sfx")
    raised = {"content": [{"type": "text", "text": "Astrid is not running"}], "details": {"astridTaskRefs": []}}
    assert journey.invocation_outcome(raised) == (None, "Astrid is not running")
    assert journey.invocation_outcome({"content": 3}) == (None, None)


def test_task_reports_the_decoded_admission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "install_journey_packs", lambda run_cli, **kwargs: None)
    response = {"content": [{"type": "text", "text": json.dumps(
        {"ok": False, "error": {"code": "capability_not_found"}, "result": {}})}],
        "details": {"astridTaskRefs": []}}
    monkeypatch.setattr(journey, "installed_cli_with_capture", lambda *a, **k: json.dumps(response))
    with pytest.raises(RuntimeError, match="invocation failed: capability_not_found"):
        journey.task({}, tmp_path, "fresh-project", create_project=False)

    def exits_one(version, argv, evidence, name, **kwargs):
        (evidence / name).write_text(json.dumps(
            {"content": [{"type": "text", "text": "Astrid is not running"}],
             "details": {"astridTaskRefs": []}}), encoding="utf-8")
        raise RuntimeError(f"{name}: exit 1; see {evidence / name}")

    monkeypatch.setattr(journey, "installed_cli_with_capture", exits_one)
    with pytest.raises(RuntimeError, match="invocation failed: Astrid is not running"):
        journey.task({}, tmp_path, "fresh-project", create_project=False)


def test_journey_pack_evidence_is_staged_for_the_failure_upload(tmp_path):
    evidence = tmp_path / "work" / "evidence"
    evidence.mkdir(parents=True)
    for name in ("journey-packs-list.json", "journey-pack-chiptune.log", "journey-pack-x.txt"):
        (evidence / name).write_text("stderr", encoding="utf-8")
    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = tmp_path / "evidence-upload" / "before-cleanup"
    assert (staged / "journey-packs-list.json").is_file()
    assert (staged / "journey-pack-chiptune.log").read_text(encoding="utf-8") == "stderr"
    assert not (staged / "journey-pack-x.txt").exists()


def test_tst_b1_production_uses_shipped_manifest_bytes_without_source_override(tmp_path):
    app, shipped = app_checkout(tmp_path)
    original = shipped.read_bytes()
    selected, manifest = journey.selected_manifest(app, source_override=False, agent=None,
                                                   runtime=None, evidence=tmp_path / "evidence")
    assert selected == shipped
    assert selected.read_bytes() == original
    assert manifest["runtime"]["schema_digest"] == "digest"
    assert not (tmp_path / "evidence").exists()



def test_wait_ready_timeout_preserves_last_health_error(tmp_path, monkeypatch):
    def unhealthy(_data):
        raise RuntimeError("discovery endpoint unavailable")

    monkeypatch.setattr(journey, "healthy", unhealthy)
    monkeypatch.setattr(journey.time, "monotonic", lambda: 0)
    monkeypatch.setattr(journey.time, "sleep", lambda _seconds: None)

    with pytest.raises(
            RuntimeError,
            match="last health check failed: RuntimeError: discovery endpoint unavailable") as exc:
        journey.wait_ready(tmp_path, timeout=0)

    assert isinstance(exc.value.__cause__, RuntimeError)
    assert str(exc.value.__cause__) == "discovery endpoint unavailable"


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
    release0_manifest = tmp_path / "release9-manifest.json"
    release9_sources = {"agent": {"sha": jumps.RELEASE9_IDENTITY["agent"]},
                        "runtime": {"sha": jumps.RELEASE9_IDENTITY["runtime"]}}
    journey.write_json(release0_manifest, {"release": 9, **release9_sources, "tools": {}})
    journey.write_json(version_root / "manifest.json", {"release": 9, **release9_sources, "tools": {}})
    app = tmp_path / "app"
    app.mkdir()
    target_manifest = {
        "release": 10, "agent": {"sha": "3" * 40},
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
        run_root=run_root, root=root, data_root=data, leg="release9-then-install", mode="direct",
        shell="sh", job_id=None, source_override=False, app_checkout=app, candidate=candidate_path,
        candidate_agent_checkout=tmp_path / "agent", candidate_runtime_checkout=tmp_path / "runtime",
        pinokio_checkout=tmp_path / "pinokio", release9_app_checkout=tmp_path / "release9-app",
        allow_release0_to_release1=True, diagnostic_start_at="release0-api-seed",
        diagnostic_state=run_root / jumps.DIAGNOSTIC_STATE_NAME,
    )
    fixture_receipt = {"fixture_sums_sha256": "8" * 64, "manifest_sha256": "9" * 64}
    checkouts = {name: {"sha": sha, "repo": str(tmp_path / name)} for name, sha in {
        "app": "5" * 40, "agent": "3" * 40, "runtime": "4" * 40, "pinokio": "6" * 40,
    }.items()}
    monkeypatch.setattr(jumps, "release9_admission", lambda unused, output: (
        tmp_path / "source", release0_manifest, fixture_receipt))
    monkeypatch.setattr(journey, "selected_manifest", lambda *unused, **kwargs: (
        tmp_path / "selected-manifest.json", target_manifest))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *unused: target_manifest)
    monkeypatch.setattr(journey, "candidate_checkouts", lambda unused: checkouts)
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *unused: {"manager": "test"})
    monkeypatch.setattr(journey, "healthy", lambda *unused: {"pid": 42, "runtime_instance_id": "r0"})
    version_path, _, version = journey.installation(root, data)
    args.candidate_checkouts = checkouts
    if write_state:
        jumps.write_diagnostic_release0_preconditions(
            args, root, data, fixture_receipt=fixture_receipt, release0_manifest=release0_manifest,
            target_manifest=target_manifest, candidate=candidate, version_path=version_path, version=version,
        )
    evidence = run_root / "evidence" / "release9-then-install-diagnostic-release0-api-seed"
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
    monkeypatch.setattr(jumps, "live_workspace", lambda *unused: calls.append(unused))
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
    def workspace(_root, _data, _evidence, action, request):
        calls.append(action)
        if action == "seed":
            assert request == {"project": "tst-m4-3b-release0", "generation": "release0",
                               "task_id": None, "create_project": True}
            return {"project": "tst-m4-3b-release0"}
        assert action == "snapshot"
        assert request == {"project": "tst-m4-3b-release0", "task_id": "task-1"}
        return {"snapshot": "ok"}
    monkeypatch.setattr(jumps, "live_workspace", workspace)
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
    base = ["--leg", "release9-then-install", "--mode", "direct", "--app-checkout", str(tmp_path),
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
    # Five: the candidate App, the frozen App candidate checkouts, and the pinned Release 9 App.
    assert workflow.count("repository: banodoco/astrid-app") == 5
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


def test_tst_b1_windows_refusal_accepts_only_its_prior_uninstalled_fresh_selection(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    default = tmp_path / "default" / "Astrid"
    default.parent.mkdir()
    monkeypatch.setattr(journey, "program_root", lambda: default)
    monkeypatch.setattr(journey.windows, "scheduled_task_diagnostics", lambda **unused: {
        "status": "absent", "tasks": [],
    })
    monkeypatch.setattr(journey, "runtime_processes", lambda *unused: [])
    prior_program, prior_data = tmp_path / "fresh" / "program", tmp_path / "fresh" / "data"
    refusal_program, refusal_data = tmp_path / "refusal" / "program", tmp_path / "refusal" / "data"
    selection = default.parent / "astrid-install.json"
    receipt = {"owner": "haus.astrid.runtime", "program_root": str(prior_program),
               "data_root": str(prior_data), "uninstalled": True}
    journey.write_json(selection, receipt)

    # The caller maps windows-upgrade-refusal to host_leg "fresh-install" (VERSION_JUMP_LEGS).
    with pytest.raises(RuntimeError, match="non-journey Astrid login installation"):
        journey.assert_host_available("fresh-install", refusal_program, refusal_data,
                                      skip_process_check=True)
    journey.assert_host_available(
        "fresh-install", refusal_program, refusal_data, skip_process_check=True,
        related_uninstalled_selection=(prior_program, prior_data),
    )
    for changed in ({**receipt, "uninstalled": False},
                    {**receipt, "program_root": str(tmp_path / "foreign" / "program")},
                    {**receipt, "foreground": {"pid": 123}}):
        journey.write_json(selection, changed)
        with pytest.raises(RuntimeError, match="non-journey Astrid login installation"):
            journey.assert_host_available(
                "fresh-install", refusal_program, refusal_data, skip_process_check=True,
                related_uninstalled_selection=(prior_program, prior_data),
            )


def test_windows_refusal_cleanup_reads_only_its_isolated_r0_receipt(tmp_path, monkeypatch):
    """The refusal leg's cleanup runs under its own LOCALAPPDATA, not the fresh leg's.

    Run 42406972807 left R0's live record at the private path (migrated by the
    candidate before it refused), so R0's uninstall wrote no receipt and the
    guard correctly reported a non-journey installation.
    """
    monkeypatch.setattr(journey.sys, "platform", "win32")
    default = tmp_path / "localappdata" / "Astrid"
    default.parent.mkdir()
    monkeypatch.setattr(journey, "program_root", lambda: default)
    monkeypatch.setattr(journey.windows, "scheduled_task_diagnostics", lambda **unused: {
        "status": "absent", "tasks": [],
    })
    root, data = tmp_path / "program", tmp_path / "data"
    record = {"owner": "haus.astrid.runtime", "program_root": str(root), "data_root": str(data)}
    legacy = default.parent / "astrid-install.json"
    private = default.parent / "Astrid Installer" / "astrid-install.json"

    journey.write_json(legacy, {**record, "uninstalled": True})
    journey.assert_host_available("cleanup", root, data, skip_process_check=True)

    legacy.unlink()
    journey.write_json(private, record)
    with pytest.raises(RuntimeError, match="non-journey Astrid login installation"):
        journey.assert_host_available("cleanup", root, data, skip_process_check=True)
    assert journey.windows_login_selection_diagnostic(root, data)["selection_location"] == "private"
    assert journey.windows_login_selection_diagnostic(root, data)["selection_uninstalled"] is False


@pytest.mark.windows_native
def test_same_path_matches_windows_spelling_variants(tmp_path):
    assert journey._same_path(r"C:\A\B", "c:/a/b/")


def test_same_path_matches_normalized_identity_and_rejects_others(tmp_path):
    base = tmp_path / "a" / "b"
    assert journey._same_path(str(base) + os.sep, base)
    assert journey._same_path(str(tmp_path / "a" / ".." / "a" / "b"), base)
    assert journey._same_path(base, str(base))
    assert not journey._same_path(tmp_path / "a", base)
    assert not journey._same_path(None, base)
    assert not journey._same_path("", base)
    assert not journey._same_path(base, "")
    assert not journey._same_path(42, base)


def test_same_path_falls_back_to_samefile_for_hard_links(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    alias = tmp_path / "alias.json"
    os.link(real, alias)
    assert journey._same_path(alias, real)
    assert not journey._same_path(tmp_path / "missing.json", real)

def test_release1_cleanup_host_check_accepts_only_owned_retained_r1_install(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "linux")
    default = tmp_path / "default" / "astrid"
    default.parent.mkdir()
    monkeypatch.setattr(journey, "program_root", lambda: default)
    root, data = tmp_path / "run" / "program", tmp_path / "run" / "data"
    r1_data = Path(str(data) + "-r1")
    version_root = root / "versions" / "r1"
    version_root.mkdir(parents=True)
    r1_data.mkdir(parents=True)
    (root / "current").write_text("r1\n", encoding="utf-8")
    journey.write_json(root / "install.json", {
        "data_root": str(data), "r1_data_root": str(r1_data),
    })
    identity = {"release": 1, "managed_by": None}
    journey.write_json(version_root / "version.json", {
        "root": str(version_root), "identity": identity,
    })
    journey.write_json(version_root / "manifest.json", {"release": 1})
    selection = {
        "owner": "haus.astrid.runtime", "program_root": str(root),
        "data_root": str(data), "uninstalled": False, "identity": identity,
    }
    selection_path = default.parent / "astrid-install.json"
    journey.write_json(selection_path, selection)
    service_checks = []
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda checked_root, checked_data, *, release: service_checks.append(
                            (checked_root, checked_data, release)))

    journey.assert_host_available("cleanup", root, data, release=1)
    assert service_checks == [(root, data, 1)]

    for changed in ({**selection, "program_root": str(tmp_path / "foreign")},
                    {**selection, "uninstalled": True},
                    {**selection, "foreground": {"pid": 123}}):
        journey.write_json(selection_path, changed)
        with pytest.raises(RuntimeError, match="non-journey Astrid login installation|selection is not owned"):
            journey.assert_host_available("cleanup", root, data, release=1)
    assert service_checks == [(root, data, 1)]


def test_cleanup_host_check_accepts_a_normalised_handoff_updated_past_release1(tmp_path, monkeypatch):
    """After UR-15 the candidate normalises the storage move: R1 is data_root, R0 retained."""
    monkeypatch.setattr(journey.sys, "platform", "linux")
    default = tmp_path / "default" / "astrid"
    default.parent.mkdir()
    monkeypatch.setattr(journey, "program_root", lambda: default)
    root, data = tmp_path / "run" / "program", tmp_path / "run" / "data"
    r1_data = Path(str(data) + "-r1")
    version_root = root / "versions" / "r11"
    version_root.mkdir(parents=True)
    r1_data.mkdir(parents=True)
    data.mkdir(parents=True)
    (root / "current").write_text("r11\n", encoding="utf-8")
    journey.write_json(root / "install.json", {"data_root": str(r1_data), "retained_r0_data_root": str(data)})
    identity = {"release": 11, "managed_by": None}
    journey.write_json(version_root / "version.json", {"root": str(version_root), "identity": identity})
    journey.write_json(version_root / "manifest.json", {"release": 11})
    selection = {"owner": "haus.astrid.runtime", "program_root": str(root), "data_root": str(r1_data),
                 "retained_r0_data_root": str(data), "uninstalled": False, "identity": identity}
    journey.write_json(default.parent / "astrid-install.json", selection)
    checks = []
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda checked_root, checked_data, *, release: checks.append((checked_root, checked_data, release)))
    journey.assert_host_available("cleanup", root, data, release=1)
    assert checks == [(root, data, 1)]
    assert journey.installation(root, data)[1] == data
    journey.write_json(default.parent / "astrid-install.json", {**selection, "retained_r0_data_root": "/other"})
    with pytest.raises(RuntimeError, match="non-journey Astrid login installation|selection is not owned"):
        journey.assert_host_available("cleanup", root, data, release=1)


def test_tst_b1_windows_manager_action_must_be_inside_exact_owned_root():
    root = Path(r"C:\runner\tst-b1\program")
    assert journey.windows_task_owned('present\nC:\\runner\\tst-b1\\program\\versions\\v1\\python.exe\n', root)
    assert not journey.windows_task_owned('present\nC:\\runner\\tst-b1\\program-other\\python.exe\n', root)
    assert not journey.windows_task_owned('present\nC:\\Users\\someone\\Astrid\\python.exe\n', root)


@pytest.mark.parametrize(("output", "expected"), [
    ('"haus.astrid.runtime" => true', True),
    ('\t"haus.astrid.runtime" => disabled', True),
    ('"haus.astrid.runtime" => false', False),
    ('"haus.astrid.runtime" => enabled', False),
    ('"haus.astrid.runtime.r1" => disabled', False),
])
def test_launchd_disabled_state_matches_only_exact_service_and_disabled_values(output, expected):
    assert journey.launchd_service_is_disabled(output, "haus.astrid.runtime") is expected


def test_windows_login_selection_diagnostic_is_classified_and_contains_no_install_values(tmp_path, monkeypatch):
    default = tmp_path / "profile" / "Local" / "Astrid"
    default.parent.mkdir(parents=True)
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "program_root", lambda: default)
    selection = default.parent / "astrid-install.json"
    selection.write_text(json.dumps({
        "owner": "haus.astrid.runtime",
        "program_root": str(tmp_path / "foreign-program"),
        "data_root": str(tmp_path / "foreign-data"),
        "uninstalled": True,
        "foreground": {"pid": 12345},
    }))

    diagnostic = journey.windows_login_selection_diagnostic(
        tmp_path / "journey-program", tmp_path / "journey-data")

    assert diagnostic == {
        "default_install_marker_present": False,
        "default_current_marker_present": False,
        "private_selection_present": False,
        "legacy_selection_present": True,
        "selection_location": "legacy",
        "selection_present": True,
        "selection_readable": True,
        "selection_is_object": True,
        "selection_owner_matches": True,
        "selection_program_root_matches_journey": False,
        "selection_data_root_matches_journey": False,
        "selection_uninstalled": True,
        "selection_foreground_present": True,
    }
    rendered = json.dumps(diagnostic)
    assert str(tmp_path) not in rendered
    assert "12345" not in rendered
    assert "haus.astrid.runtime" not in rendered


@pytest.mark.parametrize("phase,returncode,stderr,absent", [
    ("after-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\n', True),
    ("after-cleanup", 113, 'Bad request.\nCould not find service "haus.astrid.runtime" in domain for user gui: 501\n', True),
    ("before-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\n', False),
    ("after-cleanup", 113, "launchctl unavailable\n", False),
    ("after-cleanup", 1, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\n', False),
    ("after-cleanup", 113, 'Could not find service "other" in domain for user gui: 501\n', False),
    ("after-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 502\n', False),
    ("after-cleanup", 113, 'Could not find service "haus.astrid.runtime" in domain for user gui: 501\nextra error\n', False),
    ("after-cleanup", 113, 'Bad request.\nCould not find service "haus.astrid.runtime" in domain for user gui: 501\nextra error\n', False),
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


@pytest.mark.parametrize(("platform", "relative", "expected_command"), [
    ("linux", Path("home/.config/systemd/user/astrid-r1.service"),
     ["systemctl", "--user", "show", "astrid-r1.service",
      "--property=LoadState,FragmentPath,ActiveState,SubState"]),
    ("darwin", Path("home/Library/LaunchAgents/haus.astrid.runtime.r1.plist"),
     ["launchctl", "print", "gui/501/haus.astrid.runtime.r1"]),
])
def test_manager_state_captures_r1_service_when_present(
        tmp_path, monkeypatch, platform, relative, expected_command):
    monkeypatch.setattr(journey.sys, "platform", platform)
    monkeypatch.setattr(journey.os, "getuid", lambda: 501)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="LoadState=loaded\n", stderr="")

    monkeypatch.setattr(journey.subprocess, "run", fake_run)
    data, evidence = tmp_path / "run" / "data", tmp_path / "evidence"
    unit = data.parent / relative
    unit.parent.mkdir(parents=True)
    unit.write_text("R1 unit definition\n", encoding="utf-8")

    journey.capture_manager_state(evidence, data, "before-cleanup")

    assert calls == [(expected_command, {
        "capture_output": True, "text": True, "timeout": 30, "check": False,
    })]
    manager = journey.read_json(evidence / "manager-state-before-cleanup.json")
    assert manager["command"] == expected_command
    assert manager["unit_file"] == str(unit)
    assert (evidence / "unit-file-before-cleanup.txt").read_text() == "R1 unit definition\n"


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


@pytest.mark.parametrize("message,accepted", [
    (journey.UPGRADE_UNINSTALL_REFUSAL, True),
    ("unrelated uninstall failure", False),
])
def test_release1_upgrade_cleanup_requires_exact_uninstall_refusal_and_preserves_install(
        tmp_path, monkeypatch, message, accepted):
    root, data, r1_data = tmp_path / "program", tmp_path / "data", tmp_path / "data-r1"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    data.mkdir()
    r1_data.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (root / "current").write_text("v1\n")
    journey.write_json(root / "install.json", {"data_root": str(data), "r1_data_root": str(r1_data)})
    journey.write_json(version_root / "manifest.json", {"release": 1})
    version = {"root": str(version_root), "python": "python", "env": {}}
    monkeypatch.setattr(journey, "installation", lambda *_: (version_root / "version.json", data, version))
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *_, **__: {"manager": "native"})

    def refuse(_version, _argv, evidence, name, **_kwargs):
        (evidence / name).write_text(message + "\n")
        raise RuntimeError(f"{name}: exit 1; see {evidence / name}")

    monkeypatch.setattr(journey, "installed_cli", refuse)
    if accepted:
        result = journey.assert_release1_upgrade_uninstall_refusal(version, root, data, evidence)
        assert result["exit_code"] == 1
        assert result["message"] == journey.UPGRADE_UNINSTALL_REFUSAL
        assert result["r1_data_root"] == str(r1_data)
    else:
        with pytest.raises(RuntimeError, match="required upgrade refusal"):
            journey.assert_release1_upgrade_uninstall_refusal(version, root, data, evidence)
    assert (root / "current").read_text() == "v1\n"
    assert data.is_dir() and r1_data.is_dir()


@pytest.mark.parametrize("condition", ["absent", "r0-loaded", "r1-loaded", "r1-plist", "query-error"])
def test_macos_final_uninstall_requires_both_services_and_plists_absent(tmp_path, monkeypatch, condition):
    data = tmp_path / "data"
    monkeypatch.setattr(journey.os, "getuid", lambda: 501)
    if condition == "r1-plist":
        plist = tmp_path / "home/Library/LaunchAgents/haus.astrid.runtime.r1.plist"
        plist.parent.mkdir(parents=True)
        plist.write_text("retained")
    calls = []
    def query(command, **kwargs):
        label = command[-1].split("/")[-1]
        calls.append(label)
        if condition == "query-error":
            return SimpleNamespace(returncode=1, stderr="permission denied")
        if (condition == "r0-loaded" and label == "haus.astrid.runtime" or
                condition == "r1-loaded" and label == "haus.astrid.runtime.r1"):
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=113,
                               stderr=f'Could not find service "{label}" in domain for user gui: 501\n')
    monkeypatch.setattr(journey.subprocess, "run", query)
    if condition == "absent":
        journey.assert_macos_services_removed(data)
        assert calls == ["haus.astrid.runtime", "haus.astrid.runtime.r1"]
    else:
        with pytest.raises(RuntimeError, match="LaunchAgent"):
            journey.assert_macos_services_removed(data)


def test_macos_cleanup_receipt_fails_if_r1_service_remains(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    root, data = run_root / "program", run_root / "data"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="fresh-install", shell="sh", retain_for_next_leg=False)
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setattr(journey.os, "getuid", lambda: 501)
    monkeypatch.setattr(journey, "assert_host_available", lambda *a, **kw: None)
    monkeypatch.setattr(journey, "runtime_processes", lambda *a: [])
    monkeypatch.setattr(journey, "capture_manager_state", lambda *a: None)
    @contextmanager
    def sentinel(*args):
        yield None, None
    monkeypatch.setattr(journey, "sentinel_process", sentinel)
    def lifecycle(*a):
        root.mkdir()
        (root / "current").write_text("v1")
        journey.write_json(run_root / "evidence" / args.leg / "journey-result.json", {"status": "pass"})
        return {"status": "pass"}
    monkeypatch.setattr(journey, "_execute_owned", lifecycle)
    monkeypatch.setattr(journey, "installation", lambda *a: (None, data, {"python": "python"}))
    monkeypatch.setattr(journey, "installed_cli", lambda *a, **kw: (root / "current").unlink())
    def query(command, **kwargs):
        label = command[-1].split("/")[-1]
        if label.endswith(".r1"):
            return SimpleNamespace(returncode=0, stderr="")
        return SimpleNamespace(returncode=113,
                               stderr=f'Could not find service "{label}" in domain for user gui: 501')
    monkeypatch.setattr(journey.subprocess, "run", query)
    with pytest.raises(RuntimeError, match="LaunchAgent absence not confirmed"):
        journey.execute(args)
    receipt = journey.read_json(run_root / "evidence-upload" / "after-cleanup" / "journey-result.json")
    assert receipt["status"] == receipt["final_cleanup"] == "fail"
    assert "haus.astrid.runtime.r1" in receipt["cleanup_error"]


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
    ("direct-macos", "direct", "r0-handoff-then-update"),
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
    monkeypatch.setenv("PSModulePath", "hosted-pwsh-modules")
    monkeypatch.setenv("JOURNEY_KEEP_ENV", "preserved")
    monkeypatch.setenv("UV_LINK_MODE", "hardlink")
    monkeypatch.setattr(journey.sys, "platform", system)
    monkeypatch.setattr(journey, "run", lambda argv, log, **kwargs: calls.append(
        ("shared", [str(part) for part in argv], log, kwargs)))
    monkeypatch.setattr(journey.windows, "run_bootstrap_process", lambda argv, log, mode, **kwargs: calls.append(
        (mode, [str(part) for part in argv], log, kwargs)))
    journey.bootstrap(app, manifest, shell, "update", tmp_path / "evidence", root, data)
    assert len(calls) == 1
    route, argv, log, kwargs = calls[0]
    assert route == ("update" if system == "win32" else "shared")
    assert log == tmp_path / "evidence" / "update-bootstrap.log"
    if system == "win32":
        assert journey.windows.EVIDENCE == tmp_path / "evidence"
    assert argv[argv.index("--root") + 1] == str(root)
    assert argv[argv.index("--data-root") + 1] == str(data)
    assert argv[argv.index("--manifest") + 1] == str(manifest)
    assert "--update" in argv
    assert kwargs["env"]["JOURNEY_KEEP_ENV"] == "preserved"
    assert ("PSModulePath" in kwargs["env"]) == (system != "win32")
    assert kwargs["env"]["UV_LINK_MODE"] == ("copy" if system == "win32" else "hardlink")


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


def test_windows_release0_failure_retains_raw_bootstrap_and_exit_through_cleanup(tmp_path, monkeypatch):
    from scripts import journey_version_jumps

    run_root = tmp_path / "journey"
    root, data = run_root / "program", run_root / "data"
    evidence = run_root / "evidence" / "release9-then-install"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="release9-then-install", shell="powershell.exe", retain_for_next_leg=False)
    raw = b"first product error\r\n\xff\x00" + b"x" * (journey.EVIDENCE_LOG_LIMIT + 31)
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *unused: None)

    @contextmanager
    def sentinel(*unused):
        yield SimpleNamespace(pid=123), "birth"

    monkeypatch.setattr(journey, "sentinel_process", sentinel)
    monkeypatch.setattr(journey, "sentinel_observation", lambda *unused: {"alive": True})

    def process(argv, **kwargs):
        assert str(argv[0]) == "powershell.exe"
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


def test_row_direct_is_one_nine_row_run_and_keep_going_stays_on_the_legacy_rows(tmp_path):
    """Row direct is one full run of the nine Release-1 rows; keep-going never admits the new legs."""
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
               STOPPED_PATH_AGENT_REF=candidate["agent"],
               M4_4A_EXECUTOR_CONFIGS='{"linux":{},"macos":{},"windows":{}}',
               DIAGNOSTIC_KEEP_GOING="false", DIAGNOSTIC_LEG="release9-then-install",
               DIRECT_ONLY="true", JOURNEY_ROW="direct", M4_4A_BACKEND_ONLY="false",
               M4_5_ENABLED="false", M4_5_ONLY="false", M4_5_BOUNDARY="", M4_5_BOUNDARY_ARGS="[]",
               M4_4B_ENABLED="false", M4_6_ENABLED="false", M4_6_CANDIDATE="", GITHUB_OUTPUT=str(output))

    def matrix(**overrides):
        output.write_text("", encoding="utf-8")
        proc = subprocess.run([sys.executable, "-c", program], cwd=tmp_path, env=dict(env, **overrides),
                              text=True, capture_output=True)
        assert proc.returncode == 0, proc.stderr
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return json.loads(values["matrix"])["include"]

    nine = {row["job_id"]: (row["leg"], row["os"], row["runner"]) for row in matrix()}
    assert nine == {
        "direct-linux-fresh": ("fresh-install", "linux", "ubuntu-24.04"),
        "direct-macos-fresh": ("fresh-install", "macos", "macos-15"),
        "direct-windows": ("fresh-install", "windows", "windows-2022"),
        "direct-linux": ("r0-handoff-then-update", "linux", "ubuntu-24.04"),
        "direct-macos": ("r0-handoff-then-update", "macos", "macos-15"),
        "direct-linux-r9-install": ("release9-then-install", "linux", "ubuntu-24.04"),
        "direct-macos-r9-install": ("release9-then-install", "macos", "macos-15"),
        "direct-linux-r11-update": ("release11-then-update", "linux", "ubuntu-24.04"),
        "direct-macos-r11-update": ("release11-then-update", "macos", "macos-15"),
        "direct-linux-autostart-off": ("fresh-install-autostart-off", "linux", "ubuntu-24.04"),
        "direct-macos-autostart-off": ("fresh-install-autostart-off", "macos", "macos-15"),
    }
    kept = {row["job_id"] for row in matrix(DIAGNOSTIC_KEEP_GOING="true")}
    assert kept == {"direct-linux", "direct-macos", "direct-windows"}


def test_journey_workflow_selects_nine_direct_rows_and_keeps_full_gate(tmp_path):
    """Run the checked-in config program; the strict gate is the nine direct rows."""
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
               DIAGNOSTIC_KEEP_GOING="false", DIAGNOSTIC_LEG="release9-then-install",
               M4_4A_BACKEND_ONLY="false", M4_5_ENABLED="false", M4_5_ONLY="false",
               M4_5_BOUNDARY="", M4_5_BOUNDARY_ARGS="[]", M4_4B_ENABLED="false",
               M4_6_ENABLED="false", M4_6_CANDIDATE="", GITHUB_OUTPUT=str(output))
    env["JOURNEY_ROW"] = ""

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
    expected_direct = {
        ("direct-linux", "direct", "ubuntu-24.04", "r0-handoff-then-update"),
        ("direct-macos", "direct", "macos-15", "r0-handoff-then-update"),
        ("direct-windows", "direct", "windows-2022", "fresh-install"),
        ("direct-linux-fresh", "direct", "ubuntu-24.04", "fresh-install"),
        ("direct-macos-fresh", "direct", "macos-15", "fresh-install"),
        ("direct-linux-r9-install", "direct", "ubuntu-24.04", "release9-then-install"),
        ("direct-macos-r9-install", "direct", "macos-15", "release9-then-install"),
        ("direct-linux-r11-update", "direct", "ubuntu-24.04", "release11-then-update"),
        ("direct-macos-r11-update", "direct", "macos-15", "release11-then-update"),
        ("direct-linux-autostart-off", "direct", "ubuntu-24.04", "fresh-install-autostart-off"),
        ("direct-macos-autostart-off", "direct", "macos-15", "fresh-install-autostart-off"),
    }
    # An automatic push or schedule (no row input) runs only the five previous rows, scoped as five.
    automatic_five = {row for row in expected_direct
                      if row[0] not in ("direct-linux-r9-install", "direct-macos-r9-install",
                                        "direct-linux-r11-update", "direct-macos-r11-update",
                                        "direct-linux-autostart-off", "direct-macos-autostart-off")}
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert len(unrestricted) == 5
    assert {(row["job_id"], row["mode"], row["runner"], row["leg"]) for row in unrestricted} == automatic_five
    assert values["release1_scope"] == "five"
    gate = workflow.split("      - name: Aggregate the exact Release-1 receipt matrix\n", 1)[1].split("      - name:", 1)[0]
    assert "if: needs.config.outputs.m4_5_only != 'true' && needs.config.outputs.m4_4a_backend_only != 'true'" in gate
    assert "--aggregate-release1" in gate
    assert '--release1-scope "$RELEASE1_SCOPE"' in gate
    assert "RELEASE1_SCOPE: ${{ needs.config.outputs.release1_scope }}" in gate

    rows = matrix("true")
    assert len(rows) == 5
    assert {(row["job_id"], row["mode"], row["runner"], row["leg"]) for row in rows} == automatic_five
    assert workflow.count("      direct_only:\n") == 2
    assert "DIRECT_ONLY: ${{ inputs.direct_only" in config_step

    selected = matrix("false", JOURNEY_ROW="direct-windows")
    assert [row["job_id"] for row in selected] == ["direct-windows"]
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values["wait_timeout"] == "180"
    for fresh_row in ("direct-linux-fresh", "direct-macos-fresh"):
        selected = matrix("false", JOURNEY_ROW=fresh_row)
        assert [(row["job_id"], row["leg"]) for row in selected] == [(fresh_row, "fresh-install")]
    all_direct = matrix("false", JOURNEY_ROW="direct")
    assert {row["job_id"] for row in all_direct} == {job_id for job_id, *_ in expected_direct}
    values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
    assert values["wait_timeout"] == "900"
    assert values["release1_scope"] == "nine"

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
if [[ "$*" == *show-environment* ]]; then printf 'XDG_CONFIG_HOME=%s\\n' "$MANAGER_CONFIG"; fi
''')
    stub.chmod(0o755)
    probe = "journey-manager-path-probe-test.service"
    config = run_root / "home/.config"
    probe_dir = config / "systemd/user"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CALLS=str(commands),
               EXPECTED_FRAGMENT=str(probe_dir / probe), MANAGER_CONFIG=str(config), run_root=str(run_root), config=str(config),
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
    # Unit discovery working is not enough: the manager must be looking in the install's config dir.
    env.update(EXPECTED_FRAGMENT=str(probe_dir / probe), MANAGER_CONFIG=str(tmp_path / "elsewhere"))
    wrong_config = subprocess.run(["bash", "-Eeuo", "pipefail", "-c", function], env=env, capture_output=True, text=True)
    assert wrong_config.returncode != 0 and "not the probe directory" in wrong_config.stdout


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
    env = dict(os.environ, PYTHONPATH=str(tmp_path), RUNNER_TEMP=str(tmp_path), JOURNEY_LEG="release9-then-install")
    result = subprocess.run([sys.executable, "-u", "-c", code], cwd=tmp_path,
                            env=env, capture_output=True, text=True)
    assert result.returncode != 0 and "Traceback" in result.stderr and expected in result.stderr
    if "preflight sentinel" in expected:
        assert "partial stdout" in result.stdout
        assert "preflight leg: release9-then-install" in result.stdout


@pytest.mark.parametrize("exit_code", [0, 7], ids=["success", "failure"])
def test_seed_diagnostics_survive_cleanup(tmp_path, monkeypatch, exit_code):
    from scripts import journey_version_jumps

    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="release9-then-install", shell="sh", retain_for_next_leg=False)
    script = tmp_path / "driver" / "scripts" / "journey_workspace.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, pathlib, sys\n"
        "payload = pathlib.Path(sys.argv[sys.argv.index('--request') + 1]).read_bytes()\n"
        "sys.stdout.buffer.write(json.dumps({'payload': json.loads(payload)}).encode() + b'\\n')\n"
        "sys.stderr.buffer.write(b'credential-secret diagnostic-' + b'x' * 1048600)\n"
        f"sys.exit({exit_code})\n", encoding="utf-8")
    monkeypatch.setattr(journey, "ROOT", script.parent.parent)
    monkeypatch.setattr(journey, "workspace_root", lambda unused: data / "realm")
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    version_root = root / "versions" / "v0"
    monkeypatch.setattr(journey, "installation", lambda *unused: (
        version_root / "version.json", data, {"root": str(version_root), "python": sys.executable}))
    monkeypatch.setattr(journey, "runtime_processes", lambda *unused: [])
    evidence = run_root / "evidence" / args.leg
    payload = {"project": "seed ü", "generation": "before", "task_id": None}
    def run_owned(*unused):
        root.mkdir()
        version_root.mkdir(parents=True)
        journey.write_json(version_root / "manifest.json", {"release": 0})
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
    assert not any((staged / (prefix + kind + ".log")).exists()
                   for kind in ("input", "command", "stdout", "stderr"))
    assert json.loads((staged / (prefix + "exit.log")).read_bytes()) == {"exit_code": exit_code}
    assert all(b"credential-secret" not in path.read_bytes() for path in staged.iterdir())


def test_seed_diagnostics_distinguish_repeated_invocations(tmp_path, monkeypatch):
    from scripts import journey_version_jumps

    script = tmp_path / "driver" / "scripts" / "journey_workspace.py"
    script.parent.mkdir(parents=True)
    script.write_text("import pathlib, sys\n"
                      "sys.stdout.buffer.write(pathlib.Path(sys.argv[-1]).read_bytes())\n", encoding="utf-8")
    monkeypatch.setattr(journey, "ROOT", script.parent.parent)
    monkeypatch.setattr(journey, "workspace_root", lambda unused: tmp_path / "realm")
    data = tmp_path / "run" / "data"
    evidence = tmp_path / "run" / "evidence" / "release9-then-install"
    for generation in ("one", "two"):
        assert journey_version_jumps.workspace(
            {"python": sys.executable, "env": {}}, data, evidence / "seed", "seed",
            {"generation": generation}) == {"generation": generation}
    journey.retain_named_evidence(evidence, "before-cleanup")
    staged = tmp_path / "run" / "evidence-upload" / "before-cleanup"
    for index, generation in enumerate(("one", "two"), 1):
        prefix = f"runtime-service-service-seed-{index:04d}-"
        assert not (staged / (prefix + "input.log")).exists()
        assert not (staged / (prefix + "stdout.log")).exists()
        assert json.loads((staged / (prefix + "exit.log")).read_bytes()) == {"exit_code": 0}


@pytest.mark.parametrize("manager", ["systemd-user", "launchd", "task-scheduler"])
def test_release0_live_workspace_keeps_owner_running(tmp_path, monkeypatch, manager):
    from scripts import journey_version_jumps as jumps

    root, data, evidence = tmp_path / "program", tmp_path / "data", tmp_path / "evidence"
    app = tmp_path / "app"
    app.mkdir()
    manifest_path = tmp_path / "manifest.json"
    journey.write_json(manifest_path, {"release": 9})
    candidate_path = tmp_path / "candidate.json"
    candidate = {"manifest_sha256": "digest", "artifacts": {}}
    journey.write_json(candidate_path, candidate)
    manifest = {"release": 10, "agent": {}, "runtime": {}}
    args = SimpleNamespace(job_id="direct", mode="direct", leg="release9-then-install", shell="sh",
                           app_checkout=app, candidate=candidate_path)
    state = {"running": True, "pid": 10, "release": 9, "local": False, "content": None}
    calls = []

    def ownership(_root, _data, **_kwargs):
        assert state["running"] and not state["local"]
        return {"manager": manager, "service_path": str(tmp_path / "service.plist")}

    def assert_running(_data):
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

    def workspace(_root, _data, _evidence, action, request, **_kwargs):
        assert state["running"] and not state["local"]
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

    def bootstrap(*_args, **_kwargs):
        assert state["running"] and state["content"] is not None
        state.update(release=10, pid=state["pid"] + 1)

    monkeypatch.setattr(jumps, "release9_admission", lambda *_: (tmp_path, manifest_path, {}))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *_: manifest)
    monkeypatch.setattr(journey, "selected_manifest", lambda *_args, **_kw: (candidate_path, manifest))
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "app"})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *_: {"alive": True})
    monkeypatch.setattr(jumps, "bootstrap_release9", lambda *_, **__: None)
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *_, **_kwargs: None)
    monkeypatch.setattr(journey, "wait_ready", lambda _: assert_running(data))
    monkeypatch.setattr(journey, "installation", lambda *_: (tmp_path / f"v{state['release']}", data,
                                                             {"release": state["release"]}))
    monkeypatch.setattr(jumps, "installed_release", lambda version: version["release"])
    monkeypatch.setattr(journey, "selected_tuple", lambda *_: {"agent": "old", "runtime": "old"})
    monkeypatch.setattr(jumps, "selected_candidate", lambda *_: {"agent": "new", "runtime": "new"})
    monkeypatch.setattr(journey, "assert_direct_service_ownership", ownership)
    monkeypatch.setattr(journey, "runtime_processes", lambda _: [state["pid"]] if state["running"] else [])
    monkeypatch.setattr(journey, "workspace_root", lambda _: tmp_path / "realm" if state["running"]
                        else pytest.fail("discovery is unavailable after shutdown"))
    monkeypatch.setattr(journey, "healthy", lambda _: {"pid": state["pid"],
                                                       "runtime_instance_id": str(state["pid"])})
    monkeypatch.setattr(journey, "process_birth", lambda pid: str(pid) if pid == state["pid"] else None)
    monkeypatch.setattr(journey, "run", run)
    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(jumps, "live_workspace", workspace)
    monkeypatch.setattr(jumps, "task_after_update", task)
    monkeypatch.setattr(journey, "bootstrap", bootstrap)
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions",
                        lambda *_args, **_kwargs: tmp_path / jumps.DIAGNOSTIC_STATE_NAME)

    def verify(mode, _version, _data, directory, **_kwargs):
        if mode == "install":
            journey.write_json(directory / "verify-install.json", {"details": {}})
    monkeypatch.setattr(journey, "verify", verify)
    result = jumps.execute_release9_then_install(args, root, data, evidence,
                                                SimpleNamespace(pid=1), "birth")
    assert result["status"] == "pass" and result["semantic_data"]["identical"]
    assert "one_runtime" not in result["checks"]
    assert calls == ["seed", "task", "snapshot", "snapshot", "task"]
    assert state["running"] and not state["local"]


def test_direct_workspace_restores_service_when_helper_fails(tmp_path, monkeypatch):
    from scripts import journey_version_jumps as jumps

    state = {"running": True}
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda *_: {"manager": "systemd-user"} if state["running"] else pytest.fail("stopped"))
    monkeypatch.setattr(journey, "wait_ready", lambda *_: None)
    monkeypatch.setattr(journey, "healthy", lambda *_: {})
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


def test_direct_workspace_never_opens_workspace_if_runtime_remains(tmp_path, monkeypatch):
    from scripts import journey_version_jumps as jumps

    events = []
    monkeypatch.setattr(journey, "assert_direct_service_ownership",
                        lambda *_: events.append("ownership") or {"manager": "systemd-user"})
    monkeypatch.setattr(journey, "workspace_root", lambda *_: tmp_path / "realm")
    monkeypatch.setattr(journey, "healthy", lambda *_: events.append("health") or {})
    monkeypatch.setattr(journey, "runtime_processes", lambda *_: [42])
    monkeypatch.setattr(journey, "run", lambda argv, *_args, **_kwargs: events.append(argv[2]))
    monkeypatch.setattr(journey, "wait_ready", lambda *_: events.append("ready"))
    monkeypatch.setattr(jumps, "workspace", lambda *_args, **_kwargs: pytest.fail("workspace opened while Runtime is present"))
    times = iter([0, 121])
    monkeypatch.setattr(jumps.time, "monotonic", lambda: next(times))
    with pytest.raises(RuntimeError, match="did not stop"):
        jumps.direct_workspace(tmp_path, {}, tmp_path, tmp_path / "evidence", "seed", {})
    assert events == ["ownership", "health", "stop", "start", "ready", "ownership"]


def test_windows_fresh_bootstrap_capture_precedes_snapshot_and_cleanup(tmp_path, monkeypatch):
    from scripts import journey_windows_acl_capture as capture
    run_root = tmp_path / 'run'
    root, data = run_root / 'program', run_root / 'data'
    app = tmp_path / 'app'
    app.mkdir()
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data, leg='fresh-install',
                              shell='ps1', mode='direct', source_override=False,
                              agent_checkout=None, runtime_checkout=None, app_checkout=app)
    calls = []
    monkeypatch.setattr(journey.sys, 'platform', 'win32')
    monkeypatch.setattr(journey, 'runner_identity', lambda: {})
    monkeypatch.setattr(journey, 'assert_runner_identity', lambda *_: None)
    monkeypatch.setattr(journey, 'assert_host_available', lambda *a: calls.append('cleanup') if a[0] == 'cleanup' else None)
    monkeypatch.setattr(journey, 'runtime_processes', lambda *a: [])
    monkeypatch.setattr(journey, 'selected_manifest', lambda *a, **k: (tmp_path / 'manifest', {}))
    monkeypatch.setattr(journey, 'git_source', lambda *_: {'sha': 'x'})
    monkeypatch.setattr(journey, 'sentinel_observation', lambda *a: {'alive': True})
    monkeypatch.setattr(journey, 'snapshot_failure_evidence', lambda *a: calls.append(a[-1]))
    monkeypatch.setattr(journey, 'capture_manager_state', lambda *a: None)
    @contextmanager
    def sentinel(*_):
        yield SimpleNamespace(pid=1), 'birth'
    monkeypatch.setattr(journey, 'sentinel_process', sentinel)
    def bootstrap(*_, **__):
        calls.append('bootstrap')
        raise ValueError('primary')
    def broken_capture(*_):
        calls.append('capture')
        raise OSError('secret')
    monkeypatch.setattr(journey, 'bootstrap', bootstrap)
    monkeypatch.setattr(capture, 'capture', broken_capture)
    with pytest.raises(ValueError, match='primary'):
        journey.execute(args)
    assert calls[:4] == ['bootstrap', 'capture', 'before-cleanup', 'cleanup']
    assert journey.read_json(run_root / 'evidence' / 'fresh-install' / 'journey-result.json')['error'] == 'ValueError: primary'


@pytest.mark.parametrize("platform,relative_log_dir", [
    ("linux", Path("home/.local/state/astrid-runtime/logs")),
    ("darwin", Path("home/Library/Logs/Astrid Runtime")),
])
def test_capture_runtime_service_logs_retains_native_service_log(tmp_path, monkeypatch, platform,
                                                                 relative_log_dir):
    run_root = tmp_path / "run"
    log_dir = run_root / relative_log_dir
    log_dir.mkdir(parents=True)
    contents = b"old" + b"x" * (journey.EVIDENCE_LOG_LIMIT + 8)
    (log_dir / "runtime-service.log").write_bytes(contents)
    evidence = run_root / "evidence" / "release9-then-install"
    evidence.mkdir(parents=True)

    monkeypatch.setattr(journey.sys, "platform", platform)
    journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")
    journey.retain_named_evidence(evidence, "before-cleanup")

    expected = contents[-journey.EVIDENCE_LOG_LIMIT:]
    assert (evidence / "runtime-service-native.log").read_bytes() == expected
    staged = run_root / "evidence-upload" / "before-cleanup" / "runtime-service-native.log"
    assert staged.read_bytes() == expected


def test_windows_pack_host_log_is_retained_under_installer_named_path(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    log_dir = run_root / "localappdata" / "Astrid Runtime" / "logs"
    log_dir.mkdir(parents=True)
    contents = b"pack host startup failure\n" + b"x" * (journey.EVIDENCE_LOG_LIMIT + 8)
    (log_dir / "service-pack-host.log").write_bytes(contents)
    (log_dir / "unrelated-user-file.txt").write_text("must not be retained", encoding="utf-8")
    evidence = run_root / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)

    monkeypatch.setattr(journey.sys, "platform", "win32")
    journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")
    journey.retain_named_evidence(evidence, "before-cleanup")

    expected = contents[-journey.EVIDENCE_LOG_LIMIT:]
    assert (evidence / "service-pack-host.log").read_bytes() == expected
    staged = run_root / "evidence-upload" / "before-cleanup"
    assert (staged / "service-pack-host.log").read_bytes() == expected
    assert not (staged / "unrelated-user-file.txt").exists()
    status = json.loads((staged / "windows-service-log-status.json").read_text())
    assert status["logs"]["service-pack-host.log"] == {
        "evidence_name": "service-pack-host.log",
        "status": "captured",
        "source_bytes": len(contents),
        "captured_bytes": journey.EVIDENCE_LOG_LIMIT,
    }


def test_windows_service_log_status_explicitly_records_missing_pack_host_and_empty_native_log(
        tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    log_dir = run_root / "localappdata" / "Astrid Runtime" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "runtime-service.log").write_bytes(b"")
    evidence = run_root / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)
    journey.write_json(evidence / "journey-result.json", {"scenario": "fresh-install"})

    def capture_manager(_evidence, _data, phase):
        journey.write_json(evidence / f"manager-state-{phase}.json", {"phase": phase})

    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "capture_manager_state", capture_manager)
    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "before-cleanup")

    staged = run_root / "evidence-upload" / "before-cleanup"
    status = json.loads((staged / "windows-service-log-status.json").read_text())
    assert status["logs"]["service-pack-host.log"]["status"] == "missing"
    assert status["logs"]["runtime-service.log"] == {
        "evidence_name": "runtime-service-native.log",
        "status": "captured",
        "source_bytes": 0,
        "captured_bytes": 0,
    }
    receipt = json.loads((staged / "journey-result.json").read_text())
    assert receipt["failure_diagnostics"]["before-cleanup"]["windows_service_logs"] == status["logs"]
    assert not (staged / "service-pack-host.log").exists()
    assert (staged / "runtime-service-native.log").read_bytes() == b""


@pytest.mark.parametrize("actions,runner,image_os,hosted", [
    ("true", "windows-2022", "windows2022", True),
    ("false", "windows-2022", "windows2022", False),
    ("true", "self-hosted", "", False),
])
def test_hosted_windows_profile_is_preserved_only_on_hosted_actions_runner(
        tmp_path, monkeypatch, actions, runner, image_os, hosted):
    run_root = tmp_path / "run"
    original = {
        "HOME": "original-home", "USERPROFILE": "original-userprofile",
        "LOCALAPPDATA": "original-localappdata", "APPDATA": "original-appdata",
    }
    for key, value in original.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_ACTIONS", actions)
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", runner)
    if image_os:
        monkeypatch.setenv("ImageOS", image_os)
    else:
        monkeypatch.delenv("ImageOS", raising=False)
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "_windows_local_application_data", lambda: tmp_path / "real-local")

    with journey.isolated_environment(run_root, run_root / "program", run_root / "data"):
        assert journey.hosted_windows_runner() is hosted
        if hosted:
            assert {key: os.environ[key] for key in original} == original
        else:
            assert os.environ["HOME"] == str(run_root / "home")
            assert os.environ["USERPROFILE"] == str(run_root / "home")
            assert os.environ["LOCALAPPDATA"] == str(run_root / "localappdata")
            assert os.environ["APPDATA"] == str(run_root / "appdata")
        assert os.environ["ASTRID_CI_PROGRAM_ROOT"] == str(run_root / "program")
        assert os.environ["ASTRID_CI_DATA_ROOT"] == str(run_root / "data")
    assert {key: os.environ[key] for key in original} == original


def test_hosted_windows_captures_bounded_logs_from_real_profile_even_without_owned_task(
        tmp_path, monkeypatch):
    local = tmp_path / "real-local"
    log_dir = local / "Astrid Runtime" / "logs"
    log_dir.mkdir(parents=True)
    content = b"pack-host diagnostic\n" + b"x" * (journey.WINDOWS_PROFILE_SERVICE_LOG_LIMIT + 29)
    (log_dir / "service-pack-host.log").write_bytes(content)
    (log_dir / "runtime-service.log").write_bytes(b"runtime output")
    (log_dir / "unrelated.txt").write_text("exclude", encoding="utf-8")
    evidence = tmp_path / "run" / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)
    run_root = tmp_path / "run"
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("ASTRID_CI_EXPECTED_RUNNER", "windows-2022")
    monkeypatch.setenv("ImageOS", "windows2022")
    monkeypatch.setattr(journey, "_windows_local_application_data", lambda: local)
    monkeypatch.setattr(journey.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0,
        stdout=json.dumps({"task_found": False, "task_path": None, "action_count": 0,
                           "actions": [], "owned_count": 0, "profile_local": str(local)}).encode(),
    ))

    journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")

    expected = content[-journey.EVIDENCE_LOG_LIMIT:]
    assert (evidence / "service-pack-host.log").read_bytes() == expected
    assert (evidence / "runtime-service-native.log").read_bytes() == b"runtime output"
    assert (evidence / "runtime-service-service-pack-host-real-profile-before-cleanup.log").is_file()
    assert not (evidence / "unrelated.txt").exists()
    status = json.loads((evidence / "windows-real-profile-pack-host-before-cleanup.json").read_text())
    assert status["status"] == "captured"
    assert status["probe"]["task_found"] is False
    assert status["captured_bytes"] <= journey.WINDOWS_PROFILE_SERVICE_LOG_LIMIT


@pytest.mark.parametrize("system", ["linux", "win32"])
def test_selection_path_follows_the_installer_lock(tmp_path, monkeypatch, system):
    from astrid.installer.service import install_selection

    local = tmp_path / "local"
    monkeypatch.setattr(journey.sys, "platform", system)
    monkeypatch.setattr(journey, "program_root", lambda: local / "Astrid")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("XDG_DATA_HOME", str(local))
    assert journey.selection_path() == install_selection(tmp_path / "home", system)
    if system == "win32":
        # An older release under test keeps its record until the installer migrates it.
        legacy = local / "astrid-install.json"
        journey.write_json(legacy, {"owner": "haus.astrid.runtime"})
        assert journey.selection_path() == legacy
        journey.write_json(install_selection(tmp_path / "home", system), {"owner": "haus.astrid.runtime"})
        assert journey.selection_path() == local / "Astrid Installer" / "astrid-install.json"


@pytest.mark.parametrize("mode,gate,yes", [
    ("install", True, True), ("install", False, False),
    ("update", True, False), ("update", False, False),
    ("repair", True, False), ("repair", False, False)])
def test_bootstrap_accepts_recommended_setup_only_for_a_fresh_install_whose_installer_has_the_gate(
        tmp_path, monkeypatch, mode, gate, yes):
    flags = journey.bootstrap_flags(tmp_path / "m.json", mode, tmp_path / "p", tmp_path / "d", consent_gate=gate)
    assert ("--yes" in flags) is yes and ("--update" in flags) is (mode == "update")
    seen, probes = [], []
    monkeypatch.setattr(journey, "run", lambda argv, log, **kw: seen.append(argv))
    monkeypatch.setattr(journey, "installer_has_consent_gate", lambda agent, manifest: probes.append((agent, manifest)) or gate)
    monkeypatch.setattr(journey.sys, "platform", "linux")
    journey.bootstrap(tmp_path / "app", tmp_path / "m.json", "sh", mode, tmp_path, tmp_path / "p", tmp_path / "d",
                      agent=tmp_path / "agent")
    assert ("--yes" in seen[0]) is yes
    # Update and repair never even ask the installer.
    assert probes == ([(tmp_path / "agent", tmp_path / "m.json")] if mode == "install" else [])


def _agent_repo(tmp_path, source):
    """A checkout with one commit whose installer CLI is `source`; returns (checkout, sha)."""
    import subprocess as sp
    repo = tmp_path / ("agent-" + hashlib.sha256(source.encode()).hexdigest()[:8])
    (repo / "astrid" / "installer").mkdir(parents=True)
    (repo / "astrid" / "installer" / "cli.py").write_text(source)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        sp.run(["git", "-C", str(repo), *args], check=True, env=env)
    sha = sp.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    return repo, sha


def test_the_pinned_agent_decides_whether_a_fresh_install_gets_yes(tmp_path):
    gated, gated_sha = _agent_repo(tmp_path, 'result.add_argument("--yes", action="store_true", help="accept")\n')
    old, old_sha = _agent_repo(tmp_path, 'result.add_argument("--update", action="store_true")\n')

    def manifest(sha):
        path = tmp_path / f"manifest-{sha[:6]}.json"
        path.write_text(json.dumps({"agent": {"sha": sha}}))
        return path

    # The candidate pin, on a checkout that holds it, has the gate.
    assert journey.installer_has_consent_gate(gated, manifest(gated_sha)) is True
    # The release-0 pin predates it: no --yes, even though the candidate checkout has the flag.
    assert journey.installer_has_consent_gate(old, manifest(old_sha)) is False
    assert journey.installer_has_consent_gate(gated, manifest(old_sha)) is False  # pin not in this checkout
    # Unusable inputs are no gate.
    assert journey.installer_has_consent_gate(None, manifest(gated_sha)) is False
    assert journey.installer_has_consent_gate(gated, tmp_path / "absent.json") is False
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"agent": {"sha": "main"}}))
    assert journey.installer_has_consent_gate(gated, bad) is False
    flags = journey.bootstrap_flags(manifest(gated_sha), "install", tmp_path, tmp_path,
                                    consent_gate=journey.installer_has_consent_gate(gated, manifest(gated_sha)))
    assert "--yes" in flags
    flags = journey.bootstrap_flags(manifest(old_sha), "install", tmp_path, tmp_path,
                                    consent_gate=journey.installer_has_consent_gate(old, manifest(old_sha)))
    assert "--yes" not in flags


def test_the_real_agent_cli_at_head_declares_the_gate():
    import subprocess as sp
    head = sp.run(["git", "-C", str(journey.ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    cli = (journey.ROOT / "astrid/installer/cli.py").read_text()
    assert 'add_argument("--yes"' in cli and re.fullmatch(r"[0-9a-f]{40}", head)


def _capture(tmp_path, monkeypatch, platform):
    run_root = tmp_path / "run"
    evidence = run_root / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)
    monkeypatch.setattr(journey.sys, "platform", platform)
    journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")
    journey.retain_named_evidence(evidence, "before-cleanup")
    return run_root, evidence, run_root / "evidence-upload" / "before-cleanup"


def test_darwin_keeps_a_bounded_tail_of_the_data_root_runtime_log(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    log = run_root / "data" / "runtime" / "runtime.log"
    log.parent.mkdir(parents=True)
    contents = b"x" * (journey.EVIDENCE_LOG_LIMIT + 10) + b"tail"
    log.write_bytes(contents)
    _, evidence, staged = _capture(tmp_path, monkeypatch, "darwin")
    assert (evidence / "runtime-data-runtime.log").read_bytes() == contents[-journey.EVIDENCE_LOG_LIMIT:]
    assert (staged / "runtime-data-runtime.log").read_bytes() == contents[-journey.EVIDENCE_LOG_LIMIT:]


def test_the_data_root_runtime_log_is_darwin_only_and_never_followed_through_a_link(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    log = run_root / "data" / "runtime" / "runtime.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"data")
    _, evidence, _ = _capture(tmp_path, monkeypatch, "linux")
    assert not (evidence / "runtime-data-runtime.log").exists()
    log.unlink()
    secret = tmp_path / "secret"
    secret.write_text("private")
    log.symlink_to(secret)
    (evidence / "update-transaction-status.json").unlink()
    journey.sys.platform = "darwin"
    journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")
    assert not (evidence / "runtime-data-runtime.log").exists()


def test_the_update_journal_is_recorded_as_present_with_its_size_or_explicitly_absent(tmp_path, monkeypatch):
    run_root, evidence, staged = _capture(tmp_path, monkeypatch, "linux")
    absent = json.loads((staged / "update-transaction-status.json").read_text())
    assert absent["status"] == "absent" and absent["path"] == str(run_root / "program" / "update-transaction.json")
    (run_root / "program").mkdir()
    (run_root / "program" / "update-transaction.json").write_bytes(b'{"step": "switched"}')
    journey.capture_runtime_service_logs(evidence, run_root, "after-cleanup")
    present = json.loads((evidence / "update-transaction-status.json").read_text())
    assert (present["status"], present["bytes"], present["phase"]) == ("present", 20, "after-cleanup")
    link = run_root / "program" / "update-transaction.json"
    link.unlink()
    link.symlink_to(tmp_path)
    journey.capture_runtime_service_logs(evidence, run_root, "after-cleanup")
    assert json.loads((evidence / "update-transaction-status.json").read_text())["status"] == "unsafe_type"


def test_the_new_log_names_are_in_both_allowlists():
    names = {"update-transaction-status.json", "runtime-data-runtime.log"}
    assert names <= journey.JOURNEY_EVIDENCE_NAMES
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text()
    assert all(f"'{name}'" in workflow for name in names)


def _mac_tree(tmp_path):
    run_root = tmp_path / "run"
    home = tmp_path / "realhome"
    (run_root / "data" / "runtime" / "realms").mkdir(parents=True)
    (run_root / "data" / "runtime" / "runtime.log").write_bytes(b"12345")
    (run_root / "data" / "runtime" / "realms" / "state.db").write_bytes(b"x" * 7)
    (run_root / "data" / "top.txt").write_bytes(b"t")
    logs = run_root / "home" / "Library" / "Logs" / "Astrid Runtime"
    logs.mkdir(parents=True)
    (logs / "runtime-service.log").write_bytes(b"abc")
    (home / "Library" / "Logs" / "Astrid Other").mkdir(parents=True)
    (home / "Library" / "Logs" / "Astrid Other" / "o.log").write_bytes(b"o")
    (home / "Library" / "Logs" / "Unrelated").mkdir()
    (home / "Library" / "Logs" / "Unrelated" / "secret.log").write_bytes(b"s")
    return run_root, home


def test_macos_listing_shows_names_and_sizes_only_and_what_was_found(tmp_path, capsys):
    run_root, home = _mac_tree(tmp_path)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    journey.capture_macos_listing(evidence, run_root, "before-cleanup", home=home)
    text = (evidence / "macos-file-listing.txt").read_text()
    assert f"{run_root}/data/runtime/runtime.log\tfound, 5 bytes" in text
    assert "Astrid Runtime/runtime.log\tmissing" in text and "runtime-service.log\tfound, 3 bytes" in text
    assert f"{run_root}/data/runtime/realms/state.db\tfile\t7" in text
    assert f"{run_root}/data/top.txt\tfile\t1" in text and "Astrid Other/o.log\tfile\t1" in text
    assert "Unrelated" not in text and "12345" not in text  # only Astrid* logs, and never contents
    err = capsys.readouterr().err
    assert f"Journey expected macOS log {run_root}/data/runtime/runtime.log: found, 5 bytes" in err
    assert "runtime.log: missing" in err


def test_macos_listing_is_bounded_and_does_not_follow_links(tmp_path, monkeypatch):
    run_root, home = _mac_tree(tmp_path)
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "inside.txt").write_text("x")
    (run_root / "data" / "runtime" / "link").symlink_to(secret)
    monkeypatch.setattr(journey, "LISTING_LIMIT", 2)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    journey.capture_macos_listing(evidence, run_root, "before-cleanup", home=home)
    text = (evidence / "macos-file-listing.txt").read_text()
    assert "truncated at 2 entries" in text
    monkeypatch.setattr(journey, "LISTING_LIMIT", 2000)
    journey.capture_macos_listing(evidence, run_root, "before-cleanup", home=home)
    text = (evidence / "macos-file-listing.txt").read_text()
    assert "runtime/link\tlink\t-" in text and "inside.txt" not in text


def test_macos_listing_is_captured_and_staged_with_the_logs_only_on_darwin(tmp_path, monkeypatch):
    run_root, _ = _mac_tree(tmp_path)
    monkeypatch.setattr(journey.Path, "home", classmethod(lambda cls: tmp_path / "realhome"))
    for platform, expected in (("darwin", True), ("linux", False)):
        evidence = run_root / "evidence" / platform
        evidence.mkdir(parents=True)
        monkeypatch.setattr(journey.sys, "platform", platform)
        journey.capture_runtime_service_logs(evidence, run_root, "before-cleanup")
        journey.retain_named_evidence(evidence, "before-cleanup")
        assert (evidence / "macos-file-listing.txt").exists() is expected
    assert (run_root / "evidence-upload" / "before-cleanup" / "macos-file-listing.txt").is_file()
    assert (run_root / "evidence" / "darwin" / "runtime-data-runtime.log").read_bytes() == b"12345"
    assert "macos-file-listing.txt" in journey.JOURNEY_EVIDENCE_NAMES
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/journey.yml").read_text()
    assert "'macos-file-listing.txt'" in workflow
