"""Joined Pinokio activation through real session, launcher and lock custody."""

import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import app_update as update
from astrid.installer.core import Version, current, read_json, write_json
from astrid.installer.foreground import launch as foreground_launch
from astrid.installer.service import LABEL, installer_lock_path

PINOKIO_SOURCE = Path(os.environ.get(
    "ASTRID_PINOKIO_CHECKOUT", Path(__file__).resolve().parents[2] / "astrid-pinokio"))
FAKE_RUNTIME = r'''
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

def main(argv=None):
    args = list(argv if argv is not None else sys.argv[1:])
    def value(key):
        return Path(args[args.index(key) + 1])
    data = value("--data-root")
    state = data / "runtime" / "test-owner.json"
    discovery = data / "runtime" / "discovery.json"
    def alive(pid):
        try: os.kill(pid, 0); return True
        except OSError: return False
    if args[0] == "status":
        owner = json.loads(state.read_text()) if state.exists() else {}
        running = bool(owner and alive(owner.get("pid", -1)))
        instance = owner.get("instance_id", "none")
        health = {"status": "ok" if running else "unavailable", "runtime_instance_id": instance,
                  "identity": {"release": owner.get("release"), "managed_by": "pinokio"}}
        doc = {"support": {"healthy": running, "pid_alive": running},
               "health": health,
               "discovery": {"active_realm": "realm" if running else None,
                             "runtime_instance_id": instance,
                             "identity": health["identity"]},
               "stale_discovery": False,
               "custody_blocked": owner.get("custody_blocked", False)}
        print(json.dumps(doc))
    elif args[0] == "down":
        if state.exists():
            owner = json.loads(state.read_text())
            if alive(owner.get("pid", -1)):
                os.kill(owner["pid"], signal.SIGTERM)
        state.unlink(missing_ok=True)
        print(json.dumps({"ok": True, "status": "stopped"}))
    elif args[0] == "run":
        from astrid.installer.app_update import locked
        from astrid.installer.service import installer_lock_path
        program = Path(os.environ["ASTRID_START_COMMAND"]).parent
        receipt = program / ".app-update-custody.json"
        lock = (Path(json.loads(receipt.read_text())["lock"]) if receipt.is_file()
                else installer_lock_path(Path.home(), sys.platform))
        try:
            with locked(lock):
                custody_blocked = False
        except ValueError:
            custody_blocked = True
        runtime_dir = data / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        release = int(os.environ["ASTRID_INSTALL_RELEASE"])
        instance = f"test-runtime-{release}-{os.getpid()}"
        owner = {"pid": os.getpid(), "release": release, "instance_id": instance,
                 "custody_blocked": custody_blocked}
        state.write_text(json.dumps(owner))
        discovery.write_text(json.dumps({"active_realm": "realm", "runtime_instance_id": instance,
                                         "identity": {"release": release, "managed_by": "pinokio"},
                                         "pid": os.getpid(),
                                         "door_endpoint": "http://127.0.0.1:__DOOR_PORT__"}))
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"status": "ok", "protocol": "workspace.v1"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_): pass
        HTTPServer(("127.0.0.1", __DOOR_PORT__), Handler).serve_forever()
    else:
        print(json.dumps({"ok": False, "error": "unsupported action"}))
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


def _install_version(tmp_path, root, name, release, capability, repo, pinned_site, node_target, door_port):
    from tests.test_installer_app_update import _install_test_runtime

    installed = _install_test_runtime(tmp_path, name, capability=capability)
    folder = root / "versions" / name
    environment = folder / "py"
    folder.mkdir(parents=True)
    shutil.move(str(installed.environment), environment)
    if sys.platform == "win32":
        python = environment / "Scripts" / "python.exe"
        site = environment / "Lib" / "site-packages"
    else:
        python = environment / "bin" / "python"
        site = next((environment / "lib").glob("python*/site-packages"))
    # The generated foreground runner imports Runtime paths under -I.  Populate
    # the isolated version from the pinned Runtime, then replace only its CLI
    # with the controlled session double below.
    import astrid_runtime
    package = site / "astrid_runtime"
    shutil.copytree(Path(astrid_runtime.__file__).parent, package, dirs_exist_ok=True)
    (package / "cli.py").write_text(FAKE_RUNTIME.replace("__DOOR_PORT__", str(door_port)), encoding="utf-8")
    (package / "__main__.py").write_text(
        "from .cli import main\nraise SystemExit(main())\n",
        encoding="utf-8",
    )
    # The Runtime runs under -I, so a path to the harness environment does not
    # process that environment's editable Runtime .pth.  Copy the exact pinned
    # protocol package into this isolated version just as the release-0
    # launcher fixture does.
    import runtime_protocol
    shutil.copytree(Path(runtime_protocol.__file__).parent, site / "runtime_protocol")
    # Add only the task checkout and pinned purelib for the retained Agent and
    # other test-only imports.
    (site / "astrid-test-paths.pth").write_text(
        f"{repo}\n{pinned_site}\n", encoding="utf-8")

    profile = folder / "profile.json"
    write_json(profile, {"runtime_checkout": str(installed.source),
                         "runtime_environment": str(environment)})
    node = folder / "node" / "bin" / "node"
    node.parent.mkdir(parents=True, exist_ok=True)
    node.write_text(f"#!/bin/sh\nexec {node_target!s} \"$@\"\n", encoding="utf-8")
    node.chmod(0o755)
    npm_root = node.parent.parent / "lib" / "node_modules" / "npm" / "bin"
    npm_root.mkdir(parents=True, exist_ok=True)
    (npm_root / "npm-cli.js").touch()
    (npm_root / "npx-cli.js").touch()
    version = Version(folder, python, profile,
                      {"ASTRID_NODE_EXECUTABLE": str(node)}, release, "pinokio")
    write_json(folder / "version.json", version.document())
    return version


def _copy_pinokio(repo, door_port):
    source = PINOKIO_SOURCE / "pinokio"
    target = repo / "pinokio"
    target.mkdir(parents=True)
    for name in ("session.mjs", "lifecycle.mjs", "lib.js", "environment.js"):
        payload = (source / name).read_text(encoding="utf-8").replace("17340", str(door_port))
        (target / name).write_text(payload, encoding="utf-8")
    shutil.copytree(source / "platforms", target / "platforms")


def _owner_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="This joined fixture uses POSIX venv launchers; custody units run natively on Windows.")
@pytest.mark.parametrize("outcome", ["updated", "rolled_back", "recovered", "recovered_running_candidate"])
def test_update_candidate_and_rollback_activate_under_retained_custody(
        tmp_path, monkeypatch, capsys, outcome):
    if not PINOKIO_SOURCE.is_dir():
        pytest.fail(f"Pinned Pinokio test source is unavailable: {PINOKIO_SOURCE}")
    node_target = shutil.which("node")
    if not node_target:
        pytest.fail("Node is required for the canonical Pinokio session regression.")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        door_port = probe.getsockname()[1]

    repo = tmp_path / "pinokio-home" / "api" / "astrid-app"
    root, data, home = repo / "app" / "astrid", tmp_path / "workspace", tmp_path / "home"
    versions_root = root / "versions"
    versions_root.mkdir(parents=True)
    from sysconfig import get_paths
    pinned_site = get_paths()["purelib"]
    checkout = Path(__file__).resolve().parents[1]
    retained_source = tmp_path / "retained-agent"
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "a53ef0c257e4b0bbc2a65959b9119e2cb4cff453", "astrid", "pyproject.toml"],
        cwd=checkout, check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source_archive:
        source_archive.extractall(retained_source, filter="data")
    old = _install_version(tmp_path, root, "old", 1, 0, retained_source,
                           pinned_site, node_target, door_port)
    candidate = _install_version(tmp_path, root, "candidate", 2, 1, Path(__file__).resolve().parents[1],
                                 pinned_site, node_target, door_port)
    repo.mkdir(parents=True, exist_ok=True)
    _copy_pinokio(repo, door_port)
    root.mkdir(parents=True, exist_ok=True)
    (root / "run").write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    (root / "run").chmod(0o755)
    write_json(root / "install.json", {
        "owner": LABEL, "managed_by": "pinokio", "data_root": str(data),
    })
    identity = old.document()["identity"]
    record = {"owner": LABEL, "managed_by": "pinokio", "program_root": str(root),
              "data_root": str(data), "identity": identity}
    write_json(root / "install.json", {**record})
    from astrid.installer.core import switch
    switch(root, old)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASTRID_HOME", str(home))
    # The updater sees this override; the pinned Pinokio launcher omits it.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    realm = {"realm_id": "realm", "realm_root": str(data / "realm")}
    schema = {"identity": {"version": 1, "kind": "v1", "realm_id": "realm"}, "target": 1}
    observations = []
    backend = SimpleNamespace(
        root=root,
        log=tmp_path / "installer.log",
        service=SimpleNamespace(home=home, system=sys.platform,
                                recorded=lambda: dict(record)),
        workspace=lambda *_args: dict(realm),
        run=lambda *_args, **_kwargs: json.dumps(schema),
        stage=lambda *_args: candidate,
        validate_candidate=lambda *_args: None,
        existing=lambda *_args: None,
        persist_identity=lambda version: (write_json(version.root / "version.json", version.document()),
                                          record.update(identity=version.document()["identity"])),
        stop=None,
    )
    lock_path = installer_lock_path(home, sys.platform)

    def stop(version, _data):
        state = data / "runtime" / "test-owner.json"
        if state.exists():
            owner = json.loads(state.read_text())
            pid = owner.get("pid")
            if isinstance(pid, int) and _owner_alive(pid):
                os.kill(pid, 15)
                deadline = time.monotonic() + 8
                while _owner_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.025)
            state.unlink(missing_ok=True)
        return {"status": "stopped"}

    fail_candidate = {"remaining": 1 if outcome == "rolled_back" else 0}

    def verify(version, selected_data, realm_id, **_kwargs):
        assert selected_data == data
        assert realm_id == "realm"
        assert current(root) == version
        status = subprocess.run(
            [version.python, "-I", "-m", "astrid_runtime", "status",
             "--data-root", str(data), "--json"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        report = json.loads(status.stdout)
        assert report["support"]["healthy"] is True
        assert report["health"]["identity"]["release"] == version.release
        assert report["custody_blocked"] is True
        from astrid.installer.app_update import locked
        with pytest.raises(ValueError, match="lock"):
            with locked(lock_path):
                pass
        with pytest.raises(ValueError, match="^Choose Update to finish$"):
            foreground_launch(root, [])
        contender = subprocess.run([str(root / "run")], capture_output=True, text=True, timeout=10)
        assert contender.returncode != 0
        assert "Choose Update to finish" in contender.stderr
        observations.append((version.release, report["health"]["runtime_instance_id"]))
        if version == candidate and fail_candidate["remaining"]:
            fail_candidate["remaining"] -= 1
            raise ValueError("candidate verification rejected after readiness")

    def execute(argv, **_kwargs):
        if update._PINOKIO_RUNTIME_QUALIFICATION in argv or "--input-type=module" in argv:
            return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30).stdout
        return json.dumps(schema)

    backend.run = execute
    backend.stop = stop
    backend.verify = verify
    from astrid.installer import cli

    uv, manifest = tmp_path / "uv", tmp_path / "manifest.json"
    uv.touch()
    write_json(manifest, {"release": 2})
    backend.preflight_stop = lambda *_args: None
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, "validate_manifest", lambda value: value)
    lifecycle = update.PinokioLifecycle(root, backend)
    argv = ["--update-app", "--managed-by", "pinokio", "--root", str(root),
            "--manifest", str(manifest), "--uv", str(uv)]
    try:
        if outcome == "rolled_back":
            assert cli.main(argv) == 1
            assert current(root) == old
            assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
            assert [release for release, _ in observations] == [2, 1]
            running_version = old
        elif outcome in {"recovered", "recovered_running_candidate"}:
            if outcome == "recovered_running_candidate":
                assert cli.main(argv) == 0
            switch(root, candidate)
            record.update(identity=candidate.document()["identity"])
            write_json(root / "update-transaction.json", {
                "format": "astrid-install-update/v1", "operation_id": "a" * 32,
                "transaction_kind": "app", "step": "switched", "data_root": str(data),
                "old": old.document(), "candidate": candidate.document(), "realm": realm, "backup": None,
            })
            backend.stage = lambda *_args: pytest.fail("recovery must not stage a new update")
            manifest.unlink()
            uv.unlink()
            if outcome == "recovered_running_candidate":
                before = (root / "update-transaction.json").read_bytes()
                assert cli.main(argv) == 1
                assert "Astrid is still running. Choose Pinokio's Stop button, then try again." in capsys.readouterr().out.splitlines()
                assert current(root) == candidate
                assert (root / "update-transaction.json").read_bytes() == before
                assert [release for release, _ in observations] == [2]
                running_version = candidate
                lifecycle.stop(candidate, data)
            assert cli.main(argv) == 0
            assert current(root) == old
            assert read_json(root / "update-transaction.json")["step"] == "rolled_back"
            assert [release for release, _ in observations] == ([2, 1] if outcome == "recovered_running_candidate" else [1])
            running_version = old
        else:
            assert cli.main(argv) == 0
            assert current(root) == candidate
            assert read_json(root / "update-transaction.json")["step"] == "complete"
            assert [release for release, _ in observations] == [2]
            running_version = candidate
        assert len({instance for _, instance in observations}) == len(observations)
        lifecycle.stop(running_version, data)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with update.locked(lock_path):
                    break
            except ValueError:
                time.sleep(0.05)
        else:
            pytest.fail("Runtime shutdown did not release Pinokio update custody")
    finally:
        # Ensure detached Node/Runtime children do not survive a failed assert.
        try:
            lifecycle.stop(current(root), data)
        except Exception:  # noqa: BLE001 - fixture cleanup must release its own model Runtime.
            stop(current(root), data)
