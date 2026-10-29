"""Exercise the generated Pinokio foreground launcher in an isolated venv."""

from __future__ import annotations

import io
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sysconfig
import tarfile
import textwrap
import venv
from pathlib import Path

import pytest

from astrid.installer.core import Version, switch, write_json
from astrid.installer.launcher import install as install_launcher


def _runtime_source_checkout() -> Path:
    configured = os.environ.get("ASTRID_RUNTIME_CHECKOUT")
    candidates = [Path(configured)] if configured else []
    candidates.append(Path(__file__).resolve().parents[1].parent / "astrid-runtime")
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() and (candidate / "astrid_runtime").is_dir():
            return candidate.resolve()
    pytest.fail("The sibling astrid-runtime source checkout is required for the package fixture.")


def _copy_runtime_package_source(checkout: Path, destination: Path) -> Path:
    """Copy only Runtime distribution inputs, without pulling the whole checkout."""
    destination.mkdir(parents=True, exist_ok=True)
    for package in ("astrid_runtime", "runtime_protocol"):
        shutil.copytree(
            checkout / package, destination / package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    shutil.copyfile(checkout / "pyproject.toml", destination / "pyproject.toml")
    return destination


def _install_runtime_package(python: Path, source: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("uv is required to reproduce Backend.stage's non-editable Runtime install.")
    subprocess.run([
        uv, "pip", "install", "--python", str(python), "--no-deps",
        "--no-build-isolation", "--no-cache", str(source),
    ], check=True, capture_output=True, text=True, timeout=180)
    probe = subprocess.run(
        [str(python), "-I", "-c",
         "import json, importlib; b=importlib.import_module('astrid_runtime.bootstrap'); "
         "print(json.dumps({'path':b.__file__,'handoff':b.PINOKIO_STOP_HANDOFF_VERSION}))"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    installed = json.loads(probe.stdout)
    module_path = Path(installed["path"]).resolve()
    assert installed["handoff"] == 1
    prefix = Path(subprocess.run(
        [str(python), "-I", "-c", "import sys; print(sys.prefix)"],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()).resolve()
    assert module_path.is_relative_to(prefix), (module_path, prefix)


def _isolated_pinokio_ports(directory):
    """Substitute fixture addresses in copied callers; keep lifecycle code intact."""
    with socket.socket() as door, socket.socket() as editor:
        door.bind(("127.0.0.1", 0))
        editor.bind(("127.0.0.1", 0))
        runtime_port, editor_port = door.getsockname()[1], editor.getsockname()[1]
    for name in ("session.mjs", "lib.js"):
        source = directory / name
        text = source.read_text(encoding="utf-8")
        source.write_text(text.replace("17340", str(runtime_port)).replace("5180", str(editor_port)),
                          encoding="utf-8")
    return runtime_port, editor_port


@pytest.mark.posix_native
@pytest.mark.parametrize("shutdown_signal", [signal.SIGTERM, signal.SIGINT])
def test_pinokio_run_executes_foreground_runtime_and_drains_its_child(
    tmp_path, monkeypatch, shutdown_signal,
):
    import runtime_protocol

    home = tmp_path / "home"
    for key, value in {"HOME": home, "USERPROFILE": home,
                       "LOCALAPPDATA": home / "local", "XDG_DATA_HOME": home / "data"}.items():
        monkeypatch.setenv(key, str(value))

    app_root = tmp_path / "Astrid"
    version_root = app_root / "versions" / "fixture"
    environment = version_root / "py"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin" / "python"
    site_packages = next((environment / "lib").glob("python*/site-packages"))

    # `-I` still processes venv .pth files. These two paths expose this checkout's
    # launcher code and the Runtime protocol package to the isolated interpreter.
    checkout = Path(__file__).resolve().parents[1]
    protocol_root = Path(runtime_protocol.__file__).resolve().parent.parent
    (site_packages / "astrid-pinokio-test.pth").write_text(
        f"{checkout}\n{protocol_root}\n", encoding="utf-8"
    )

    data_root = tmp_path / "workspace"
    profile = version_root / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    version = Version(version_root, python, profile, {
        "ASTRID_TEST_VERSION": "from-version",
        "ASTRID_INSTALL_MANAGED_BY": "wrong-manager",
        "PATH": os.defpath,
    }, managed_by="pinokio")
    write_json(version_root / "version.json", version.document())
    write_json(app_root / "install.json", {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio",
        "data_root": str(data_root),
    })
    write_json(app_root / "service-env.json", {
        "ASTRID_WARM_IDLE_SECONDS": "73",
        "ASTRID_ALLOW_UNQUALIFIED_GPU": "true",
    })
    switch(app_root, version)
    install_launcher(app_root)

    capture = tmp_path / "runtime-capture.json"
    stopped = tmp_path / "runtime-stopped.json"
    fake_runtime = site_packages / "astrid_runtime"
    fake_runtime.mkdir()
    (fake_runtime / "__init__.py").write_text("", encoding="utf-8")
    (fake_runtime / "cli.py").write_text(
        "import json, os, signal, subprocess, sys\n"
        "from pathlib import Path\n"
        "def main(argv):\n"
        "    capture = Path(os.environ['ASTRID_TEST_CAPTURE'])\n"
        "    stopped = Path(os.environ['ASTRID_TEST_STOPPED'])\n"
        "    child = subprocess.Popen([sys.executable, '-I', '-c', 'import signal; signal.pause()'])\n"
        "    record = {'argv': list(argv), 'pid': os.getpid(), 'child_pid': child.pid, "
        "'env': {key: os.environ.get(key) for key in ('ASTRID_INSTALL_MANAGED_BY', "
        "'ASTRID_TEST_BASE', 'ASTRID_TEST_VERSION', 'ASTRID_WARM_IDLE_SECONDS', "
        "'ASTRID_ALLOW_UNQUALIFIED_GPU', 'PYTHONPATH', 'PYTHONHOME', "
        "'ASTRID_WORKSPACE_ENDPOINT', 'ASTRID_WORKSPACE_CREDENTIAL', "
        "'ASTRID_RUNTIME_SOURCE_MANIFEST', 'ASTRID_RUNTIME_DATA_ROOT', 'ASTRID_PACKS_PATH')}}\n"
        "    capture.write_text(json.dumps(record), encoding='utf-8')\n"
        "    def stop(signum, frame):\n"
        "        child.terminate()\n"
        "        child.wait()\n"
        "        stopped.write_text(json.dumps({'signal': signum, 'child_pid': child.pid}), encoding='utf-8')\n"
        "        raise SystemExit(0)\n"
        "    signal.signal(signal.SIGTERM, stop)\n"
        "    signal.signal(signal.SIGINT, stop)\n"
        "    fd = int(os.environ['ASTRID_TEST_READY_FD'])\n"
        "    os.write(fd, (json.dumps(record) + '\\n').encode())\n"
        "    os.close(fd)\n"
        "    signal.pause()\n"
        "    return 0\n",
        encoding="utf-8",
    )

    read_fd, write_fd = os.pipe()
    caller_env = os.environ.copy()
    caller_env.update({
        "ASTRID_TEST_CAPTURE": str(capture),
        "ASTRID_TEST_STOPPED": str(stopped),
        "ASTRID_TEST_READY_FD": str(write_fd),
        "ASTRID_TEST_BASE": "from-pinokio",
        "PYTHONPATH": str(tmp_path / "must-be-scrubbed"),
        "PYTHONHOME": str(tmp_path / "must-be-scrubbed-home"),
        "ASTRID_WORKSPACE_ENDPOINT": "must-be-scrubbed",
        "ASTRID_WORKSPACE_CREDENTIAL": "must-be-scrubbed",
        "ASTRID_RUNTIME_SOURCE_MANIFEST": "must-be-scrubbed",
        "ASTRID_RUNTIME_DATA_ROOT": "must-be-scrubbed",
        "ASTRID_PACKS_PATH": "must-be-scrubbed",
    })
    process = None
    child_pid = None
    try:
        process = subprocess.Popen(
            [str(app_root / "run"), "--hosted-origin", "off"],
            cwd=tmp_path,
            env=caller_env,
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        os.close(write_fd)
        write_fd = -1

        ready = bytearray()
        while b"\n" not in ready:
            chunk = os.read(read_fd, 1)
            if not chunk:
                stderr = process.stderr.read().decode("utf-8", errors="replace")
                process.wait()
                raise AssertionError(f"The foreground Runtime exited before readiness: {stderr}")
            ready.extend(chunk)
        observed = json.loads(ready)
        child_pid = observed["child_pid"]
        record = json.loads(capture.read_text(encoding="utf-8"))

        assert process.pid == observed["pid"] == record["pid"]
        assert record["argv"] == [
            "run", "--profile", "astrid", "--data-root", str(data_root),
            "--source-manifest", str(profile), "--hosted-origin", "off",
        ]
        assert record["env"] == {
            "ASTRID_INSTALL_MANAGED_BY": "pinokio",
            "ASTRID_TEST_BASE": "from-pinokio",
            "ASTRID_TEST_VERSION": "from-version",
            "ASTRID_WARM_IDLE_SECONDS": "73",
            "ASTRID_ALLOW_UNQUALIFIED_GPU": "true",
            "PYTHONPATH": None,
            "PYTHONHOME": None,
            "ASTRID_WORKSPACE_ENDPOINT": None,
            "ASTRID_WORKSPACE_CREDENTIAL": None,
            "ASTRID_RUNTIME_SOURCE_MANIFEST": None,
            "ASTRID_RUNTIME_DATA_ROOT": None,
            "ASTRID_PACKS_PATH": None,
        }
        os.kill(child_pid, 0)

        process.send_signal(shutdown_signal)
        process.wait()
        assert process.returncode == 0
        termination = json.loads(stopped.read_text(encoding="utf-8"))
        assert termination == {"signal": int(shutdown_signal), "child_pid": child_pid}
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait()
        if process is not None and process.stderr is not None:
            process.stderr.close()
        if child_pid is None and capture.exists():
            child_pid = json.loads(capture.read_text(encoding="utf-8"))["child_pid"]
        if child_pid is not None:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                pass
            else:
                os.kill(child_pid, signal.SIGKILL)


@pytest.mark.integration
@pytest.mark.linux_native
@pytest.mark.parametrize("transaction_kind", ["app", "migration"])
def test_pinokio_session_binds_real_runtime_to_pending_launch_intent(
        tmp_path, monkeypatch, transaction_kind):
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    from astrid.installer.app_update import PinokioLifecycle
    from astrid.installer.backend import Backend
    from astrid.installer.core import current, read_json

    canonical_pinokio = Path(os.environ.get(
        "ASTRID_PINOKIO_CHECKOUT",
        Path(__file__).resolve().parents[1].parent / "astrid-pinokio",
    ))
    canonical_session = canonical_pinokio / "pinokio" / "session.mjs"
    node_binary = shutil.which("node")
    if not canonical_session.is_file() or node_binary is None:
        pytest.fail("This integration lane requires the canonical Pinokio checkout and Node executable.")

    user_home = tmp_path / "pinokio-home"
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    for key, value in {
        "HOME": host_home,
        "USERPROFILE": user_home,
        "LOCALAPPDATA": user_home / "local",
        # Pinokio's restricted child environment drops XDG_DATA_HOME. Keep the
        # installer selection in HOME's default location so fresh recovery
        # reads the same per-user selection as the parent controller.
        "XDG_DATA_HOME": host_home / ".local" / "share",
    }.items():
        monkeypatch.setenv(key, str(value))

    repo = user_home / "api" / "astrid-pinokio"
    pinokio_directory = repo / "pinokio"
    pinokio_directory.mkdir(parents=True)
    for name in ("session.mjs", "environment.js", "lifecycle.mjs", "lib.js",
                 "update.js", "start.js", "shell.mjs"):
        shutil.copyfile(canonical_pinokio / "pinokio" / name, pinokio_directory / name)
    platforms = pinokio_directory / "platforms"
    platforms.mkdir()
    for name in ("posix.js", "windows.js"):
        shutil.copyfile(canonical_pinokio / "pinokio" / "platforms" / name, platforms / name)

    runtime_port, editor_port = _isolated_pinokio_ports(pinokio_directory)

    app_root = repo / "app" / "astrid"
    data_root = tmp_path / "workspace"
    old_root = app_root / "versions" / "old"
    version_root = app_root / "versions" / "candidate"
    environment = version_root / "py"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin" / "python"
    site_packages = next((environment / "lib").glob("python*/site-packages"))
    checkout = Path(__file__).resolve().parents[1]
    runtime_root = _runtime_source_checkout()
    prepared_packages = Path(sysconfig.get_paths()["purelib"])
    (site_packages / "astrid-pinokio-runtime-test.pth").write_text(
        "\n".join(map(str, (checkout, prepared_packages))) + "\n",
        encoding="utf-8",
    )
    runtime_source = _copy_runtime_package_source(runtime_root, version_root / "r")
    _install_runtime_package(python, runtime_source)

    service_declaration = version_root / "services.json"
    pack_host_script = (
        "from pathlib import Path; import os,signal; "
        "Path(os.environ['ASTRID_RUNTIME_SUPPORT'], 'generic-host.ready.json').write_text('{}'); "
        "signal.pause()"
    )
    write_json(service_declaration, {"version": 1, "services": [{
        "name": "pack-host", "argv": [str(python), "-c", pack_host_script],
        "env": {"ASTRID_RUNTIME_SUPPORT": "{support_root}"}, "restart": "always",
        "ready": {"file": "generic-host.ready.json", "timeout_seconds": 10},
    }]})
    profile = version_root / "profile.json"
    write_json(profile, {
        "profile": "astrid",
        "runtime_checkout": str(runtime_source),
        "source_checkout": str(checkout),
        "runtime_environment": str(environment),
        "service_declaration": str(service_declaration),
    })
    node = version_root / "tools" / "node"
    node.parent.mkdir(parents=True)
    node.write_text(f"#!/bin/sh\nexec {shlex.quote(node_binary)} \"$@\"\n", encoding="utf-8")
    node.chmod(0o755)
    npm_directory = version_root / "lib" / "node_modules" / "npm" / "bin"
    npm_directory.mkdir(parents=True)
    npm = npm_directory / "npm-cli.js"
    npx = npm_directory / "npx-cli.js"
    npm.touch()
    npx.touch()
    candidate = Version(version_root, python, profile, {
        "ASTRID_NODE_EXECUTABLE": str(node),
        "ASTRID_NPM_EXECUTABLE": str(npm),
        "ASTRID_NPX_EXECUTABLE": str(npx),
        "ASTRID_RUNTIME_DOOR_PORT": str(runtime_port),
        "PATH": os.defpath,
    }, release=18, managed_by="pinokio")
    # A fresh Update can now recover a switched migration journal after a
    # normal Stop, so the retained old release must be runnable rather than a
    # status-script stub that emits no Runtime report.
    old_environment = old_root / "py"
    venv.EnvBuilder(with_pip=False).create(old_environment)
    old_python = old_environment / "bin" / "python"
    old_site_packages = next((old_environment / "lib").glob("python*/site-packages"))
    (old_site_packages / "astrid-pinokio-retained-test.pth").write_text(
        "\n".join(map(str, (checkout, prepared_packages))) + "\n",
        encoding="utf-8",
    )
    old_runtime_source = _copy_runtime_package_source(runtime_root, old_root / "r")
    _install_runtime_package(old_python, old_runtime_source)
    old_services = old_root / "services.json"
    old_pack_host_script = (
        "from pathlib import Path; import os,signal; "
        "Path(os.environ['ASTRID_RUNTIME_SUPPORT'], 'generic-host.ready.json').write_text('{}'); "
        "signal.pause()"
    )
    write_json(old_services, {"version": 1, "services": [{
        "name": "pack-host", "argv": [str(old_python), "-c", old_pack_host_script],
        "env": {"ASTRID_RUNTIME_SUPPORT": "{support_root}"}, "restart": "always",
        "ready": {"file": "generic-host.ready.json", "timeout_seconds": 10},
    }]})
    old_profile = old_root / "profile.json"
    write_json(old_profile, {
        "profile": "astrid", "runtime_checkout": str(old_runtime_source),
        "source_checkout": str(checkout), "runtime_environment": str(old_environment),
        "service_declaration": str(old_services),
    })
    old_node = old_root / "tools" / "node"
    old_node.parent.mkdir(parents=True)
    old_node.write_text(f"#!/bin/sh\nexec {shlex.quote(node_binary)} \"$@\"\n", encoding="utf-8")
    old_node.chmod(0o755)
    old_npm_directory = old_root / "lib" / "node_modules" / "npm" / "bin"
    old_npm_directory.mkdir(parents=True)
    old_npm, old_npx = old_npm_directory / "npm-cli.js", old_npm_directory / "npx-cli.js"
    old_npm.touch()
    old_npx.touch()
    old = Version(old_root, old_python, old_profile, {
        "ASTRID_NODE_EXECUTABLE": str(old_node),
        "ASTRID_NPM_EXECUTABLE": str(old_npm),
        "ASTRID_NPX_EXECUTABLE": str(old_npx),
        "ASTRID_RUNTIME_DOOR_PORT": str(runtime_port),
        "PATH": os.defpath,
    }, release=17, managed_by="pinokio")
    write_json(version_root / "version.json", candidate.document())
    write_json(old_root / "version.json", old.document())
    write_json(app_root / "install.json", {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio", "data_root": str(data_root),
    })
    write_json(app_root / "service-env.json", {})
    switch(app_root, candidate)
    install_launcher(app_root)

    backend = Backend(app_root, tmp_path / "install.log", tmp_path / "uv", say=lambda _line: None,
                      managed_by="pinokio")
    write_json(backend.service.selection, {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio",
        "program_root": str(app_root), "data_root": str(data_root),
        "identity": {"release": candidate.release, "managed_by": "pinokio"},
    })
    realm = backend.create(candidate, data_root)
    backend.register(candidate, data_root)
    lifecycle = PinokioLifecycle(app_root, backend)
    operation_id = "b" * 32
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": operation_id,
        "transaction_kind": transaction_kind, "step": "switched", "data_root": str(data_root),
        "old": old.document(), "candidate": candidate.document(),
        "realm": {"realm_id": realm["realm_id"], "realm_root": realm["realm_root"]},
    }
    write_json(app_root / "update-transaction.json", transaction)
    started = False
    try:
        lifecycle.start(candidate, data_root, realm["realm_id"])
        started = True

        pending = read_json(app_root / "update-transaction.json")
        assert pending["operation_id"] == operation_id
        assert pending["step"] == "switched"
        assert "launch_intent" not in pending

        support = data_root / "runtime"
        discovery = read_json(support / "discovery.json")
        owner_lock = read_json(support / "instance.lock")
        assert "installer_operation_id" not in discovery
        assert "installer_operation_id" not in owner_lock
        boundary = LocalRuntimeBoundary()
        health = boundary.owner_startup_identity(
            endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
        )
        assert health["runtime_instance_id"] == discovery["runtime_instance_id"]
        metadata = boundary.endpoint_metadata(
            endpoint=discovery["endpoint"], credential_file=support / "credentials" / "owner.token",
        )
        assert metadata["realm_id"] == realm["realm_id"]
        assert metadata["runtime_instance_id"] == discovery["runtime_instance_id"]
        assert lifecycle.session.read_text(encoding="utf-8") == (
            canonical_session.read_text(encoding="utf-8")
            .replace("17340", str(runtime_port)).replace("5180", str(editor_port)))
        if transaction_kind == "migration":
            # A normal Stop has no receipt. A fresh Pinokio Update controller
            # still replays the switched journal through C1 after the session
            # has removed its Runtime publication.
            lifecycle.stop(candidate, data_root)
            started = False
            recovery_code = (
                f"const update=require({json.dumps(str(pinokio_directory / 'update.js'))});"
                "update().then(script=>script.run[0].method({},event=>process.stdout.write(event.raw)))"
                ".catch(error=>{console.error(error.message);process.exitCode=1;});"
            )
            update_env = {key: value for key, value in os.environ.items()
                          if key not in {"PYTHONPATH", "PYTHONHOME"}}
            recovery = subprocess.run(
                [str(lifecycle.node(candidate)), "-e", recovery_code], env=update_env,
                cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
            )
            assert recovery.returncode == 0, recovery.stdout
            recovered = read_json(app_root / "update-transaction.json")
            assert recovered["step"] == "rolled_back"
            assert current(app_root) == old
            discovery = read_json(data_root / "runtime" / "discovery.json")
            owner_lock = read_json(data_root / "runtime" / "instance.lock")
            old_health = boundary.owner_startup_identity(
                endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
            )
            assert old_health["runtime_instance_id"] == discovery["runtime_instance_id"]
            assert discovery["active_realm"] == realm["realm_id"]
            assert owner_lock["runtime_instance_id"] == discovery["runtime_instance_id"]
            assert "installer_operation_id" not in discovery
            assert "installer_operation_id" not in owner_lock
            assert "stop_authority" not in recovered
            started = True
    finally:
        support = data_root / "runtime"
        if started or (support / "discovery.json").is_file():
            lifecycle.stop(current(app_root), data_root)
        elif (app_root / "pinokio-control.json").is_file():
            backend.run([lifecycle.node(candidate), lifecycle.lifecycle, "stop", app_root],
                        timeout=50, clean_bun_env=True)


# Mixed old/new regression: the retained Version runs a genuinely unpatched
# Runtime archived from this commit (never the worktree), while the session
# uses the current launcher. It is the earliest landed prog/upd Runtime with
# PINOKIO_STOP_HANDOFF_VERSION 1, which the installer requires before stopping
# a retained Runtime, and it predates UPD-EL-X3's C1 stop report.
RETAINED_RUNTIME_REVISION = "efcb3264e854fc312faa0bc6e702a9034edc2e06"


@pytest.mark.integration
@pytest.mark.linux_native
@pytest.mark.parametrize("runtime_kind", ["retained", "current"])
@pytest.mark.parametrize(("interrupt_at", "mode"), [
    ("stopping", "backend"), ("candidate_ready", "backend"),
    ("migrated", "backend"), ("migrated", "local"), ("switched", "backend"),
    ("before_owner", "backend"),
])
def test_pre_e2_pinokio_foreground_rolls_back_and_recovers_migration(
        tmp_path, monkeypatch, interrupt_at, mode, runtime_kind):
    """Rollback and fresh CLI recovery launch the committed pre-E2 Agent."""
    fail_before_owner = interrupt_at == "before_owner"
    if fail_before_owner:
        interrupt_at = "switched"
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    from astrid.installer.app_update import PinokioLifecycle, update_pinokio
    from astrid.installer.backend import Backend
    from astrid.installer.core import Version, current, read_json, switch, write_json

    checkout = Path(__file__).resolve().parents[1]
    old_revision = "a53ef0c257e4b0bbc2a65959b9119e2cb4cff453"
    legacy_source = tmp_path / "retained-agent"
    archive = subprocess.run(
        ["git", "archive", "--format=tar", old_revision],
        cwd=checkout, check=True, stdout=subprocess.PIPE,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source_archive:
        source_archive.extractall(legacy_source, filter="data")
    legacy_package = legacy_source / "astrid"

    canonical_pinokio = Path(os.environ.get(
        "ASTRID_PINOKIO_CHECKOUT", checkout.parent / "astrid-pinokio",
    ))
    canonical_session = canonical_pinokio / "pinokio" / "session.mjs"
    node_binary = shutil.which("node")
    if not canonical_session.is_file() or node_binary is None:
        pytest.fail("This integration lane requires the canonical Pinokio checkout and Node executable.")

    user_home = tmp_path / "pinokio-home"
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    for key, value in {
        "HOME": host_home,
        "USERPROFILE": user_home,
        "LOCALAPPDATA": user_home / "local",
        # Pinokio's production environment filter drops custom XDG paths. Keep
        # selection at the normal host-home location so actual Update recovery
        # can discover it through that same filtered environment.
        "XDG_DATA_HOME": host_home / ".local" / "share",
        "ASTRID_RUNTIME_CHECKOUT": str(_runtime_source_checkout()),
        "ASTRID_PINOKIO_CHECKOUT": str(canonical_pinokio),
    }.items():
        monkeypatch.setenv(key, str(value))

    repo = user_home / "api" / "astrid-pinokio"
    pinokio_directory = repo / "pinokio"
    pinokio_directory.mkdir(parents=True)
    for name in ("session.mjs", "environment.js", "lifecycle.mjs", "lib.js",
                 "start.js", "update.js", "shell.mjs"):
        shutil.copyfile(canonical_pinokio / "pinokio" / name, pinokio_directory / name)
    shutil.copyfile(canonical_pinokio / "pinokio.js", repo / "pinokio.js")
    platforms = pinokio_directory / "platforms"
    platforms.mkdir()
    for name in ("posix.js", "windows.js"):
        shutil.copyfile(canonical_pinokio / "pinokio" / "platforms" / name, platforms / name)

    runtime_port, editor_port = _isolated_pinokio_ports(pinokio_directory)

    if mode == "local":
        # Minimal local-editor seam: dependency/build invocations succeed, and
        # the preview serves the two health endpoints required by the canonical
        # session's real local readiness check.
        editor = repo / "app" / "editor"
        scripts = editor / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "dev-local-workspace.mjs").write_text(
            "import fs from 'node:fs'; import http from 'node:http';\n"
            "if (process.argv.includes('--preview')) {\n"
            "  const discoveryPath = process.env.ASTRID_WORKSPACE_DISCOVERY;\n"
            "  http.createServer((request, response) => {\n"
            "    if (request.url === '/tools/video-editor') { response.writeHead(200); response.end('editor'); return; }\n"
            "    if (request.url === '/runtime-config/v1/workspace.json') {\n"
            "      const owner = JSON.parse(fs.readFileSync(discoveryPath, 'utf8'));\n"
            "      response.writeHead(200, {'content-type':'application/json'});\n"
            "      response.end(JSON.stringify({schemaVersion:1,runtimeInstanceId:owner.runtime_instance_id,realmId:owner.active_realm})); return;\n"
            "    }\n"
            "    response.writeHead(404); response.end();\n"
            f"  }}).listen({editor_port}, '127.0.0.1');\n"
            "}\n",
            encoding="utf-8",
        )

    app_root = repo / "app" / "astrid"
    data_root = tmp_path / "workspace"
    old_root = app_root / "versions" / "retained-c2"
    candidate_root = app_root / "versions" / "e2-candidate"
    runtime_root = _runtime_source_checkout()
    prepared_packages = Path(sysconfig.get_paths()["purelib"])

    def version(root, release, source_root, runtime_checkout=runtime_root):
        environment = root / "py"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / "bin" / "python"
        site_packages = next((environment / "lib").glob("python*/site-packages"))
        (site_packages / "astrid-retained-foreground-test.pth").write_text(
            "\n".join(map(str, (source_root, prepared_packages))) + "\n",
            encoding="utf-8",
        )
        runtime_source = _copy_runtime_package_source(
            runtime_checkout, root / "runtime-source",
        )
        if fail_before_owner and root == candidate_root:
            boundary_source = runtime_source / "astrid_runtime" / "runtime_boundary.py"
            text = boundary_source.read_text(encoding="utf-8")
            declaration = "    def start(self, *, realm_id, realm_root, owner_lock, source_profile):\n"
            assert text.count(declaration) == 1
            boundary_source.write_text(text.replace(declaration, declaration + (
                "        failure = Path(source_profile.runtime_checkout) / 'fail-foreground-once'\n"
                "        if failure.exists():\n"
                "            failure.unlink()\n"
                "            raise BootstrapError('injected before Runtime owner publication')\n"
            )), encoding="utf-8")
        _install_runtime_package(python, runtime_source)
        services = root / "services.json"
        host_script = (
            "from pathlib import Path; import os,signal; "
            "Path(os.environ['ASTRID_RUNTIME_SUPPORT'], 'generic-host.ready.json').write_text('{}'); "
            "signal.pause()"
        )
        write_json(services, {"version": 1, "services": [{
            "name": "pack-host", "argv": [str(python), "-c", host_script],
            "env": {"ASTRID_RUNTIME_SUPPORT": "{support_root}"}, "restart": "always",
            "ready": {"file": "generic-host.ready.json", "timeout_seconds": 10},
        }]})
        profile = root / "profile.json"
        write_json(profile, {
            "profile": "astrid", "runtime_checkout": str(runtime_source),
            "source_checkout": str(source_root), "runtime_environment": str(environment),
            "service_declaration": str(services),
        })
        node = root / "tools" / "node"
        node.parent.mkdir()
        node.write_text(f"#!/bin/sh\nexec {shlex.quote(node_binary)} \"$@\"\n", encoding="utf-8")
        node.chmod(0o755)
        npm_directory = root / "lib" / "node_modules" / "npm" / "bin"
        npm_directory.mkdir(parents=True)
        npm = npm_directory / "npm-cli.js"
        npx = npm_directory / "npx-cli.js"
        npm.touch()
        npx.touch()
        env = {
            "ASTRID_NODE_EXECUTABLE": str(node), "ASTRID_NPM_EXECUTABLE": str(npm),
            "ASTRID_NPX_EXECUTABLE": str(npx), "ASTRID_RUNTIME_DOOR_PORT": str(runtime_port),
            "PATH": os.defpath,
        }
        result = Version(root, python, profile, env, release=release, managed_by="pinokio")
        write_json(root / "version.json", result.document())
        return result

    candidate_runtime = candidate_root / "runtime-source"
    candidate_source = candidate_root / "agent-source"
    candidate_source.mkdir(parents=True)
    shutil.copytree(checkout / "astrid", candidate_source / "astrid")
    shutil.copyfile(checkout / "pyproject.toml", candidate_source / "pyproject.toml")
    if runtime_kind == "retained":
        retained_runtime = tmp_path / "retained-runtime"
        archive = subprocess.run(["git", "archive", "--format=tar", RETAINED_RUNTIME_REVISION],
                                 cwd=runtime_root, check=True, stdout=subprocess.PIPE).stdout
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source_archive:
            source_archive.extractall(retained_runtime, filter="data")
    else:
        retained_runtime = runtime_root
    old = version(old_root, 17, legacy_source, runtime_checkout=retained_runtime)
    candidate = version(candidate_root, 18, candidate_source)

    def installed_runtime_file(python, name):
        probe = subprocess.run(
            [str(python), "-I", "-c",
             f"import importlib; print(importlib.import_module('astrid_runtime.{name}').__file__)"],
            check=True, text=True, stdout=subprocess.PIPE)
        return Path(probe.stdout.strip()).resolve()

    # Each interpreter imports its own installed Runtime copy, never a worktree.
    for selected in (old, candidate):
        module = installed_runtime_file(selected.python, "cli")
        assert module.is_relative_to(selected.root / "py"), module
        assert not module.is_relative_to(runtime_root), module
    retained_cli = installed_runtime_file(old.python, "cli").read_bytes()
    if runtime_kind == "retained":
        original_cli = subprocess.run(
            ["git", "show", f"{RETAINED_RUNTIME_REVISION}:astrid_runtime/cli.py"],
            cwd=runtime_root, check=True, stdout=subprocess.PIPE).stdout
        assert hashlib.sha256(retained_cli).digest() == hashlib.sha256(original_cli).digest()
        assert b'if args.command in {"restart", "down"}' in retained_cli
        # This identifies the revision's own restart branch; it does not reproduce P1.
        assert b'No selected runtime owner to restart' in retained_cli
        original_bootstrap = subprocess.run(
            ["git", "show", f"{RETAINED_RUNTIME_REVISION}:astrid_runtime/bootstrap.py"],
            cwd=runtime_root, check=True, stdout=subprocess.PIPE).stdout
        assert installed_runtime_file(old.python, "bootstrap").read_bytes() == original_bootstrap
        handoff = subprocess.run(
            [str(old.python), "-I", "-c",
             "import importlib; print(importlib.import_module('astrid_runtime.bootstrap').PINOKIO_STOP_HANDOFF_VERSION)"],
            check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
        assert handoff == "1"
    write_json(app_root / "install.json", {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio", "data_root": str(data_root),
    })
    write_json(repo / "app" / "install-mode.json", {"schema_version": 1, "mode": mode})
    write_json(app_root / "service-env.json", {})
    switch(app_root, old)
    install_launcher(app_root)

    backend = Backend(app_root, tmp_path / "install.log", tmp_path / "uv", say=lambda _line: None,
                      managed_by="pinokio")
    write_json(backend.service.selection, {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio",
        "program_root": str(app_root), "data_root": str(data_root),
        "identity": {"release": old.release, "managed_by": "pinokio"},
    })
    realm = backend.create(old, data_root)
    backend.register(old, data_root)
    lifecycle = PinokioLifecycle(app_root, backend)
    backend.stage = lambda *_args, **_kwargs: candidate
    backend.sync_skills = lambda *_args, **_kwargs: None
    backend.finish = lambda *_args, **_kwargs: None

    # Prove the archived foreground itself has the reported admission defect;
    # preparation must publish current admission before this retained launcher
    # can be asked to launch through an update transaction.
    control_id = "c" * 32
    write_json(app_root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": control_id,
        "transaction_kind": "migration", "step": "switched", "data_root": str(data_root),
        "old": old.document(), "candidate": candidate.document(), "realm": realm,
        "launch_intent": {"operation_id": control_id, "purpose": "pinokio_start",
                          "version": old.document()},
    })
    legacy_env = {key: value for key, value in os.environ.items()
                  if key not in {"PYTHONPATH", "PYTHONHOME"}}
    legacy_env["ASTRID_INSTALLER_OPERATION_ID"] = control_id
    negative_control = subprocess.run(
        [str(old.python), "-I", "-c",
         "import sys,astrid; from pathlib import Path; "
         "assert Path(astrid.__file__).resolve()==Path(sys.argv[2]).resolve(); "
         "from astrid.installer.foreground import _run; "
         "_run(Path(sys.argv[1]),[],sys.platform,-1)", str(app_root), str(legacy_package / "__init__.py")],
        env=legacy_env, cwd=tmp_path, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30,
    )
    assert negative_control.returncode != 0
    assert "transaction launch binding does not match" in negative_control.stdout
    (app_root / "update-transaction.json").unlink()

    # Let the candidate Runtime really start and report healthy, then make the
    # transaction's readiness gate fail so C2 exercises its real rollback path.
    real_verify = backend.verify
    candidate_health = {}

    def fail_candidate_verification(version, data, realm_id, **kwargs):
        result = real_verify(version, data, realm_id, **kwargs)
        if version == candidate:
            discovery = read_json(data / "runtime" / "discovery.json")
            candidate_health.update(LocalRuntimeBoundary().owner_startup_identity(
                endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
            ))
            raise ValueError("injected candidate readiness failure after Runtime health")
        return result

    real_start = lifecycle.start
    if fail_before_owner:
        def fail_foreground_start(version, workspace, realm_id):
            if version == candidate:
                (candidate_runtime / "fail-foreground-once").touch()
            return real_start(version, workspace, realm_id)

        lifecycle.start = fail_foreground_start
    else:
        backend.verify = fail_candidate_verification
    original_pointer = (app_root / "current").read_text(encoding="utf-8")
    original_identity = read_json(backend.service.selection)["identity"]
    workspace_marker = data_root / "runtime" / "retained-workspace.json"
    workspace_marker.parent.mkdir(parents=True, exist_ok=True)
    workspace_marker.write_text('{"preserve":"across-migration"}\n', encoding="utf-8")
    original_workspace = workspace_marker.read_bytes()
    original_upgrade = backend.upgrade

    def migrate_workspace(version, workspace):
        original_upgrade(version, workspace)
        workspace_marker.write_text('{"preserve":"migrated"}\n', encoding="utf-8")

    backend.upgrade = migrate_workspace
    try:
        lifecycle.start(old, data_root, realm["realm_id"])
        expected_failure = ("did not become ready" if fail_before_owner
                            else "injected candidate readiness failure")
        with pytest.raises(ValueError, match=expected_failure):
            update_pinokio(app_root, backend, {"release": candidate.release},
                           lifecycle=lifecycle, say=lambda _line: None)
        lifecycle.start = real_start
        if fail_before_owner:
            assert "injected before Runtime owner publication" in (
                app_root / "app-update-session.log").read_text(encoding="utf-8")
        rolled_back = read_json(app_root / "update-transaction.json")
        assert rolled_back["transaction_kind"] == "migration"
        assert rolled_back["step"] == "rolled_back"
        assert "installer_operation_id" not in candidate_health
        assert (app_root / "current").read_text(encoding="utf-8") == original_pointer
        assert current(app_root) == old
        assert read_json(backend.service.selection)["identity"] == original_identity
        assert workspace_marker.read_bytes() == original_workspace
        boundary = LocalRuntimeBoundary()
        discovery = read_json(data_root / "runtime" / "discovery.json")
        old_health = boundary.owner_startup_identity(
            endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
        )
        assert old_health["runtime_instance_id"] == discovery["runtime_instance_id"]
        assert "installer_operation_id" not in read_json(data_root / "runtime" / "instance.lock")
        assert boundary.endpoint_metadata(
            endpoint=discovery["endpoint"],
            credential_file=data_root / "runtime" / "credentials" / "owner.token",
        )["runtime_instance_id"] == discovery["runtime_instance_id"]
        assert discovery["active_realm"] == realm["realm_id"]
        assert discovery["realm_root"] == realm["realm_root"]

        # Once the journal is complete, ordinary Pinokio Stop/Start carries no
        # installer operation ID and still leaves the same Runtime healthy.
        lifecycle.stop(old, data_root)
        lifecycle.start(old, data_root, realm["realm_id"])
        discovery = read_json(data_root / "runtime" / "discovery.json")
        assert boundary.owner_startup_identity(
            endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
        )["runtime_instance_id"] == discovery["runtime_instance_id"]

        # Restore the actual pre-E2 launcher, as on a first E2 update. The
        # candidate update preparation must durably replace it before stopping
        # the retained owner; recovery then uses that prepared runner.
        child_env = {key: value for key, value in os.environ.items()
                     if key not in {"PYTHONPATH", "PYTHONHOME", "ASTRID_INSTALLER_OPERATION_ID"}}
        legacy_launcher = subprocess.run(
            [str(old.python), "-I", "-c",
             "import sys; from pathlib import Path; from astrid.installer.launcher import install; "
             "install(Path(sys.argv[1]))", str(app_root)],
            env=child_env, cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert legacy_launcher.returncode == 0, legacy_launcher.stderr

        # Kill a separate controller at an actual durable transaction boundary.
        # The later case retains a real candidate session and migration backup;
        # the earlier case preserves the original owner and workspace untouched,
        # with C1's captured owner authority durable before its first stop.
        crash_code = textwrap.dedent("""\
            import json, os, signal, sys
            from pathlib import Path
            from astrid.installer.backend import Backend
            from astrid.installer.app_update import PinokioLifecycle, update_pinokio
            from astrid.installer.core import Installer, Version

            root, data = Path(sys.argv[1]), Path(sys.argv[2])
            candidate = Version.load(json.loads(Path(sys.argv[3]).read_text()))
            marker = Path(sys.argv[4])
            interrupt_at = sys.argv[7]
            original_save = Installer._save_update

            def save(installer, transaction, step, **details):
                result = original_save(installer, transaction, step, **details)
                if step == "stopping" and interrupt_at == "stopping":
                    os.kill(os.getpid(), signal.SIGKILL)
                if step == "migrated" and interrupt_at == "migrated":
                    os.kill(os.getpid(), signal.SIGKILL)
                if step == "switched" and interrupt_at == "switched":
                    os.kill(os.getpid(), signal.SIGKILL)
                return result

            Installer._save_update = save
            backend = Backend(root, Path(sys.argv[5]), Path(sys.argv[6]),
                              say=lambda _: None, managed_by="pinokio")
            backend.stage = lambda *_args, **_kwargs: candidate
            backend.sync_skills = lambda *_args, **_kwargs: None
            backend.finish = lambda *_args, **_kwargs: None
            original_upgrade = backend.upgrade

            def upgrade(version, workspace):
                original_upgrade(version, workspace)
                marker.write_text('{"preserve":"interrupted-migration"}\\n')

            backend.upgrade = upgrade
            original_verify = backend.verify

            def verify(version, workspace, realm_id, **kwargs):
                result = original_verify(version, workspace, realm_id, **kwargs)
                if version == candidate and interrupt_at == "candidate_ready":
                    os.kill(os.getpid(), signal.SIGKILL)
                return result

            backend.verify = verify
            lifecycle = PinokioLifecycle(root, backend)
            update_pinokio(root, backend, {"release": candidate.release}, lifecycle=lifecycle,
                           say=lambda _: None)
            """)
        candidate_record = tmp_path / "candidate-version.json"
        candidate_record.write_text(json.dumps(candidate.document()), encoding="utf-8")
        crash = subprocess.run(
            [str(candidate.python), "-I", "-c", crash_code, str(app_root), str(data_root),
             str(candidate_record), str(workspace_marker), str(tmp_path / "crash-controller.log"),
             str(tmp_path / "uv"), interrupt_at],
            env=child_env,
            cwd=tmp_path, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240,
        )
        assert crash.returncode == -int(signal.SIGKILL), crash.stdout
        pending = read_json(app_root / "update-transaction.json")
        assert pending["transaction_kind"] == "migration"
        recovery_id = pending["operation_id"]
        if interrupt_at == "candidate_ready":
            assert pending["step"] == "switched"
            assert pending["backup"]
            assert "launch_intent" not in pending
            assert current(app_root) == candidate
            assert workspace_marker.read_text(encoding="utf-8") == '{"preserve":"interrupted-migration"}\n'
            candidate_discovery = read_json(data_root / "runtime" / "discovery.json")
            candidate_owner = boundary.owner_startup_identity(
                endpoint=candidate_discovery["endpoint"],
                instance_id=candidate_discovery["runtime_instance_id"],
            )
            assert candidate_owner["runtime_instance_id"] == candidate_discovery["runtime_instance_id"]
        elif interrupt_at == "stopping":
            assert pending["step"] == "stopping"
            assert pending["backup"] is None
            assert "stop_authority" not in pending
            assert "launch_intent" not in pending
            assert current(app_root) == old
            assert workspace_marker.read_bytes() == original_workspace
            assert read_json(data_root / "runtime" / "discovery.json") == discovery
        else:
            assert pending["step"] == interrupt_at
            assert pending["backup"]
            assert "launch_intent" not in pending
            assert "stop_authority" not in pending
            assert current(app_root) == (candidate if interrupt_at == "switched" else old)
            assert workspace_marker.read_text(encoding="utf-8") == '{"preserve":"interrupted-migration"}\n'
            assert not (data_root / "runtime" / "discovery.json").exists()

        if interrupt_at == "migrated":
            # Exercise the canonical menu, ordinary Start script and its real
            # session shell payload after the durable migrated interruption.
            menu_code = (
                "const fs=require('node:fs'); const path=require('node:path');"
                f"const menu=require({json.dumps(str(repo / 'pinokio.js'))}).menu;"
                f"const start=require({json.dumps(str(pinokio_directory / 'start.js'))});"
                f"const root={json.dumps(str(repo))};"
                "(async()=>{const items=await menu({}, {running:()=>false,"
                "exists:p=>fs.existsSync(path.join(root,p))});"
                "if(items[0].text!=='Start'||items[0].default!==true)throw Error(JSON.stringify(items));"
                "const steps=(await start()).run; await steps[0].method();"
                "console.log(JSON.stringify({menu:items,start:steps[1].params}));})()"
                ".catch(e=>{console.error(e);process.exitCode=1;});"
            )
            menu = subprocess.run([node_binary, "-e", menu_code], cwd=repo, env=child_env,
                                  text=True, capture_output=True, timeout=30)
            assert menu.returncode == 0, menu.stderr
            offered = json.loads(menu.stdout)
            assert offered["menu"][0]["text"] == "Start"
            assert offered["menu"][0]["default"] is True
            try:
                start_attempt = subprocess.run(
                    ["bash", "-c", offered["start"]["message"]],
                    cwd=offered["start"]["path"], env=child_env,
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
                )
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                raise AssertionError(f"Ordinary Start did not finish refusing admission:\n{output}") from exc
            assert start_attempt.returncode != 0, start_attempt.stdout
            assert "Choose Pinokio's Update button" in start_attempt.stdout
            assert "ASTRID_PINOKIO_READY" not in start_attempt.stdout
            assert not (app_root / "pinokio-control.json").exists()
            assert not (data_root / "runtime" / "discovery.json").exists()
            assert read_json(app_root / "update-transaction.json")["step"] == interrupt_at
        if interrupt_at in {"migrated", "switched"}:
            # The actual Update script takes its existing pending branch and
            # invokes recovery using the candidate interpreter in a fresh
            # process, just as the Pinokio UI does.
            recovery_code = (
                f"const update=require({json.dumps(str(pinokio_directory / 'update.js'))});"
                "update().then(script=>script.run[0].method({},event=>process.stdout.write(event.raw)))"
                ".catch(error=>{console.error(error.message);process.exitCode=1;});"
            )
            recovery = subprocess.run(
                [str(lifecycle.node(candidate)), "-e", recovery_code], env=child_env,
                cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
            )
        else:
            recovery_code = (
                "import sys; from astrid.installer.cli import main; "
                "raise SystemExit(main(['--root',sys.argv[1],'--managed-by','pinokio']))"
            )
            recovery = subprocess.run(
                [str(candidate.python), "-I", "-c", recovery_code, str(app_root)],
                env=child_env,
                cwd=tmp_path, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180,
            )
        assert recovery.returncode == 0, recovery.stdout
        recovered = read_json(app_root / "update-transaction.json")
        assert recovered["step"] == "rolled_back"
        assert recovered["operation_id"] == recovery_id
        assert (app_root / "current").read_text(encoding="utf-8") == original_pointer
        assert current(app_root) == old
        assert read_json(backend.service.selection)["identity"] == original_identity
        assert workspace_marker.read_bytes() == original_workspace
        discovery = read_json(data_root / "runtime" / "discovery.json")
        boundary = LocalRuntimeBoundary()
        health = boundary.owner_startup_identity(
            endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
        )
        assert health["runtime_instance_id"] == discovery["runtime_instance_id"]
        assert "installer_operation_id" not in read_json(data_root / "runtime" / "instance.lock")
        assert discovery["active_realm"] == realm["realm_id"]
        assert discovery["realm_root"] == realm["realm_root"]
        assert boundary.endpoint_metadata(
            endpoint=discovery["endpoint"],
            credential_file=data_root / "runtime" / "credentials" / "owner.token",
        )["runtime_instance_id"] == discovery["runtime_instance_id"]

        # A repeated fresh recovery is a no-op and leaves the retained owner healthy.
        recover_code = (
            "import sys; from pathlib import Path; "
            "from astrid.installer.backend import Backend; "
            "from astrid.installer.app_update import recover_app, PinokioLifecycle; "
            "root=Path(sys.argv[1]); "
            "backend=Backend(root,Path(sys.argv[2]),Path(sys.argv[3]),say=lambda _:None,managed_by='pinokio'); "
            "life=PinokioLifecycle(root,backend); "
            "assert not recover_app(root,backend,lifecycle=life,say=lambda _:None)"
        )
        for _attempt in range(2):
            again = subprocess.run(
                [str(candidate.python), "-I", "-c", recover_code, str(app_root),
                 str(tmp_path / "fresh-recovery-again.log"), str(tmp_path / "uv")],
                env=child_env, cwd=tmp_path, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=60,
            )
            assert again.returncode == 0, again.stdout
            assert current(app_root) == old
            assert workspace_marker.read_bytes() == original_workspace
            assert boundary.owner_startup_identity(
                endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
            )["runtime_instance_id"] == discovery["runtime_instance_id"]
        # A fresh Update from the retained owner, whose current-launcher session
        # captured the retained Runtime CLI, completes after C1's stop report.
        backend.verify = real_verify
        backend.upgrade = original_upgrade
        update_pinokio(app_root, backend, {"release": candidate.release}, lifecycle=lifecycle,
                       say=lambda _line: None)
        assert read_json(app_root / "update-transaction.json")["step"] == "complete"
        assert current(app_root) == candidate
        assert workspace_marker.read_bytes() == original_workspace
    finally:
        support = data_root / "runtime"
        if (support / "discovery.json").is_file() or (app_root / "pinokio-control.json").is_file():
            lifecycle.stop(current(app_root), data_root)
        assert not (app_root / "pinokio-control.json").exists()
        assert not (support / "discovery.json").exists()
        if mode == "local":
            with pytest.raises(OSError):
                socket.create_connection(("127.0.0.1", editor_port), timeout=1)
