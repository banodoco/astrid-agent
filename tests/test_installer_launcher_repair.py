"""Installed launcher behavior and repair's service health decision."""

import json
import os
import runpy
import signal
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol.door import DEFAULT_DOOR_PORT

from astrid.core.util import process as _process
from astrid.installer import launcher
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, VerificationError, Version, persist_install_identity, write_json
from astrid.installer.launcher import install


def test_absolute_launcher_works_with_clean_home_and_unchanged_path(tmp_path):
    root = tmp_path / "Astrid Program Folder"
    version_root = root / "versions" / "v1"
    python = version_root / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    record = {
        "root": str(version_root), "python": str(python),
        "profile": str(version_root / "profile.json"),
        "env": {"ASTRID_TEST_MANAGED": "yes", "ASTRID_INSTALL_RELEASE": "0",
                "ASTRID_INSTALL_MANAGED_BY": ""},
    }
    write_json(version_root / "version.json", record)
    (root / "current").write_text("v1\n", encoding="utf-8")
    launcher, runner = install(root)
    assert launcher.is_absolute() and runner.is_file()
    launcher_text = launcher.read_text(encoding="utf-8")
    assert "-X utf8 -I" in launcher_text

    source_root = str(Path(__file__).resolve().parents[1])
    fake_python = (
        f"#!{sys.executable}\n"
        "import json, os, runpy, sys\n"
        "if len(sys.argv) > 4 and sys.argv[1:4] == ['-X', 'utf8', '-I'] and sys.argv[4].endswith('run_astrid.py'):\n"
        f"    sys.path.insert(0, {source_root!r})\n"
        "    script = sys.argv[4]; sys.argv = [script, *sys.argv[5:]]; runpy.run_path(script, run_name='__main__')\n"
        "else:\n"
        "    print(json.dumps({'args': sys.argv[1:], 'path': os.environ['PATH'], 'home': os.environ['HOME'], 'managed': os.environ['ASTRID_TEST_MANAGED'], 'root': os.environ['ASTRID_INSTALL_PROGRAM_ROOT'], 'release': os.environ['ASTRID_INSTALL_RELEASE'], 'managed_by': os.environ['ASTRID_INSTALL_MANAGED_BY']}))\n"
    )
    python.write_text(fake_python, encoding="utf-8")
    python.chmod(0o755)
    home = tmp_path / "fresh-home"
    home.mkdir()
    inherited_path = os.environ.get("PATH", "/usr/bin:/bin")
    env = {"HOME": str(home), "PATH": inherited_path}
    result = subprocess.run([str(launcher)], cwd=tmp_path, env=env,
                            check=True, capture_output=True, text=True)

    assert json.loads(result.stdout) == {
        "args": ["-X", "utf8", "-I", "-m", "astrid.omp_agent"],
        "path": inherited_path,
        "home": str(home),
        "managed": "yes",
        "root": str(root),
        "release": "0",
        "managed_by": "",
    }

    credentials = subprocess.run(
        [str(launcher), "credential", "--help"], cwd=tmp_path, env=env,
        check=True, capture_output=True, text=True,
    )
    assert "usage: astrid-credential" in credentials.stdout
    assert "prompt for and store a provider credential" in credentials.stdout

    next_root = root / "versions" / "v2"
    next_python = next_root / "py" / "bin" / "python"
    next_python.parent.mkdir(parents=True)
    next_python.write_text(fake_python, encoding="utf-8")
    next_python.chmod(0o755)
    record.update({"root": str(next_root), "python": str(next_python),
                   "profile": str(next_root / "profile.json"),
                   "env": {"ASTRID_TEST_MANAGED": "new", "ASTRID_INSTALL_RELEASE": "0",
                           "ASTRID_INSTALL_MANAGED_BY": ""}})
    write_json(next_root / "version.json", record)
    (root / "current").write_text("v2\n", encoding="utf-8")
    result = subprocess.run([str(launcher)], cwd=tmp_path, env=env,
                            check=True, capture_output=True, text=True)
    assert json.loads(result.stdout)["managed"] == "new"


