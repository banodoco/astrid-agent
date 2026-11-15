"""CPU proof for the fresh-install guard; no Windows or download processes."""

import json
import subprocess

import pytest

from scripts import ci_windows_install_guard as guard


@pytest.mark.parametrize("exit_code", [0, 1])
def test_guard_budget_is_one_real_bootstrap_without_retry(tmp_path, monkeypatch, exit_code):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    local = tmp_path / "local"
    logs = local / "Astrid Runtime" / "logs"
    logs.mkdir(parents=True)
    (logs / "install.log").write_text("Source staging operations require Git on PATH")
    (logs / "last-run.json").write_text('{"outcome": "failed"}')
    manifest = {"agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}}
    (evidence / "install-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(guard.windows, "EVIDENCE", evidence)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(guard.windows, "version", lambda: {"root": str(tmp_path / "version")})
    version = tmp_path / "version"
    version.mkdir()
    (version / "manifest.json").write_text(json.dumps(manifest))
    calls = []

    def bootstrap(argv, log_path, mode, *, env, timeout):
        calls.append((argv, env, timeout))
        assert mode == "install"
        log_path.write_text("bootstrap output")
        if exit_code:
            raise subprocess.CalledProcessError(exit_code, argv)

    monkeypatch.setattr(guard.windows, "run_bootstrap_process", bootstrap)
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as failure:
            guard.install()
        assert failure.value.returncode == exit_code
    else:
        guard.install()

    # This budget counts the actual launch boundary on both success and failure.
    # A retry would hide the fresh-profile failure and make a second install warm.
    assert len(calls) == 1
    argv, env, timeout = calls[0]
    assert argv[0] == "powershell.exe"
    assert "--manifest" in argv
    assert "--autostart" in argv and "--command" in argv
    assert "--skills" in argv and "--skill-copies" in argv
    assert "--yes" in argv  # unattended installs stop for consent without it
    assert timeout == 3600
    assert env["LOCALAPPDATA"] == str(local)
    assert (evidence / "install.log").read_text() == (logs / "install.log").read_text()
    assert (evidence / "last-run.json").read_text() == (logs / "last-run.json").read_text()
    receipt = json.loads((evidence / "guard-result.json").read_text())
    assert receipt["status"] == ("failed" if exit_code else "passed")


@pytest.mark.parametrize(("message", "guards"), [
    ("Astrid directories require a protected owner/SYSTEM DACL.", ["F4 DACL"]),
    ("FileNotFoundError: C:\\Users\\long\\a.unpack\\archive-member", ["B1/B2 long path"]),
    ("Source staging operations require Git on PATH", ["B3 Git missing"]),
    ("CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate", ["F1 TLS"]),
    ("Unexpected bootstrap failure", []),
])
def test_guard_early_failure_keeps_receipt_without_installer_logs(tmp_path, monkeypatch, message, guards):
    monkeypatch.setattr(guard.windows, "EVIDENCE", tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    (tmp_path / "install-manifest.json").write_text("{}")

    def bootstrap(argv, log_path, mode, **kwargs):
        log_path.write_text(message)
        raise RuntimeError("bootstrap exit 1")

    monkeypatch.setattr(guard.windows, "run_bootstrap_process", bootstrap)
    with pytest.raises(RuntimeError, match="bootstrap exit 1"):
        guard.install()
    receipt = json.loads((tmp_path / "guard-result.json").read_text())
    assert receipt["guards"] == guards
    assert receipt["files"]["install.log"] == "unavailable"
    assert receipt["files"]["last-run.json"] == "unavailable"


def test_prepare_selects_checkout_archives_without_local_git_sources(tmp_path, monkeypatch):
    app = tmp_path / "journey-app" / "public"
    app.mkdir(parents=True)
    (app / "install-manifest.json").write_text(json.dumps({
        "release": 13, "tools": {"unchanged": True},
        "agent": {"sha": "0" * 40},
        "runtime": {"sha": "0" * 40, "schema_digest": "old"},
    }))
    (tmp_path / "runtime.lock.json").write_text(json.dumps({"source_commit": "b" * 40}))
    monkeypatch.setattr(guard.windows, "ROOT", tmp_path)
    monkeypatch.setattr(guard.windows, "EVIDENCE", tmp_path / "evidence")
    monkeypatch.setattr(guard.windows, "source", lambda path: {
        "sha": ("b" if path.name == "journey-runtime" else "a") * 40,
        "repo": str(path),
    })
    guard.prepare()
    manifest = json.loads((tmp_path / "evidence" / "install-manifest.json").read_text())
    assert manifest["agent"] == {"sha": "a" * 40}
    assert manifest["runtime"] == {"sha": "b" * 40}
    assert manifest["tools"] == {"unchanged": True}
    assert manifest["release"] == 13
