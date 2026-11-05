from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest
import runtime_protocol

from astrid.installer import cli
from astrid.installer.service import LABEL
from tests.lanes import unavailable

AGENT_ROOT = Path(__file__).resolve().parents[1]
APP_CHECKOUT_ENV = "ASTRID_APP_CHECKOUT"
SOURCE_REFUSAL = "This Astrid runs from source checkouts; update those instead."


def _app_root() -> Path:
    checkout = os.environ.get(APP_CHECKOUT_ENV)
    if not checkout:
        unavailable(f"set {APP_CHECKOUT_ENV} to a banodoco/astrid-app checkout")
    root = Path(checkout).expanduser().resolve()
    if not (root / "public/install").is_file() or not (root / "public/install.ps1").is_file():
        unavailable(f"{APP_CHECKOUT_ENV} must point to a checkout containing public/install and public/install.ps1")
    return root


def _git_checkout(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return path


@pytest.mark.parametrize("selection_mode", ["explicit-custom", "default-selection"])
def test_cli_source_refusal_precedes_record_writes_and_stop(tmp_path, monkeypatch, capsys, selection_mode):
    from runtime_protocol import platform

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    root = (tmp_path / "program" if selection_mode == "explicit-custom"
            else cli.default_program_root(home, sys.platform))
    data = tmp_path / "workspace"
    root.mkdir(parents=True)
    if selection_mode == "default-selection":
        (root / "install.json").write_text(json.dumps({"owner": LABEL, "data_root": str(data)}), encoding="utf-8")
    marker_before = (root / "install.json").read_bytes() if (root / "install.json").exists() else None
    writes, stops = [], []

    class FakeService:
        selection = home / "data" / "astrid-install.json"
        system = sys.platform
        path = home / "service" / "astrid.service"

        def recorded(self):
            if selection_mode == "default-selection":
                return {"owner": LABEL, "program_root": str(root), "data_root": str(data)}
            return {}

    FakeService.home = home

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            self.service = FakeService()

        def source_checkout_ownership(self, _data, _installed):
            return True

        def preflight_stop(self, *_args):
            stops.append("preflight")

    monkeypatch.setattr(cli, "Backend", FakeBackend)
    monkeypatch.setattr(cli, "write_json", lambda *args: writes.append(args))
    argv = (["--root", str(root), "--data-root", str(data), "--yes"]
            if selection_mode == "explicit-custom" else [])
    assert cli.main(argv) == 1
    output = capsys.readouterr().out
    assert SOURCE_REFUSAL in output
    assert writes == []
    assert stops == []
    assert ((root / "install.json").read_bytes() if (root / "install.json").exists() else None) == marker_before
    assert not (FakeService.selection).exists()


def test_real_source_guard_preserves_retained_ownership_bytes(tmp_path, monkeypatch, capsys):
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol import platform

    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "workspace"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "process_identity", lambda _pid: None)
    root.mkdir()
    runtime, agent = _git_checkout(tmp_path / "runtime"), _git_checkout(tmp_path / "agent")
    paths = RuntimePaths.current(data_root=data)
    paths.source_profiles_dir.mkdir(parents=True)
    profile = paths.source_profiles_dir / "astrid.json"
    profile.write_text(json.dumps({"profile": "astrid", "runtime_checkout": str(runtime),
                                   "source_checkout": str(agent)}), encoding="utf-8")
    paths.catalog_path.parent.mkdir(parents=True, exist_ok=True)
    paths.catalog_path.write_text('{"version":1,"selected_realm_id":null,"realms":[],"source_profiles":{}}\n',
                                  encoding="utf-8")
    paths.discovery_path.write_text('{"pid":4242,"process_birth_id":"stale-birth",'
                                    '"runtime_instance_id":"stale-instance","endpoint":"http://127.0.0.1:1"}\n',
                                    encoding="utf-8")
    before = {path: path.read_bytes() for path in (profile, paths.catalog_path)}
    discovery_before = paths.discovery_path.read_bytes()

    assert cli.main(["--root", str(root), "--data-root", str(data), "--yes"]) == 1
    assert SOURCE_REFUSAL in capsys.readouterr().out
    assert {path: path.read_bytes() for path in before} == before
    assert paths.discovery_path.read_bytes() == discovery_before
    assert not (root / "install.json").exists()
    assert not (home / "data" / "astrid-install.json").exists()


