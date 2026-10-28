"""Prove that canonical Pinokio Stop hands off authority to fresh Update."""

from __future__ import annotations

import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import sysconfig
import tarfile
import time
import venv
from pathlib import Path

import pytest

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
    """Copy only the installable Runtime package inputs into the version tree."""
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


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.mark.integration
@pytest.mark.linux_native
@pytest.mark.parametrize(("mode", "start_stopped", "marker_fault", "operation"), [
    ("backend", True, None, "update"), ("local", True, None, "update"),
    ("backend", False, None, "update"), ("local", False, None, "update"),
    ("backend", False, "incapable-runtime", "update"),
    ("backend", False, "unrelated-runtime", "update"),
    ("backend", False, "incapable-candidate", "update"),
    ("backend", False, "unrelated-candidate", "update"),
    ("backend", True, "missing", "update"), ("backend", True, "malformed", "update"),
    ("backend", True, "unknown", "update"),
    ("backend", False, None, "add-local"),
    ("backend", False, "midstop-after-stop", "update"),
    ("backend", False, "midstop-during-term", "update"),
], ids=["stopped-backend", "stopped-local", "running-backend", "running-local",
       "incapable-retained-runtime", "unrelated-retained-runtime",
       "incapable-candidate-runtime", "unrelated-candidate-runtime",
       "missing-stop-marker", "malformed-stop-marker", "unknown-stop-binding",
       "add-local-two-stop-build", "midstop-crash-after-stop",
       "midstop-crash-during-term"])
