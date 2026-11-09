"""Runtime's real Update request driven through the real selected Agent helper.

Runtime's ``UpdateAdmission.request`` runs unchanged and calls the Agent
helper in a fresh ``-I`` interpreter from a selected version folder. Only the
prepare URLs are redirected (by wrapping ``_helper``) to a loopback HTTPS
server, and ``systemctl``/``systemd-run`` are fakes on PATH.
"""
from __future__ import annotations

import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer.core import Version, read_json, write_json
from astrid.installer.service import LABEL, install_selection
from astrid.installer.tools import DEFAULT_OMP_SOURCE
from tests.helpers.runtime import initialize_runtime_realm

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="user systemd update jobs are Linux-only")

INSTANCE = "instance-before"
IDENTITY = {"release": 7, "managed_by": None}
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")


_SOURCE_SUFFIXES = (".py", ".json", ".toml")


def _copy_sources(package_dir: Path, destination: Path) -> None:
    """Copy one package's sources and packaged metadata into the selected version."""
    shutil.copytree(package_dir, destination / package_dir.name,
                    ignore=lambda folder, names: [
                        name for name in names
                        if name == "__pycache__"
                        or (not Path(folder, name).is_dir() and not name.endswith(_SOURCE_SUFFIXES))])


@contextmanager
def _serve(handler_factory, *, context=None):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def _static_handler(routes: dict, hits: list):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            hits.append(self.path)
            body = routes.get(self.path)
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass
    return Handler


def _fake_manager(bin_dir: Path, state: Path) -> None:
    """Fake user systemd: version/manager probes, one transient start, state queries."""
    bin_dir.mkdir(parents=True)
    state.mkdir(parents=True)
    (state / "active_state").write_text("active\n")
    (state / "load_state").write_text("loaded\n")
    (state / "run_exit").write_text("0\n")
    common = (f"#!{sys.executable} -I\n"
              "import json, sys\n"
              f"state = {str(state)!r}\n"
              "def read(name):\n"
              "    return open(state + '/' + name).read()\n"
              "with open(state + '/calls.jsonl', 'a') as stream:\n"
              "    stream.write(json.dumps([sys.argv[0].rsplit('/', 1)[-1], *sys.argv[1:]]) + '\\n')\n"
              "args = sys.argv[1:]\n")
    systemctl = common + (
        "if args == ['--user', '--version']:\n"
        "    print('systemd 256 (256.7)'); sys.exit(0)\n"
        "if args == ['--user', 'show-environment']:\n"
        "    print('HOME=/nonexistent'); sys.exit(0)\n"
        "if args[:2] == ['--user', 'show'] and '--property=ActiveState' in args:\n"
        "    value = read('active_state').strip()\n"
        "    if value == 'query-error':\n"
        "        print('Failed to connect to bus', file=sys.stderr); sys.exit(1)\n"
        "    print(value); sys.exit(0)\n"
        "if args[:2] == ['--user', 'show'] and '--property=LoadState' in args:\n"
        "    print(read('load_state').strip()); sys.exit(0)\n"
        "if args[:2] == ['--user', 'reset-failed']:\n"
        "    sys.exit(0)\n"
        "print('unexpected systemctl call', args, file=sys.stderr); sys.exit(2)\n")
    systemd_run = common + (
        "code = int(read('run_exit').strip())\n"
        "if code:\n"
        "    print('Failed to start transient service unit: Connection timed out', file=sys.stderr)\n"
        "sys.exit(code)\n")
    for name, text in (("systemctl", systemctl), ("systemd-run", systemd_run)):
        path = bin_dir / name
        path.write_text(text)
        path.chmod(0o755)