@pytest.mark.parametrize("health", [
    {"runtime_instance_id": "instance", "identity": None},
    {"runtime_instance_id": "instance", "identity": {"release": True, "managed_by": None}},
    {"runtime_instance_id": "other-owner", "identity": {"release": 1, "managed_by": None}},
])
def test_real_cli_invalid_live_identity_refuses_before_writes_and_stop(tmp_path, monkeypatch, capsys, health):
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol import platform

    from astrid.installer.backend import Backend

    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": "birth"})
    root.mkdir()
    paths = RuntimePaths.current(data_root=data)
    paths.discovery_path.parent.mkdir(parents=True)
    paths.discovery_path.write_text(json.dumps({
        "pid": 4242, "process_birth_id": "birth", "runtime_instance_id": "instance",
        "endpoint": "http://127.0.0.1:12345",
    }))
    before = paths.discovery_path.read_bytes()
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity",
                        lambda *_args, **_kwargs: health)
    writes, stops = [], []
    monkeypatch.setattr(cli, "write_json", lambda *args: writes.append(args))
    monkeypatch.setattr(Backend, "preflight_stop", lambda *_args: stops.append("preflight"))
    monkeypatch.setattr(Backend, "stop", lambda *_args: stops.append("stop"))
    assert cli.main(["--root", str(root), "--data-root", str(data), "--yes"]) == 1
    assert "Your work is kept" in capsys.readouterr().out
    assert "Live Runtime startup identity cannot be verified" in (tmp_path / "logs" / "install.log").read_text()
    assert writes == stops == []
    assert paths.discovery_path.read_bytes() == before
    assert not (root / "install.json").exists()
    assert not (home / "data" / "astrid-install.json").exists()