def test_generated_runner_windows_handoff_waits_and_keeps_argv_and_environment(tmp_path, monkeypatch):
    root = tmp_path / "Astrid Program Folder"
    version_root = root / "versions" / "v1"
    python = version_root / "py" / "Scripts" / "python.exe"
    write_json(version_root / "version.json", {
        "root": str(version_root), "python": str(python),
        "env": {"MANAGED": "selected version", "ASTRID_INSTALL_RELEASE": "0",
                "ASTRID_INSTALL_MANAGED_BY": ""},
    })
    (root / "current").write_text("v1\n", encoding="utf-8")
    _, runner = install(root)
    arguments = ["documents", "create", 'a title with "quotes"', ""]
    monkeypatch.setattr(sys, "argv", [str(runner), *arguments])
    monkeypatch.setattr(_process.sys, "platform", "win32")
    monkeypatch.delenv("MANAGED", raising=False)
    for name in ("execvpe", "execvp", "execve"):
        monkeypatch.setattr(_process.os, name, lambda *_a: pytest.fail("Windows runner must wait"))
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        signal.raise_signal(signal.SIGINT)
        return subprocess.CompletedProcess(argv, 41)

    monkeypatch.setattr(_process.subprocess, "run", run)
    previous_handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(runner), run_name="__main__")
    assert exc.value.code == 41
    assert captured["argv"] == [str(python), "-X", "utf8", "-I", "-m", "astrid.omp_agent", *arguments]
    assert captured["kwargs"] == {
        "env": {**os.environ, "MANAGED": "selected version", "ASTRID_INSTALL_PROGRAM_ROOT": str(root),
                "ASTRID_INSTALL_RELEASE": "0", "ASTRID_INSTALL_MANAGED_BY": ""},
        "check": False,
    }
    assert "MANAGED" not in os.environ
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_generated_windows_launcher_sets_utf8_before_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "os", SimpleNamespace(name="nt", fdopen=os.fdopen, fsync=os.fsync))
    root = tmp_path / "Astrid Program"
    root.mkdir()
    generated, _runner = install(root)
    text = generated.read_text(encoding="utf-8")
    assert 'python.exe" -X utf8 -I "%ASTRID_PROGRAM_ROOT%run_astrid.py"' in text


def test_generated_runner_consumes_stamped_env_without_deriving_identity(tmp_path, monkeypatch):
    root = tmp_path / "program"
    folder = root / "versions/v1"
    version = Version(folder, folder / "python", folder / "profile.json", {}, 31, "pinokio")
    record = version.document()
    # The runner only needs env: legacy identity projection and derivation
    # inputs cannot override a stamp already committed by the installer.
    record["identity"] = None
    write_json(folder / "version.json", record)
    (root / "current").write_text("v1\n")
    (folder / "manifest.json").write_text("invalid json")
    (root / "install.json").write_text("invalid json")
    _, runner = install(root)
    monkeypatch.setenv("ASTRID_INSTALL_RELEASE", "999")
    monkeypatch.setenv("ASTRID_INSTALL_MANAGED_BY", "source")
    monkeypatch.setattr(sys, "argv", [str(runner), "--help"])
    captured = []
    def replace(*args):
        captured.append(args)
        raise SystemExit(0)

    monkeypatch.setattr(_process.os, "execvpe", replace)

    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(runner), run_name="__main__")

    assert result.value.code == 0
    assert captured[0][2]["ASTRID_INSTALL_RELEASE"] == "31"
    assert captured[0][2]["ASTRID_INSTALL_MANAGED_BY"] == "pinokio"


