"""Exercise the generated Pinokio foreground launcher in an isolated venv."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sysconfig
import venv
from pathlib import Path

import pytest

from astrid.installer.core import Version, switch, write_json
from astrid.installer.launcher import install as install_launcher


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
    })
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
def test_pinokio_app_session_binds_real_runtime_to_pending_launch_intent(tmp_path, monkeypatch):
    import astrid_runtime
    import runtime_protocol
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    from astrid.installer.app_update import PinokioLifecycle
    from astrid.installer.backend import Backend
    from astrid.installer.core import (
        Installer,
        read_json,
        transaction_launch_version,
        transaction_stop_context,
    )

    canonical_pinokio = Path(os.environ.get(
        "ASTRID_PINOKIO_CHECKOUT",
        Path(__file__).resolve().parents[1].parent / "astrid-pinokio",
    ))
    canonical_session = canonical_pinokio / "pinokio" / "session.mjs"
    node_binary = shutil.which("node")
    if not canonical_session.is_file() or node_binary is None:
        pytest.skip("The read-only canonical Pinokio checkout and Node executable are unavailable.")

    user_home = tmp_path / "pinokio-home"
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    for key, value in {
        "HOME": host_home,
        "USERPROFILE": user_home,
        "LOCALAPPDATA": user_home / "local",
        "XDG_DATA_HOME": user_home / "data",
    }.items():
        monkeypatch.setenv(key, str(value))

    repo = user_home / "api" / "astrid-pinokio"
    pinokio_directory = repo / "pinokio"
    pinokio_directory.mkdir(parents=True)
    for name in ("session.mjs", "environment.js", "lifecycle.mjs", "lib.js"):
        shutil.copyfile(canonical_pinokio / "pinokio" / name, pinokio_directory / name)
    platforms = pinokio_directory / "platforms"
    platforms.mkdir()
    for name in ("posix.js", "windows.js"):
        shutil.copyfile(canonical_pinokio / "pinokio" / "platforms" / name, platforms / name)

    app_root = repo / "app" / "astrid"
    data_root = tmp_path / "workspace"
    old_root = app_root / "versions" / "old"
    version_root = app_root / "versions" / "candidate"
    environment = version_root / "py"
    venv.EnvBuilder(with_pip=False).create(environment)
    python = environment / "bin" / "python"
    site_packages = next((environment / "lib").glob("python*/site-packages"))
    checkout = Path(__file__).resolve().parents[1]
    runtime_root = Path(astrid_runtime.__file__).resolve().parent.parent
    protocol_root = Path(runtime_protocol.__file__).resolve().parent.parent
    prepared_packages = Path(sysconfig.get_paths()["purelib"])
    (site_packages / "astrid-pinokio-runtime-test.pth").write_text(
        "\n".join(map(str, (checkout, runtime_root, protocol_root, prepared_packages))) + "\n",
        encoding="utf-8",
    )

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
        "runtime_checkout": str(runtime_root),
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
        "ASTRID_RUNTIME_DOOR_PORT": "17340",
        "PATH": os.defpath,
    }, release=18, managed_by="pinokio")
    old_root.mkdir(parents=True)
    old = Version(old_root, python, profile, candidate.env, release=17, managed_by="pinokio")
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
    lifecycle = PinokioLifecycle(app_root, backend)
    operation_id = "b" * 32
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": operation_id,
        "transaction_kind": "app", "step": "switched", "data_root": str(data_root),
        "old": old.document(), "candidate": candidate.document(),
        "realm": {"realm_id": realm["realm_id"], "realm_root": realm["realm_root"]},
    }
    installer = Installer(app_root, data_root, backend, say=lambda _line: None, managed_by="pinokio")
    write_json(app_root / "update-transaction.json", transaction)
    started = False
    try:
        with transaction_stop_context(
            backend, transaction,
            lambda step, **details: installer._save_update(transaction, step, **details),
        ):
            lifecycle.start(candidate, data_root, realm["realm_id"])
        started = True

        pending = read_json(app_root / "update-transaction.json")
        assert pending["launch_intent"]["operation_id"] == operation_id
        assert pending["launch_intent"]["purpose"] == "pinokio_start"
        assert transaction_launch_version(pending) == candidate

        support = data_root / "runtime"
        discovery = read_json(support / "discovery.json")
        owner_lock = read_json(support / "instance.lock")
        assert discovery["installer_operation_id"] == operation_id
        assert owner_lock["installer_operation_id"] == operation_id
        boundary = LocalRuntimeBoundary()
        health = boundary.owner_startup_identity(
            endpoint=discovery["endpoint"], instance_id=discovery["runtime_instance_id"],
        )
        assert health["installer_operation_id"] == operation_id
        metadata = boundary.endpoint_metadata(
            endpoint=discovery["endpoint"], credential_file=support / "credentials" / "owner.token",
        )
        assert metadata["realm_id"] == realm["realm_id"]
        assert metadata["installer_operation_id"] == operation_id
        assert lifecycle.session.read_bytes() == canonical_session.read_bytes()
    finally:
        support = data_root / "runtime"
        if started or (support / "discovery.json").is_file():
            lifecycle.stop(candidate, data_root)
        elif (app_root / "pinokio-control.json").is_file():
            backend.run([lifecycle.node(candidate), lifecycle.lifecycle, "stop", app_root],
                        timeout=50, clean_bun_env=True)
