"""Focused disposable tests for the Q-owned real surface commands."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import installed_browser_adapter as boundary_adapter
from scripts import installed_product_surface as installed_surface
from scripts import update_operation_surface as update_surface

PROTOCOL = "astrid-installed-browser-interface/v1"
ROOT = Path(__file__).resolve().parents[1]


def _run(argv: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _repo(path: Path, files: dict[str, str] | None = None) -> str:
    path.mkdir(parents=True)
    for relative, content in (files or {"owned.txt": "owned\n"}).items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    commands = (
        ["git", "init", "-q", str(path)],
        ["git", "-C", str(path), "config", "user.name", "Surface Test"],
        ["git", "-C", str(path), "config", "user.email", "surface@example.invalid"],
        ["git", "-C", str(path), "add", "."],
        ["git", "-C", str(path), "commit", "-q", "-m", "fixture"],
    )
    for command in commands:
        result = _run(command)
        assert result.returncode == 0, result.stderr
    return _run(["git", "-C", str(path), "rev-parse", "HEAD"]).stdout.strip()


def _checkouts(tmp_path: Path, app_files: dict[str, str] | None = None):
    paths = {name: tmp_path / name for name in ("app", "agent", "runtime", "pinokio")}
    refs = {name: _repo(path, app_files if name == "app" else None) for name, path in paths.items()}
    return paths, refs


def _os_name() -> str:
    return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[platform.system()]


def _request(
    *,
    action: str,
    refs: dict[str, str],
    root: Path,
    data: Path,
    channel: str,
    mode: str,
    stand_in: dict | None = None,
    payload: dict | None = None,
) -> dict:
    os_name = _os_name()
    return {
        "protocol": "astrid-release2-boundary/v1",
        "sequence": 1,
        "action": action,
        "scenario": {
            "scenario_id": f"release2-{channel}-{mode}-{os_name}",
            "channel": channel,
            "mode": mode,
            "os": os_name,
            "job_id": f"{mode}-{os_name}",
        },
        "inputs": {
            "candidate": refs,
            "stand_in": stand_in
            or {
                "manifest_sha256": "a" * 64,
                "target": {
                    "agent": "b" * 40,
                    "runtime": "c" * 40,
                    "schema_digest": "sha256:" + "d" * 64,
                },
            },
            "stand_in_marker": "M4-5 test stand-in",
            "root": str(root),
            "data_root": str(data),
        },
        "payload": payload or {},
    }


def _envelope(surface: str, request: dict, *, handoff: str = "e" * 64) -> dict:
    return {
        "protocol": PROTOCOL,
        "handoff_id": handoff,
        "surface": surface,
        "action": request["action"],
        "request": request,
        "adapter_state": {"phase": "release1", "operation_id": None, "next_sequence": 1},
        "prior_observations": {},
    }


def _install(
    root: Path,
    data: Path,
    manifest: dict,
    *,
    managed_by: str | None,
    python: Path | None = None,
) -> Path:
    version_root = root / "versions" / f"release-{manifest.get('release', 0)}"
    version_root.mkdir(parents=True)
    if python is None:
        python = version_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        python.parent.mkdir(parents=True)
        try:
            python.symlink_to(Path(sys.executable).resolve())
        except OSError:
            shutil.copy2(Path(sys.executable).resolve(), python)
    profile = version_root / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    version = {
        "root": str(version_root),
        "python": str(python),
        "profile": str(profile),
        "env": {},
        "identity": {"release": manifest.get("release", 0), "managed_by": managed_by},
    }
    (version_root / "version.json").write_text(
        json.dumps(version, indent=2) + "\n", encoding="utf-8"
    )
    (version_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    root.mkdir(exist_ok=True)
    (root / "current").write_text(version_root.name + "\n", encoding="utf-8")
    (root / "install.json").write_text(
        json.dumps(
            {
                "owner": "haus.astrid.runtime",
                "data_root": str(data),
                "managed_by": managed_by,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return version_root


def _surface_args(paths: dict[str, Path]) -> list[str]:
    return [
        "--app-checkout",
        str(paths["app"]),
        "--agent-checkout",
        str(paths["agent"]),
        "--runtime-checkout",
        str(paths["runtime"]),
        "--pinokio-checkout",
        str(paths["pinokio"]),
    ]


def test_installed_probe_observes_release1_from_install_records_not_source(tmp_path):
    paths, refs = _checkouts(tmp_path)
    root, data = tmp_path / "program", tmp_path / "data"
    (data / "qualification").mkdir(parents=True)
    snapshot = data / "qualification" / "semantic-snapshot.json"
    snapshot.write_text('{"project":"preserved"}\n', encoding="utf-8")
    _install(
        root,
        data,
        {
            "release": 1,
            "agent": {"sha": refs["agent"]},
            "runtime": {"sha": refs["runtime"]},
        },
        managed_by="pinokio",
    )
    request = _request(
        action="inspect-release1",
        refs=refs,
        root=root,
        data=data,
        channel="pinokio-update",
        mode="pinokio-backend",
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "installed_product_surface.py"),
        *_surface_args(paths),
    ]
    result = _run(command, input_text=json.dumps(_envelope("installed", request)))

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["observation"] == {
        "release": 1,
        "candidate_checkouts": {name: {"sha": sha} for name, sha in refs.items()},
        "semantic_data": {"sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()},
        "os_build": response["observation"]["os_build"],
    }
    installed_python = (
        root
        / "versions"
        / "release-1"
        / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    assert response["evidence"]["installed_executable"]["path"] == str(installed_python)
    assert response["evidence"]["installed_executable"]["resolved_path"] == str(
        Path(sys.executable).resolve()
    )
    journal = json.loads((data / ".astrid-m4-5-installed-surface.json").read_text())
    assert journal["actions"][0]["action"] == "inspect-release1"

    version_path = root / "versions" / "release-1" / "version.json"
    version = json.loads(version_path.read_text())
    version["python"] = sys.executable
    version_path.write_text(json.dumps(version) + "\n")
    with pytest.raises(installed_surface.SurfaceError, match="installed Python executable"):
        installed_surface.execute(
            Namespace(
                app_checkout=paths["app"],
                agent_checkout=paths["agent"],
                runtime_checkout=paths["runtime"],
                pinokio_checkout=paths["pinokio"],
                semantic_path="qualification/semantic-snapshot.json",
                sentinel_path="qualification/unrelated-sentinel.json",
                journal_name=".astrid-m4-5-installed-surface.json",
            ),
            _envelope("installed", request, handoff="2" * 64),
        )


def test_release2_probe_requires_real_health_one_runtime_settlement_and_sentinel(
    tmp_path, monkeypatch
):
    root, data = tmp_path / "program", tmp_path / "data"
    (data / "qualification").mkdir(parents=True)
    (data / "qualification" / "semantic-snapshot.json").write_text("preserved\n")
    birth = "disposable-process-birth"
    (data / "qualification" / "unrelated-sentinel.json").write_text(
        json.dumps({"pid": os.getpid(), "birth": birth}) + "\n"
    )
    target = {
        "agent": "b" * 40,
        "runtime": "c" * 40,
        "schema_digest": "sha256:" + "d" * 64,
    }
    release2_manifest = {
        "release": 2,
        "qualification_marker": "M4-5 test stand-in",
        "agent": {"sha": target["agent"]},
        "runtime": {"sha": target["runtime"], "schema_digest": target["schema_digest"]},
    }
    version_root = _install(
        root,
        data,
        release2_manifest,
        managed_by="pinokio",
    )
    refs = {name: (name[0] * 40) for name in ("app", "agent", "runtime", "pinokio")}
    manifest_path = version_root / "manifest.json"
    manifest_sha = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    stand_in = {"manifest_sha256": manifest_sha, "target": target}
    request = _request(
        action="inspect-release2",
        refs=refs,
        root=root,
        data=data,
        channel="pinokio-update",
        mode="pinokio-backend",
        stand_in=stand_in,
    )
    status = {
        "health": {"status": "ok", "runtime_instance_id": "instance"},
        "discovery": {
            "runtime_instance_id": "instance",
            "pid": os.getpid(),
        },
        "support": {"pid_alive": True},
    }
    monkeypatch.setattr(installed_surface, "_checkouts", lambda *_args: {})
    monkeypatch.setattr(
        installed_surface,
        "_runtime_status",
        lambda *_args: (status, Path(sys.executable).resolve()),
    )
    monkeypatch.setattr(installed_surface, "_runtime_owner_pids", lambda: [os.getpid()])
    monkeypatch.setattr(installed_surface, "_process_birth", lambda _pid: birth)
    args = Namespace(
        app_checkout=Path("/unused"),
        agent_checkout=Path("/unused"),
        runtime_checkout=Path("/unused"),
        pinokio_checkout=Path("/unused"),
        semantic_path="qualification/semantic-snapshot.json",
        sentinel_path="qualification/unrelated-sentinel.json",
        journal_name=".installed-probe.json",
    )

    observation, evidence = installed_surface.execute(
        args, _envelope("installed", request, handoff="f" * 64)
    )

    assert observation["installed"] == {
        "release": 2,
        "qualification_marker": "M4-5 test stand-in",
        "manifest_sha256": manifest_sha,
        **target,
    }
    assert all(
        observation[name] == "pass"
        for name in ("service", "health", "task_settled", "one_runtime", "unrelated_sentinel")
    )
    assert evidence["runtime_instance_id"] == "instance"

    # Equal parsed fields are insufficient: the installed manifest bytes must
    # still be the exact accepted stand-in artifact.
    manifest_path.write_text(json.dumps(release2_manifest) + "\n", encoding="utf-8")
    with pytest.raises(installed_surface.SurfaceError, match="manifest differs"):
        installed_surface.execute(args, _envelope("installed", request, handoff="1" * 64))


def test_native_service_binds_exact_runtime_pid_and_installed_command(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    version_root = _install(
        root,
        data,
        {"release": 2, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}},
        managed_by=None,
    )
    installed = installed_surface._installed(root, data)
    python = Path(installed["python"])
    profile = version_root / "profile.json"
    script = (
        f"Version(Path({str(version_root)!r}),Path({str(python)!r}),Path({str(profile)!r}),{{}});"
        f"Path({str(data)!r})"
    )
    argv = [str(python), "-X", "utf8", "-I", "-c", script]
    unit = tmp_path / "astrid.service"
    unit.write_text("[Service]\nExecStart=" + " ".join(shlex.quote(value) for value in argv) + "\n")
    runtime_pid = 4242

    monkeypatch.setattr(installed_surface, "_process_executable", lambda _pid: python.resolve())
    monkeypatch.setattr(installed_surface, "_canonical_service_argv", lambda *_args: list(argv))
    monkeypatch.setattr(
        installed_surface,
        "_command",
        lambda *_args, **_kwargs: (
            f"LoadState=loaded\nActiveState=active\nFragmentPath={unit}\nMainPID={runtime_pid}\n"
        ),
    )
    installed_surface._native_service(installed, data, "linux", runtime_pid)

    monkeypatch.setattr(
        installed_surface,
        "_command",
        lambda *_args, **_kwargs: (
            "LoadState=loaded\nActiveState=active\n"
            f"FragmentPath={unit}\nMainPID={runtime_pid + 1}\n"
        ),
    )
    with pytest.raises(installed_surface.SurfaceError, match="observed Runtime PID"):
        installed_surface._native_service(installed, data, "linux", runtime_pid)

    fake_script = "print('fake');" + script
    unit.write_text(
        "[Service]\nExecStart="
        + " ".join(shlex.quote(value) for value in [*argv[:-1], fake_script])
        + "\n"
    )
    monkeypatch.setattr(
        installed_surface,
        "_command",
        lambda *_args, **_kwargs: (
            f"LoadState=loaded\nActiveState=active\nFragmentPath={unit}\nMainPID={runtime_pid}\n"
        ),
    )
    with pytest.raises(installed_surface.SurfaceError, match="exact installed Runtime command"):
        installed_surface._native_service(installed, data, "linux", runtime_pid)


def test_windows_service_binds_exact_action_and_running_instance_pid(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    _install(
        root,
        data,
        {"release": 2, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}},
        managed_by=None,
    )
    installed = installed_surface._installed(root, data)
    pythonw = Path(installed["python"]).with_name("pythonw.exe")
    expected = [str(pythonw), "-X", "utf8", "-I", "-c", "exact installed source"]
    runtime_pid = 5252
    monkeypatch.setattr(installed_surface, "_process_executable", lambda _pid: pythonw.resolve())
    monkeypatch.setattr(installed_surface, "_canonical_service_argv", lambda *_args: list(expected))
    responses = iter(
        [
            json.dumps(
                {
                    "Execute": expected[0],
                    "Arguments": subprocess.list2cmdline(expected[1:]),
                }
            ),
            json.dumps([runtime_pid]),
        ]
    )
    monkeypatch.setattr(installed_surface, "_powershell", lambda *_args: next(responses))
    installed_surface._native_service(installed, data, "windows", runtime_pid)

    responses = iter(
        [
            json.dumps(
                {
                    "Execute": expected[0],
                    "Arguments": subprocess.list2cmdline(expected[1:]),
                }
            ),
            json.dumps([runtime_pid + 1]),
        ]
    )
    monkeypatch.setattr(installed_surface, "_powershell", lambda *_args: next(responses))
    with pytest.raises(installed_surface.SurfaceError, match="observed Runtime PID"):
        installed_surface._native_service(installed, data, "windows", runtime_pid)


def test_installed_cleanup_rejects_any_owned_program_root_residue(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    data.mkdir()
    refs = {name: (name[0] * 40) for name in ("app", "agent", "runtime", "pinokio")}
    request = _request(
        action="cleanup",
        refs=refs,
        root=root,
        data=data,
        channel="install-line",
        mode="direct",
        payload={"owned_roots_only": True},
    )
    monkeypatch.setattr(installed_surface, "_checkouts", lambda *_args: {})
    monkeypatch.setattr(installed_surface, "_runtime_owner_pids", lambda: [])
    args = Namespace(
        app_checkout=Path("/unused"),
        agent_checkout=Path("/unused"),
        runtime_checkout=Path("/unused"),
        pinokio_checkout=Path("/unused"),
        semantic_path="qualification/semantic-snapshot.json",
        sentinel_path="qualification/unrelated-sentinel.json",
        journal_name=".installed-probe.json",
    )

    with pytest.raises(installed_surface.SurfaceError, match="left the program root"):
        installed_surface.execute(args, _envelope("installed", request))


def test_browser_command_authenticates_candidate_then_refuses_before_browser_or_post(tmp_path):
    app = tmp_path / "app"
    app_ref = _repo(app)
    data = tmp_path / "data"
    request = _request(
        action="button-unsaved-draft",
        refs={
            "app": app_ref,
            "agent": "a" * 40,
            "runtime": "b" * 40,
            "pinokio": "c" * 40,
        },
        root=tmp_path / "program",
        data=data,
        channel="button",
        mode="direct",
        payload={"surface": "browser", "save": "failed", "request": "explicit-update"},
    )
    result = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "browser_automation_surface.py"),
            "--app-checkout",
            str(app),
        ],
        input_text=json.dumps(_envelope("browser", request)),
    )

    assert result.returncode == 1
    assert (
        "NEEDS: App must expose an installed draft-flush guard and browser command" in result.stderr
    )
    assert not data.exists()


FAKE_BOOTSTRAP = r"""#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