def _fake_uv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("""#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
args = sys.argv[1:]
if args == ['--version']:
    print('uv 0.11.32')
    raise SystemExit(0)
if args[:2] == ['python', 'install']:
    pathlib.Path(os.environ['UV_PYTHON_INSTALL_DIR']).mkdir(parents=True, exist_ok=True)
    raise SystemExit(0)
if args[:1] == ['venv']:
    target = pathlib.Path(args[1])
    bindir = target / ('Scripts' if os.name == 'nt' else 'bin')
    bindir.mkdir(parents=True, exist_ok=True)
    (bindir / ('python.exe' if os.name == 'nt' else 'python')).symlink_to(sys.executable)
    raise SystemExit(0)
if args[:2] == ['pip', 'install']:
    raise SystemExit(0)
if '-c' in args:
    index = args.index('-c')
    script = args[index + 1]
    tail = args[index + 2:]
    raise SystemExit(subprocess.call([sys.executable, '-c', script, *tail], env=os.environ.copy()))
if '-m' in args and args[args.index('-m') + 1:args.index('-m') + 3] == ['astrid', 'install']:
    index = args.index('-m') + 3
    pathlib.Path(os.environ['MOCK_CORE_ARGS']).write_text(json.dumps(args[index:]))
    code = "import runpy,sys; sys.argv=['astrid','install',*sys.argv[1:]]; runpy.run_module('astrid',run_name='__main__')"
    raise SystemExit(subprocess.call([sys.executable, '-c', code, *args[index:]], env=os.environ.copy()))
raise SystemExit('unhandled fake uv args: ' + repr(args))
""", encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("handoff", ["direct", "pipe"])
@pytest.mark.parametrize("forwarded_flags", [[], ["--update"]])
@pytest.mark.app_checkout
def test_real_posix_bootstrap_handoff_preserves_refusal_status_and_update_flag(
        tmp_path, handoff, forwarded_flags):
    app_root = _app_root()
    home = tmp_path / "home"
    xdg = home / ".local" / "share"
    bootstrap = xdg / "astrid" / "bootstrap"
    uv = bootstrap / "uv"
    _fake_uv(uv)
    data, root = tmp_path / "workspace", tmp_path / "program"
    runtime, agent = _git_checkout(tmp_path / "runtime"), _git_checkout(tmp_path / "agent")
    profile = data / "runtime" / "source-profiles" / "astrid.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(json.dumps({"profile": "astrid", "runtime_checkout": str(runtime),
                                   "source_checkout": str(agent)}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "tools": {"uv": {"version": "0.11.32"}, "python": {"version": "3.11.13"}},
        "agent": {"sha": "a" * 40, "repo": str(AGENT_ROOT)},
        "runtime": {"sha": "b" * 40, "repo": str(AGENT_ROOT)},
    }), encoding="utf-8")
    runtime_import_root = Path(runtime_protocol.__file__).resolve().parent.parent
    env = os.environ.copy()
    env.update({"HOME": str(home), "USERPROFILE": str(home), "XDG_DATA_HOME": str(xdg),
                "ASTRID_INSTALL_UV": str(uv),
                "MOCK_CORE_ARGS": str(tmp_path / "core-args.json"),
                "PYTHONPATH": os.pathsep.join((str(AGENT_ROOT), str(runtime_import_root)))})
    args = ["--manifest", str(manifest), "--root", str(root), "--data-root", str(data),
            *forwarded_flags]
    command = (["sh", str(app_root / "public/install"), *args] if handoff == "direct"
               else ["sh", "-s", "--", *args])
    result = subprocess.run(command, input=(app_root / "public/install").read_bytes() if handoff == "pipe" else None,
                            cwd=tmp_path, env=env, text=False, capture_output=True, timeout=90)
    combined = result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
    assert result.returncode != 0
    assert SOURCE_REFUSAL in combined
    forwarded = json.loads((tmp_path / "core-args.json").read_text(encoding="utf-8"))
    assert forwarded[forwarded.index("--manifest") + 1] == str(manifest)
    assert forwarded[forwarded.index("--root") + 1] == str(root)
    assert ("--update" in forwarded) is ("--update" in forwarded_flags)
    assert not (root / "install.json").exists()
    assert not (xdg / "astrid-install.json").exists()


@pytest.mark.app_checkout
def test_windows_bootstrap_forwards_core_refusal_and_keeps_scriptblock_open():
    script = (_app_root() / "public/install.ps1").read_text(encoding="utf-8")
    assert "$RunArgs += $InstallerArgs" in script
    assert "if (-not $ManifestProvided) { $RunArgs += @('--manifest', $Manifest) }" in script
    assert "--update" not in script.split("$RunArgs += $InstallerArgs", 1)[0]
    assert "if ($FileInvocation) {" in script and "exit $NativeExitCode" in script
    assert "throw $Message" in script
    assert "# Throw instead of exit: irm | iex must never close the user's shell." in script


