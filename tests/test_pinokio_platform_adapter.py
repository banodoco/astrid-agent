"""Disposable local checks for the Pinokio platform command adapter.

These prove adapter behavior only. Hosted lifecycle evidence still requires
the authentic macos-14, ubuntu-24.04, and windows-2022 runners.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pinokio_backend_executor
from scripts import pinokio_platform_adapter as adapter


def _port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    value = sock.getsockname()[1]
    sock.close()
    return value


def _spec(tmp_path: Path, *, port: int | None = None) -> dict:
    selected_port = port or _port()
    script = f"""import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
 def do_GET(self):
  identity=os.environ['ASTRID_RUNTIME_IDENTITY']
  instance=os.environ['ASTRID_RUNTIME_INSTANCE_ID']
  identity='not-the-candidate' if self.path=='/negative/identity' else identity
  instance='not-a-configured-instance' if self.path=='/negative/instance' else instance
  payload={{'identity':identity,'runtime_instance_id':instance,'pid':os.getpid()}}
  payload.update({{'/status':{{'support':{{'healthy':True}}}}, '/health':{{'status':'ok','protocol':'workspace.v1','install':{{'managed_by':'pinokio'}}}}, '/menu':{{'open_astrid':True}}}}.get(self.path,{{}}))
  if self.path.startswith('/negative/'):
   payload.update({{'ready':False,'ready_sentinel':False,'local_set':False,'open_menu':False}})
  overrides=json.loads(os.environ.get('ASTRID_ENDPOINT_OVERRIDES','{{}}'))
  payload.update(overrides.get(self.path,{{}}))
  for key in overrides.get('__remove__',{{}}).get(self.path,[]): payload.pop(key,None)
  b=json.dumps(payload).encode()
  self.send_response(200)
  self.send_header('Content-Type','application/json')
  self.send_header('Content-Length',str(len(b)))
  self.end_headers()
  self.wfile.write(b)
 def log_message(self,*a): pass