values = sys.argv[1:]
def option(name):
    return values[values.index(name) + 1]

root = Path(option("--root"))
data = Path(option("--data-root"))
manifest = json.loads(Path(option("--manifest")).read_text())
version_root = root / "versions" / "release-2"
version_root.mkdir(parents=True, exist_ok=True)
python = version_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
python.parent.mkdir(parents=True, exist_ok=True)
try:
    python.symlink_to(Path(sys.executable).resolve())
except OSError:
    shutil.copy2(Path(sys.executable).resolve(), python)
profile = version_root / "profile.json"
profile.write_text("{}\n")
version = {
    "root": str(version_root),
    "python": str(python),
    "profile": str(profile),
    "env": {},
    "identity": {"release": 2, "managed_by": None},
}
(version_root / "version.json").write_text(json.dumps(version) + "\n")
(version_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
(root / "current").write_text("release-2\n")
(root / "install.json").write_text(json.dumps({
    "owner": "haus.astrid.runtime", "data_root": str(data), "managed_by": None,
}) + "\n")
"""


def test_update_command_runs_real_public_bootstrap_with_separate_argv_and_journals(tmp_path):
    paths, refs = _checkouts(tmp_path, {"public/install.py": FAKE_BOOTSTRAP})
    root, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    _install(
        root,
        data,
        {
            "release": 1,
            "agent": {"sha": refs["agent"]},
            "runtime": {"sha": refs["runtime"]},
        },
        managed_by=None,
    )
    target = {
        "agent": "1" * 40,
        "runtime": "2" * 40,
        "schema_digest": "sha256:" + "3" * 64,
    }
    manifest = tmp_path / "stand-in.json"
    manifest.write_text(
        json.dumps(
            {
                "release": 2,
                "qualification_marker": "M4-5 test stand-in",
                "agent": {"sha": target["agent"]},
                "runtime": {
                    "sha": target["runtime"],
                    "schema_digest": target["schema_digest"],
                },
                "vibecomfy": {"sha": "4" * 40},
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    request = _request(
        action="install-line-update",
        refs=refs,
        root=root,
        data=data,
        channel="install-line",
        mode="direct",
        stand_in={"manifest_sha256": digest, "target": target},
        payload={
            "update_flag": True,
            "manifest_sha256": digest,
            "root_bound": True,
            "data_root_bound": True,
        },
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "update_operation_surface.py"),
        *_surface_args(paths),
        "--stand-in-manifest",
        str(manifest),
        "--bootstrap",
        str(paths["app"] / "public" / "install.py"),
        "--shell",
        sys.executable,
    ]
    result = _run(command, input_text=json.dumps(_envelope("update", request)))

    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response["observation"] == {
        "channel": "install-line",
        "entrypoint": "public-bootstrap",
        **request["payload"],
    }
    journal = json.loads((data / ".astrid-m4-5-update-surface.json").read_text())
    operation = journal["operation"]
    assert operation["state"] == "complete"
    assert operation["argv"][-7:] == [
        "--manifest",
        str(manifest),
        "--root",
        str(root),
        "--data-root",
        str(data),
        "--update",
    ]
    installed_manifest = root / "versions" / "release-2" / "manifest.json"
    installed_value = json.loads(installed_manifest.read_text())
    assert installed_value["qualification_marker"] == "M4-5 test stand-in"

    installed_manifest.write_text(json.dumps(installed_value, separators=(",", ":")) + "\n")
    with pytest.raises(update_surface.SurfaceError, match="did not select"):
        update_surface._validate_release2(root, data, request, digest)


def test_current_accepted_stand_in_fails_before_bootstrap_or_journal(tmp_path):
    marker = tmp_path / "bootstrap-invoked"
    bootstrap = (
        "#!/usr/bin/env python3\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('invoked')\n"
    )
    paths, refs = _checkouts(tmp_path, {"public/install.py": bootstrap})
    root, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    _install(
        root,
        data,
        {
            "release": 1,
            "agent": {"sha": refs["agent"]},
            "runtime": {"sha": refs["runtime"]},
        },
        managed_by=None,
    )
    accepted = ROOT / "tests" / "fixtures" / "release0" / "release2" / "installable-stand-in.json"
    manifest = json.loads(accepted.read_text())
    digest = hashlib.sha256(accepted.read_bytes()).hexdigest()
    assert digest == "66af38efb5109920cc01685487d4594b3787674aba8f49c6e5d951ac8f763fe8"
    assert "vibecomfy" not in manifest
    request = _request(
        action="install-line-update",
        refs=refs,
        root=root,
        data=data,
        channel="install-line",
        mode="direct",
        stand_in={
            "manifest_sha256": digest,
            "target": {
                "agent": manifest["agent"]["sha"],
                "runtime": manifest["runtime"]["sha"],
                "schema_digest": manifest["runtime"]["schema_digest"],
            },
        },
        payload={
            "update_flag": True,
            "manifest_sha256": digest,
            "root_bound": True,
            "data_root_bound": True,
        },
    )
    request["inputs"]["stand_in_marker"] = manifest["qualification_marker"]
    result = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "update_operation_surface.py"),
            *_surface_args(paths),
            "--stand-in-manifest",
            str(accepted),
            "--bootstrap",
            str(paths["app"] / "public" / "install.py"),
            "--shell",
            sys.executable,
        ],
        input_text=json.dumps(_envelope("update", request)),
    )

    assert result.returncode == 1
    assert (
        "NEEDS: Agent installer must preserve the accepted stand-in manifest bytes" in result.stderr
    )
    assert digest in result.stderr
    assert "omits vibecomfy" in result.stderr
    assert not marker.exists()
    assert not (data / ".astrid-m4-5-update-surface.json").exists()
    assert (root / "current").read_text().strip() == "release-1"


@pytest.mark.parametrize("action", ["button-update-lost-ack", "button-update-explicit"])
def test_update_command_never_mints_or_posts_a_replacement_operation(tmp_path, action):
    paths, refs = _checkouts(tmp_path, {"public/install.py": "# unused\n"})
    manifest = tmp_path / "stand-in.json"
    manifest.write_text("{}\n")
    data = tmp_path / "data"
    request = _request(
        action=action,
        refs=refs,
        root=tmp_path / "program",
        data=data,
        channel="button",
        mode="direct",
        payload=(
            {"request": "explicit-update", "draft_flush": "pass", "simulate_lost_ack": True}
            if action == "button-update-lost-ack"
            else {"request": "explicit-update", "operation_id": "owned-operation"}
        ),
    )
    result = _run(
        [
            sys.executable,
            str(ROOT / "scripts" / "update_operation_surface.py"),
            *_surface_args(paths),
            "--stand-in-manifest",
            str(manifest),
            "--bootstrap",
            str(paths["app"] / "public" / "install.py"),
        ],
        input_text=json.dumps(_envelope("update", request)),
    )

    assert result.returncode == 1
    assert (
        "NEEDS: Runtime must reconcile the same recovery-needed operation under lock"
        in result.stderr
    )
    assert not data.exists()


def test_cleanup_invokes_installed_uninstall_as_separate_argv_and_verifies_selection_removed(
    tmp_path, monkeypatch
):
    root, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    version_root = _install(
        root,
        data,
        {"release": 2, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}},
        managed_by=None,
    )
    installed_python = Path(json.loads((version_root / "version.json").read_text())["python"])
    refs = {name: (name[0] * 40) for name in ("app", "agent", "runtime", "pinokio")}
    request = _request(
        action="cleanup",
        refs=refs,
        root=root,
        data=data,
        channel="install-line",
        mode="direct",
        payload={"owned_roots_only": True},
    )
    journal = {
        "format": update_surface.JOURNAL_FORMAT,
        "scenario": request["scenario"],
        "candidate": refs,
        "operation": {
            "action": "install-line-update",
            "state": "complete",
            "installed_python_sha256": hashlib.sha256(installed_python.read_bytes()).hexdigest(),
        },
        "cleanup": "pending",
    }
    journal_path = data / ".update-journal.json"
    journal_path.write_text(json.dumps(journal) + "\n")
    captured: list[str] = []

    def run_logged(argv, **_kwargs):
        captured.extend(argv)
        (root / "current").unlink()
        return "f" * 64

    monkeypatch.setattr(update_surface, "_run_logged", run_logged)
    args = Namespace(
        app_checkout=Path("/unused"),
        agent_checkout=Path("/unused"),
        runtime_checkout=Path("/unused"),
        pinokio_checkout=Path("/unused"),
        stand_in_manifest=Path("/unused"),
        bootstrap=Path("/unused"),
        shell=None,
        pinokio_action=None,
        journal_name=journal_path.name,
        timeout=90.0,
    )

    observation, _ = update_surface._cleanup(
        args,
        _envelope("update", request, handoff="f" * 64),
        refs,
        time.monotonic() + 10,
    )

    assert observation == {"cleanup": "pass"}
    assert captured == [
        str(installed_python),
        "-I",
        "-m",
        "astrid",
        "install",
        "--uninstall",
        "--root",
        str(root),
        "--data-root",
        str(data),
    ]
    assert json.loads(journal_path.read_text())["cleanup"] == "complete"
    assert not os.path.lexists(root)


def test_cleanup_refuses_same_owner_residue_when_complete_marker_changes(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "data"
    data.mkdir()
    version_root = _install(
        root,
        data,
        {"release": 2, "agent": {"sha": "a" * 40}, "runtime": {"sha": "b" * 40}},
        managed_by=None,
    )
    installed_python = Path(json.loads((version_root / "version.json").read_text())["python"])
    refs = {name: (name[0] * 40) for name in ("app", "agent", "runtime", "pinokio")}
    request = _request(
        action="cleanup",
        refs=refs,
        root=root,
        data=data,
        channel="install-line",
        mode="direct",
        payload={"owned_roots_only": True},
    )
    journal_path = data / ".update-journal.json"
    journal_path.write_text(
        json.dumps(
            {
                "format": update_surface.JOURNAL_FORMAT,
                "scenario": request["scenario"],
                "candidate": refs,
                "operation": {
                    "action": "install-line-update",
                    "state": "complete",
                    "installed_python_sha256": hashlib.sha256(
                        installed_python.read_bytes()
                    ).hexdigest(),
                },
                "cleanup": "pending",
            }
        )
        + "\n"
    )

    def retain_broken_selection(_argv, **_kwargs):
        (root / "current").unlink()
        (root / "current").symlink_to("missing-version")
        marker = json.loads((root / "install.json").read_text())
        marker["unexpected_retained_field"] = "changed-after-uninstall"
        (root / "install.json").write_text(json.dumps(marker) + "\n")
        return "f" * 64

    monkeypatch.setattr(update_surface, "_run_logged", retain_broken_selection)
    args = Namespace(
        app_checkout=Path("/unused"),
        agent_checkout=Path("/unused"),
        runtime_checkout=Path("/unused"),
        pinokio_checkout=Path("/unused"),
        stand_in_manifest=Path("/unused"),
        bootstrap=Path("/unused"),
        shell=None,
        pinokio_action=None,
        journal_name=journal_path.name,
        timeout=90.0,
    )

    with pytest.raises(
        update_surface.SurfaceError, match="without the exact retained owner/data-root marker"
    ):
        update_surface._cleanup(
            args,
            _envelope("update", request, handoff="f" * 64),
            refs,
            time.monotonic() + 10,
        )
    assert os.path.lexists(root / "current")
    assert not (root / "current").exists()


def test_product_child_inherits_the_adapter_owned_process_group(tmp_path, monkeypatch):
    observed: dict = {}

    class Process:
        returncode = 0

        def communicate(self):
            return b"product output", b""

    def popen(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr(update_surface.subprocess, "Popen", popen)
    log = tmp_path / "operation.log"
    update_surface._run_logged(
        ["product", "--separate", "argv"],
        log=log,
        deadline=time.monotonic() + 5,
        env={"PATH": os.defpath},
        label="disposable operation",
    )

    assert observed["argv"] == ["product", "--separate", "argv"]
    assert "start_new_session" not in observed["kwargs"]
    assert "creationflags" not in observed["kwargs"]
    assert log.read_text() == "product output"


def test_private_log_is_portable_and_hashes_exact_crlf_bytes(tmp_path, monkeypatch):
    content = b"windows line\r\n"

    class Process:
        returncode = 0

        def communicate(self):
            return content, b""

    monkeypatch.delattr(update_surface.os, "fchmod", raising=False)
    monkeypatch.setattr(update_surface.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    log = tmp_path / "windows-private.log"

    digest = update_surface._run_logged(
        ["product"],
        log=log,
        deadline=time.monotonic() + 5,
        env={"PATH": os.defpath},
        label="portable Windows-style operation",
    )

    assert log.read_bytes() == content
    assert digest == hashlib.sha256(content).hexdigest()


def test_communication_error_kills_adapter_owned_group(tmp_path, monkeypatch):
    events: list[object] = []

    class GroupKilled(BaseException):
        pass

    class Process:
        returncode = None

        def communicate(self):
            events.append("communicate")
            raise OSError("broken process pipe")

    monkeypatch.setattr(update_surface.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(update_surface.os, "getpid", lambda: 4141)
    monkeypatch.setattr(update_surface.os, "getpgrp", lambda: 4141)

    def killpg(group, sent_signal):
        events.append(("killpg", group, sent_signal))
        raise GroupKilled

    monkeypatch.setattr(update_surface.os, "killpg", killpg)

    with pytest.raises(GroupKilled):
        update_surface._run_logged(
            ["product"],
            log=tmp_path / "failed-communication.log",
            deadline=time.monotonic() + 5,
            env={"PATH": os.defpath},
            label="broken communication operation",
        )

    assert events == ["communicate", ("killpg", 4141, update_surface.signal.SIGKILL)]


@pytest.mark.posix_native
def test_communication_error_kills_real_product_descendant(tmp_path):
    child_pid_path = tmp_path / "communication-child.pid"
    log = tmp_path / "communication-product.log"
    launcher = tmp_path / "communication-launcher.py"
    child_source = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    launcher.write_text(
        "import os,subprocess,sys,time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts import update_operation_surface as surface\n"
        "real_popen = subprocess.Popen\n"
        "class BrokenPopen:\n"
        "    def __init__(self, argv, **kwargs):\n"
        "        self.child = real_popen(argv, **kwargs)\n"
        "        self.pid = self.child.pid\n"
        "        self.returncode = None\n"
        "    def communicate(self):\n"
        f"        marker = {str(child_pid_path)!r}\n"
        "        deadline = time.monotonic() + 5\n"
        "        while not os.path.exists(marker) and time.monotonic() < deadline:\n"
        "            time.sleep(0.01)\n"
        "        raise OSError('disposable pipe failure')\n"
        "surface.subprocess.Popen = BrokenPopen\n"
        f"surface._run_logged([sys.executable, '-c', {child_source!r}, "
        f"{str(child_pid_path)!r}], log=surface.Path({str(log)!r}), "
        "deadline=time.monotonic()+90, env={'PATH': os.defpath}, "
        "label='adapter-group communication failure')\n"
    )

    surface_process = subprocess.Popen(
        [sys.executable, str(launcher)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert surface_process.wait(timeout=10) == -update_surface.signal.SIGKILL
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 3
    while update_surface._pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not update_surface._pid_alive(child_pid)


def test_adapter_short_deadline_kills_surface_product_descendant(tmp_path):
    child_pid_path = tmp_path / "adapter-child.pid"
    log = tmp_path / "adapter-product.log"
    launcher = tmp_path / "surface-launcher.py"
    child_source = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    )
    launcher.write_text(
        "import os,sys,time\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts.update_operation_surface import _run_logged\n"
        f"_run_logged([sys.executable, '-c', {child_source!r}, {str(child_pid_path)!r}], "
        f"log=__import__('pathlib').Path({str(log)!r}), deadline=time.monotonic()+90, "
        "env={'PATH': os.defpath}, label='adapter-owned product')\n"
    )

    with pytest.raises(boundary_adapter.AdapterError, match="update interface timed out"):
        boundary_adapter._invoke_interface(
            surface="update",
            command=[sys.executable, str(launcher)],
            envelope={"probe": "adapter-owned-tree"},
            deadline=time.monotonic() + 0.5,
        )

    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 3
    while update_surface._pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not update_surface._pid_alive(child_pid)


def test_product_child_env_and_private_log_exclude_ambient_credentials(tmp_path, monkeypatch):
    gh_secret = "gh-secret-sentinel"
    api_secret = "api-secret-sentinel"
    monkeypatch.setenv("GH_TOKEN", gh_secret)
    monkeypatch.setenv("EXAMPLE_API_KEY", api_secret)
    env = update_surface._child_env(
        {"SAFE_INSTALLED_VALUE": "kept", "INSTALLED_PRIVATE_KEY": "not-kept"}
    )
    assert "GH_TOKEN" not in env
    assert "EXAMPLE_API_KEY" not in env
    assert "INSTALLED_PRIVATE_KEY" not in env
    assert env["SAFE_INSTALLED_VALUE"] == "kept"

    log = tmp_path / "private.log"
    update_surface._run_logged(
        [
            sys.executable,
            "-c",
            "import json,os; print(json.dumps(dict(os.environ),sort_keys=True))",
        ],
        log=log,
        deadline=time.monotonic() + 5,
        env=env,
        label="credential isolation",
    )
    logged = log.read_text()
    assert gh_secret not in logged
    assert api_secret not in logged
    assert "INSTALLED_PRIVATE_KEY" not in logged
    assert stat.S_IMODE(log.stat().st_mode) == 0o600

    target = tmp_path / "target.log"
    target.write_text("preserved")
    linked = tmp_path / "linked.log"
    linked.symlink_to(target)
    with pytest.raises(update_surface.SurfaceError, match="pre-existing"):
        update_surface._run_logged(
            [sys.executable, "-c", "print('must not run')"],
            log=linked,
            deadline=time.monotonic() + 5,
            env=env,
            label="symlinked",
        )
    assert target.read_text() == "preserved"