@pytest.mark.parametrize("case", ["source", "unreadable", "changed", "legacy_birth", "legacy_unknown"])
def test_selected_r0_refuses_before_preflight_or_stop(tmp_path, monkeypatch, capsys, case):
    from astrid.installer.backend import Backend
    from astrid.installer.core import current, write_json
    from astrid_runtime.paths import RuntimePaths
    from runtime_protocol import platform

    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "data"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    # The R0 Linux/macOS service captures launch versions/<id>/py/bin/python
    # with versions/<id>/profile.json. Reconstruct the R0 record format.
    version = root / "versions" / "3204bb00-6d53a190-a813c6d5"
    python = version / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("fixture, never executed")
    write_json(version / "version.json", {"root": str(version), "python": str(python),
                                         "profile": str(version / "profile.json"), "env": {}})
    write_json(version / "manifest.json", {"release": 0})
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    (root / "current").write_text(version.name + "\n")
    assert current(root).release == 0
    paths = RuntimePaths.current(data_root=data)
    birth = "proc-start-ticks:12812" if case in {"legacy_birth", "legacy_unknown"} else "birth"
    discovery = {"pid": 4242, "process_birth_id": birth, "runtime_instance_id": "instance",
                 "endpoint": "http://127.0.0.1:12345"}
    write_json(paths.discovery_path, discovery)
    before = {path: path.read_bytes() for path in (root / "current", root / "install.json",
                                                     version / "version.json", paths.discovery_path)}
    observed = {"legacy_birth": "proc-start-ticks:12812;boot:00000000-0000-0000-0000-000000000001;pidns:7",
                "legacy_unknown": None}.get(case, "birth")
    monkeypatch.setattr(platform, "process_identity", lambda pid: {"pid": pid, "birth_id": observed})
    source = {"release": 0, "managed_by": "source", "runtime_head": "a" * 40,
              "agent_head": "b" * 40, "dirty": False, "dirty_hash": "c" * 64}
    def health(*_args, **_kwargs):
        if case == "changed":
            write_json(paths.discovery_path, {**discovery, "runtime_instance_id": "replacement"})
        return {"runtime_instance_id": "instance", "identity": source}
    monkeypatch.setattr("astrid_runtime.runtime_boundary.LocalRuntimeBoundary.owner_startup_identity", health)
    if case == "unreadable":
        read_bytes = Path.read_bytes
        def read(path):
            if path == paths.discovery_path:
                raise PermissionError("simulated unreadable discovery")
            return read_bytes(path)
        monkeypatch.setattr(Path, "read_bytes", read)
    calls = []
    monkeypatch.setattr(Backend, "preflight_stop", lambda *_args: calls.append("preflight"))
    monkeypatch.setattr(Backend, "stop", lambda *_args: calls.append("stop"))
    monkeypatch.setattr(cli, "write_json", lambda *_args: calls.append("write"))
    assert cli.main(["--root", str(root), "--data-root", str(data)]) == 1
    assert "Your work is kept" in capsys.readouterr().out
    assert calls == []
    log = (tmp_path / "logs" / "install.log").read_text()
    expected = {"source": "source identity conflicts", "unreadable": "cannot be read",
                "changed": "Live Runtime startup identity cannot be verified",
                "legacy_birth": "discovery owner identity changed",
                "legacy_unknown": "discovery owner identity changed"}
    assert expected[case] in log
    for path, contents in before.items():
        if path != paths.discovery_path:
            assert path.read_bytes() == contents
    if case != "unreadable":
        assert (paths.discovery_path.read_bytes() != before[paths.discovery_path]) == (case == "changed")
    assert not (home / "data" / "astrid-install.json").exists()


@pytest.mark.app_checkout
def test_windows_bootstrap_streams_each_customize_prompt_before_input():
    script = (_app_root() / "public/install.ps1").read_text(encoding="utf-8")
    assert "process.OutputDataReceived += (sender, data) => {" in script
    assert "if (streamOutput && !failurePending) Console.WriteLine(data.Data);" in script
    assert "process.BeginOutputReadLine();" in script
    assert "$Result = Invoke-InstallerCommand $Uv $RunArgs -StreamOutput" in script

    child_code = (
        "from astrid.installer.cli import _prompt_choices; "
        "_prompt_choices()"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(AGENT_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code],
        cwd=AGENT_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    delivered = queue.Queue()

    def read_lines():
        assert process.stdout is not None
        for line in process.stdout:
            delivered.put(line)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()
    answers = iter(["c", "", "n", "", ""])
    expected = [
        "Continue with this setup? [Y/n/c]",
        "Start Astrid at sign-in? [Y/n]",
        "Add the astrid terminal command? [Y/n]",
        "Install Astrid skills for detected agents? [Y/n]",
        "Use copies for skills? [y/N]",
    ]
    observed = []
    try:
        for prompt in expected:
            while True:
                line = delivered.get(timeout=5)
                observed.append(line.rstrip("\r\n"))
                if observed[-1] == prompt:
                    break
            assert line.endswith("\n"), f"prompt was not newline-terminated: {line!r}"
            assert process.stdin is not None
            process.stdin.write(next(answers) + "\n")
            process.stdin.flush()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()
    assert [line for line in observed if "? [" in line] == expected
