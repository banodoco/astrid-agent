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
from tests.lanes import unavailable
from tests.test_installer_pinokio_integration import (
    _copy_runtime_package_source,
    _install_runtime_package,
    _runtime_source_checkout,
)

from astrid.installer.core import Version, switch, write_json
from astrid.installer.launcher import install as install_launcher


@pytest.mark.integration
@pytest.mark.posix_native  # POSIX generated shell and process control
@pytest.mark.parametrize("initially_running", [False, True])
def test_switch_after_installer_starts_session(tmp_path, monkeypatch, initially_running):
    mode = "backend"
    from astrid.installer.app_update import PinokioLifecycle
    from astrid.installer.backend import Backend
    from astrid.installer.core import Version, current, read_json, switch, write_json

    checkout = Path(__file__).resolve().parents[1]
    canonical_pinokio = Path(os.environ.get(
        "ASTRID_PINOKIO_CHECKOUT", checkout.parent / "astrid-pinokio",
    ))
    canonical_session = canonical_pinokio / "pinokio" / "session.mjs"
    node_binary = shutil.which("node")
    if not canonical_session.is_file() or node_binary is None:
        unavailable("The read-only canonical Pinokio checkout (ASTRID_PINOKIO_CHECKOUT) and a Node executable are required.")

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
    shutil.copyfile(canonical_pinokio / "pinokio.js", repo / "pinokio.js")
    lib = pinokio_directory / "lib.js"
    lib.write_text(lib.read_text().replace("17340", "18342"))
    session = pinokio_directory / "session.mjs"
    session.write_text(session.read_text().replace("17340", "18342"))
    platforms = pinokio_directory / "platforms"
    platforms.mkdir()
    for name in ("posix.js", "windows.js"):
        shutil.copyfile(canonical_pinokio / "pinokio" / "platforms" / name, platforms / name)

    # Minimal local-editor seam: dependency/build invocations succeed, and
    # the preview serves the two health endpoints required by the canonical
    # session's real local readiness check.
    editor = repo / "app" / "editor"
    scripts = editor / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "dev-local-workspace.mjs").write_text(
        "import fs from 'node:fs'; import http from 'node:http';\n"
        "if (process.argv.includes('--build') && fs.existsSync('fail-build')) process.exit(9);\n"
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
        "  }).listen(5180, '127.0.0.1');\n"
        "}\n",
        encoding="utf-8",
    )

    subprocess.run(["git", "init", "-q", str(editor)], check=True)
    subprocess.run(["git", "-C", str(editor), "remote", "add", "origin",
                    "https://github.com/banodoco/astrid-app"], check=True)

    app_root = repo / "app" / "astrid"
    data_root = tmp_path / "workspace"
    old_root = app_root / "versions" / "retained-c2"
    candidate_root = app_root / "versions" / "e2-candidate"
    runtime_root = _runtime_source_checkout()
    prepared_packages = Path(sysconfig.get_paths()["purelib"])

    def version(root, release, source_root, *, profile_source=None):
        environment = root / "py"
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / "bin" / "python"
        site_packages = next((environment / "lib").glob("python*/site-packages"))
        (site_packages / "astrid-retained-foreground-test.pth").write_text(
            "\n".join(map(str, (source_root, prepared_packages))) + "\n",
            encoding="utf-8",
        )
        # Backend.stage installs Runtime non-editably; the installer's
        # hand-off qualification only accepts such an installed distribution.
        runtime_package = _copy_runtime_package_source(runtime_root, root / "runtime-source")
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
            "ASTRID_NPX_EXECUTABLE": str(npx), "ASTRID_RUNTIME_DOOR_PORT": "18342",
            "PATH": os.defpath,
        }
        result = Version(root, python, profile, env, release=release, managed_by="pinokio")
        write_json(root / "version.json", result.document())
        return result

    candidate_source = candidate_root / "agent-source"
    candidate_source.mkdir(parents=True)
    shutil.copytree(checkout / "astrid", candidate_source / "astrid")
    shutil.copyfile(checkout / "pyproject.toml", candidate_source / "pyproject.toml")
    old = version(old_root, 17, checkout)
    candidate = version(candidate_root, 18, candidate_source,
                        profile_source=candidate_source)
    write_json(app_root / "install.json", {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio", "data_root": str(data_root),
    })
    write_json(repo / "app" / "install-mode.json", {"schema_version": 1, "mode": mode})
    write_json(app_root / "service-env.json", {})
    switch(app_root, old)
    install_launcher(app_root)

    backend = Backend(app_root, tmp_path / "install.log", tmp_path / "uv", say=lambda _line: None)
    backend.managed_by = "pinokio"
    write_json(backend.service.selection, {
        "owner": "haus.astrid.runtime", "managed_by": "pinokio",
        "program_root": str(app_root), "data_root": str(data_root),
        "identity": {"release": old.release, "managed_by": "pinokio"},
    })
    print("CREATE REALM", flush=True)
    realm = backend.create(old, data_root)
    # A real install registers the realm in its selection record; the Runtime
    # stop binding requires that record (realm_id, realm_root) to match.
    backend.register(old, data_root)
    lifecycle = PinokioLifecycle(app_root, backend)
    backend.stage = lambda *_args, **_kwargs: candidate
    backend.sync_skills = lambda *_args, **_kwargs: None
    backend.finish = lambda *_args, **_kwargs: None

    for name in ("stop.js", "installer-lock.py"):
        shutil.copyfile(canonical_pinokio / "pinokio" / name, pinokio_directory / name)
    child_env = {key: value for key, value in os.environ.items()
                 if key not in {"PYTHONPATH", "PYTHONHOME", "ASTRID_INSTALLER_OPERATION_ID"}}
    print("START HOSTED", flush=True)
    lifecycle.start(old, data_root, realm["realm_id"])
    print("HOSTED READY", flush=True)
    stop_code = (
        f"const stop=require({json.dumps(str(pinokio_directory / 'stop.js'))});"
        "(async()=>{const s=stop;"
        "for(const step of s.run)await step.method({},undefined,{api:{running:{}}});})()"
        ".catch(e=>{console.error(e);process.exitCode=1;});"
    )
    if not initially_running:
        print("STOP INITIAL", flush=True)
        stopped = subprocess.run([node_binary,"-e",stop_code],cwd=repo,env=child_env,
                                 capture_output=True,text=True,timeout=60)
        assert stopped.returncode == 0, stopped.stderr
    get_action = (
        f"const action=require({json.dumps(str(pinokio_directory / 'add-local.js'))});"
        f"const menu=require({json.dumps(str(repo / 'pinokio.js'))}).menu;"
        "const fs=require('node:fs'),path=require('node:path');"
        "(async()=>{const items=await menu({}, {running:()=>false,exists:p=>fs.existsSync(path.join(process.cwd(),p))});"
        f"if(!{str(initially_running).lower()} && !items.some(x=>x.text==='Switch to fully local'))throw Error('No Switch menu item');"
        "const s=await action(); "
        "console.log(JSON.stringify(s.run.map(x=>x.params||null)));})()"
        ".catch(e=>{console.error(e);process.exitCode=1;});"
    )
    print("GET ACTION", flush=True)
    action = subprocess.run([node_binary,"-e",get_action],cwd=repo,env=child_env,
                            capture_output=True,text=True,timeout=60)
    assert action.returncode == 0, action.stderr
    steps = json.loads(action.stdout)
    assert "--update" in steps[3]["message"]
    assert steps[4] is None
    assert "session.mjs" in steps[5]["message"]
    # The first custom step is the real Switch stop for either initial state.
    print("SWITCH INITIAL STOP", flush=True)
    initial_stop_code = (
        f"const action=require({json.dumps(str(pinokio_directory / 'add-local.js'))});"
        "(async()=>{const s=await action(); await s.run[0].method({},undefined,{api:{running:{}}});})()"
        ".catch(e=>{console.error(e);process.exitCode=1;});"
    )
    initial_stop = subprocess.run([node_binary, "-e", initial_stop_code], cwd=repo, env=child_env,
                                  capture_output=True, text=True, timeout=60)
    assert initial_stop.returncode == 0, initial_stop.stderr
    # Acquisition is supplied from prepared candidate files; execute exactly
    # the install dispatch used by the canonical Update bootstrap.
    manifest=tmp_path / "manifest.json"
    write_json(manifest,{"release":candidate.release})
    uv=tmp_path / "uv"
    uv.touch()
    cli_code="\n".join([
        "import sys,json; from pathlib import Path",
        "from astrid.installer import cli",
        "from astrid.installer.backend import Backend",
        "from astrid.installer.core import Version",
        "candidate=Version.load(json.loads(Path(sys.argv[2]).read_text()))",
        "Backend.stage=lambda *args,**kwargs:candidate",
        "Backend.sync_skills=lambda *args,**kwargs:None",
        "cli.validate_manifest=lambda value:value",
        "raise SystemExit(cli.main(['--update','--root',sys.argv[1],'--managed-by','pinokio','--manifest',sys.argv[3],'--uv',sys.argv[4]]))",
    ])
    def run_cli():
        return subprocess.run([str(candidate.python),"-I","-c",cli_code,str(app_root),
            str(candidate.root / "version.json"),str(manifest),str(uv)],cwd=repo,env=child_env,
            text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180)
    try:
        print("UPDATE CLI", flush=True)
        result=run_cli()
        print(f"UPDATE: {result.returncode}\n{result.stdout}")
        assert result.returncode == 0, result.stdout
        assert current(app_root) == candidate
        assert read_json(app_root / "update-transaction.json")["step"] == "complete"
        assert (app_root / "pinokio-control.json").exists()
        # Run the actual post-update custom step, which must use C1 before build.
        handoff_code = (
            f"const action=require({json.dumps(str(pinokio_directory / 'add-local.js'))});"
            "(async()=>{const s=await action(); await s.run[4].method({},undefined,{api:{running:{}}});})()"
            ".catch(e=>{console.error(e);process.exitCode=1;});"
        )
        handoff = subprocess.run([node_binary, "-e", handoff_code], cwd=repo, env=child_env,
                                 capture_output=True, text=True, timeout=60)
        assert handoff.returncode == 0, handoff.stderr
        marker = data_root / "switch-preserved.txt"
        marker.write_text("workspace preserved")
        editor = repo / "app" / "editor"
        (editor / "fail-build").touch()
        def build():
            return subprocess.run(["/bin/sh", "-c", steps[5]["message"]], cwd=repo,
                                  env=child_env, capture_output=True, text=True, timeout=90)
        failed = build()
        print(f"BUILD FAILURE: {failed.returncode}\n{failed.stdout}\n{failed.stderr}")
        assert failed.returncode != 0
        assert read_json(repo / "app" / "install-mode.json")["mode"] == "backend"
        assert marker.read_text() == "workspace preserved"
        (editor / "fail-build").unlink()
        # A user retries the whole Switch action, including the stopped-owner
        # admission and the installer's newly started session handoff.
        retry_stop = subprocess.run([node_binary, "-e", initial_stop_code], cwd=repo,
                                    env=child_env, capture_output=True, text=True, timeout=60)
        assert retry_stop.returncode == 0, retry_stop.stderr
        retry_update = run_cli()
        print(f"UPDATE RETRY: {retry_update.returncode}\n{retry_update.stdout}")
        assert retry_update.returncode == 0, retry_update.stdout
        retry_handoff = subprocess.run([node_binary, "-e", handoff_code], cwd=repo,
                                       env=child_env, capture_output=True, text=True, timeout=60)
        assert retry_handoff.returncode == 0, retry_handoff.stderr
        assert read_json(repo / "app" / "install-mode.json")["mode"] == "backend"
        assert marker.read_text() == "workspace preserved"
        success = build()
        print(f"BUILD RETRY: {success.returncode}\n{success.stdout}\n{success.stderr}")
        assert success.returncode == 0, success.stderr
        assert read_json(repo / "app" / "install-mode.json")["mode"] == "backend"
        save_code = (
            f"const action=require({json.dumps(str(pinokio_directory / 'add-local.js'))});"
            "(async()=>{const s=await action(); await s.run.at(-1).method();})()"
            ".catch(e=>{console.error(e);process.exitCode=1;});"
        )
        saved = subprocess.run([node_binary, "-e", save_code], cwd=repo, env=child_env,
                               capture_output=True, text=True, timeout=30)
        assert saved.returncode == 0, saved.stderr
        assert read_json(repo / "app" / "install-mode.json")["mode"] == "local"
        assert marker.read_text() == "workspace preserved"
        # A fresh local serve session must reach its real readiness checks.
        start_code = (
            f"const action=require({json.dumps(str(pinokio_directory / 'start.js'))});"
            "(async()=>{const s=await action(); console.log(JSON.stringify(s.run[1].params.message));})()"
            ".catch(e=>{console.error(e);process.exitCode=1;});"
        )
        start = subprocess.run([node_binary, "-e", start_code], cwd=repo, env=child_env,
                               capture_output=True, text=True, timeout=30)
        assert start.returncode == 0, start.stderr
        import time
        launch_log = tmp_path / "launch.log"
        with launch_log.open("w+") as output:
            launch = subprocess.Popen(["/bin/sh", "-c", json.loads(start.stdout)], cwd=repo,
                                      env=child_env, stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    output.flush()
                    if "ASTRID_PINOKIO_READY" in launch_log.read_text(): break
                    assert launch.poll() is None, launch_log.read_text()
                    time.sleep(0.25)
                else: pytest.fail(launch_log.read_text())
                print("LOCAL LAUNCH: ASTRID_PINOKIO_READY")
                stopped = subprocess.run([node_binary, "-e", stop_code], cwd=repo, env=child_env,
                                         capture_output=True, text=True, timeout=60)
                assert stopped.returncode == 0, stopped.stderr
                assert launch.wait(timeout=30) == 0
            finally:
                if launch.poll() is None: launch.kill(); launch.wait()
    finally:
        if (data_root / "runtime" / "discovery.json").exists() or (app_root / "pinokio-control.json").exists():
            try:
                lifecycle.stop(current(app_root),data_root)
            except Exception:
                # Only this fixture's retained test controller remains after C1.
                record = read_json(app_root / "pinokio-control.json")
                if record.get("pid") and not (data_root / "runtime" / "discovery.json").exists():
                    os.kill(record["pid"], signal.SIGKILL)
                else:
                    raise