@pytest.mark.parametrize("stamp", [{}, {"ASTRID_INSTALL_RELEASE": "true", "ASTRID_INSTALL_MANAGED_BY": ""},
                                  {"ASTRID_INSTALL_RELEASE": "1", "ASTRID_INSTALL_MANAGED_BY": "source"}])
def test_generated_runner_requires_repair_for_missing_or_invalid_stamp(tmp_path, monkeypatch, stamp):
    root = tmp_path / "program"
    folder = root / "versions/v1"
    write_json(folder / "version.json", {"root": str(folder), "python": str(folder / "python"), "env": stamp})
    (root / "current").write_text("v1\n")
    _, runner = install(root)
    monkeypatch.setenv("ASTRID_INSTALL_RELEASE", "1")
    monkeypatch.setenv("ASTRID_INSTALL_MANAGED_BY", "pinokio")
    monkeypatch.setattr(_process.os, "execvpe", lambda *_args: pytest.fail("invalid stamp launched"))

    with pytest.raises(SystemExit, match="needs repair"):
        runpy.run_path(str(runner), run_name="__main__")


@pytest.mark.parametrize("platform", [None, "win32"], ids=["native", "windows-wait"])
@pytest.mark.parametrize("credential", [False, True], ids=["agent", "credential"])
def test_generated_runner_supports_selected_older_package_under_isolation(tmp_path, platform, credential):
    root = tmp_path / "Astrid Program Folder"
    version_root = root / "versions" / "v1"
    environment = version_root / "py"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_packages = subprocess.run(
        [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    package = Path(site_packages) / "astrid"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    # The selected version predates this installer and has no process helper.
    assert not (package / "_process.py").exists()
    assert not (package / "core" / "util" / "process.py").exists()
    (package / "omp_agent.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'isolated': sys.flags.isolated, 'utf8_mode': sys.flags.utf8_mode, "
        "'managed': os.environ['MANAGED'], 'root': os.environ['ASTRID_INSTALL_PROGRAM_ROOT'], "
        "'release': os.environ['ASTRID_INSTALL_RELEASE'], 'managed_by': os.environ['ASTRID_INSTALL_MANAGED_BY']}))\n"
        "sys.exit(37)\n",
        encoding="utf-8",
    )
    core = package / "core"
    util = core / "util"
    util.mkdir(parents=True)
    (core / "__init__.py").write_text("", encoding="utf-8")
    (util / "__init__.py").write_text("", encoding="utf-8")
    (util / "credential_store.py").write_text(
        "import json, os, sys\n"
        "def main(args):\n"
        "    print(json.dumps({'args': args, 'isolated': sys.flags.isolated, 'utf8_mode': sys.flags.utf8_mode, "
        "'managed': os.environ['MANAGED'], 'root': os.environ['ASTRID_INSTALL_PROGRAM_ROOT'], "
        "'release': os.environ['ASTRID_INSTALL_RELEASE'], 'managed_by': os.environ['ASTRID_INSTALL_MANAGED_BY']}))\n"
        "    return 37\n",
        encoding="utf-8",
    )
    legacy = {"root": str(version_root), "python": str(python),
              "profile": str(version_root / "profile.json"), "env": {"MANAGED": "selected version"}}
    write_json(version_root / "version.json", legacy)
    (root / "current").write_text("v1\n", encoding="utf-8")
    write_json(version_root / "manifest.json", {"release": 31})
    write_json(root / "install.json", {"owner": "haus.astrid.runtime", "managed_by": "pinokio"})
    # Repair stamps legacy metadata before launching even when the retained
    # package has none of the current installer helpers.
    persist_install_identity(Version.load(legacy), root)
    _, runner = install(root)
    ambient = tmp_path / "ambient"
    (ambient / "astrid").mkdir(parents=True)
    (ambient / "astrid" / "__init__.py").write_text("raise AssertionError('ambient import')\n", encoding="utf-8")
    arguments = ["documents", "create", 'a title with "quotes"', "", "trailing\\"]
    # Import subprocess before patching the platform: on POSIX its native
    # implementation still launches a real child while exercising our wait path.
    bootstrap = (
        "import runpy, subprocess, sys; "
        + ("sys.platform = 'win32'; " if platform else "")
        + "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    request = ["credential", *arguments] if credential else arguments
    result = subprocess.run(
        [str(python), "-X", "utf8", "-I", "-c", bootstrap, str(runner), *request],
        env={**os.environ, "PYTHONPATH": str(ambient), "PYTHONUTF8": "0"}, cwd=ambient,
        check=False, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 37, result.stderr
    assert json.loads(result.stdout) == {
        "args": arguments, "isolated": 1, "utf8_mode": 1, "managed": "selected version", "root": str(root),
        "release": "31", "managed_by": "pinokio",
    }
    assert not (package / "_process.py").exists()
    assert not (package / "core" / "util" / "process.py").exists()


def test_generated_runner_utf8_flag_proves_windows_default_off_control(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.flags.utf8_mode)"],
        env={**os.environ, "PYTHONUTF8": "0"}, check=True, capture_output=True, text=True,
    )
    assert result.stdout.strip() == "0"


@pytest.mark.parametrize("service_state,raises", [("running", False), ("failed", True)])
def test_verification_rejects_failed_declared_service(tmp_path, monkeypatch, service_state, raises):
    root, data = tmp_path / "program", tmp_path / "workspace"
    version_root = root / "versions" / "v1"
    version = Version(version_root, Path(sys.executable), version_root / "profile.json", {})
    endpoint = f"http://127.0.0.1:{DEFAULT_DOOR_PORT}"
    write_json(data / "runtime" / "discovery.json", {
        "door_endpoint": endpoint, "active_realm": "realm", "runtime_instance_id": "instance",
        "identity": version.document()["identity"],
    })
    status = {"discovery": {"runtime_instance_id": "instance"}, "support": {"pid_alive": True}}
    health = {"status": "ok", "runtime_instance_id": "instance", "services": [
        {"name": "pack-host", "state": "running"},
        {"name": "chat", "state": service_state},
    ]}
    health["identity"] = version.document()["identity"]
    (data / "runtime" / "generic-host.ready.json").write_text("{}")
    realm = {"realm_id": "realm"}

    class Response:
        def __init__(self, value):
            self.value = value

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return json.dumps(self.value).encode()

    class Opener:
        def open(self, request, timeout):
            path = request.full_url.removeprefix(endpoint)
            return Response({"/api/runtime/v1/health": health, "/api/runtime/v1/realm": realm}[path])

    backend = Backend(root, tmp_path / "log", Path(sys.executable))
    monkeypatch.setattr(backend, "runtime", lambda *_args, **_kwargs: status)
    monkeypatch.setattr("astrid.installer.backend.urllib.request.build_opener", lambda *_: Opener())
    monkeypatch.setattr("astrid.installer.backend.time.sleep", lambda _: None)

    if raises:
        with pytest.raises(VerificationError, match="An Astrid service stopped while starting"):
            backend.verify(version, data, "realm", timeout=0.01)
    else:
        backend.verify(version, data, "realm", timeout=0.01)


@pytest.mark.parametrize("state", ["restarting", "running"])
def test_verification_rejects_pack_host_without_ready_file_with_service_evidence(tmp_path, monkeypatch, state):
    from astrid.installer import backend as backend_module

    root, data = tmp_path / "program", tmp_path / "workspace"
    version_root = root / "versions" / "v1"
    version = Version(version_root, Path(sys.executable), version_root / "profile.json", {})
    endpoint = f"http://127.0.0.1:{DEFAULT_DOOR_PORT}"
    write_json(data / "runtime" / "discovery.json", {
        "door_endpoint": endpoint, "active_realm": "realm", "runtime_instance_id": "instance",
        "identity": version.document()["identity"],
    })
    status = {"discovery": {"runtime_instance_id": "instance"}, "support": {"pid_alive": True}}
    health = {"status": "ok", "runtime_instance_id": "instance", "services": [
        {"name": "pack-host", "state": state, "reason": "worker exited during startup",
         "restart_count": 1, "last_exit": 7},
    ]}
    health["identity"] = version.document()["identity"]
    realm = {"realm_id": "realm"}

    class Response:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self): return json.dumps(self.value).encode()

    class Opener:
        def open(self, request, timeout):
            path = request.full_url.removeprefix(endpoint)
            return Response({"/api/runtime/v1/health": health, "/api/runtime/v1/realm": realm}[path])

    logs = tmp_path / "runtime-logs"
    logs.mkdir()
    (logs / "service-pack-host.log").write_text("startup failed: missing adapter\nrestart scheduled\n")
    monkeypatch.setattr(backend_module.platform, "log_dir", lambda: logs)
    monkeypatch.setattr(backend_module.urllib.request, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(backend_module.time, "sleep", lambda _: None)
    backend = Backend(root, tmp_path / "install.log", Path(sys.executable))
    backend.runtime = lambda *_args, **_kwargs: status

    with pytest.raises(VerificationError) as failure:
        backend.verify(version, data, "realm", timeout=0.01)

    message = str(failure.value)
    assert f"state={state}" in message
    assert "worker exited during startup" in message and "last_exit=7" in message
    assert "missing adapter" in message and "service-pack-host.log" in message
    assert f"state={state}" in failure.value.safe_message
    assert "worker exited during startup" in failure.value.safe_message
    assert "last_exit=7" in failure.value.safe_message
    assert "missing adapter" not in failure.value.safe_message
    assert "service-pack-host.log" in failure.value.safe_message


class ProcessBackend:
    def __init__(self, root, data, *, failed_service):
        self.root, self.data, self.failed_service = root, data, failed_service
        self.runtime = self._child()
        self.service = self._child()
        self.old_processes = ()
        self.verify_count = 0

    @staticmethod
    def _child():
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

    def workspace(self, *_):
        return {"realm_id": "realm", "realm_root": str(self.data / "realm")}

    def verify(self, *_args, **_kwargs):
        self.verify_count += 1
        if self.failed_service and self.verify_count == 1:
            raise VerificationError("a declared service is failed")

    def restart_required(self):
        return False

    def stop(self, *_):
        self.old_processes = (self.runtime, self.service)
        for child in self.old_processes:
            child.terminate()
            child.wait(timeout=5)

    def register(self, *_):
        self.runtime = self._child()
        self.service = self._child()

    def start(self, *_):
        pass

    def open(self):
        pass

    def close(self):
        for child in (self.runtime, self.service):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


@pytest.mark.parametrize("failed_service", [False, True])
def test_repair_restart_decision_preserves_or_replaces_real_processes(tmp_path, failed_service):
    root, data = tmp_path / "program", tmp_path / "workspace"
    version_root = root / "versions" / "v1"
    version_root.mkdir(parents=True)
    version = Version(version_root, Path(sys.executable), version_root / "profile.json", {})
    write_json(version_root / "version.json", version.document())
    (root / "current").write_text("v1\n", encoding="utf-8")
    backend = ProcessBackend(root, data, failed_service=failed_service)
    old_pids = (backend.runtime.pid, backend.service.pid)
    try:
        Installer(root, data, backend).run("install", {})
        if failed_service:
            assert backend.verify_count == 2
            assert backend.runtime.pid != old_pids[0]
            assert backend.service.pid != old_pids[1]
            assert all(child.poll() is not None for child in backend.old_processes)
        else:
            assert backend.verify_count == 1
            assert (backend.runtime.pid, backend.service.pid) == old_pids
            assert backend.runtime.poll() is None
            assert backend.service.poll() is None
    finally:
        backend.close()