HTTPServer.allow_reuse_address = True
HTTPServer(('127.0.0.1',{selected_port}),H).serve_forever()
"""
    return {
        "identity": "candidate-agent-identity",
        "instance_ids": ["instance-one", "instance-two"],
        "runtime_argv": [str(Path(sys.executable).resolve()), "-u", "-c", script],
        "runtime_executable_path": adapter._observed_executable(
            os.getpid(), adapter.PLATFORM_NAMES[sys.platform]
        ),
        "runtime_cwd": str(tmp_path),
        "state_path": str(tmp_path / "adapter-state.json"),
        "host": "127.0.0.1",
        "port": selected_port,
        "health_path": "/health",
        "status_path": "/status",
        "menu_path": "/menu",
        "negative_control_paths": {
            "identity_mismatch": "/negative/identity",
            "instance_mismatch": "/negative/instance",
        },
        "runtime_env": {},
        "stop_controls": {
            reason: {
                "argv": [
                    str(Path(sys.executable).resolve()),
                    "-c",
                    f"import sys; print('{reason}'); sys.exit(7)",
                ],
                "cwd": str(tmp_path),
                "env": {},
                "expected_exit_code": 7,
                "reason": reason,
                "stage": "local-test-stage",
            }
            for reason in ("busy", "unknown")
        },
        "platform": {
            "os": adapter.PLATFORM_NAMES[sys.platform],
            "runner": "local-test",
            "hosted": False,
        },
        "mismatch_identity": "not-the-candidate",
        "mismatch_instance_id": "not-a-configured-instance",
        "start_timeout_seconds": 5,
    }


def _operation(spec: dict, name: str) -> tuple[dict, int]:
    return adapter.run_operation(name, spec)


@pytest.mark.parametrize(
    "os_name,runner,shell",
    [
        ("macos", "macos-14", "sh"),
        ("linux", "ubuntu-24.04", "sh"),
        ("windows", "windows-2022", "powershell.exe"),
    ],
)
def test_command_builder_is_explicit_and_executor_compatible(tmp_path, os_name, runner, shell):
    # Structural matrix only: no foreign OS execution is claimed here.
    spec_path = tmp_path / "adapter.json"
    spec_path.write_text(
        json.dumps(
            {
                "identity": "candidate",
                "instance_ids": ["first", "second"],
                "platform": {"os": os_name, "runner": runner, "hosted": False},
                "guard_paths": [str(tmp_path / "guard.txt")],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "guard.txt").write_text("guard", encoding="utf-8")
    result = adapter.build_executor_commands(spec_path, cwd=tmp_path)
    runtime = {
        name: result["runtime"][name]
        for name in ("ready", "status", "health", "menu", "processes", "stop", "start")
    }
    assert set(runtime) == {"ready", "status", "health", "menu", "processes", "stop", "start"}
    assert set(result["negative_controls"]) == {
        "busy_stop",
        "unknown_stop",
        "identity_mismatch",
        "instance_mismatch",
    }
    for command in [
        *runtime.values(),
        result["negative_controls"]["busy_stop"]["refusal"],
        result["negative_controls"]["unknown_stop"]["refusal"],
        result["negative_controls"]["identity_mismatch"]["probe"],
        result["negative_controls"]["instance_mismatch"]["probe"],
    ]:
        assert command["argv"][0] == sys.executable
        assert command["argv"][1].endswith("pinokio_platform_adapter.py")
        assert "--spec" in command["argv"]
        assert command["cwd"] == str(tmp_path.resolve())
        assert command["id"] and command["timeout_seconds"] > 0
        assert set(adapter.RUNNER_IDENTITY_ENV).issubset(command["inherit_env"])
        assert {"PATH", "SYSTEMROOT", "HOME", "USERPROFILE"}.issubset(command["inherit_env"])
        assert (
            "shell" not in command
        )  # backend executor always launches these argv arrays with shell=False
    assert result["runtime"]["expected_identity"] == "candidate"
    assert result["runtime"]["expected_instance_id"] == "first"
    assert result["runtime"]["expected_restart_instance_id"] == "second"
    assert result["negative_controls"]["busy_stop"]["expected_reason"] == "busy"
    assert result["negative_controls"]["unknown_stop"]["expected_reason"] == "unknown"
    assert str(spec_path) in result["negative_controls"]["busy_stop"]["guard_paths"]
    assert str(tmp_path / "guard.txt") in result["negative_controls"]["busy_stop"]["guard_paths"]
    assert shell in {"sh", "powershell.exe"}  # documents the declared matrix shell
    pinokio_backend_executor._validate_probe_set(result["runtime"])
    for name, control in result["negative_controls"].items():
        if name in {"busy_stop", "unknown_stop"}:
            pinokio_backend_executor._validate_command(control["refusal"], f"{name}.refusal")
            pinokio_backend_executor._validate_command(control["recovery"], f"{name}.recovery")
        else:
            pinokio_backend_executor._validate_command(control["probe"], f"{name}.probe")


def test_command_builder_rejects_runner_identity_env_overrides(tmp_path):
    spec_path = tmp_path / "adapter.json"
    spec_path.write_text(
        json.dumps(
            {
                "identity": "candidate",
                "instance_ids": ["first", "second"],
                "platform": {
                    "os": adapter.PLATFORM_NAMES[sys.platform],
                    "runner": "local",
                    "hosted": False,
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(adapter.AdapterError, match="must be inherited"):
        adapter.build_executor_commands(spec_path, env={"RUNNER_OS": "forged"})


def test_runner_identity_requires_authentic_platform_and_hosted_provenance():
    expected_os = adapter.PLATFORM_NAMES[sys.platform]
    row = adapter.HOSTED_RUNNERS[expected_os]
    platform = {"os": expected_os, "runner": row[0], "hosted": True}
    identity_kwargs = {}
    if expected_os == "windows":
        platform["windows_powershell"] = {"required_version_prefix": "5.1"}
        identity_kwargs["powershell_version"] = "5.1.20348"
    env = {"RUNNER_OS": row[1], "ImageOS": row[2], "ImageVersion": "2026.10.31"}
    provenance = adapter.validate_runner_identity(
        platform, observed_platform=sys.platform, env=env, **identity_kwargs
    )["runner_provenance"]
    assert all(provenance[key] == env[key] for key in env)
    assert provenance["observed_platform"] == sys.platform
    with pytest.raises(adapter.AdapterError, match="does not match observed"):
        adapter.validate_runner_identity(
            platform, observed_platform="win32" if sys.platform != "win32" else "linux", env=env
        )
    for bad_env in ({}, {**env, "ImageVersion": " "}, {**env, "ImageOS": "wrong"}):
        with pytest.raises(adapter.AdapterError, match="provenance"):
            adapter.validate_runner_identity(
                platform, observed_platform=sys.platform, env=bad_env, **identity_kwargs
            )
    with pytest.raises(adapter.AdapterError, match="requires runner"):
        adapter.validate_runner_identity(
            {**platform, "runner": "local"},
            observed_platform=sys.platform,
            env=env,
            **identity_kwargs,
        )


def test_windows_powershell_requirement_is_explicit_and_fail_closed():
    with pytest.raises(adapter.AdapterError, match="PowerShell version"):
        adapter.validate_runner_identity(
            {
                "os": "windows",
                "runner": "windows-2022",
                "hosted": True,
                "windows_powershell": {"required_version_prefix": "5.1"},
            },
            observed_platform="win32",
            env={"RUNNER_OS": "Windows", "ImageOS": "win22", "ImageVersion": "2026"},
            powershell_version="7.4.1",
        )
    with pytest.raises(adapter.AdapterError, match="expressible"):
        adapter.validate_runner_identity(
            {"os": "windows", "runner": "windows-2022", "hosted": True},
            observed_platform="win32",
            env={"RUNNER_OS": "Windows", "ImageOS": "win22", "ImageVersion": "2026"},
        )
    got = adapter.validate_runner_identity(
        {
            "os": "windows",
            "runner": "windows-2022",
            "hosted": True,
            "windows_powershell": {"required_version_prefix": "5.1"},
        },
        observed_platform="win32",
        env={"RUNNER_OS": "Windows", "ImageOS": "win22", "ImageVersion": "2026"},
        powershell_version="5.1.20348",
    )
    assert got["powershell_version"] == "5.1.20348"
    with pytest.raises(adapter.AdapterError, match="Windows PowerShell 5.1"):
        adapter.validate_runner_identity(
            {
                "os": "windows",
                "runner": "windows-2022",
                "hosted": True,
                "windows_powershell": {"required_version_prefix": "5.", "executable": "pwsh.exe"},
            },
            observed_platform="win32",
            env={"RUNNER_OS": "Windows", "ImageOS": "win22", "ImageVersion": "2026"},
            powershell_version="5.1.20348",
        )


def test_real_local_start_http_readiness_snapshot_stop_and_port_free(tmp_path):
    spec = _spec(tmp_path)
    started, code = _operation(spec, "start")
    assert code == 0 and started["result"] == "ready"
    assert started["identity"] == spec["identity"]
    assert started["runtime_instance_id"] == "instance-one"
    state_path = Path(spec["state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["active"] is True
    assert state["argv_digest"] and state["controller_token_digest"]
    assert state["executable_path"] == spec["runtime_executable_path"]
    assert "ASTRID_CONTROLLER_TOKEN" not in state
    assert state["evidence_class"] == "executable-boundary"
    assert state["platform_identity"]["runner"] == "local-test"
    snapshot, code = _operation(spec, "processes")
    assert code == 0 and snapshot["running"] is True
    assert snapshot["owned_processes"] == [state["pid"]]
    assert snapshot["owned_ports_free"] is False
    assert snapshot["controller_token"] == state["controller_token_digest"]
    assert snapshot["controller_token_representation"] == "sha256"
    sanitized = pinokio_backend_executor._sanitize(snapshot)
    stable = pinokio_backend_executor._stable_identity(sanitized)
    assert stable["controller_token_digest"]
    stopped, code = _operation(spec, "stop")
    assert code == 0 and stopped["result"] == "stopped"
    snapshot, code = _operation(spec, "processes")
    assert code == 0 and snapshot["running"] is False
    assert snapshot["owned_processes"] == [] and snapshot["owned_ports_free"] is True
    assert not adapter._pid_alive(state["pid"])


def test_restart_launch_observation_failure_cleans_new_process_not_inactive_state(
    tmp_path, monkeypatch
):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    _operation(spec, "stop")
    monkeypatch.setattr(
        adapter,
        "_birth",
        lambda *_args: (_ for _ in ()).throw(adapter.AdapterError("birth unavailable")),
    )
    with pytest.raises(adapter.AdapterError, match="birth unavailable"):
        _operation(spec, "start")
    monkeypatch.undo()
    snapshot, code = _operation(spec, "processes")
    assert code == 0 and snapshot["running"] is False
    assert snapshot["owned_ports_free"] is True


def test_real_external_process_can_be_adopted_without_substitute_launch(tmp_path):
    spec = _spec(tmp_path)
    spec["preexisting_session"] = {
        "identity": "release-zero-identity",
        "instance_id": "release-zero-instance",
    }
    env = dict(os.environ)
    env.update(
        {
            "ASTRID_RUNTIME_IDENTITY": "release-zero-identity",
            "ASTRID_RUNTIME_INSTANCE_ID": "release-zero-instance",
            "ASTRID_CONTROLLER_TOKEN": "raw-token-never-given-to-adapter",
        }
    )
    if sys.platform == "win32":
        launch = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        }
    else:
        launch = {"start_new_session": True}
    process = subprocess.Popen(
        spec["runtime_argv"],
        cwd=spec["runtime_cwd"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        **launch,
    )
    digest = hashlib.sha256(b"raw-token-never-given-to-adapter").hexdigest()
    try:
        deadline = adapter.time.monotonic() + 5
        while adapter.time.monotonic() < deadline and adapter._port_free(
            spec["host"], spec["port"]
        ):
            adapter.time.sleep(0.05)
        adopted = adapter.adopt_process(
            spec,
            pid=process.pid,
            identity="release-zero-identity",
            instance_id="release-zero-instance",
            controller_token_digest=digest,
        )
        assert adopted["result"] == "adopted" and adopted["pid"] == process.pid
        assert adopted["controller_token_representation"] == "sha256"
        assert "raw-token-never-given-to-adapter" not in json.dumps(adopted)
        snapshot, _ = _operation(spec, "processes")
        assert (
            snapshot["running"] is True
            and snapshot["runtime_instance_id"] == "release-zero-instance"
        )
        _operation(spec, "stop")
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, adapter.signal.SIGKILL)
            process.wait(timeout=5)


def test_adoption_rejects_unknown_session_and_raw_token(tmp_path):
    spec = _spec(tmp_path)
    with pytest.raises(adapter.AdapterError, match="not an allowed session"):
        adapter.adopt_process(
            spec,
            pid=99999,
            identity="unknown",
            instance_id="unknown",
            controller_token_digest="a" * 64,
        )
    with pytest.raises(adapter.AdapterError, match="SHA-256"):
        adapter.adopt_process(
            spec,
            pid=99999,
            identity=spec["identity"],
            instance_id=spec["instance_ids"][0],
            controller_token_digest="raw-token",
        )


@pytest.mark.parametrize("name,reason", [("busy-refusal", "busy"), ("unknown-refusal", "unknown")])
def test_refusal_controls_preserve_same_session_then_recovery_stops_it(tmp_path, name, reason):
    spec = _spec(tmp_path)
    spec["stop_controls"][reason]["argv"] = [
        str(Path(sys.executable).resolve()),
        "-c",
        f"import sys; print('{reason} private-output-secret'); sys.exit(7)",
    ]
    _operation(spec, "start")
    path = Path(spec["state_path"])
    before_bytes = path.read_bytes()
    before = json.loads(before_bytes)
    refusal, code = _operation(spec, name)
    assert code == 7 and refusal["outcome"] == "refused" and refusal["reason"] == reason
    assert refusal["stage"] == "local-test-stage"
    assert refusal["expected_exit_code"] == refusal["observed_exit_code"] == 7
    assert refusal["command_digest"] and refusal["cwd"] == str(tmp_path)
    assert "private-output-secret" not in json.dumps(refusal)
    assert path.read_bytes() == before_bytes
    after = json.loads(path.read_bytes())
    assert (
        after["pid"],
        after["birth"],
        after["runtime_instance_id"],
        after["controller_token_digest"],
    ) == (
        before["pid"],
        before["birth"],
        before["runtime_instance_id"],
        before["controller_token_digest"],
    )
    recovered, code = _operation(spec, "recovery")
    assert code == 0 and recovered["same_session"] is True
    assert recovered["session"]["pid"] == before["pid"]
    assert _operation(spec, "processes")[0]["owned_ports_free"] is True
    restarted, code = _operation(spec, "start")
    assert code == 0 and restarted["runtime_instance_id"] == "instance-two"
    _operation(spec, "stop")


def test_identity_and_instance_mismatch_suppress_all_readiness_surfaces(tmp_path):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    for operation in ("identity-mismatch", "instance-mismatch"):
        result, code = _operation(spec, operation)
        assert code == 0
        assert result["ready"] is False
        if operation == "identity-mismatch":
            assert result["identity"] != spec["identity"]
            assert result["runtime_instance_id"] == "instance-one"
        else:
            assert result["identity"] == spec["identity"]
            assert result["runtime_instance_id"] == spec["mismatch_instance_id"]
        assert result["ready_sentinel"] is False
        assert result["local_set"] is False
        assert result["open_menu"] is False
    _operation(spec, "stop")


def test_mismatch_suppression_must_be_observed_not_assumed(tmp_path):
    spec = _spec(tmp_path)
    spec["runtime_env"]["ASTRID_ENDPOINT_OVERRIDES"] = json.dumps(
        {"/negative/identity": {"local_set": True}}
    )
    _operation(spec, "start")
    with pytest.raises(adapter.AdapterError, match="suppression"):
        _operation(spec, "identity-mismatch")
    _operation(spec, "stop")


@pytest.mark.parametrize(
    "path,override",
    [
        ("/status", {"support": {"healthy": False}}),
        ("/health", {"protocol": "wrong"}),
        ("/health", {"install": {"managed_by": "other"}}),
        ("/menu", {"open_astrid": False}),
        ("/menu", {"identity": "foreign"}),
        ("/status", {"__remove__": ["support"]}),
        ("/health", {"__remove__": ["protocol"]}),
        ("/health", {"__remove__": ["install"]}),
        ("/menu", {"__remove__": ["open_astrid"]}),
    ],
)
def test_endpoint_operations_require_real_observed_fields(tmp_path, path, override):
    spec = _spec(tmp_path)
    if "__remove__" in override:
        spec["runtime_env"]["ASTRID_ENDPOINT_OVERRIDES"] = json.dumps(
            {"__remove__": {path: override["__remove__"]}}
        )
    else:
        spec["runtime_env"]["ASTRID_ENDPOINT_OVERRIDES"] = json.dumps({path: override})
    _operation(spec, "start")
    operation = {"/status": "status", "/health": "health", "/menu": "menu"}[path]
    with pytest.raises(adapter.AdapterError):
        _operation(spec, operation)
    _operation(spec, "stop")


def test_status_health_menu_return_observed_endpoint_payloads(tmp_path):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    status, _ = _operation(spec, "status")
    health, _ = _operation(spec, "health")
    menu, _ = _operation(spec, "menu")
    assert status["support"] == {"healthy": True} and status["pid"]
    assert health["protocol"] == "workspace.v1" and health["install"]["managed_by"] == "pinokio"
    assert menu["open_astrid"] is True and menu["pid"]
    _operation(spec, "stop")


def test_local_runner_label_is_bound_to_persisted_session(tmp_path):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    original = spec["platform"]["runner"]
    spec["platform"]["runner"] = "changed-local-label"
    with pytest.raises(adapter.AdapterError, match="does not authenticate"):
        _operation(spec, "processes")
    spec["platform"]["runner"] = original
    _operation(spec, "stop")


def test_stop_requires_active_owned_session_even_when_port_is_free(tmp_path):
    spec = _spec(tmp_path)
    with pytest.raises(adapter.AdapterError, match="requires an active recorded"):
        _operation(spec, "stop")


def test_configured_refusal_requires_nonzero_code_and_declared_reason(tmp_path):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    state_bytes = Path(spec["state_path"]).read_bytes()
    spec["stop_controls"]["busy"]["argv"] = [
        str(Path(sys.executable).resolve()),
        "-c",
        "print('wrong-reason')",
    ]
    with pytest.raises(adapter.AdapterError, match="nonzero code and reason"):
        _operation(spec, "busy-refusal")
    assert Path(spec["state_path"]).read_bytes() == state_bytes
    _operation(spec, "stop")


def test_configured_refusal_reason_cannot_be_relabelled(tmp_path):
    spec = _spec(tmp_path)
    spec["stop_controls"]["busy"]["reason"] = "unknown"
    with pytest.raises(adapter.AdapterError, match="must be exactly"):
        adapter._validate_spec(spec)


def test_runtime_executable_must_be_regular_absolute_and_not_symlink(tmp_path):
    spec = _spec(tmp_path)
    spec["runtime_argv"][0] = "python"
    with pytest.raises(adapter.AdapterError, match="absolute regular"):
        adapter._validate_spec(spec)


def test_macos_process_info_uses_136_byte_abi_and_uint64_birth_offsets():
    raw = bytearray(136)
    struct.pack_into("=I", raw, 12, 1234)
    struct.pack_into("=I", raw, 100, 1200)
    struct.pack_into("=Q", raw, 120, 0x1_0000_0001)
    struct.pack_into("=Q", raw, 128, 999999)
    assert adapter._parse_mac_process_info(bytes(raw), 1234) == (1234, 1200, 0x1_0000_0001, 999999)
    with pytest.raises(adapter.AdapterError, match="too small"):
        adapter._parse_mac_process_info(bytes(raw[:128]), 1234)


def test_linux_proc_stat_parser_observes_start_time_and_process_group():
    fields = ["S", "1", "456", *("0" for _ in range(16)), "987654"]
    raw = f"123 (worker (with spaces)) {' '.join(fields)}"
    assert adapter._parse_linux_process_info(raw) == ("987654", 456)
    with pytest.raises(adapter.AdapterError, match="malformed"):
        adapter._parse_linux_process_info("123 no-closing-parenthesis")


def test_linux_proc_net_parser_requires_listen_state_and_exact_port():
    raw = (
        "sl local_address rem_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
    )
    raw += "0: 0100007F:43BC 00000000:0000 0A 0:0 00:0 0 1000 0 12345\n"
    raw += "1: 0100007F:43BD 00000000:0000 01 0:0 00:0 0 1000 0 99999\n"
    assert adapter._parse_linux_listening_inodes(raw, 17340) == {"12345"}


def test_windows_listener_owner_uses_native_owning_process(monkeypatch):
    captured = {}

    def observe(argv, *, timeout):
        captured.update(argv=argv, timeout=timeout)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "OwningProcess": 4321,
                    "LocalAddress": "127.0.0.1",
                    "LocalPort": 17340,
                    "State": "Listen",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(adapter, "_run_observation", observe)
    assert adapter._listener_owned_by(4321, "127.0.0.1", 17340, "windows") is True
    assert captured["argv"][:4] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]
    assert "OwningProcess -eq 4321" in captured["argv"][-1]


def test_listener_owner_mismatch_fails_readiness(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    monkeypatch.setattr(adapter, "_listener_owned_by", lambda *_args: False)
    with pytest.raises(adapter.AdapterError, match="listener owner"):
        _operation(spec, "ready")
    monkeypatch.undo()
    _operation(spec, "stop")


def test_windows_birth_uses_explicit_cim_powershell_argv(monkeypatch):
    captured = {}

    def observe(argv, *, timeout):
        captured.update(argv=argv, timeout=timeout)
        return SimpleNamespace(returncode=0, stdout="2026-10-31T01:02:03.0000000Z\n", stderr="")

    monkeypatch.setattr(adapter, "_run_observation", observe)
    assert adapter._birth(4321, "windows") == "2026-10-31T01:02:03.0000000Z"
    assert captured["argv"][:4] == ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive"]
    assert "ProcessId = 4321" in captured["argv"][-1]
    assert captured["timeout"] == 15


def test_windows_executable_observation_uses_cim_and_normalizes_path(monkeypatch):
    captured = {}

    def observe(argv, *, timeout):
        captured.update(argv=argv, timeout=timeout)
        return SimpleNamespace(returncode=0, stdout=r"C:\Python\python.exe" + "\n", stderr="")

    monkeypatch.setattr(adapter, "_run_observation", observe)
    observed = adapter._observed_executable(4321, "windows")
    assert observed == adapter.os.path.normcase(adapter.os.path.realpath(r"C:\Python\python.exe"))
    assert "ExecutablePath" in captured["argv"][-1]


@pytest.mark.parametrize("os_name", ["linux", "macos"])
def test_posix_stop_targets_authenticated_process_group(monkeypatch, tmp_path, os_name):
    state = {
        "active": True,
        "pid": 2222,
        "pgid": 2222,
        "birth": "birth",
        "host": "127.0.0.1",
        "port": 17340,
        "runtime_instance_id": "instance",
    }
    calls = []
    written = {}
    monkeypatch.setattr(adapter, "_process_matches", lambda *_args: True)
    monkeypatch.setattr(adapter, "_birth", lambda *_args: "birth")
    monkeypatch.setattr(adapter, "_pid_alive", lambda *_args: False)
    monkeypatch.setattr(
        adapter.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)), raising=False
    )
    monkeypatch.setattr(adapter, "_wait_port_free", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(adapter, "_write_state", lambda path, value: written.update(value))
    result = adapter._stop(
        {"platform": {"os": os_name}, "state_path": tmp_path / "state.json"}, state
    )
    assert result == {"result": "stopped", "runtime_instance_id": "instance"}
    assert calls == [(2222, adapter.signal.SIGTERM)]
    assert written["active"] is False


def test_windows_stop_uses_native_tree_kill_after_birth_recheck(monkeypatch, tmp_path):
    state = {
        "active": True,
        "pid": 3333,
        "pgid": None,
        "birth": "birth",
        "host": "127.0.0.1",
        "port": 17340,
        "runtime_instance_id": "instance",
    }
    captured = {}
    monkeypatch.setattr(adapter, "_process_matches", lambda *_args: True)
    monkeypatch.setattr(adapter, "_birth", lambda *_args: "birth")
    monkeypatch.setattr(adapter, "_pid_alive", lambda *_args: False)
    monkeypatch.setattr(adapter, "_wait_port_free", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(adapter, "_write_state", lambda *_args: None)

    def observe(argv, *, timeout):
        captured.update(argv=argv, timeout=timeout)
        return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

    monkeypatch.setattr(adapter, "_run_observation", observe)
    result = adapter._stop(
        {"platform": {"os": "windows"}, "state_path": tmp_path / "state.json"}, state
    )
    assert result["result"] == "stopped"
    assert captured == {"argv": ["taskkill.exe", "/PID", "3333", "/T", "/F"], "timeout": 20}


def test_builder_rejects_malformed_guard_paths(tmp_path):
    spec_path = tmp_path / "adapter.json"
    spec_path.write_text(
        json.dumps(
            {
                "identity": "candidate",
                "instance_ids": ["first", "second"],
                "platform": {
                    "os": adapter.PLATFORM_NAMES[sys.platform],
                    "runner": "local",
                    "hosted": False,
                },
                "guard_paths": ["relative.txt"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(adapter.AdapterError, match="absolute non-symlink"):
        adapter.build_executor_commands(spec_path, cwd=tmp_path)


def test_state_tampering_cannot_redirect_stop_to_another_identity(tmp_path):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    path = Path(spec["state_path"])
    original = path.read_text(encoding="utf-8")
    tampered = json.loads(original)
    tampered["identity"] = "other-candidate"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(adapter.AdapterError, match="does not authenticate"):
        _operation(spec, "stop")
    path.write_text(original, encoding="utf-8")
    _operation(spec, "stop")


def test_missing_process_observation_pid_reuse_and_malformed_state_fail_closed(
    tmp_path, monkeypatch
):
    spec = _spec(tmp_path)
    _operation(spec, "start")
    state_path = Path(spec["state_path"])
    monkeypatch.setattr(
        adapter,
        "_birth",
        lambda *_args: (_ for _ in ()).throw(
            adapter.AdapterError("process observation unavailable")
        ),
    )
    with pytest.raises(adapter.AdapterError, match="process observation unavailable"):
        _operation(spec, "processes")
    monkeypatch.setattr(adapter, "_birth", lambda *_args: "reused-pid")
    with pytest.raises(adapter.AdapterError, match="reused"):
        _operation(spec, "processes")
    monkeypatch.undo()
    # Clean up while the authentic birth observation is still available.
    _operation(spec, "stop")
    state_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(adapter.AdapterError, match="malformed"):
        _operation(spec, "processes")


def test_secret_never_appears_in_receipt_state_or_cli_output(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    secret = "private-controller-token-test"

    class FixedToken:
        hex = secret

    monkeypatch.setattr(adapter.uuid, "uuid4", lambda: FixedToken())
    started, code = _operation(spec, "start")
    assert code == 0
    spec_path = tmp_path / "adapter.json"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    state_text = Path(spec["state_path"]).read_text(encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(adapter.__file__).resolve()),
            "processes",
            "--spec",
            str(spec_path),
        ],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert secret not in state_text and secret not in result.stdout
    assert "controller_token_representation" in result.stdout
    _operation(spec, "stop")


def test_cli_help_and_local_receipt_is_never_hosted(tmp_path):
    module = Path(adapter.__file__).resolve()
    result = subprocess.run(
        [sys.executable, str(module), "--help"],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    assert result.returncode == 0
    assert "--spec" in result.stdout and "--controller-token-digest" in result.stdout
    spec_path = tmp_path / "adapter.json"
    spec_path.write_text(json.dumps(_spec(tmp_path)), encoding="utf-8")
    missing = subprocess.run(
        [sys.executable, str(module), "adopt", "--spec", str(spec_path)],
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    assert missing.returncode == 2 and "adopt requires" in missing.stderr
    spec = _spec(tmp_path)
    _operation(spec, "start")
    snapshot, _ = _operation(spec, "processes")
    assert snapshot["evidence_class"] == "executable-boundary"
    _operation(spec, "stop")