def _calls(state: Path) -> list[list[str]]:
    path = state / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def world(tmp_path, monkeypatch):
    import astrid_runtime
    import runtime_protocol
    from astrid_runtime import update
    from astrid_runtime.runtime_boundary import WIRE_PROTOCOL

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    root, data = tmp_path / "program", tmp_path / "data"
    folder = root / "versions" / "old-7"
    agent, runtime = folder / "agent", folder / "runtime"
    agent.mkdir(parents=True)
    runtime.mkdir()
    # The selected checkouts are real copies; the helper interpreter runs -I.
    checkout = Path(__file__).resolve().parents[1]
    _copy_sources(checkout / "astrid", agent)
    shutil.copy2(checkout / "pyproject.toml", agent / "pyproject.toml")
    _copy_sources(Path(runtime_protocol.__file__).resolve().parent, runtime)
    _copy_sources(Path(astrid_runtime.__file__).resolve().parent, runtime)
    python = folder / "bin" / "python"
    python.parent.mkdir()
    python.symlink_to(sys.executable)
    profile = folder / "profile.json"
    write_json(profile, {"profile": "astrid", "source_checkout": str(agent),
                         "runtime_checkout": str(runtime)})
    old = Version(folder, python, profile, {}, release=7)
    write_json(folder / "version.json", old.document())
    write_json(folder / "manifest.json", {"release": 7})
    (root / "current").write_text("old-7\n")
    realm = data / "runtime" / "realms" / "realm-1"
    initialize_runtime_realm(realm)
    selection = install_selection(home, sys.platform)
    assert selection == update._install_record_path(home)
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data),
                           "realm_id": "realm-1", "realm_root": str(realm), "identity": IDENTITY})
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})

    # Live owner health for the helper's identity probe.
    health = json.dumps({"protocol": WIRE_PROTOCOL, "status": "ok",
                         "runtime_instance_id": INSTANCE, "identity": IDENTITY}).encode()
    health_hits: list = []

    # Update inputs, served over verified HTTPS from loopback.
    manifest = json.dumps({
        "schema_version": 1, "release": 8, "agent": {"sha": "a" * 40},
        "runtime": {"sha": "b" * 40},
        "tools": {**{name: {"version": "20.19.4" if name == "node" else "1.2.3"}
                    for name in ("uv", "python", "bun", "node", "ffmpeg")},
                  "omp": {"source": DEFAULT_OMP_SOURCE}},
    }).encode()
    bootstrap = b"#!/bin/sh\nexit 0\n"
    input_hits: list = []
    key, cert = tmp_path / "tls.key", tmp_path / "tls.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(key), "-out", str(cert)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))

    bin_dir, state = tmp_path / "fake-bin", tmp_path / "fake-systemd"
    _fake_manager(bin_dir, state)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))

    with _serve(_static_handler({"/v1/health": health}, health_hits)) as owner, \
            _serve(_static_handler({"/install-manifest.json": manifest, "/install": bootstrap},
                                   input_hits), context=context) as inputs:
        write_json(data / "runtime" / "discovery.json", {
            "runtime_instance_id": INSTANCE,
            "endpoint": f"http://127.0.0.1:{owner.server_address[1]}"})
        daemon = SimpleNamespace(
            startup_identity=dict(IDENTITY), startup_identity_instance_id=INSTANCE,
            instance_id=INSTANCE, root=realm, support_root=data / "runtime",
            service=SimpleNamespace(realm={"id": "realm-1"}),
            _identity_agent_checkout=str(agent), _identity_runtime_checkout=str(runtime),
            update_launcher=None)
        admission = update.UpdateAdmission(daemon)
        base = f"https://127.0.0.1:{inputs.server_address[1]}"
        helper_calls: list = []
        real_helper = admission._helper

        def helper(selected_python, selected_agent, selected_runtime, payload):
            # The only test seam: prepare's fixed public URLs point at loopback.
            assert (selected_python, selected_agent, selected_runtime) == (python, agent, runtime)
            if "manifest_url" in payload:
                assert payload["manifest_url"] == update.MANIFEST_URL
                assert payload["bootstrap_url"] == update.BOOTSTRAP_URL
                payload = {**payload, "manifest_url": base + "/install-manifest.json",
                           "bootstrap_url": base + "/install"}
            helper_calls.append(payload)
            return real_helper(selected_python, selected_agent, selected_runtime, payload)

        monkeypatch.setattr(admission, "_helper", helper)
        yield SimpleNamespace(admission=admission, update=update, root=root, data=data, home=home,
                              state=state, helper_calls=helper_calls, input_hits=input_hits,
                              health_hits=health_hits)


def _job(world) -> dict:
    return read_json(world.root / "install.json")["update_job"]


def _status(world) -> dict:
    return world.update.read_update_status(world.home, available=True)


def _starts(world) -> list[list[str]]:
    return [call for call in _calls(world.state) if call[0] == "systemd-run"]


# Every launch_status the Agent writes; Runtime keys its reply on these values.
AGENT_LAUNCH_STATUSES = {"launching", "acknowledged", "unknown", "failed", "refused"}


