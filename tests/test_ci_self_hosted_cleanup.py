import json
import os
from types import SimpleNamespace

import pytest
from scripts import ci_self_hosted_cleanup as cleanup


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup, "UID", os.getuid())
    monkeypatch.setenv("RUNNER_NAME", "hannah-mac")
    monkeypatch.setattr(cleanup.pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(tmp_path)))
    work = tmp_path / "actions-runner" / "_work"
    temp = work / "_temp"
    temp.mkdir(parents=True)
    monkeypatch.setenv("RUNNER_TEMP", str(temp))
    target = work / "astrid-agent"
    target.mkdir()
    (target / "file").write_text("disposable")
    other = work / "_tool"
    other.mkdir()
    monkeypatch.setattr(cleanup, "assert_no_launchagent", lambda: None)
    monkeypatch.setattr(cleanup, "assert_no_referencing_processes", lambda _: None)
    monkeypatch.setattr(cleanup, "assert_checkout_not_open", lambda _: None)
    return temp, target, other


def test_removes_only_runner_checkout(isolated, capsys):
    temp, target, other = isolated
    cleanup.main()
    row = json.loads(capsys.readouterr().out)
    assert row["status"] == "checkout removed"
    assert row["free_bytes_after"] > 0
    assert not target.exists() and temp.exists() and other.exists()


def test_unlinks_symlink_entry_without_touching_external_target(isolated):
    temp, target, other = isolated
    (other / "keep").write_text("preserve")
    (target / "symlink").symlink_to(other, target_is_directory=True)
    cleanup.main()
    assert not target.exists() and (other / "keep").read_text() == "preserve" and temp.exists()


def test_refuses_symlink_checkout_root(isolated):
    _, target, other = isolated
    (target / "file").unlink()
    target.rmdir()
    target.symlink_to(other, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        cleanup.main()
    assert target.is_symlink() and other.exists()


def test_refuses_existing_smoke_root_and_service(isolated, monkeypatch):
    temp, target, _ = isolated
    (temp / "astrid-r1-direct-macos").mkdir()
    with pytest.raises(RuntimeError, match="Smoke root still exists"):
        cleanup.main()
    monkeypatch.setattr(cleanup, "assert_no_launchagent", lambda: (_ for _ in ()).throw(RuntimeError("service present")))
    with pytest.raises(RuntimeError, match="service present"):
        cleanup.main()
    assert target.exists()


def test_refuses_referencing_process_on_final_recheck(isolated, monkeypatch):
    _, target, _ = isolated
    calls = []
    def processes(_):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("A process references the smoke root or checkout")
    monkeypatch.setattr(cleanup, "assert_no_referencing_processes", processes)
    with pytest.raises(RuntimeError, match="process references"):
        cleanup.main()
    assert target.exists()


def test_lsof_fails_closed(monkeypatch):
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr=""))
    cleanup.assert_checkout_not_open("test")
    for result in (SimpleNamespace(returncode=0, stdout="open", stderr=""),
                   SimpleNamespace(returncode=1, stdout="", stderr="permission denied")):
        monkeypatch.setattr(cleanup.subprocess, "run", lambda *a, **k: result)
        with pytest.raises(RuntimeError): cleanup.assert_checkout_not_open("test")


def test_manual_workflow_embeds_tested_script_and_no_checkout():
    from pathlib import Path
    import textwrap
    base = Path(__file__).parents[1]
    workflow = (base / ".github/workflows/journey.yml").read_text()
    embedded = workflow.split("          /opt/homebrew/opt/python@3.11/bin/python3.11 - <<'PY'\n", 1)[1].rsplit("\n          PY\n", 1)[0]
    assert textwrap.dedent(embedded).strip() == (base / "scripts/ci_self_hosted_cleanup.py").read_text().strip()
    assert "actions/checkout" not in workflow and "working-directory: /" in workflow


@pytest.mark.parametrize("prefix", ["", "Bad request.\n", "Bad request.\r\n"])
def test_only_exact_missing_service_is_accepted(monkeypatch, prefix):
    missing = f'Could not find service "{cleanup.LABEL}" in domain for user gui: {cleanup.UID}'
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=113, stderr=prefix + missing, stdout=""))
    cleanup.assert_no_launchagent()
    monkeypatch.setattr(cleanup.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=113, stderr=prefix + missing + " unrelated", stdout=""))
    with pytest.raises(RuntimeError): cleanup.assert_no_launchagent()


def test_process_scan_checks_other_users_and_continuations(tmp_path, monkeypatch):
    path = tmp_path / "checkout"
    monkeypatch.setattr(cleanup.subprocess, "check_output", lambda *a, **k: f" 99 python -c import os\npath={path}/file\n")
    with pytest.raises(RuntimeError, match="process references"):
        cleanup.assert_no_referencing_processes([path])


def test_smoke_root_created_during_final_check_refuses(isolated, monkeypatch):
    temp, target, _ = isolated
    count = []
    def check(_):
        count.append(1)
        if len(count) == 2:
            (temp / "astrid-r1-direct-macos").mkdir()
    monkeypatch.setattr(cleanup, "assert_checkout_not_open", check)
    with pytest.raises(RuntimeError, match="appeared"):
        cleanup.main()
    assert target.exists()


def test_rejects_mounted_filesystem_entries(isolated, monkeypatch):
    from pathlib import Path
    _, target, _ = isolated
    mounted = target / "file"
    original = Path.lstat
    def lstat(path):
        info = original(path)
        if path == mounted:
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev + 1)
        return info
    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(RuntimeError, match="mounted filesystem"):
        cleanup.main()
    assert target.exists()
