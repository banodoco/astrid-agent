"""TST-B1: focused custody and result-contract tests for named installed journeys."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import shutil
import subprocess
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


def test_authentic_r0_capture_path_chain_and_sanitizer_preserve_structure(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir()
    target = run_root / "program" / "service-env.json"
    target.parent.mkdir()
    target.write_text('TOKEN=do-not-publish\nPATH=/run-owned/path\n', encoding="utf-8")
    link = run_root / "selected-env"
    link.symlink_to(target.relative_to(run_root))

    chain = journey.capture_path_chain(link, run_root)
    assert chain["status"] == "observed"
    assert chain["chain"][0] == {
        "path": "$RUN_ROOT/selected-env", "kind": "symlink", "target": "program/service-env.json",
    }
    assert chain["chain"][-1] == {"path": "$RUN_ROOT/program/service-env.json", "kind": "file"}
    assert journey._sanitize_capture_value({"access_token": "do-not-publish", "path": "Bearer local"}) == {
        "access_token": "<redacted>", "path": "Bearer <redacted>",
    }
    copied = journey._capture_regular_file(target, run_root / "captured-service-env.txt", run_root)
    assert copied["status"] == "captured"
    assert "do-not-publish" not in (run_root / "captured-service-env.txt").read_text(encoding="utf-8")


def test_authentic_r0_capture_caps_all_regular_artifacts_after_sanitization(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    root, data = run_root / "program", run_root / "data"
    source_bytes = b"TOKEN=do-not-publish\n"
    sources = (
        root / "install.json",
        run_root / "astrid-install.json",
        run_root / "home" / ".local" / "share" / "astrid-install.json",
        run_root / "home" / ".config" / "systemd" / "user" / "astrid.service",
    )
    for source in sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(source_bytes)
    calls = []

    def expanding_redactor(text):
        calls.append(text)
        return text.replace("do-not-publish", "<redacted>") + ("x" * journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES)

    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey.windows, "_redact_pack_host_text", expanding_redactor)
    monkeypatch.setattr(journey, "_capture_manager_state_sanitized", lambda *unused: {"status": "observed"})

    observation = journey.capture_authentic_r0_observation(
        run_root / "capture-upload", phase="post-seed", run_root=run_root,
        root=root, data=data, version=None)

    artifacts = [
        run_root / "capture-upload" / "post-seed" / "program_install_record.json",
        run_root / "capture-upload" / "post-seed" / "run_selection_owner.json",
        run_root / "capture-upload" / "post-seed" / "home_selection_owner.json",
        run_root / "capture-upload" / "post-seed" / "service-definition.txt",
    ]
    assert calls == [source_bytes.decode("utf-8")] * len(artifacts)
    assert all(artifact.stat().st_size == journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES for artifact in artifacts)
    assert all(b"do-not-publish" not in artifact.read_bytes() for artifact in artifacts)
    assert all(entry["captured_bytes"] == journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES
               and entry["truncated"] is True
               for entry in [*observation["fields"]["installer_owner_records"].values(),
                             observation["fields"]["service_definition"]])


def test_authentic_r0_capture_shared_route_does_not_select_release1_driver(monkeypatch):
    calls = []
    monkeypatch.setattr(journey, "_authentic_r0_capture_main", lambda argv: calls.append(argv) or 0)

    assert journey.main(["--shared-executor", "authentic-r0-capture", "--fixture", "fixture"]) == 0
    assert calls == [["--fixture", "fixture"]]


def test_authentic_r0_capture_manager_state_records_native_service_pid(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    unit = run_root / "home" / ".config" / "systemd" / "user" / "astrid.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\n", encoding="utf-8")
    data = run_root / "data"

    class Completed:
        returncode = 0
        stdout = "LoadState=loaded\nMainPID=4242\n"
        stderr = ""

    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey.subprocess, "run", lambda *unused, **kwargs: Completed())
    observed = journey._capture_manager_state_sanitized(tmp_path / "manager.json", run_root, run_root / "program", data)

    assert observed["status"] == "observed"
    assert observed["service_pid"] == 4242


def test_authentic_r0_capture_accepts_a_bounded_post_seed_windows_snapshot(tmp_path, monkeypatch):
    data = tmp_path / "data"
    (data / "logs").mkdir(parents=True)
    (data / "runtime").mkdir()
    (data / "logs" / "service-pack-host.log").write_text("token=do-not-publish\n", encoding="utf-8")
    monkeypatch.setattr(journey.windows, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(journey.windows, "data_root", lambda: data)

    journey.windows.capture_pack_host_readiness_diagnostics("post-seed-state", {
        "status": "ok", "runtime_instance_id": "instance", "services": [{"name": "pack-host", "state": "starting"}],
    })

    manifest = journey.read_json(tmp_path / "evidence" / "pack-host-readiness-post-seed-state.json")
    assert manifest["observation"] == "post-seed-state"
    assert "do-not-publish" not in (tmp_path / "evidence" / "pack-host-readiness-post-seed-state-service-pack-host.log").read_text(encoding="utf-8")


def test_authentic_r0_capture_executes_r0_seed_only_and_marks_receipt_ineligible(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    root, data, evidence = run_root / "program", run_root / "data", run_root / "evidence"
    run_root.mkdir()
    root.mkdir()
    data.mkdir()
    version_root = root / "versions" / "r0"
    version_root.mkdir(parents=True)
    version = {"root": str(version_root), "python": sys.executable, "env": {}}
    args = argparse.Namespace(
        run_root=run_root, root=root, data_root=data, shell="sh",
        release0_fixture=tmp_path / "fixture", release0_agent_checkout=tmp_path / "agent",
        release0_runtime_checkout=tmp_path / "runtime",
    )
    calls = []
    fixture_receipt = {"sources": {"agent": "a" * 40, "runtime": "b" * 40}}
    monkeypatch.setattr(journey, "run_scope", lambda *unused, **kwargs: (run_root, root, data, evidence))
    monkeypatch.setattr(journey, "runner_identity", lambda: {"runner_image": "local", "runner_provenance": {}})
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "_capture_release0_sources", lambda *unused: {"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}})
    monkeypatch.setattr(jumps, "verified_release0_fixture", lambda unused: (
        tmp_path / "fixture", tmp_path / "release0-manifest.json", fixture_receipt))
    monkeypatch.setattr(jumps, "bootstrap_release0", lambda *unused: calls.append("release0-bootstrap"))
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *unused: calls.append("release0-start"))
    monkeypatch.setattr(journey, "wait_ready", lambda *unused: None)
    monkeypatch.setattr(journey, "installation", lambda *unused: (version_root / "version.json", data, version))
    monkeypatch.setattr(jumps, "installed_release", lambda unused: 0)
    monkeypatch.setattr(journey, "read_json", lambda path: {"release": 0, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}})
    monkeypatch.setattr(journey, "selected_tuple", lambda *unused: {"agent": "a" * 40, "runtime": "b" * 40})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_authentic_r0_observation", lambda output, **kwargs: {"phase": kwargs["phase"]})
    monkeypatch.setattr(jumps, "direct_workspace", lambda *unused: calls.append("release0-seed") or {})
    monkeypatch.setattr(journey, "installed_cli", lambda *unused, **kwargs: calls.append("uninstall") or "")
    monkeypatch.setattr(journey, "runtime_processes", lambda unused: [])

    result = journey.execute_authentic_r0_capture(args)

    assert result["status"] == "captured"
    assert result["qualification"]["eligible"] is False
    assert result["r1_update_invoked"] is False
    assert calls == ["release0-bootstrap", "release0-start", "release0-seed", "uninstall"]
    assert (run_root / "capture-upload" / "capture-receipt.json").is_file()


def _authentic_r0_bootstrap_failure_args(tmp_path, monkeypatch, raw: bytes | None,
                                         install_raw: bytes | None = None):
    run_root = tmp_path / "run"
    root, data, evidence = run_root / "program", run_root / "data", run_root / "evidence"
    args = argparse.Namespace(
        run_root=run_root, root=root, data_root=data, shell="sh",
        release0_fixture=tmp_path / "fixture", release0_agent_checkout=tmp_path / "agent",
        release0_runtime_checkout=tmp_path / "runtime",
    )
    fixture_receipt = {"sources": {"agent": "a" * 40, "runtime": "b" * 40}}
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey, "run_scope", lambda *unused, **kwargs: (run_root, root, data, evidence))
    monkeypatch.setattr(journey, "runner_identity", lambda: {"runner_image": "local", "runner_provenance": {}})
    monkeypatch.setattr(journey, "assert_host_available", lambda *unused: None)
    monkeypatch.setattr(journey, "_capture_release0_sources", lambda *unused: {
        "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40},
    })
    monkeypatch.setattr(jumps, "verified_release0_fixture", lambda unused: (
        tmp_path / "fixture", tmp_path / "release0-manifest.json", fixture_receipt))
    monkeypatch.setattr(journey, "capture_authentic_r0_observation",
                        lambda output, **kwargs: {"phase": kwargs["phase"]})
    monkeypatch.setattr(journey, "runtime_processes", lambda unused: [])

    def failed_bootstrap(*unused):
        root.mkdir()
        data.mkdir()
        source = evidence / "release0" / "release0-bootstrap.log"
        if raw is not None:
            source.parent.mkdir(parents=True)
            source.write_bytes(raw)
        install_log = run_root / "home" / ".local" / "state" / "astrid-runtime" / "logs" / "install.log"
        if install_raw is not None:
            install_log.parent.mkdir(parents=True)
            install_log.write_bytes(install_raw)
        raise RuntimeError("release0-bootstrap.log: exit 1")

    monkeypatch.setattr(jumps, "bootstrap_release0", failed_bootstrap)
    return args, run_root, root, data


def test_authentic_r0_failed_bootstrap_retains_redacted_bounded_log_before_cleanup(tmp_path, monkeypatch):
    raw = (b"prelude\n" + b"x" * (journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES + 29) +
           b"\nTOKEN=do-not-publish\nBearer secret-token-value\nfirst underlying bootstrap error\n")
    args, run_root, root, data = _authentic_r0_bootstrap_failure_args(tmp_path, monkeypatch, raw)

    result = journey.execute_authentic_r0_capture(args)

    entry = result["failure_diagnostics"]["release0_bootstrap"]
    artifact = run_root / entry["path"]
    assert result["status"] == "failed"
    assert result["evidence_class"] == "capture-only"
    assert result["qualification"]["eligible"] is False
    assert result["r1_update_invoked"] is False
    assert entry["status"] == "captured"
    assert entry["path"] == "capture-upload/failure/release0-bootstrap.log"
    assert entry["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert entry["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert entry["bytes"] == len(raw)
    assert entry["captured_bytes"] == artifact.stat().st_size <= journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES
    assert entry["truncated"] is True
    retained = artifact.read_text(encoding="utf-8")
    assert "first underlying bootstrap error" in retained
    assert "do-not-publish" not in retained and "secret-token-value" not in retained
    assert result["cleanup"]["status"] == "pass"
    assert not root.exists() and not data.exists()
    assert artifact.is_file()
    receipt = journey.read_json(run_root / "capture-upload" / "capture-receipt.json")
    assert receipt["artifacts"]["failure/release0-bootstrap.log"] == entry["artifact_sha256"]


def test_authentic_r0_failed_bootstrap_records_missing_log(tmp_path, monkeypatch):
    args, run_root, root, data = _authentic_r0_bootstrap_failure_args(tmp_path, monkeypatch, None)

    result = journey.execute_authentic_r0_capture(args)

    assert result["status"] == "failed"
    assert result["failure_diagnostics"]["release0_bootstrap"] == {
        "status": "missing",
        "source_path": "$RUN_ROOT/evidence/release0/release0-bootstrap.log",
        "chain": [{
            "path": "$RUN_ROOT/evidence/release0/release0-bootstrap.log",
            "kind": "absent",
        }],
        "path": None,
        "raw_sha256": None,
        "artifact_sha256": None,
        "bytes": None,
        "captured_bytes": None,
        "truncated": False,
        "limit_bytes": journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES,
    }
    assert result["cleanup"]["status"] == "pass"
    assert not root.exists() and not data.exists()
    assert not (run_root / "capture-upload" / "failure" / "release0-bootstrap.log").exists()


def test_authentic_r0_failed_bootstrap_retains_actual_install_log_before_cleanup(tmp_path, monkeypatch):
    install_raw = (b"prelude\n" + b"x" * (journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES + 29) +
                   b"\nTOKEN=do-not-publish\nBearer secret-token-value\nfirst underlying installer error\n")
    args, run_root, root, data = _authentic_r0_bootstrap_failure_args(
        tmp_path, monkeypatch, b"release0 bootstrap failed\n", install_raw)

    result = journey.execute_authentic_r0_capture(args)

    entry = result["failure_diagnostics"]["install_log"]
    artifact = run_root / entry["path"]
    assert entry["status"] == "captured"
    assert entry["source_path"] == "$RUN_ROOT/home/.local/state/astrid-runtime/logs/install.log"
    assert entry["path"] == "capture-upload/failure/install.log"
    assert entry["raw_sha256"] == hashlib.sha256(install_raw).hexdigest()
    assert entry["artifact_sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert entry["bytes"] == len(install_raw)
    assert entry["captured_bytes"] == artifact.stat().st_size <= journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES
    assert entry["truncated"] is True
    retained = artifact.read_text(encoding="utf-8")
    assert "first underlying installer error" in retained
    assert "do-not-publish" not in retained and "secret-token-value" not in retained
    assert result["cleanup"]["status"] == "pass"
    assert not root.exists() and not data.exists()
    assert artifact.is_file()
    receipt = journey.read_json(run_root / "capture-upload" / "capture-receipt.json")
    assert receipt["artifacts"]["failure/install.log"] == entry["artifact_sha256"]


def test_authentic_r0_failed_bootstrap_reports_missing_actual_install_log(tmp_path, monkeypatch):
    args, run_root, root, data = _authentic_r0_bootstrap_failure_args(
        tmp_path, monkeypatch, b"release0 bootstrap failed\n")

    result = journey.execute_authentic_r0_capture(args)

    assert result["failure_diagnostics"]["install_log"] == {
        "status": "missing",
        "source_path": "$RUN_ROOT/home/.local/state/astrid-runtime/logs/install.log",
        "chain": [{
            "path": "$RUN_ROOT/home/.local/state/astrid-runtime/logs/install.log",
            "kind": "absent",
        }],
        "path": None,
        "raw_sha256": None,
        "artifact_sha256": None,
        "bytes": None,
        "captured_bytes": None,
        "truncated": False,
        "limit_bytes": journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES,
    }
    assert result["cleanup"]["status"] == "pass"
    assert not root.exists() and not data.exists()
    assert not (run_root / "capture-upload" / "failure" / "install.log").exists()


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


def _direct_candidate_vibecomfy(tmp_path, installed_vibecomfy):
    """Build the candidate checkout and installed record consumed by a direct row."""
    agent = tmp_path / "candidate-agent"
    pin_path = agent / "astrid" / "packs" / "vibecomfy" / "pins.json"
    pin_path.parent.mkdir(parents=True)
    pin_path.write_text(json.dumps({"sources": {"vibecomfy": {"revision": "a" * 40}}}),
                        encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(agent)], check=True)
    subprocess.run(["git", "-C", str(agent), "add", "astrid/packs/vibecomfy/pins.json"], check=True)
    subprocess.run(["git", "-C", str(agent), "-c", "user.name=journey-test",
                    "-c", "user.email=journey-test@example.invalid", "commit", "-qm", "pin"], check=True)
    candidate_agent_sha = subprocess.check_output(
        ["git", "-C", str(agent), "rev-parse", "HEAD"], text=True).strip()
    # The derivation must read the candidate SHA's committed pin, never a mutable checkout file.
    pin_path.write_text(json.dumps({"sources": {"vibecomfy": {"revision": "b" * 40}}}),
                        encoding="utf-8")
    installed = tmp_path / "installed"
    installed.mkdir()
    manifest = {
        "release": 1,
        "agent": {"sha": candidate_agent_sha},
        "runtime": {"sha": "r" * 40},
    }
    installed_manifest = dict(manifest)
    if installed_vibecomfy is not None:
        installed_manifest["vibecomfy"] = installed_vibecomfy
    (installed / "manifest.json").write_text(json.dumps(installed_manifest), encoding="utf-8")
    version = {"root": str(installed), "identity": {"release": 1, "managed_by": None}}
    return manifest, version, {"agent": "old-agent", "runtime": "old-runtime"}, agent


def test_direct_candidate_vibecomfy_identity_matches_committed_candidate_pin(tmp_path):
    manifest, version, before, agent = _direct_candidate_vibecomfy(
        tmp_path, {"sha": "a" * 40})
    selected = jumps.selected_candidate(
        manifest, version, before, candidate_agent_checkout=agent)
    assert selected["vibecomfy"] == "a" * 40


def test_direct_candidate_vibecomfy_identity_mismatch_fails_closed(tmp_path):
    manifest, version, before, agent = _direct_candidate_vibecomfy(
        tmp_path, {"sha": "b" * 40})
    with pytest.raises(RuntimeError, match="differs from the candidate Agent pin"):
        jumps.selected_candidate(manifest, version, before, candidate_agent_checkout=agent)


def test_direct_candidate_vibecomfy_identity_absence_fails_closed(tmp_path):
    manifest, version, before, agent = _direct_candidate_vibecomfy(tmp_path, None)
    with pytest.raises(RuntimeError, match="absent or unknown"):
        jumps.selected_candidate(manifest, version, before, candidate_agent_checkout=agent)


def test_direct_candidate_vibecomfy_identity_unknown_value_fails_closed(tmp_path):
    manifest, version, before, agent = _direct_candidate_vibecomfy(
        tmp_path, {"sha": "unknown"})
    with pytest.raises(RuntimeError, match="absent or unknown"):
        jumps.selected_candidate(manifest, version, before, candidate_agent_checkout=agent)


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


def _windows_census_owner(pid, parent_pid, command):
    """Build the bounded record emitted by the Windows census for a matcher hit."""
    return {
        "ProcessId": pid,
        "ParentProcessId": parent_pid,
        "CreationDate": "2026-11-02T18:51:58.6815710Z",
        "NativeIdentity": f"windows-creation:{pid}",
        "ExecutablePath": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        if "powershell.exe" in command else r"C:\Astrid\python.exe",
        "Matcher": "runtime_protocol-start",
        "CommandLength": len(command),
        "CommandSha256": hashlib.sha256(command.encode()).hexdigest(),
        "SanitizedCommandLine": "-m runtime_protocol start",
    }


def test_tst_b1_windows_runtime_owner_census_is_empty_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")

    def powershell(code, name, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        assert name == "runtime-owner-processes.json"
        return "[]"

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    assert journey.runtime_processes(tmp_path) == []


def test_tst_b1_windows_runtime_owner_census_keeps_genuine_runtime_protocol_start(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    command = r"C:\Astrid\python.exe -X utf8 -m runtime_protocol start --support-root C:\data\runtime"
    row = _windows_census_owner(8661, 7200, command)

    def powershell(code, *unused, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        return json.dumps([row])

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    assert journey.runtime_processes(tmp_path) == [8661]


def test_tst_b1_windows_runtime_owner_census_excludes_its_own_powershell_probe(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    probe_command = (
        "powershell.exe -NoProfile -NonInteractive -Command "
        "$ErrorActionPreference='Stop'; "
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        + journey.WINDOWS_RUNTIME_OWNER_CENSUS
    )
    runtime_child_command = (
        r"C:\Astrid\python.exe -X utf8 -m runtime_protocol start "
        r"--support-root C:\data\runtime"
    )
    # The pre-fix census matched both: its own literal-bearing PowerShell
    # command and a genuine Runtime child.  The source guard must remove only
    # the former from the records it serializes.
    candidates = [
        (8660, 7200, probe_command),
        (8661, 8660, runtime_child_command),
    ]
    assert all(journey._runtime_owner_command(command) for _pid, _parent, command in candidates)

    def powershell(code, *unused, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        assert "Where-Object { $_.ProcessId -ne $PID }" in code
        return json.dumps([
            _windows_census_owner(pid, parent_pid, command)
            for pid, parent_pid, command in candidates
            if pid != 8660
        ])

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    assert journey.runtime_processes(tmp_path) == [8661]


def test_tst_b1_windows_runtime_owner_census_errors_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "healthy", lambda _data: {"pid": 8661})

    def powershell(code, *unused, **kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        raise RuntimeError("CIM enumeration unavailable")

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    evidence = tmp_path / "evidence"
    with pytest.raises(RuntimeError, match="CIM enumeration unavailable"):
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    snapshot = journey.read_json(evidence / "runtime-owner-assertion.json")
    assert snapshot["assertion"]["asserted_owner_pids"] == {
        "status": "unavailable", "reason": "owner census failed: RuntimeError",
    }


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


def test_tst_b1_windows_duplicate_owner_error_retains_bounded_identity_evidence(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    owner = (r"C:\Astrid\pythonw.exe -X utf8 -I -c "
             "os.environ.update({'API_TOKEN':'must-not-be-retained'}); "
             "from astrid_runtime.cli import main; main(['run', '--profile', 'astrid'])")
    selected = {
        "ProcessId": 3032, "ParentProcessId": 1920,
        "CreationDate": "2026-11-02T17:18:21.973746+00:00",
        "NativeIdentity": "windows-creation:134381135019737465",
        "ExecutablePath": r"C:\Astrid\pythonw.exe", "CommandLine": owner,
    }
    duplicate = {
        "ProcessId": 2972, "ParentProcessId": 1920,
        "CreationDate": "2026-11-02T17:18:18.000000+00:00",
        "NativeIdentity": "windows-creation:134381135010000000",
        "ExecutablePath": r"C:\Astrid\pythonw.exe", "CommandLine": owner,
    }
    rows = [selected]

    def powershell(code, *unused, **kwargs):
        assert "GetProcessTimes" in code
        assert "SanitizedCommandLine" in code
        assert "CommandSha256" in code
        return json.dumps(rows)

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(journey, "healthy", lambda data: {
        "pid": 3032,
        "process_birth_id": "windows-creation:134381135019737465",
        "runtime_instance_id": "instance-3032",
        "active_realm": "realm-3032",
        "coordinator_epoch": "epoch-3032",
    })
    evidence = tmp_path / "evidence" / "release0"
    # The unchanged strict assertion passes and creates no diagnostic until it
    # actually observes the duplicate failure.
    journey.assert_one_runtime(tmp_path, evidence=evidence)
    assert not (evidence / "runtime-owner-assertion.json").exists()
    rows.insert(0, duplicate)
    with pytest.raises(RuntimeError) as failure:
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    message = str(failure.value)
    assert "selected Runtime owner 3032, found [2972, 3032]" in message
    assert '"matcher":"astrid_runtime-cli-run"' in message
    assert '"parent_pid":1920' in message
    assert '"creation_date":"2026-11-02T17:18:21.973746+00:00"' in message
    assert hashlib.sha256(owner.encode()).hexdigest() in message
    assert owner not in message
    assert "must-not-be-retained" not in message

    snapshot_path = evidence / "runtime-owner-assertion.json"
    snapshot_bytes = snapshot_path.read_bytes()
    snapshot = journey.read_json(snapshot_path)
    assert snapshot["capture"]["timing"] == "captured before the assertion raises"
    assert snapshot["capture"]["atomicity"]["status"] == "not-established"
    assert snapshot["assertion"]["asserted_owner_pids"] == [2972, 3032]
    assert snapshot["assertion"]["selected_pid_bound_to_asserted_owner"] == {
        "status": "observed", "record_index": 1,
    }
    assert snapshot["selected_discovery_identity"]["process_birth_id"] == {
        "status": "observed", "value": "windows-creation:134381135019737465",
    }
    owners = {row["pid"]["value"]: row for row in snapshot["asserted_owners"]}
    assert set(owners) == {2972, 3032}
    selected_snapshot = owners[3032]
    assert selected_snapshot["command_line"] == {
        "status": "observed", "value": "-c <astrid_runtime.cli main(['run', ...])>",
    }
    assert selected_snapshot["command_sha256"]["value"] == hashlib.sha256(owner.encode()).hexdigest()
    assert selected_snapshot["executable_path"] == {
        "status": "observed", "value": r"C:\Astrid\pythonw.exe",
    }
    assert selected_snapshot["creation_time"]["value"] == "2026-11-02T17:18:21.973746+00:00"
    assert selected_snapshot["native_identity"]["value"] == "windows-creation:134381135019737465"
    assert selected_snapshot["parent_child_relation"] == {
        "status": "observed", "parent_is_asserted_owner": False,
        "asserted_children": [], "shared_parent_with": [2972],
    }
    assert "must-not-be-retained" not in snapshot_path.read_text()

    # A later census may observe different rows, but cannot replace the first
    # failure-time record or its staged copies.
    rows[:] = [selected, {**duplicate, "ExecutablePath": r"C:\Other\pythonw.exe"}]
    with pytest.raises(RuntimeError):
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    assert snapshot_path.read_bytes() == snapshot_bytes
    journey.retain_named_evidence(evidence, "before-cleanup")
    journey.retain_named_evidence(evidence, "after-cleanup")
    assert snapshot_path.read_bytes() == snapshot_bytes
    assert (tmp_path / "evidence-upload" / "before-cleanup" / "runtime-owner-assertion.json").read_bytes() == snapshot_bytes
    assert (tmp_path / "evidence-upload" / "after-cleanup" / "runtime-owner-assertion.json").read_bytes() == snapshot_bytes


def test_tst_b1_windows_owner_failure_snapshot_marks_missing_fields_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    rows = [
        {"ProcessId": 2972, "ParentProcessId": 1920,
         "CreationDate": None, "NativeIdentity": None, "ExecutablePath": None,
         "Matcher": "astrid_runtime-cli-run", "CommandLength": None,
         "CommandSha256": None, "SanitizedCommandLine": None},
        {"ProcessId": 3032, "ParentProcessId": None,
         "CreationDate": None, "NativeIdentity": None, "ExecutablePath": None,
         "Matcher": "astrid_runtime-cli-run", "CommandLength": None,
         "CommandSha256": None, "SanitizedCommandLine": None},
    ]

    def powershell(code, *unused, **kwargs):
        assert "NativeIdentity" in code
        return json.dumps(rows)

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    monkeypatch.setattr(journey, "healthy", lambda data: {"pid": 3032})
    evidence = tmp_path / "evidence" / "release0"
    with pytest.raises(RuntimeError) as failure:
        journey.assert_one_runtime(tmp_path, evidence=evidence)
    assert "selected Runtime owner 3032, found [2972, 3032]" in str(failure.value)
    snapshot = journey.read_json(evidence / "runtime-owner-assertion.json")
    selected = next(row for row in snapshot["asserted_owners"] if row["pid"]["value"] == 3032)
    assert snapshot["selected_discovery_identity"]["process_birth_id"] == {
        "status": "unavailable", "reason": "discovery process_birth_id was unavailable",
    }
    assert selected["command_sha256"] == {
        "status": "unavailable", "reason": "command-line digest was unavailable",
    }
    assert selected["executable_path"] == {
        "status": "unavailable", "reason": "Win32_Process.ExecutablePath was unavailable",
    }
    assert selected["creation_time"] == {
        "status": "unavailable", "reason": "Win32_Process.CreationDate was unavailable",
    }
    assert selected["native_identity"] == {
        "status": "unavailable", "reason": "GetProcessTimes creation identity was unavailable",
    }
    assert selected["parent_child_relation"] == {
        "status": "unavailable", "parent_is_asserted_owner": None,
        "asserted_children": [], "shared_parent_with": [],
        "reason": "parent PID was unavailable",
    }


_ARTIFACT_VENV = (r"D:\a\_temp\astrid-r1-direct-windows\program\versions\3204bb00-"
                  r"6d53a190-f042d729\py\Scripts\pythonw.exe")
_ARTIFACT_BASE = (r"D:\a\_temp\astrid-r1-direct-windows\localappdata\Astrid Installer" "\\"
                  r"bootstrap\python\cpython-3.11.13-windows-x86_64-none\pythonw.exe")
_ARTIFACT_TAIL_SHA256 = "00d7b317229dfb6c2e46d4f0a0a903af124a578f2997ccfd1c84596b36f7389e"


def _artifact_shaped_windows_owners(*, child_identity="windows-creation:134381205278019339",
                                    child_parent=6976, child_image=_ARTIFACT_BASE,
                                    action_image=_ARTIFACT_VENV,
                                    parent_tail=_ARTIFACT_TAIL_SHA256,
                                    child_tail=_ARTIFACT_TAIL_SHA256,
                                    child_arguments_match=True,
                                    action_tail=_ARTIFACT_TAIL_SHA256,
                                    parent_created="2026-11-02T19:15:27.791539+00:00",
                                    child_created="2026-11-02T19:15:27.801933+00:00",
                                    extra=()):
    """The retained 41507782325 process topology, without secret command text."""
    parent = {
        "pid": 6976,
        "parent_pid": 1880,
        "creation_date": parent_created,
        "native_identity": "windows-creation:134381205277915396",
        "executable_path": _ARTIFACT_VENV,
        "command_tail_sha256": parent_tail,
        "command_tail_argument_count": 4,
        "registered_arguments_match": True,
    }
    child = {
        "pid": 5224,
        "parent_pid": child_parent,
        "creation_date": child_created,
        "native_identity": child_identity,
        "executable_path": child_image,
        "command_tail_sha256": child_tail,
        "command_tail_argument_count": 4,
        "registered_arguments_match": child_arguments_match,
    }
    task_action = {
        "status": "observed",
        "task_path": "\\",
        "executable_path": action_image,
        "arguments_sha256": action_tail,
        "argument_count": 4,
    }
    details = [parent, child, *extra]
    return journey._RuntimeOwnerPids([row["pid"] for row in details], details, task_action)


def _artifact_shaped_discovery():
    return {
        "pid": 5224,
        "process_birth_id": "windows-creation:134381205278019339",
        "runtime_instance_id": "eac11355544e43cb87aa37c391715b25",
        "active_realm": "d924bbae-6693-4db1-9b57-cfc8651ed7b9",
        "coordinator_epoch": "eac11355544e43cb87aa37c391715b25",
    }


def test_tst_b1_windows_artifact_shaped_venv_redirector_is_one_logical_owner(
        tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    owners = _artifact_shaped_windows_owners()
    monkeypatch.setattr(journey, "healthy", lambda _data: _artifact_shaped_discovery())
    monkeypatch.setattr(journey, "runtime_processes", lambda _data: owners)
    monkeypatch.setattr(journey, "_windows_venv_base_executable", lambda _path: _ARTIFACT_BASE)

    journey.assert_one_runtime(tmp_path, evidence=tmp_path / "evidence" / "release0")

    snapshot = journey.read_json(tmp_path / "evidence" / "release0" / "runtime-owner-assertion.json")
    assert snapshot["receipt_kind"] == "windows-runtime-owner-logical-owner-assertion"
    assert snapshot["assertion"]["asserted_owner_pids"] == [6976, 5224]
    assert snapshot["assertion"]["logical_owner_pids"] == [5224]
    assert snapshot["assertion"]["logical_owner_normalization"]["status"] == "collapsed"
    assert snapshot["assertion"]["logical_owner_normalization"]["selected_native_identity"] == (
        "windows-creation:134381205278019339")
    assert snapshot["assertion"]["logical_owner_normalization"]["task_action"] == {
        "executable_path": _ARTIFACT_VENV,
        "arguments_sha256": _ARTIFACT_TAIL_SHA256,
        "argument_count": 4,
    }


def test_tst_b1_windows_census_retains_raw_pair_and_redacted_task_action(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    owners = _artifact_shaped_windows_owners()
    observation = {
        "owners": [{
            "ProcessId": row["pid"], "ParentProcessId": row["parent_pid"],
            "CreationDate": row["creation_date"], "NativeIdentity": row["native_identity"],
            "ExecutablePath": row["executable_path"], "Matcher": "astrid_runtime-cli-run",
            "CommandLength": 3710, "CommandSha256": "a" * 64,
            "CommandTailSha256": row["command_tail_sha256"],
            "CommandTailArgumentCount": row["command_tail_argument_count"],
            "RegisteredArgumentsMatch": row["registered_arguments_match"],
            "SanitizedCommandLine": "-c <astrid_runtime.cli main(['run', ...])>",
        } for row in owners.details],
        "task_action": owners.task_action,
    }

    def powershell(code, *unused, **_kwargs):
        assert code == journey.WINDOWS_RUNTIME_OWNER_CENSUS
        return json.dumps(observation)

    monkeypatch.setattr(journey.windows, "powershell", powershell)
    retained = journey.runtime_processes(tmp_path)
    assert list(retained) == [6976, 5224]
    assert retained.task_action == owners.task_action
    assert [row["parent_pid"] for row in retained.details] == [1880, 6976]
    assert all(row["registered_arguments_match"] for row in retained.details)


@pytest.mark.parametrize("case", [
    "wrong-base-image", "changed-argv", "unreadable-pyvenv-cfg", "missing-selected-identity",
    "missing-discovery-identity", "unrelated-parent", "creation-order", "third-owner",
    "genuine-second-interpreter",
])
def test_tst_b1_windows_logical_owner_normalization_rejects_every_unproven_owner(
        tmp_path, monkeypatch, case):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    kwargs = {}
    if case == "wrong-base-image":
        kwargs["child_image"] = r"D:\foreign\pythonw.exe"
    elif case == "changed-argv":
        kwargs["child_tail"] = "f" * 64
        kwargs["child_arguments_match"] = False
    elif case == "missing-selected-identity":
        kwargs["child_identity"] = None
    elif case == "creation-order":
        kwargs["parent_created"] = "2026-11-02T19:15:27.901933+00:00"
    elif case == "unrelated-parent":
        kwargs["child_parent"] = 1880
    elif case == "third-owner":
        kwargs["extra"] = ({
            "pid": 7111, "parent_pid": 6976,
            "creation_date": "2026-11-02T19:15:27.802000+00:00",
            "native_identity": "windows-creation:134381205278019400",
            "executable_path": _ARTIFACT_VENV,
            "command_tail_sha256": _ARTIFACT_TAIL_SHA256,
            "command_tail_argument_count": 4,
            "registered_arguments_match": True,
        },)
    elif case == "genuine-second-interpreter":
        kwargs["extra"] = ({
            "pid": 8111, "parent_pid": 1880,
            "creation_date": "2026-11-02T19:15:27.802000+00:00",
            "native_identity": "windows-creation:134381205278019401",
            "executable_path": _ARTIFACT_BASE,
            "command_tail_sha256": _ARTIFACT_TAIL_SHA256,
            "command_tail_argument_count": 4,
            "registered_arguments_match": True,
        },)
    owners = _artifact_shaped_windows_owners(**kwargs)
    discovery = _artifact_shaped_discovery()
    if case == "missing-discovery-identity":
        discovery.pop("process_birth_id")
    monkeypatch.setattr(journey, "healthy", lambda _data: discovery)
    monkeypatch.setattr(journey, "runtime_processes", lambda _data: owners)
    monkeypatch.setattr(journey, "_windows_venv_base_executable",
                        lambda _path: None if case == "unreadable-pyvenv-cfg" else _ARTIFACT_BASE)

    with pytest.raises(RuntimeError, match=r"Expected exactly the selected Runtime owner 5224"):
        journey.assert_one_runtime(tmp_path, evidence=tmp_path / "evidence" / "release0")

    snapshot = journey.read_json(tmp_path / "evidence" / "release0" / "runtime-owner-assertion.json")
    assert snapshot["receipt_kind"] == "windows-runtime-owner-cardinality-failure"
    assert snapshot["assertion"]["asserted_owner_pids"] == list(owners)
    assert "logical_owner_pids" not in snapshot["assertion"]


def test_tst_b1_windows_venv_cfg_requires_one_readable_absolute_home(monkeypatch):
    class Config:
        def __init__(self, text):
            self.text = text
        def is_symlink(self):
            return False
        def is_file(self):
            return True
        def read_text(self, **_kwargs):
            return self.text

    config = Config("home = D:\\base\\cpython-3.11.13\n"
                    "include-system-site-packages = false\n")
    monkeypatch.setattr(journey, "Path", lambda _path: config)
    assert journey._windows_venv_base_executable(_ARTIFACT_VENV) == r"D:\base\cpython-3.11.13\pythonw.exe"
    config.text = "home = D:\\one\nhome = D:\\two\n"
    assert journey._windows_venv_base_executable(_ARTIFACT_VENV) is None
    config.text = "home = relative\\python\n"
    assert journey._windows_venv_base_executable(_ARTIFACT_VENV) is None


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


def test_windows_release0_failure_retains_raw_bootstrap_and_exit_through_cleanup(tmp_path, monkeypatch):
    from scripts import journey_version_jumps

    run_root = tmp_path / "journey"
    root, data = run_root / "program", run_root / "data"
    evidence = run_root / "evidence" / "release0-to-release1"
    args = argparse.Namespace(run_root=run_root, root=root, data_root=data,
                              leg="release0-to-release1", shell="powershell.exe", retain_for_next_leg=False)
    raw = b"first product error\r\n\xff\x00" + b"x" * (journey.EVIDENCE_LOG_LIMIT + 31)
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setenv("JOURNEY_LAUNCHER_PSEDITION", "Core")
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
        diagnostic = json.loads((staged / phase / "windows-bootstrap-environment.log").read_text())
        assert diagnostic == {"bootstrap": "release0-bootstrap.log", "PSEdition": "Core",
                              "PSModulePath": r"C:\Program Files\PowerShell\7\Modules"}


def test_linux_update_bootstrap_failure_stages_nested_log_through_cleanup(tmp_path, monkeypatch):
    run_root = tmp_path / "journey"
    evidence = run_root / "evidence" / "release0-to-release1"
    raw = b"first real update-bootstrap error\n"
    source = evidence / "release1" / "update-bootstrap.log"
    source.parent.mkdir(parents=True)
    source.write_bytes(raw)
    journey.write_json(evidence / "journey-result.json", {
        "status": "fail", "scenario": "release0-to-release1",
        "error": "RuntimeError: update-bootstrap.log: exit 1",
    })
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *unused: None)

    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "before-cleanup")
    shutil.rmtree(source.parent)
    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "after-cleanup")

    identity = hashlib.sha256(b"release1/update-bootstrap.log").hexdigest()
    name = f"runtime-service-service-bootstrap-{identity}.log"
    staged = run_root / "evidence-upload"
    for phase in ("before-cleanup", "after-cleanup"):
        receipt = journey.read_json(staged / phase / "journey-result.json")
        bootstrap = receipt["failure_diagnostics"][phase]["bootstrap"]
        path = staged / phase / name
        assert path.read_bytes() == raw
        assert bootstrap["status"] == "captured"
        assert bootstrap["paths"] == [path.relative_to(run_root).as_posix()]
    assert receipt["failure_diagnostics"]["before-cleanup"]["bootstrap"]["source_paths"] == [
        "release1/update-bootstrap.log"]
    assert receipt["failure_diagnostics"]["after-cleanup"]["bootstrap"]["source_paths"] == []


@pytest.mark.parametrize("platform,legacy", [("win32", True), ("win32", False), ("linux", True)])
def test_bootstrap_module_path_is_scoped_to_windows_legacy_launch(tmp_path, monkeypatch, platform, legacy):
    monkeypatch.setattr(journey.sys, "platform", platform)
    inherited = {"pSmOdUlEpAtH": r"C:\Users\test\Documents\PowerShell\Modules;C:\PowerShell\7\Modules",
                 "PROGRAMFILES": r"C:\Program Files", "SYSTEMROOT": r"C:\Windows",
                 "JOURNEY_LAUNCHER_PSEDITION": "Core", "KEEP": "value"}
    calls = []
    def process(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(journey.subprocess, "run", process)
    log = tmp_path / "release1" / "update-bootstrap.log"
    journey.run(["powershell.exe", "-File", "install.ps1"], log, env=inherited, legacy_bootstrap=legacy)
    child = calls[0][1]
    diagnostic = tmp_path / "windows-bootstrap-environment.log"
    if platform == "win32" and legacy:
        assert child["PSModulePath"] == (r"C:\Program Files\WindowsPowerShell\Modules;"
                                         r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules")
        assert "pSmOdUlEpAtH" not in child and child["KEEP"] == "value"
        assert json.loads(diagnostic.read_text()) == {"bootstrap": "update-bootstrap.log",
                                                     "PSModulePath": inherited["pSmOdUlEpAtH"],
                                                     "PSEdition": "Core"}
    else:
        assert child == inherited
        assert not diagnostic.exists()
    assert inherited["pSmOdUlEpAtH"].endswith(r"C:\PowerShell\7\Modules")


def test_release0_and_release1_bootstraps_use_normalized_windows_path(tmp_path, monkeypatch):
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PSModulePath", "inherited-pwsh-path")
    monkeypatch.setenv("JOURNEY_LAUNCHER_PSEDITION", "Core")
    calls = []
    def process(argv, **kwargs):
        calls.append((argv, kwargs["env"]["PSModulePath"]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(journey.subprocess, "run", process)
    evidence = tmp_path / "evidence"
    jumps.bootstrap_release0(tmp_path / "fixture", tmp_path / "manifest", "powershell.exe",
                             evidence / "release0", tmp_path / "program", tmp_path / "data")
    journey.bootstrap(tmp_path / "app", tmp_path / "manifest", "powershell.exe", "update",
                      evidence / "release1", tmp_path / "program", tmp_path / "data", True)
    assert [Path(argv[6]).as_posix() for argv, _ in calls] == [
        (tmp_path / "fixture" / "served" / "install.ps1").as_posix(),
        (tmp_path / "app" / "public" / "install.ps1").as_posix()]
    assert all(path == (r"C:\Program Files\WindowsPowerShell\Modules;"
                        r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules") for _, path in calls)
    records = [json.loads(line) for line in (evidence / "windows-bootstrap-environment.log").read_text().splitlines()]
    assert [record["bootstrap"] for record in records] == ["release0-bootstrap.log", "update-bootstrap.log"]
    assert all(record["PSModulePath"] == "inherited-pwsh-path" and record["PSEdition"] == "Core"
               for record in records)


def test_direct_release0_to_release1_lifecycle_sanitizes_both_windows_children(tmp_path, monkeypatch):
    """Keep R0 and R1 sanitation bound to the real direct lifecycle ordering."""
    app = tmp_path / "app"
    app.mkdir()
    evidence, root, data = (tmp_path / name for name in ("evidence", "program", "data"))
    r0_manifest, candidate_path = tmp_path / "r0.json", tmp_path / "candidate.json"
    journey.write_json(r0_manifest, {"release": 0})
    journey.write_json(candidate_path, {"manifest_sha256": "digest", "artifacts": {}})
    manifest = {"release": 1, "agent": {}, "runtime": {}}
    args = SimpleNamespace(job_id="direct-windows", mode="direct", leg="release0-to-release1",
                           shell="powershell.exe", app_checkout=app, candidate=candidate_path)
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("PSModulePath", "inherited-pwsh-path")
    monkeypatch.setenv("JOURNEY_LAUNCHER_PSEDITION", "Core")
    children, owners = [], []

    def process(argv, **kwargs):
        children.append((argv, kwargs["env"].copy()))
        # Exercise the R1 failure boundary after a successfully sanitized R0 child.
        return SimpleNamespace(returncode=0 if len(children) == 1 else 37)

    monkeypatch.setattr(journey.subprocess, "run", process)
    monkeypatch.setattr(jumps, "release0_admission", lambda _: (tmp_path / "fixture", r0_manifest, {}))
    monkeypatch.setattr(journey, "selected_manifest", lambda *a, **kw: (candidate_path, manifest))
    monkeypatch.setattr(jumps.evidence_gate, "validate_tuple", lambda *a: manifest)
    monkeypatch.setattr(journey, "git_source", lambda _: {"sha": "app"})
    monkeypatch.setattr(journey, "sentinel_observation", lambda *a: {"alive": True})
    monkeypatch.setattr(jumps, "start_direct_runtime", lambda *a: None)
    monkeypatch.setattr(journey, "wait_ready", lambda *a: None)
    monkeypatch.setattr(journey, "installation", lambda *a: (tmp_path / "old", data, {"release": 0}))
    monkeypatch.setattr(jumps, "installed_release", lambda version: version["release"])
    monkeypatch.setattr(journey, "selected_tuple", lambda *a: {})
    monkeypatch.setattr(journey, "assert_direct_service_ownership", lambda *a: {})
    monkeypatch.setattr(journey, "assert_one_runtime", lambda *a: owners.append("checked"))
    monkeypatch.setattr(jumps, "write_diagnostic_release0_preconditions", lambda *a, **kw: None)
    monkeypatch.setattr(journey, "verify", lambda *a, **kw: None)
    monkeypatch.setattr(jumps, "direct_workspace", lambda *a: {})
    monkeypatch.setattr(jumps, "task_after_update", lambda *a: "task")
    monkeypatch.setattr(journey, "healthy", lambda *a: {"pid": 123})
    monkeypatch.setattr(journey, "process_birth", lambda *a: "birth")

    with pytest.raises(RuntimeError, match=r"update-bootstrap.log: exit 37"):
        jumps.execute_release0_to_release1(args, root, data, evidence, SimpleNamespace(pid=1), "birth")

    assert owners == ["checked"]
    assert [Path(argv[6]).as_posix() for argv, _ in children] == [
        (tmp_path / "fixture/served/install.ps1").as_posix(), (app / "public/install.ps1").as_posix()]
    assert all(env["PSModulePath"] == (r"C:\Program Files\WindowsPowerShell\Modules;"
                                       r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules")
               for _, env in children)
    records = [json.loads(line) for line in (evidence / "windows-bootstrap-environment.log").read_text().splitlines()]
    assert records == [{"bootstrap": name, "PSModulePath": "inherited-pwsh-path", "PSEdition": "Core"}
                       for name in ("release0-bootstrap.log", "update-bootstrap.log")]


def test_windows_bootstrap_edition_and_diagnostic_are_wired_to_workflow():
    workflow = (journey.ROOT / ".github" / "workflows" / "journey.yml").read_text()
    lifecycle = workflow.split("      - name: Run the matrix-owned lifecycle driver\n", 1)[1].split(
        "      - name:", 1)[0]
    assert "$env:JOURNEY_LAUNCHER_PSEDITION = $PSVersionTable.PSEdition" in lifecycle
    assert lifecycle.index("$env:JOURNEY_LAUNCHER_PSEDITION") < lifecycle.index("python -u agent/scripts/ci_journey.py")
    boundary = workflow.split("      - name: Validate the named lifecycle evidence artifact boundary\n", 1)[1].split(
        "      - name:", 1)[0]
    assert "'windows-bootstrap-environment.log'" in boundary
    assert "windows-bootstrap-environment.log" in journey.JOURNEY_EVIDENCE_NAMES


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


def test_retain_named_evidence_stages_nested_release0_owner_assertion_only(tmp_path):
    evidence = tmp_path / "run" / "evidence" / "release0-to-release1"
    release0 = evidence / "release0"
    release0.mkdir(parents=True)
    snapshot = b'{"capture":"release0-write-once"}\n'
    (release0 / journey.OWNER_ASSERTION_SNAPSHOT).write_bytes(snapshot)
    (release0 / "unapproved-nested.json").write_text("must not stage", encoding="utf-8")
    # A top-level file could be produced by later diagnostics. The nested R0
    # snapshot remains the artifact-boundary source for this release leg.
    (evidence / journey.OWNER_ASSERTION_SNAPSHOT).write_text("later diagnostic", encoding="utf-8")
    (evidence / "unapproved-top-level.json").write_text("must not stage", encoding="utf-8")

    journey.retain_named_evidence(evidence, "before-cleanup")

    staged = tmp_path / "run" / "evidence-upload" / "before-cleanup"
    assert (staged / journey.OWNER_ASSERTION_SNAPSHOT).read_bytes() == snapshot
    assert {path.name for path in staged.iterdir()} == {journey.OWNER_ASSERTION_SNAPSHOT}


def test_windows_pack_host_readiness_capture_is_retained_and_receipted(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    evidence = run_root / "evidence" / "fresh-install"
    evidence.mkdir(parents=True)
    journey.write_json(evidence / "journey-result.json", {"status": "fail", "scenario": "fresh-install"})
    journey.write_json(evidence / "pack-host-readiness-first-failed-poll.json", {
        "schema_version": 1, "observation": "first-failed-poll", "health": {"status": "ok"},
    })
    (evidence / "pack-host-readiness-first-failed-poll-service-pack-host.log").write_text(
        "first service error\n", encoding="utf-8",
    )
    (evidence / "unrelated-secret.txt").write_text("must not stage", encoding="utf-8")
    monkeypatch.setattr(journey.sys, "platform", "win32")
    monkeypatch.setattr(journey, "capture_runtime_service_logs", lambda *unused: None)
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)

    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "before-cleanup")

    staged = run_root / "evidence-upload" / "before-cleanup"
    assert (staged / "pack-host-readiness-first-failed-poll.json").is_file()
    assert (staged / "pack-host-readiness-first-failed-poll-service-pack-host.log").read_text() == (
        "first service error\n"
    )
    assert not (staged / "unrelated-secret.txt").exists()
    receipt = journey.read_json(staged / "journey-result.json")
    assert receipt["failure_diagnostics"]["before-cleanup"]["pack_host_readiness"] == [
        {
            "observation": "first-failed-poll", "status": "captured",
            "path": "evidence-upload/before-cleanup/pack-host-readiness-first-failed-poll.json",
            "capture_status": "observed",
        },
        {"observation": "first-service-exit", "status": "unavailable", "path": None},
        {"observation": "definitive-failure", "status": "unavailable", "path": None},
    ]


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
                           app_checkout=app, candidate=candidate_path,
                           candidate_agent_checkout=tmp_path / "candidate-agent")
    state = {"running": True, "pid": 10, "release": 0, "local": False, "content": None}
    calls = []

    def ownership(_root, _data):
        assert state["running"] and not state["local"]
        return {"manager": manager, "service_path": str(tmp_path / "service.plist")}

    def one_runtime(_data, _evidence=None):
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

    bootstrap_calls = []

    def bootstrap(*bootstrap_args, **bootstrap_kwargs):
        assert state["running"] and state["content"] is not None
        bootstrap_calls.append((bootstrap_args, bootstrap_kwargs))
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
    monkeypatch.setattr(jumps, "selected_candidate",
                        lambda *_args, **_kwargs: {"agent": "new", "runtime": "new"})
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
    assert bootstrap_calls == [(
        (app, candidate_path, "sh", "update", evidence / "release1", root, data),
        {"legacy_bootstrap": True},
    )]
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


def test_mac_stop_capture_reaches_bootstrap_child_only_when_opted_in(tmp_path, monkeypatch):
    app = tmp_path / "app"
    (app / "public").mkdir(parents=True)
    # Use a shell wrapper that launches an isolated Python child like the App bootstrap.
    (app / "public" / "install").write_text(
        f"{sys.executable} -I -c 'import os; print(os.getenv(\"ASTRID_INSTALLER_MAC_STOP_CAPTURE\", \"absent\"))'\n")
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setenv("ASTRID_INSTALLER_MAC_STOP_CAPTURE", "1")
    journey.bootstrap(app, app / "manifest", "sh", "update", tmp_path / "evidence", tmp_path / "root", tmp_path / "data")
    assert (tmp_path / "evidence" / "update-bootstrap.log").read_text().strip() == "1"
    monkeypatch.delenv("ASTRID_INSTALLER_MAC_STOP_CAPTURE")
    journey.bootstrap(app, app / "manifest", "sh", "update", tmp_path / "evidence", tmp_path / "root", tmp_path / "data")
    assert (tmp_path / "evidence" / "update-bootstrap.log").read_text().strip() == "absent"


def test_mac_stop_capture_retains_only_valid_bounded_records(tmp_path):
    run_root = tmp_path / "run"
    source = run_root / "home" / "Library" / "Logs" / "Astrid Runtime" / "install.log"
    source.parent.mkdir(parents=True)
    record = {"format": "astrid-mac-stop-capture/v1", "capture": "a" * 32, "seq": 1,
              "stage": "pre_admission", "wall_ns": 1, "monotonic_ns": 2,
              "discovery": "same", "process": "unobserved"}
    invalid = dict(record, token="secret-value")
    source.write_bytes(b"x" * (1024 * 1024) + b"\nnormal secret-value\n" +
                       json.dumps(invalid).encode() + b"\n" + json.dumps(record).encode() + b"\n")
    dest = run_root / "evidence-upload" / "before-cleanup"
    result = journey.capture_mac_stop_records(run_root, dest)
    assert result["status"] == "captured" and result["truncated"] and result["records"] == 1
    retained = (run_root / result["path"]).read_bytes()
    assert json.loads(retained) == record and b"secret-value" not in retained
    assert result["sha256"] == hashlib.sha256(retained).hexdigest()
    source.unlink()
    source.symlink_to(tmp_path / "secret")
    assert journey.capture_mac_stop_records(run_root, dest)["status"] == "unavailable"


def test_mac_stop_capture_workflow_scopes_opt_in_and_artifact():
    workflow = (journey.ROOT / ".github" / "workflows" / "journey.yml").read_text()
    assert workflow.count("      mac_stop_capture:") == 2
    assert "matrix.job_id == 'direct-macos' && inputs.mac_stop_capture && '1' || '0'" in workflow
    assert "'mac-stop-capture.jsonl'" in workflow


def test_mac_stop_snapshot_retains_capture_before_cleanup_without_raw_install_log(tmp_path, monkeypatch):
    run_root = tmp_path / "run"
    evidence = run_root / "evidence" / "release0-to-release1"
    evidence.mkdir(parents=True)
    source = run_root / "home" / "Library" / "Logs" / "Astrid Runtime" / "install.log"
    source.parent.mkdir(parents=True)
    record = {"format": "astrid-mac-stop-capture/v1", "capture": "b" * 32,
              "seq": 1, "stage": "bootout_enter", "wall_ns": 1, "monotonic_ns": 2}
    source.write_text("ordinary private installer log\n" + json.dumps(record) + "\n")
    journey.write_json(evidence / "journey-result.json", {"status": "fail"})
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setenv("ASTRID_INSTALLER_MAC_STOP_CAPTURE", "1")
    monkeypatch.setattr(journey, "capture_manager_state", lambda *unused: None)
    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "before-cleanup")
    source.unlink()
    journey.snapshot_failure_evidence(evidence, run_root, run_root / "data", "after-cleanup")
    receipt = journey.read_json(evidence / "journey-result.json")
    before = receipt["failure_diagnostics"]["before-cleanup"]["mac_stop_capture"]
    after = receipt["failure_diagnostics"]["after-cleanup"]["mac_stop_capture"]
    assert before["status"] == "captured" and after["status"] == "unavailable"
    assert json.loads((run_root / before["path"]).read_text()) == record
    assert not list((run_root / "evidence-upload").rglob("install.log"))
    staged = journey.read_json(run_root / "evidence-upload" / "after-cleanup" / "journey-result.json")
    assert staged["failure_diagnostics"]["before-cleanup"]["mac_stop_capture"] == before


def test_linux_lock_capture_workflow_scopes_opt_in():
    workflow = (journey.ROOT / '.github' / 'workflows' / 'journey.yml').read_text()
    assert workflow.count('      linux_lock_capture:') == 2
    assert "matrix.job_id == 'direct-linux' && inputs.linux_lock_capture && '1' || '0'" in workflow
    assert not journey.credential_name('ASTRID_LINUX_LOCK_CAPTURE')


def test_linux_lock_capture_reaches_bootstrap_subprocess(tmp_path, monkeypatch):
    app, manifest = app_checkout(tmp_path)
    (app / 'public' / 'install').write_text('#!/bin/sh\nprintf "%s" "$ASTRID_LINUX_LOCK_CAPTURE"\n')
    evidence = tmp_path / 'evidence'
    evidence.mkdir()
    monkeypatch.setattr(journey.sys, 'platform', 'linux')
    monkeypatch.setenv('ASTRID_LINUX_LOCK_CAPTURE', '1')
    with journey.without_provider_credentials():
        journey.bootstrap(app, manifest, 'sh', 'update', evidence, tmp_path / 'root', tmp_path / 'data')
    assert (evidence / 'update-bootstrap.log').read_text() == '1'


def test_linux_lock_snapshot_retains_sanitized_bounded_log_and_refuses_symlink(tmp_path, monkeypatch):
    run_root = tmp_path / 'run'
    evidence = run_root / 'evidence' / 'release0-to-release1'
    evidence.mkdir(parents=True)
    source = run_root / 'home' / '.local' / 'state' / 'astrid-runtime' / 'logs' / 'install.log'
    source.parent.mkdir(parents=True)
    source.write_text('x' * (journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES + 50) +
                      '\nTOKEN=do-not-publish\ninstaller-lock-parent {"schema":"installer-lock-parent-v1"}\n')
    journey.write_json(evidence / 'journey-result.json', {'status': 'fail'})
    monkeypatch.setattr(journey.sys, 'platform', 'linux')
    monkeypatch.setenv('ASTRID_LINUX_LOCK_CAPTURE', '1')
    monkeypatch.setattr(journey, 'capture_manager_state', lambda *unused: None)
    journey.snapshot_failure_evidence(evidence, run_root, run_root / 'data', 'before-cleanup')
    record = journey.read_json(evidence / 'journey-result.json')['failure_diagnostics']['before-cleanup']['linux_lock_install_log']
    retained = run_root / 'evidence-upload' / 'before-cleanup' / 'install.log'
    data = retained.read_bytes()
    assert record['status'] == 'captured' and record['truncated']
    assert len(data) <= journey.AUTHENTIC_R0_CAPTURE_MAX_TEXT_BYTES
    assert b'do-not-publish' not in data and b'installer-lock-parent' in data
    assert record['sha256'] == hashlib.sha256(data).hexdigest()
    assert not (evidence / 'install.log').exists()
    source.unlink()
    source.symlink_to(tmp_path / 'secret')
    journey.snapshot_failure_evidence(evidence, run_root, run_root / 'data', 'after-cleanup')
    after = journey.read_json(evidence / 'journey-result.json')['failure_diagnostics']['after-cleanup']['linux_lock_install_log']
    assert after['status'] == 'unavailable'
    assert retained.read_bytes() == data