def test_canonical_stop_then_fresh_update_cli_uses_retained_c1_authority(
        tmp_path, monkeypatch, mode, start_stopped, marker_fault, operation):
    from astrid.installer.app_update import PinokioLifecycle
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

    canonical_pinokio = Path(os.environ.get(
        "ASTRID_PINOKIO_CHECKOUT", checkout.parent / "astrid-pinokio",
    ))
    canonical_session = canonical_pinokio / "pinokio" / "session.mjs"
    node_binary = shutil.which("node")
    if not canonical_session.is_file() or node_binary is None:
        pytest.fail("The canonical Pinokio checkout and Node executable are required for this integration proof.")

    door_port = _free_loopback_port()
    preview_port = _free_loopback_port() if mode == "local" or operation == "add-local" else None
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
                 "start.js", "update.js", "add-local.js", "shell.mjs"):
        shutil.copyfile(canonical_pinokio / "pinokio" / name, pinokio_directory / name)
    for name in ("session.mjs", "lib.js"):
        script = pinokio_directory / name
        source = script.read_text(encoding="utf-8")
        # Keep the canonical lifecycle/session code but move its fixed listener
        # and probe ports to isolated loopback ports for parallel test runs.
        source = source.replace("17340", str(door_port))
        if preview_port is not None:
            source = source.replace("5180", str(preview_port))
        script.write_text(source, encoding="utf-8")
    shutil.copyfile(canonical_pinokio / "pinokio.js", repo / "pinokio.js")
    platforms = pinokio_directory / "platforms"
    platforms.mkdir()
    for name in ("posix.js", "windows.js"):
        shutil.copyfile(canonical_pinokio / "pinokio" / "platforms" / name, platforms / name)

    if mode == "local" or operation == "add-local":
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
            f"  }}).listen({preview_port}, '127.0.0.1');\n"
            "}\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(editor)], check=True)
        subprocess.run(["git", "-C", str(editor), "remote", "add", "origin",
                        "https://github.com/banodoco/astrid-app"], check=True)

    app_root = repo / "app" / "astrid"
    data_root = tmp_path / "workspace"
    old_root = app_root / "versions" / "retained-c2"
    candidate_root = app_root / "versions" / "e2-candidate"
    runtime_root = _runtime_source_checkout()
    prepared_packages = Path(sysconfig.get_paths()["purelib"])

    def version(root, release, source_root, *, profile_runtime=None, profile_source=None):
        environment = root / "py"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / "bin" / "python"
        site_packages = next((environment / "lib").glob("python*/site-packages"))
        (site_packages / "astrid-retained-foreground-test.pth").write_text(
            "\n".join(map(str, (source_root, prepared_packages))) + "\n",
            encoding="utf-8",
        )
        runtime_package = _copy_runtime_package_source(
            runtime_root, root / "runtime-source",
        )
        _install_runtime_package(python, runtime_package)
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
            "profile": "astrid", "runtime_checkout": str(runtime_package),
            "source_checkout": str(profile_source or source_root), "runtime_environment": str(environment),
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
            "ASTRID_NPX_EXECUTABLE": str(npx), "ASTRID_RUNTIME_DOOR_PORT": str(door_port),
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
    old = version(old_root, 17, legacy_source)
    candidate = version(candidate_root, 18, candidate_source,
                        profile_runtime=candidate_runtime, profile_source=candidate_source)
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



    for name in ("stop.js", "installer-lock.py"):
        shutil.copyfile(canonical_pinokio / "pinokio" / name, pinokio_directory / name)
    child_env = {key: value for key, value in os.environ.items()
                 if key not in {"PYTHONPATH", "PYTHONHOME", "ASTRID_INSTALLER_OPERATION_ID"}}
    lifecycle.start(old, data_root, realm["realm_id"])
    original_pointer = (app_root / "current").read_bytes()
    if start_stopped:
        stop_code = (
            f"const stop=require({json.dumps(str(pinokio_directory / 'stop.js'))});"
            "(async()=>{const s=stop;"
            "for(const step of s.run)await step.method({},undefined,{api:{running:{}}});})()"
            ".catch(e=>{console.error(e);process.exitCode=1;});"
        )
        stopped = subprocess.run([node_binary, "-e", stop_code], cwd=repo, env=child_env,
                                 capture_output=True, text=True, timeout=60)
        assert stopped.returncode == 0, stopped.stderr
        assert not (data_root / "runtime" / "discovery.json").exists()
        assert not (app_root / "pinokio-control.json").exists()
        stopped_owner = read_json(data_root / "runtime" / "instance.lock")
        receipt = stopped_owner["completed_stop"]
        binding = receipt["binding"]
        assert stopped_owner["state"] == "completed_stop"
        assert receipt["version"] == 1
        assert stopped_owner["runtime_instance_id"]
        assert stopped_owner["realm_id"] == realm["realm_id"]
        assert stopped_owner["realm_root"] == realm["realm_root"]
        assert stopped_owner["data_root"] == str(data_root)
        assert binding["program_root"] == str(app_root)
        assert binding["data_root"] == str(data_root)
        assert binding["owner_version_root"] == str(old.root)
        assert binding["realm_id"] == realm["realm_id"]
        assert binding["realm_root"] == realm["realm_root"]
        if marker_fault == "missing":
            (data_root / "runtime" / "instance.lock").unlink()
        elif marker_fault == "malformed":
            write_json(data_root / "runtime" / "instance.lock", {
                "state": "completed_stop", "completed_stop": {"version": 1},
            })
        elif marker_fault == "unknown":
            stopped_owner["completed_stop"]["binding"]["owner_release"] += 100
            write_json(data_root / "runtime" / "instance.lock", stopped_owner)
    elif marker_fault is None:
        running_owner = read_json(data_root / "runtime" / "instance.lock")
        running_discovery = read_json(data_root / "runtime" / "discovery.json")
        assert "completed_stop" not in running_owner
        assert running_owner["runtime_instance_id"] == running_discovery["runtime_instance_id"]
        assert running_owner["realm_id"] == realm["realm_id"]
    # Acquisition is supplied from prepared candidate files; execute exactly
    # the install dispatch used by the canonical Update bootstrap.
    manifest = tmp_path / "manifest.json"
    write_json(manifest, {"release": candidate.release})
    uv = tmp_path / "uv"
    uv.touch()
    cli_code = "\n".join([
        "import sys,json; from pathlib import Path",
        "from astrid.installer import cli",
        "from astrid.installer.backend import Backend",
        "from astrid.installer.core import Version",
        "candidate=Version.load(json.loads(Path(sys.argv[2]).read_text()))",
        "Backend.stage=lambda *args,**kwargs:candidate",
        "Backend.sync_skills=lambda *args,**kwargs:None",
        "cli.validate_manifest=lambda value:value",
        "raise SystemExit(cli.main(['--root',sys.argv[1],'--managed-by','pinokio','--manifest',sys.argv[3],'--uv',sys.argv[4]]))",
    ])
    def run_cli():
        return subprocess.run([str(candidate.python), "-I", "-c", cli_code, str(app_root),
            str(candidate.root / "version.json"), str(manifest), str(uv)], cwd=repo, env=child_env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)

    if marker_fault in {"midstop-after-stop", "midstop-during-term"}:
        # The controller dies after Runtime signalled the owner: either right
        # after the real stop returned, or while it waits for the TERMed owner.
        kill_body = (
            "    r=_orig(self,*a,**k)\n"
            if marker_fault == "midstop-after-stop" else
            "    os.kill(int(k['pid']),signal.SIGTERM)\n"
        )
        crash_code = cli_code.replace(
            "cli.validate_manifest=lambda value:value",
            "cli.validate_manifest=lambda value:value\n"
            "import os,signal\n"
            "from astrid_runtime.runtime_boundary import LocalRuntimeBoundary as _B\n"
            "_orig=_B.stop_owner\n"
            "def _kill(self,*a,**k):\n"
            + kill_body +
            "    os.kill(os.getpid(),signal.SIGKILL)\n"
            "_B.stop_owner=_kill")
        before_lock = read_json(data_root / "runtime" / "instance.lock")
        crashed = subprocess.run([str(candidate.python), "-I", "-c", crash_code, str(app_root),
            str(candidate.root / "version.json"), str(manifest), str(uv)], cwd=repo, env=child_env,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        assert crashed.returncode == -9, crashed.stdout[-2000:]
        assert read_json(app_root / "update-transaction.json").get("step") == "stopping"
        crashed_lock = read_json(data_root / "runtime" / "instance.lock")
        assert crashed_lock["state"] == "stopping"
        assert crashed_lock["pid"] == before_lock["pid"]
        assert crashed_lock["process_birth_id"] == before_lock["process_birth_id"]
        # The user retries after the TERMed owner has exited on its own.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                os.kill(int(crashed_lock["pid"]), 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            pytest.fail("The TERMed owner did not exit.")
        assert not (data_root / "runtime" / "discovery.json").exists()
        retry = run_cli()
        assert retry.returncode == 0, retry.stdout[-3000:]
        assert read_json(app_root / "update-transaction.json")["step"] in {"complete", "rolled_back"}
        final_lock = read_json(data_root / "runtime" / "instance.lock")
        final_discovery = read_json(data_root / "runtime" / "discovery.json")
        assert "state" not in final_lock
        assert final_lock["pid"] != before_lock["pid"]
        assert final_lock["runtime_instance_id"] == final_discovery["runtime_instance_id"]
        os.kill(int(final_lock["pid"]), 0)
        return

    if marker_fault in {
        "incapable-runtime", "unrelated-runtime",
        "incapable-candidate", "unrelated-candidate",
    }:
        fault_version = candidate if marker_fault.endswith("candidate") else old
        fault_site = next((fault_version.root / "py" / "lib").glob("python*/site-packages"))
        if marker_fault.startswith("incapable"):
            bootstrap = fault_site / "astrid_runtime" / "bootstrap.py"
            source = bootstrap.read_text(encoding="utf-8")
            assert source.count("PINOKIO_STOP_HANDOFF_VERSION = 1") == 1
            bootstrap.write_text(source.replace(
                "PINOKIO_STOP_HANDOFF_VERSION = 1", "PINOKIO_STOP_HANDOFF_VERSION = 0",
            ), encoding="utf-8")
            for bytecode in (bootstrap.parent / "__pycache__").glob("bootstrap*.pyc"):
                bytecode.unlink()
        else:
            distributions = list(fault_site.glob("astrid_runtime-*.dist-info/direct_url.json"))
            assert len(distributions) == 1
            write_json(distributions[0], {
                "url": (tmp_path / "unrelated-runtime-source").as_uri(), "dir_info": {},
            })
        original_discovery = read_json(data_root / "runtime" / "discovery.json")
        original_owner = read_json(data_root / "runtime" / "instance.lock")
        controller_path = app_root / "pinokio-control.json"
        original_controller = controller_path.read_bytes() if controller_path.exists() else None
        try:
            rejected = run_cli()
            assert rejected.returncode == 1, rejected.stdout
            detail_path = next(
                Path(line.removeprefix("Details: ").strip())
                for line in rejected.stdout.splitlines() if line.startswith("Details: ")
            )
            assert "cannot safely hand off Pinokio Stop" in detail_path.read_text(encoding="utf-8")
            assert current(app_root) == old
            assert (app_root / "current").read_bytes() == original_pointer
            assert not (app_root / "update-transaction.json").exists()
            assert read_json(data_root / "runtime" / "instance.lock") == original_owner
            assert "completed_stop" not in original_owner
            assert read_json(data_root / "runtime" / "discovery.json") == original_discovery
            assert (controller_path.read_bytes() if controller_path.exists() else None) == original_controller
        finally:
            if (data_root / "runtime" / "discovery.json").exists() or controller_path.exists():
                backend.stop(current(app_root), data_root)
        return

    if operation == "update":
        get_action = (
            f"const action=require({json.dumps(str(pinokio_directory / 'update.js'))});"
            f"const menu=require({json.dumps(str(repo / 'pinokio.js'))}).menu;"
            "const fs=require('node:fs'),path=require('node:path');"
            "(async()=>{const items=await menu({}, {running:()=>false,exists:p=>fs.existsSync(path.join(process.cwd(),p))});"
            "if(!items.some(x=>['Update','Update and restart'].includes(x.text)))throw Error('No Update menu item');"
            "const s=await action();"
            "console.log(JSON.stringify(s.run.map(x=>({params:x.params||null,method:typeof x.method}))));})()"
            ".catch(e=>{console.error(e);process.exitCode=1;});"
        )
        action = subprocess.run([node_binary, "-e", get_action], cwd=repo, env=child_env,
                                capture_output=True, text=True, timeout=60)
        assert action.returncode == 0, action.stderr
        steps = json.loads(action.stdout)
        if mode == "backend":
            assert len(steps) == 2
            assert "--root" in steps[-1]["params"]["message"]
            assert "--update" not in steps[-1]["params"]["message"]
        else:
            assert len(steps) == 3
            assert steps[-2]["method"] == steps[-1]["method"] == "function"
            local_ensure_ready = tmp_path / "local-update-session-ready"
            local_ensure_log = tmp_path / "local-update-session.log"
            ensure_local_code = (
                f"const action=require({json.dumps(str(pinokio_directory / 'update.js'))});"
                f"const fs=require('node:fs'); const ready={json.dumps(str(local_ensure_ready))};"
                "(async()=>{const s=await action();"
                "const result=await s.run[1].method({},event=>process.stdout.write(event.raw||''));"
                "fs.writeFileSync(ready,JSON.stringify(result));})()"
                ".catch(e=>{console.error(e);process.exitCode=1;});"
            )
            local_ensure_output = local_ensure_log.open("w", encoding="utf-8")
            local_ensure_process = subprocess.Popen(
                [node_binary, "-e", ensure_local_code], cwd=repo, env=child_env,
                stdout=local_ensure_output, stderr=subprocess.STDOUT, text=True,
            )
            ensure_deadline = time.monotonic() + 240
            while not local_ensure_ready.exists() and local_ensure_process.poll() is None:
                if time.monotonic() >= ensure_deadline:
                    break
                time.sleep(0.1)
            if not local_ensure_ready.exists():
                cleanup_message = ""
                if (data_root / "runtime" / "discovery.json").exists() or (app_root / "pinokio-control.json").exists():
                    try:
                        lifecycle.stop(current(app_root), data_root)
                    except Exception as cleanup_error:  # noqa: BLE001 - preserve the readiness failure.
                        cleanup_message = f"\nCleanup after ensure failure: {cleanup_error}"
                try:
                    ensure_failure = local_ensure_process.communicate(timeout=60)
                except subprocess.TimeoutExpired:
                    local_ensure_process.terminate()
                    ensure_failure = local_ensure_process.communicate(timeout=10)
                    cleanup_message += "\nThe local Update ensure controller remained alive after C1 cleanup."
                local_ensure_output.close()
                raise AssertionError(
                    f"The canonical local Update ensure step did not reach readiness. "
                    f"Process result: {ensure_failure}; output:\n{local_ensure_log.read_text(encoding='utf-8')}"
                    f"{cleanup_message}"
                )
    else:
        public = repo / "app" / "editor" / "public"
        public.mkdir(parents=True, exist_ok=True)
        local_manifest = public / "install-manifest.json"
        write_json(local_manifest, {"release": candidate.release})
        installer = public / "install"
        installer.write_text(
            "#!/bin/sh\nexec " + " ".join(map(shlex.quote, [
                str(candidate.python), "-I", "-c", cli_code, str(app_root),
                str(candidate.root / "version.json"), str(local_manifest), str(uv),
            ])) + "\n",
            encoding="utf-8",
        )
        installer.chmod(0o755)

        shim = tmp_path / "command-shims" / "git"
        shim.parent.mkdir(parents=True)
        shim.write_text(
            "#!/bin/sh\ncase \" $* \" in *' pull '*) exit 0 ;; esac\nexec /usr/bin/git \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
        child_env["PATH"] = str(shim.parent) + os.pathsep + str(Path(node_binary).parent) + os.pathsep + os.defpath
        add_local_code = (
            f"const action=require({json.dumps(str(pinokio_directory / 'add-local.js'))});"
            "(async()=>{const s=await action();"
            "await s.run[0].method({},()=>{},{api:{running:{}}});"
            "await s.run[1].method({},()=>{});"
            "console.log(JSON.stringify(s.run.map(x=>({method:typeof x.method,params:x.params||null}))));})()"
            ".catch(e=>{console.error(e);process.exitCode=1;});"
        )
        action = subprocess.run([node_binary, "-e", add_local_code], cwd=repo, env=child_env,
                                capture_output=True, text=True, timeout=180)
        assert action.returncode == 0, action.stderr
        steps = json.loads(action.stdout)
        assert len(steps) == 7
        assert [steps[index]["method"] for index in (0, 1, 4, 5, 6)] == [
            "function", "function", "function", "string", "function",
        ]
        assert steps[2]["method"] == steps[3]["method"] == "string"

    def run_pinokio_shell(step):
        params = step["params"]
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", params["message"]],
            cwd=params["path"], env=child_env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=300,
        )

    try:
        if operation == "add-local":
            checkout = run_pinokio_shell(steps[2])
            assert checkout.returncode == 0, checkout.stdout
            result = run_pinokio_shell(steps[3])
        else:
            result = run_cli()
        if marker_fault:
            assert result.returncode == 1, result.stdout
            assert "stop" in result.stdout.lower() and "refus" in result.stdout.lower()
            assert current(app_root) == old
            assert (app_root / "current").read_bytes() == original_pointer
            rejected = read_json(app_root / "update-transaction.json")
            assert rejected["step"] == "complete"
            assert rejected["outcome"] == "stop_refused"
            assert not (data_root / "runtime" / "discovery.json").exists()
            assert not (app_root / "pinokio-control.json").exists()
            return
        assert result.returncode == 0, result.stdout
        assert current(app_root) == candidate
        completed = read_json(app_root / "update-transaction.json")
        assert completed["step"] == "complete"
        assert completed["transaction_kind"] == "migration"
        operation_id = completed["operation_id"]
        assert completed["data_root"] == str(data_root)
        assert completed["old"]["root"] == str(old.root)
        assert completed["candidate"]["root"] == str(candidate.root)
        assert completed["realm"]["realm_id"] == realm["realm_id"]
        assert completed["realm"]["realm_root"] == realm["realm_root"]
        assert (app_root / "pinokio-control.json").exists()

        if operation == "update" and mode == "local":
            local_ensure_process.communicate(timeout=60)
            local_ensure_output.close()
            assert local_ensure_process.returncode == 0, local_ensure_log.read_text(encoding="utf-8")

        if operation == "add-local":
            second_stop_code = (
                f"const action=require({json.dumps(str(pinokio_directory / 'add-local.js'))});"
                "(async()=>{const s=await action();"
                "const r=await s.run[4].method({},()=>{},{api:{running:{}}});"
                "console.log(JSON.stringify(r));})()"
                ".catch(e=>{console.error(e);process.exitCode=1;});"
            )
            second_stop = subprocess.run([node_binary, "-e", second_stop_code], cwd=repo, env=child_env,
                                         capture_output=True, text=True, timeout=180)
            assert second_stop.returncode == 0, second_stop.stderr
            completed_stop = read_json(data_root / "runtime" / "instance.lock")
            assert completed_stop["state"] == "completed_stop"
            assert completed_stop["completed_stop"]["binding"]["owner_version_root"] == str(candidate.root)
            assert not (data_root / "runtime" / "discovery.json").exists()
            assert not (app_root / "pinokio-control.json").exists()

            build = run_pinokio_shell(steps[5])
            assert build.returncode == 0, build.stdout
            assert not (data_root / "runtime" / "discovery.json").exists()
            assert not (app_root / "pinokio-control.json").exists()

            save_mode_code = (
                f"const action=require({json.dumps(str(pinokio_directory / 'add-local.js'))});"
                "(async()=>{const s=await action();"
                "console.log(JSON.stringify(await s.run[6].method()));})()"
                ".catch(e=>{console.error(e);process.exitCode=1;});"
            )
            saved = subprocess.run([node_binary, "-e", save_mode_code], cwd=repo, env=child_env,
                                   capture_output=True, text=True, timeout=60)
            assert saved.returncode == 0, saved.stderr
            assert read_json(repo / "app" / "install-mode.json")["mode"] == "local"
            assert current(app_root) == candidate
            assert read_json(app_root / "update-transaction.json")["operation_id"] == operation_id
            assert read_json(backend.service.selection)["identity"] == {
                "release": candidate.release, "managed_by": "pinokio",
            }
            return

        # A second fresh Update exercises the retry path while keeping the
        # selected candidate and its foreground owner healthy.
        retry = run_cli()
        retry_diagnostic = retry.stdout
        if retry.returncode != 0:
            diagnostic = [retry.stdout]
            for line in retry.stdout.splitlines():
                if line.startswith("Details: "):
                    reported = Path(line.removeprefix("Details: ").strip())
                    if reported.is_file():
                        diagnostic.append(
                            f"\n--- reported Runtime details: {reported} ---\n"
                            f"{reported.read_text(encoding='utf-8', errors='replace')}"
                        )
            for label, path in (
                ("Runtime log", host_home / ".local/state/astrid-runtime/logs/install.log"),
                ("Runtime source profile", data_root / "runtime/source-profiles/astrid.json"),
                ("selected pointer", app_root / "current"),
                ("discovery", data_root / "runtime/discovery.json"),
                ("owner lock", data_root / "runtime/instance.lock"),
                ("journal", app_root / "update-transaction.json"),
                ("Pinokio controller", app_root / "pinokio-control.json"),
            ):
                if path.is_file():
                    if label == "Pinokio controller":
                        controller = read_json(path)
                        controller.pop("token", None)
                        content = json.dumps(controller, sort_keys=True)
                    else:
                        content = path.read_text(encoding="utf-8", errors="replace")
                    diagnostic.append(f"\n--- {label}: {path} ---\n{content}")
            diagnostic.append(f"\n--- fresh CLI code ---\n{cli_code}\n")
            retry_diagnostic = "".join(diagnostic)
        assert retry.returncode == 0, retry_diagnostic
        assert current(app_root) == candidate
        retried = read_json(app_root / "update-transaction.json")
        assert retried["step"] == "complete"
        assert retried["candidate"]["root"] == str(candidate.root)
        assert retried["operation_id"] != operation_id
        assert (app_root / "current").read_bytes() != original_pointer
        discovery = read_json(data_root / "runtime" / "discovery.json")
        boundary_probe = "\n".join([
            "import json,sys; from pathlib import Path",
            "from astrid_runtime.runtime_boundary import LocalRuntimeBoundary",
            "boundary=LocalRuntimeBoundary()",
            "identity=boundary.owner_startup_identity(endpoint=sys.argv[1],instance_id=sys.argv[2])",
            "metadata=boundary.endpoint_metadata(endpoint=sys.argv[1],credential_file=Path(sys.argv[3]))",
            "print(json.dumps({'identity':identity,'metadata':metadata}))",
        ])
        inspected = subprocess.run(
            [str(candidate.python), "-I", "-c", boundary_probe, discovery["endpoint"],
             discovery["runtime_instance_id"], str(data_root / "runtime" / "credentials" / "owner.token")],
            cwd=repo, env=child_env, check=True, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=30,
        )
        boundary_result = json.loads(inspected.stdout)
        identity = boundary_result["identity"]
        assert identity["runtime_instance_id"] == discovery["runtime_instance_id"]
        metadata = boundary_result["metadata"]
        assert metadata["runtime_instance_id"] == discovery["runtime_instance_id"]
        assert discovery["active_realm"] == realm["realm_id"]
        assert discovery["realm_root"] == realm["realm_root"]
        assert read_json(backend.service.selection)["identity"] == {
            "release": candidate.release, "managed_by": "pinokio",
        }
        assert read_json(candidate.profile)["runtime_checkout"] == str(candidate_runtime)
    finally:
        if (data_root / "runtime" / "discovery.json").exists() or (app_root / "pinokio-control.json").exists():
            active_failure = sys.exc_info()[0] is not None
            try:
                lifecycle.stop(current(app_root), data_root)
            except Exception as cleanup_error:
                (tmp_path / "cleanup-failure.log").write_text(
                    f"{type(cleanup_error).__name__}: {cleanup_error}\n", encoding="utf-8",
                )
                if not active_failure:
                    raise
        if operation == "update" and mode == "local" and "local_ensure_process" in locals():
            if local_ensure_process.poll() is None:
                local_ensure_process.communicate(timeout=60)
            if not local_ensure_output.closed:
                local_ensure_output.close()
