"""Installed Astrid wheel can discover and use the bundled Hivemind executors."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[2]


class _HivemindHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, *_args) -> None:
        pass

    def do_GET(self) -> None:
        type(self).requests.append({"method": "GET", "path": self.path,
                                    "apikey": self.headers.get("apikey")})
        body = json.dumps([]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).requests.append({"method": "POST", "path": urlsplit(self.path).path,
                                    "payload": payload,
                                    "contributor": self.headers.get("X-Contributor-Key")})
        body = json.dumps({"ok": True, "accepted": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _run(argv: list[str], *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=cwd, env=env, text=True, capture_output=True, timeout=240)


@pytest.mark.integration
@pytest.mark.timeout(1800)
def test_zero_source_installed_core_discovers_and_invokes_read_write_with_fake_http(tmp_path: Path) -> None:
    assert sys.version_info[:2] == (3, 12), "this integration must use the running Python 3.12"
    source = ROOT
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    base_env = {key: value for key, value in os.environ.items()
                if key in {"PATH", "LANG", "LC_ALL", "SYSTEMROOT", "PIP_INDEX_URL", "PIP_TRUSTED_HOST"}}
    base_env.update({"HOME": str(tmp_path / "build-home"), "TMPDIR": str(tmp_path),
                     "PYTHONNOUSERSITE": "1",
                     "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"})
    Path(base_env["HOME"]).mkdir()

    build = _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                  "--wheel-dir", str(wheel_dir), str(source)], env=base_env, cwd=tmp_path)
    assert build.returncode == 0, f"wheel build failed:\n{build.stdout}\n{build.stderr}"
    wheel = next(wheel_dir.glob("astrid-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_files = set(archive.namelist())
    assert "astrid/core/hivemind/pack.yaml" in wheel_files
    assert "astrid/core/hivemind/docs/SKILL.md" in wheel_files
    assert "astrid/core/hivemind/LICENSE" in wheel_files

    venv_dir = tmp_path / "fresh-venv"
    venv.EnvBuilder(with_pip=True).create(venv_dir)
    python = venv_dir / "bin" / "python"
    install = _run([str(python), "-m", "pip", "install", str(wheel)], env=base_env, cwd=tmp_path)
    assert install.returncode == 0, f"noneditable wheel install failed:\n{install.stdout}\n{install.stderr}"
    runtime = _run([str(python), str(source / "scripts" / "ci_install_runtime.py")], env=base_env, cwd=tmp_path)
    assert runtime.returncode == 0, f"pinned Runtime install failed:\n{runtime.stdout}\n{runtime.stderr}"

    class VenvHandler(_HivemindHandler):
        requests = []

    server = ThreadingHTTPServer(("127.0.0.1", 0), VenvHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        home = tmp_path / "isolated-home"
        data = tmp_path / "isolated-data"
        home.mkdir()
        data.mkdir()
        env = dict(base_env)
        env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                    "XDG_DATA_HOME": str(data), "ASTRID_DATA_ROOT": str(data / "astrid"),
                    "ASTRID_RUNTIME_DATA_ROOT": str(data / "runtime"),
                    "HIVEMIND_API_URL": f"http://127.0.0.1:{server.server_port}",
                    "HIVEMIND_CONTRIBUTE_URL": f"http://127.0.0.1:{server.server_port}/contribute",
                    "HIVEMIND_CONTRIBUTOR_KEY": "hm_" + "a" * 64,
                    "HIVEMIND_ANON_KEY": "local-test-key"})
        probe = r'''
import ipaddress, json, pathlib, runpy, socket, sys
_connect = socket.socket.connect
def _loopback_only(sock, address):
    if not isinstance(address, tuple):
        raise RuntimeError("installed Hivemind test denied non-IP network access")
    try:
        allowed = address[0] == "localhost" or ipaddress.ip_address(address[0]).is_loopback
    except ValueError:
        allowed = False
    if not allowed:
        raise RuntimeError("installed Hivemind test denied non-loopback network access")
    return _connect(sock, address)
socket.socket.connect = _loopback_only
import astrid
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.executor.runner import ExecutorRunRequest, _expand_external_command, _request_values
from astrid.core.foundation.paths import PACKAGE_ROOT

# Discover from the installed package with no configured source roots, then
# resolve each operation from the exact admitted action record.
host = GenericPackHost(pack_roots=[])
assert host.pack_roots == ()
records = {record.id: record for record in host.discover()}
search_record = records["hivemind.search"]
contribute_record = records["hivemind.contribute"]
assert search_record.source_root == PACKAGE_ROOT / "core" / "hivemind"
assert contribute_record.source_root == search_record.source_root

def invoke_discovered(record, inputs, out):
    request = ExecutorRunRequest(executor_id=record.id, out=out, inputs=inputs, python_exec=sys.executable)
    values = _request_values(request, record.definition)
    argv, _cwd, _env = _expand_external_command(record.definition, request, values)
    assert len(argv) >= 2 and argv[1].startswith("actions/"), argv
    action = record.source_root / argv[1]
    assert action.is_file(), action
    # Execute the exact manifest-expanded command declaration in-process. The
    # command remains the source of argv and the wrapper remains the entrypoint.
    sys.argv = [str(action), *argv[2:]]
    try:
        runpy.run_path(str(action), run_name="__main__")
    except SystemExit as exc:
        assert exc.code in (None, 0), f"{record.id} exited with {exc.code}"
    return argv

body = pathlib.Path(sys.argv[1])
print(json.dumps({"origin": astrid.__file__, "packs": ["hivemind"],
                  "capabilities": sorted(key for key in records if key.startswith("hivemind."))}))
search_argv = invoke_discovered(search_record, {"query": "installedtest", "limit": 3}, pathlib.Path("search-out"))
assert "installedtest" in search_argv and "--limit" in search_argv and "3" in search_argv, search_argv
contribute_argv = invoke_discovered(contribute_record, {
    "type": "submit-resource", "kind": "guide", "title": "Installed wheel test",
    "body_file": str(body), "idempotency_token": "installed-wheel-test",
}, pathlib.Path("contribute-out"))
assert "submit-resource" in contribute_argv and "--kind" in contribute_argv and "guide" in contribute_argv, contribute_argv
assert "--title" in contribute_argv and "Installed wheel test" in contribute_argv, contribute_argv
assert "--body-file" in contribute_argv and str(body) in contribute_argv, contribute_argv
assert "--idempotency-token" in contribute_argv and "installed-wheel-test" in contribute_argv, contribute_argv
'''
        body_file = tmp_path / "body.txt"
        body_file.write_text("Local fake service integration payload.\n", encoding="utf-8")
        result = _run([str(python), "-I", "-c", probe, str(body_file)], env=env, cwd=tmp_path)
        assert result.returncode == 0, f"installed Hivemind operations failed:\n{result.stdout}\n{result.stderr}"
        lines = result.stdout.splitlines()
        identity = json.loads(lines[0])
        origin = Path(identity["origin"]).resolve()
        assert venv_dir.resolve() in origin.parents and "site-packages" in origin.parts, (
            f"Astrid imported outside the fresh venv site-packages: {origin}"
        )
        assert "hivemind" in identity["packs"], f"Hivemind missing from discovered packs: {identity['packs']}"
        assert {"hivemind.search", "hivemind.contribute"}.issubset(identity["capabilities"]), identity["capabilities"]
        requests = VenvHandler.requests
        assert any(row["method"] == "GET" and "message_feed" in str(row["path"]) for row in requests), requests
        assert any(row["method"] == "POST" and row["path"] == "/contribute" for row in requests), requests
        write = next(row for row in requests if row["method"] == "POST")
        assert write["contributor"] == "hm_" + "a" * 64
        assert write["payload"]["action"] == "submit_resource"
        assert write["payload"]["data"]["title"] == "Installed wheel test"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