def test_update_request_prepares_accepts_and_launches_through_the_real_helper(world):
    result = world.admission.request()

    assert result == {"accepted": True, "state": "accepted", "operation_id": result["operation_id"],
                      "message": "Update accepted."}
    operation_id = result["operation_id"]
    # Reconcile, the implicit prepare (no action key), accept, then launch.
    assert [call.get("action") for call in world.helper_calls] == [
        "reconcile", None, "accept", "launch"]
    assert "action" not in world.helper_calls[1]
    assert world.input_hits == ["/install-manifest.json", "/install"]
    assert world.health_hits == ["/v1/health"]
    job = _job(world)
    assert job["operation_id"] == operation_id and job["target_release"] == 8
    assert job["metadata"]["manifest_path"] == world.helper_calls[2]["metadata"]["manifest_path"]
    # The Agent acknowledges a started job as "acknowledged", never "started".
    assert job["state"] == "launching" and job["launch_status"] == "acknowledged"
    assert job["launch_status"] in AGENT_LAUNCH_STATUSES
    assert job["attempt"]["submit"] == "acknowledged"
    starts = _starts(world)
    assert len(starts) == 1
    assert f"--unit=astrid-update-{operation_id}.service" in starts[0]
    assert starts[0][starts[0].index("--") + 1:] == job["platform"]["command"]
    status = _status(world)
    assert status["state"] == "active" and status["operation_id"] == operation_id
    assert status["launch_status"] == "acknowledged"


def test_lost_launch_acknowledgement_is_recovery_needed_then_continues_once(world):
    from runtime_protocol.errors import ConflictError

    # systemd-run fails and the unit state cannot be queried: OS acceptance is unknown.
    (world.state / "run_exit").write_text("1\n")
    (world.state / "active_state").write_text("query-error\n")

    with pytest.raises(ConflictError) as refused:
        world.admission.request()

    assert refused.value.details["reason"] == "launch-acknowledgement-unknown"
    assert [call.get("action") for call in world.helper_calls] == [
        "reconcile", None, "accept", "launch"]
    job = _job(world)
    operation_id = job["operation_id"]
    assert job["state"] == "launching" and job["launch_status"] == "unknown"
    assert job["attempt"] == {**job["attempt"], "epoch": 1, "submit": "unknown"}
    status = _status(world)
    assert status["state"] == "recovery-needed" and status["operation_id"] == operation_id
    assert len(_starts(world)) == 1

    # The next click reaches the Agent's locked reconciler, which proves the
    # unit absent and makes the sole continuation submission itself.
    (world.state / "run_exit").write_text("0\n")
    (world.state / "active_state").write_text("inactive\n")
    (world.state / "load_state").write_text("not-found\n")
    world.helper_calls.clear()

    result = world.admission.request()

    assert result == {"accepted": True, "state": "accepted", "operation_id": operation_id,
                      "message": "Update continued."}
    assert [call.get("action") for call in world.helper_calls] == ["reconcile"]
    assert world.input_hits == ["/install-manifest.json", "/install"]
    job = _job(world)
    assert job["operation_id"] == operation_id
    assert job["state"] == "launching" and job["launch_status"] == "acknowledged"
    assert job["attempt"]["epoch"] == 2 and job["attempt"]["submit"] == "acknowledged"
    starts = _starts(world)
    assert len(starts) == 2
    assert starts[1][starts[1].index("--") + 1:] == job["platform"]["command"]
    assert _status(world)["state"] == "active"


def test_refused_launch_is_reported_with_its_reason_not_as_an_os_rejection(world, monkeypatch):
    from runtime_protocol.errors import ConflictError

    selection = install_selection(world.home, sys.platform)
    helper = world.admission._helper

    def autostart_turned_off_before_launch(python, agent, runtime, payload):
        if payload.get("action") == "launch":
            record = read_json(selection)
            record["choices"] = {"autostart": False}  # the saved choice the update guard reads (see e8f7021)
            write_json(selection, record)
        return helper(python, agent, runtime, payload)

    monkeypatch.setattr(world.admission, "_helper", autostart_turned_off_before_launch)

    with pytest.raises(ConflictError) as refused:
        world.admission.request()

    assert refused.value.details["reason"] == "launch-failed"
    assert "autostart is off" in str(refused.value)
    assert "operating system" not in str(refused.value)
    job = _job(world)
    assert job["state"] == "failed" and job["launch_status"] == "refused"
    assert _starts(world) == []
    assert _status(world)["outcome"] == "failed"


def test_unfinished_installer_journal_refuses_before_any_preparation(world):
    from runtime_protocol.errors import ConflictError

    write_json(world.root / "update-transaction.json", {
        "format": "astrid-install-update/v1", "operation_id": "c" * 32, "step": "stopping"})

    with pytest.raises(ConflictError) as refused:
        world.admission.request()

    assert refused.value.details["reason"] == "busy"
    assert "recover its pending update" in str(refused.value)
    assert [call.get("action") for call in world.helper_calls] == ["reconcile"]
    assert world.input_hits == [] and _starts(world) == []
    assert "update_job" not in read_json(world.root / "install.json")
    assert _status(world)["state"] == "recovery-needed"
