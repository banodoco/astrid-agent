from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astrid.sdk import autobootstrap
from astrid.sdk.client import AstridClient
from astrid.sdk.exceptions import ServiceUnavailableError
from astrid.sdk.host_bootstrap import _provision_render_runtime_env
from astrid.sdk.workspace_client import WorkspaceClientError
from astrid_runtime_client.contract_metadata import SCHEMA_DIGEST


def test_launcher_wait_outlasts_runtime_admission(monkeypatch):
    monkeypatch.delenv("ASTRID_RUNTIME_ADMISSION_TIMEOUT_SECONDS", raising=False)
    assert autobootstrap._launcher_timeout() == 135.0
    monkeypatch.setenv("ASTRID_RUNTIME_ADMISSION_TIMEOUT_SECONDS", "300")
    assert autobootstrap._launcher_timeout() == 315.0


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "invalid"])
def test_invalid_admission_budget_is_rejected(monkeypatch, value):
    monkeypatch.setenv("ASTRID_RUNTIME_ADMISSION_TIMEOUT_SECONDS", value)
    with pytest.raises(autobootstrap.AutoBootstrapError):
        autobootstrap._launcher_timeout()


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    """Keep upgrade-guard tests hermetic around the operator's live home."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _runtime_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "runtime"
    (checkout / "astrid_runtime").mkdir(parents=True)
    return checkout


def _persisted_profile(home: Path, runtime: Path, source: Path) -> Path:
    profile = (
        home
        / "Library"
        / "Application Support"
        / "Astrid Runtime"
        / "runtime"
        / "source-profiles"
        / "astrid.json"
    )
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "profile": "astrid",
                "runtime_checkout": str(runtime),
                "source_checkout": str(source),
                "runtime_command": [],
                "protocol_version": "workspace.v1",
                "schema_version": "workspace-schema-v1",
            }
        ),
        encoding="utf-8",
    )
    return profile


def test_source_profile_provisions_explicit_render_runtime(monkeypatch, tmp_path):
    source = tmp_path / "source"
    remotion = source / "remotion"
    schema = (
        remotion
        / "node_modules"
        / "@banodoco"
        / "timeline-schema"
        / "python"
        / "banodoco_timeline_schema"
    )
    schema.mkdir(parents=True)
    (schema / "__init__.py").write_text("", encoding="utf-8")
    (remotion / "package.json").write_text("{}", encoding="utf-8")
    # The resolver probes each candidate with ``--version``; an unpinned
    # project (no engines.node) takes the first working Node on PATH.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\necho v20.19.4\n", encoding="utf-8")
    node.chmod(0o700)

    child_env = {"PATH": str(bin_dir)}
    _provision_render_runtime_env(source, child_env)

    assert child_env["ASTRID_REMOTION_PROJECT_DIR"] == str(remotion.resolve())
    assert child_env["ASTRID_NODE_EXECUTABLE"] == str(node.resolve())
    assert child_env["ASTRID_TIMELINE_SCHEMA_PYTHONPATH"] == str(schema.parent.resolve())


def test_neutral_launcher_is_invoked_with_ephemeral_profile(monkeypatch, tmp_path):
    runtime = _runtime_checkout(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    manifest_path = tmp_path / "source-profile.json"
    manifest_path.write_text(json.dumps({"profile": "astrid", "runtime_checkout": str(runtime), "source_checkout": str(source)}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(manifest_path))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    # This test exercises the explicit cold/up command.  Keep it hermetic
    # when the developer checkout happens to have a live discovery file.
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)

    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        manifest = Path(command[command.index("--source-manifest") + 1])
        seen["manifest"] = json.loads(manifest.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0, '{"status":"started","realm_id":"realm-1","endpoint":"http://127.0.0.1:1","actor_id":"owner"}\n', "")

    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    result = autobootstrap.ensure_runtime()

    assert result["status"] == "started"
    command = seen["command"]
    assert command[1:4] == ["up", "--profile", "astrid"]
    assert "serve" not in command
    assert seen["manifest"] == {"profile": "astrid", "runtime_checkout": str(runtime), "source_checkout": str(source)}
    assert command[-1] == "--json"


def test_read_runtime_connection_skips_pack_host_setup(monkeypatch, tmp_path):
    from astrid.sdk import host_bootstrap

    worker = tmp_path / "worker.token"
    worker.write_text("worker-secret", encoding="utf-8")
    credential = tmp_path / "astrid.json"
    credential.write_text("{}", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "status": "reconnected",
                "realm_id": "realm-1",
                "endpoint": "http://127.0.0.1:1",
                "actor_id": "actor-1",
                "credential_file": str(credential),
                "worker_credential_file": str(worker),
                "worker_actor": "astrid-pack-host",
                "worker_scopes": [
                    "handshake", "worker:register", "worker:execute",
                    "tasks:read", "objects:read", "objects:write",
                ],
            }),
            "",
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(
        host_bootstrap,
        "ensure_pack_host",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("host setup must be skipped")),
        raising=False,
    )

    result = autobootstrap.ensure_runtime(start_pack_host=False)

    assert result["status"] == "reconnected"
    assert seen["command"][:4] == ["/usr/bin/astrid-runtime", "up", "--profile", "astrid"]


def test_default_runtime_connection_still_starts_pack_host(monkeypatch, tmp_path):
    from astrid.sdk import host_bootstrap

    worker = tmp_path / "worker.token"
    worker.write_text("worker-secret", encoding="utf-8")
    credential = tmp_path / "astrid.json"
    credential.write_text("{}", encoding="utf-8")
    seen: list[dict[str, object]] = []

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "status": "reconnected",
                "realm_id": "realm-1",
                "endpoint": "http://127.0.0.1:1",
                "actor_id": "actor-1",
                "credential_file": str(credential),
                "worker_credential_file": str(worker),
                "worker_actor": "astrid-pack-host",
                "worker_scopes": [
                    "handshake", "worker:register", "worker:execute",
                    "tasks:read", "objects:read", "objects:write",
                ],
            }),
            "",
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(
        host_bootstrap,
        "ensure_pack_host",
        lambda *args, **kwargs: seen.append(dict(kwargs)) or {"host_status": "ready"},
        raising=False,
    )

    result = autobootstrap.ensure_runtime()

    assert result["host_status"] == "ready"
    assert seen == [{"reconfigure_action": autobootstrap.RECONFIGURE_ACTION, "attach_healthy": True}]


def test_real_worker_source_handoff_starts_pack_host_after_lifecycle_filter(
    monkeypatch, tmp_path
):
    """Lifecycle filtering must retain the fields needed by host bootstrap."""
    from astrid.sdk import host_bootstrap

    worker = tmp_path / "worker.token"
    worker.write_text("worker-secret", encoding="utf-8")
    credential = tmp_path / "astrid.json"
    credential.write_text("{}", encoding="utf-8")
    source = tmp_path / "source-checkout"
    source.mkdir()
    handoffs: list[dict[str, object]] = []

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "status": "started",
                    "realm_id": "realm-1",
                    "endpoint": "http://127.0.0.1:1",
                    "actor_id": "owner",
                    "credential_file": str(credential),
                    "worker_credential_file": str(worker),
                    "worker_actor": "astrid-pack-host",
                    "worker_scopes": [
                        "handshake",
                        "worker:register",
                        "worker:execute",
                        "tasks:read",
                        "objects:read",
                        "objects:write",
                    ],
                    "source_checkout": str(source),
                    "runtime_instance_id": "runtime-1",
                    "coordinator_epoch": "coordinator-1",
                    "runtime_epoch": 7,
                    "schema_digest": "schema-7",
                }
            ),
            "",
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)

    def fake_ensure_pack_host(value, **kwargs):
        handoffs.append(dict(value))
        return {"host_status": "ready"}

    monkeypatch.setattr(host_bootstrap, "ensure_pack_host", fake_ensure_pack_host)

    result = autobootstrap.ensure_runtime(start_pack_host=True)

    assert result["host_status"] == "ready"
    assert len(handoffs) == 1
    assert handoffs[0]["source_checkout"] == str(source)
    assert handoffs[0]["runtime_epoch"] == 7
    assert handoffs[0]["schema_digest"] == "schema-7"


def test_installed_runtime_module_is_used_when_console_script_is_off_path(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("ASTRID_RUNTIME_LAUNCHER", raising=False)
    monkeypatch.delenv("ASTRID_RUNTIME_SOURCE_MANIFEST", raising=False)
    monkeypatch.setattr(autobootstrap.shutil, "which", lambda _name: None)
    monkeypatch.setattr(autobootstrap, "find_spec", lambda name: object())
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            '{"status":"started","realm_id":"realm-1",'
            '"endpoint":"http://127.0.0.1:1","actor_id":"owner"}',
            "",
        )

    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)

    assert autobootstrap.ensure_runtime()["status"] == "started"
    assert seen["command"] == [
        sys.executable,
        "-m",
        "astrid_runtime",
        "up",
        "--profile",
        "astrid",
        "--data-root",
        str(Path(__file__).resolve().parents[2] / ".astrid-data"),
        "--json",
    ]


def test_missing_runtime_package_returns_install_action(monkeypatch):
    monkeypatch.delenv("ASTRID_RUNTIME_LAUNCHER", raising=False)
    monkeypatch.setattr(autobootstrap.shutil, "which", lambda _name: None)
    monkeypatch.setattr(autobootstrap, "find_spec", lambda name: None)

    with pytest.raises(autobootstrap.AutoBootstrapError) as caught:
        autobootstrap.ensure_runtime()

    assert "not installed" in str(caught.value)
    assert caught.value.next_action == autobootstrap.INSTALL_RUNTIME_ACTION


def test_launcher_bootstrap_preserves_typed_install_action(monkeypatch):
    def missing_runtime():
        raise autobootstrap.AutoBootstrapError(
            "runtime missing",
            next_action=autobootstrap.INSTALL_RUNTIME_ACTION,
        )

    monkeypatch.setattr(autobootstrap, "ensure_runtime", missing_runtime)

    with pytest.raises(ServiceUnavailableError) as caught:
        AstridClient.open_from_launcher()

    assert caught.value.details["next_action"] == autobootstrap.INSTALL_RUNTIME_ACTION


def test_launcher_rejection_preserves_structured_runtime_cause(monkeypatch, tmp_path):
    """A neutral admission failure remains actionable at the Astrid boundary."""
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)

    response = {
        "ok": False,
        "error": {
            "code": "realm_admission_failed",
            "message": "realm failed startup admission",
            "details": {
                "checks": {"sqlite": "timeout"},
                "recovery_action": "inspect the realm admission report",
            },
        },
    }
    monkeypatch.setattr(
        autobootstrap.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 1, json.dumps(response), ""
        ),
    )

    with pytest.raises(autobootstrap.AutoBootstrapError) as caught:
        autobootstrap.ensure_runtime(start_pack_host=False, data_root=tmp_path / "support")

    assert caught.value.code == "runtime_bootstrap_rejected"
    assert caught.value.details["cause_code"] == "realm_admission_failed"
    assert caught.value.details["checks"]["sqlite"] == "timeout"
    assert caught.value.details["recovery_action"] == "inspect the realm admission report"


def test_open_from_launcher_relays_lifecycle_code_and_cause(monkeypatch):
    def rejected():
        raise autobootstrap.AutoBootstrapError(
            "runtime rejected",
            code="runtime_bootstrap_rejected",
            details={"cause_code": "realm_admission_failed", "state": "blocked"},
        )

    monkeypatch.setattr(autobootstrap, "ensure_runtime", rejected)

    with pytest.raises(ServiceUnavailableError) as caught:
        AstridClient.open_from_launcher()

    assert caught.value.code == "unavailable"
    assert caught.value.details["lifecycle_code"] == "runtime_bootstrap_rejected"
    assert caught.value.details["cause_code"] == "realm_admission_failed"
    assert caught.value.details["next_action"] == autobootstrap.RECONFIGURE_ACTION


def test_configured_manifest_does_not_require_editable_source_inference(monkeypatch, tmp_path):
    runtime = _runtime_checkout(tmp_path)
    manifest = tmp_path / "source-profile.json"
    manifest.write_text(
        json.dumps(
            {
                "profile": "astrid",
                "runtime_checkout": str(runtime),
                "source_checkout": str(tmp_path / "pack-source"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(manifest))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(
        autobootstrap.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, '{"status":"reconnected","realm_id":"realm-1","endpoint":"http://127.0.0.1:1","actor_id":"owner"}', ""
        ),
    )

    result = autobootstrap.ensure_runtime()
    assert result["status"] == "reconnected"


def test_persisted_source_profile_relaunches_without_environment(monkeypatch, tmp_path):
    runtime = _runtime_checkout(tmp_path)
    source = tmp_path / "astrid-source"
    source.mkdir()
    home = tmp_path / "home"
    profile = _persisted_profile(home, runtime, source)
    assert profile.is_file()
    launcher = tmp_path / "astrid-runtime"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", str(launcher))
    monkeypatch.delenv("ASTRID_RUNTIME_SOURCE_MANIFEST", raising=False)
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)

    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            '{"status":"reconnected","realm_id":"realm-1","endpoint":"http://127.0.0.1:1","actor_id":"owner"}\n',
            "",
        )

    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    result = autobootstrap.ensure_runtime()

    assert result["status"] == "reconnected"
    assert seen["command"] == [
        str(launcher), "up", "--profile", "astrid", "--data-root",
        str(Path(__file__).resolve().parents[2] / ".astrid-data"), "--json"
    ]


def test_envless_bootstrap_delegates_missing_profile_to_neutral_launcher(monkeypatch, tmp_path):
    launcher = tmp_path / "astrid-runtime"
    monkeypatch.setenv("HOME", str(tmp_path / "fresh-home"))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", str(launcher))
    monkeypatch.delenv("ASTRID_RUNTIME_SOURCE_MANIFEST", raising=False)
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(
            command,
            1,
            '{"ok":false,"error":"Source profile manifest is missing or invalid"}',
            "",
        )

    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    with pytest.raises(autobootstrap.AutoBootstrapError, match="not ready"):
        autobootstrap.ensure_runtime()
    assert seen["command"] == [
        str(launcher), "up", "--profile", "astrid", "--data-root",
        str(Path(__file__).resolve().parents[2] / ".astrid-data"), "--json"
    ]


def test_envless_bootstrap_never_infers_checkout_authority(monkeypatch, tmp_path):
    launcher = tmp_path / "astrid-runtime"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", str(launcher))
    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", str(tmp_path / "attacker-runtime"))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER_CHECKOUT", str(tmp_path / "other-runtime"))
    monkeypatch.setenv("UNTRUSTED_ASTRID_SOURCE_CHECKOUT", str(tmp_path / "attacker-source"))
    monkeypatch.setenv("ASTRID_SOURCE_CHECKOUT", str(tmp_path / "other-source"))
    monkeypatch.delenv("ASTRID_RUNTIME_SOURCE_MANIFEST", raising=False)
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: False)
    seen: dict[str, object] = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            '{"status":"started","realm_id":"realm-1","endpoint":"http://127.0.0.1:1","actor_id":"owner"}',
            "",
        )

    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    assert autobootstrap.ensure_runtime()["status"] == "started"
    assert "--source-manifest" not in seen["command"]


def test_sdk_open_uses_explicit_context_without_bootstrap(monkeypatch):
    calls: list[str] = []

    def resolve(endpoint, credential):
        assert endpoint == "http://127.0.0.1:1" and credential == "token"
        return endpoint, credential

    class FakeWorkspace:
        def __init__(self, endpoint, token):
            assert endpoint == "http://127.0.0.1:1"
            assert token == "token"

        def health(self):
            calls.append("health")
            return {
                "status": "ok",
                "protocol": "workspace.v1",
                "schema_digest": SCHEMA_DIGEST,
                "runtime_epoch": 1,
                "runtime_instance_id": "runtime-1",
                "runtime_session_id": "session-1",
            }

        def handshake(self, *args):
            calls.append("handshake")
            return {
                "protocol": "workspace.v1",
                "schema_digest": SCHEMA_DIGEST,
                "session_id": "session",
                "realm_id": "realm",
                "actor_id": "actor",
                "scopes": [
                    "projects:read", "projects:write", "objects:read",
                    "objects:write", "tasks:read", "tasks:write",
                ],
                "capabilities": ["execution_binding.targeted.v1"],
            }

    monkeypatch.setattr("astrid.sdk.workspace_client.resolve_runtime_connection", resolve)
    monkeypatch.setattr("astrid.sdk.workspace_client.WorkspaceClient", FakeWorkspace)
    monkeypatch.setattr("astrid.sdk.autobootstrap.ensure_runtime", lambda: pytest.fail("ordinary SDK bootstrapped runtime"))

    AstridClient.open(endpoint="http://127.0.0.1:1", credential="token", realm_id="realm", actor_id="actor", client_name="test", client_version="1", protocol_version="workspace.v1")
    assert calls == ["health", "handshake"]


def test_open_existing_from_launcher_observes_before_connecting_without_acquisition(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from astrid_runtime_client.contract_metadata import PROTOCOL

    support_root = tmp_path / "support"
    realm_root = tmp_path / "realm"
    credential_file = support_root / "credentials" / "astrid.json"
    credential_file.parent.mkdir(parents=True)
    credential_file.write_text(
        json.dumps({"version": 1, "scope": "astrid", "actor_id": "astrid-user", "token": "opaque-test-token"}),
        encoding="utf-8",
    )
    calls: list[tuple[str, object]] = []

    class FakeRuntime:
        def inspect(self, *, support_root, timeout):
            calls.append(("inspect", (support_root, timeout)))
            return SimpleNamespace(
                ok=True,
                data={
                    "ok": True,
                    "realm_id": "realm-1",
                    "realm_root": str(realm_root),
                    "support_root": str(support_root),
                },
            )

        def observe(self, command, *, support_root, timeout):
            calls.append(("observe", (command, support_root, timeout)))
            return SimpleNamespace(
                ok=True,
                data={
                    "discovery": {
                        "active_realm": "realm-1",
                        "realm_root": str(realm_root),
                        "runtime_instance_id": "runtime-1",
                        "endpoint": "http://127.0.0.1:43123",
                        "credential_file": str(credential_file),
                    },
                    "support": {"healthy": True},
                    "health": {
                        "status": "ok",
                        "protocol": PROTOCOL,
                        "schema_digest": SCHEMA_DIGEST,
                        "runtime_instance_id": "runtime-1",
                    },
                },
            )

    monkeypatch.setattr("astrid.runtime_cli.RuntimeCLI", FakeRuntime)
    monkeypatch.setattr("astrid.sdk.storage_root.resolve_runtime_data_root", lambda: support_root)
    monkeypatch.setattr(
        "astrid.sdk.autobootstrap.ensure_runtime",
        lambda **kwargs: pytest.fail("read-only task listing entered Runtime acquisition"),
    )

    opened: dict[str, object] = {}

    def fake_open(cls, **kwargs):
        opened.update(kwargs)
        return object()

    monkeypatch.setattr(AstridClient, "open", classmethod(fake_open))

    assert AstridClient.open_existing_from_launcher() is not None
    assert [name for name, _ in calls] == ["inspect", "observe"]
    assert calls[0][1][1] == 5.0
    assert calls[1][1][0] == "status" and calls[1][1][2] == 5.0
    assert opened == {
        "endpoint": "http://127.0.0.1:43123",
        "credential": credential_file,
        "realm_id": "realm-1",
        "actor_id": "astrid-user",
        "client_name": "astrid",
        "client_version": "stage1",
        "protocol_version": PROTOCOL,
    }
    assert not (support_root / "runtime" / "acquisition.lock").exists()


def test_open_existing_from_launcher_cold_workspace_fails_without_creating_support(monkeypatch, tmp_path):
    from types import SimpleNamespace

    support_root = tmp_path / "cold-support"
    calls: list[str] = []

    class FakeRuntime:
        def inspect(self, *, support_root, timeout):
            calls.append("inspect")
            return SimpleNamespace(ok=False, data={"problem_code": "workspace_missing"})

        def observe(self, *args, **kwargs):
            calls.append("observe")
            pytest.fail("cold workspace should fail at read-only inspect")

    monkeypatch.setattr("astrid.runtime_cli.RuntimeCLI", FakeRuntime)
    monkeypatch.setattr("astrid.sdk.storage_root.resolve_runtime_data_root", lambda: support_root)
    monkeypatch.setattr(
        "astrid.sdk.autobootstrap.ensure_runtime",
        lambda **kwargs: pytest.fail("cold task list must not start Runtime"),
    )
    monkeypatch.setattr(
        AstridClient,
        "open",
        classmethod(lambda cls, **kwargs: pytest.fail("cold workspace must not connect")),
    )

    with pytest.raises(ServiceUnavailableError, match="selected workspace"):
        AstridClient.open_existing_from_launcher()
    assert calls == ["inspect"]
    assert not support_root.exists()


def test_diagnostics_can_remain_side_effect_free(monkeypatch):
    monkeypatch.setattr(
        "astrid.sdk.workspace_client.resolve_runtime_connection",
        lambda _endpoint, _credential: (_ for _ in ()).throw(WorkspaceClientError(0, "unavailable", "missing")),
    )
    monkeypatch.setattr(
        "astrid.sdk.autobootstrap.ensure_runtime",
        lambda: pytest.fail("diagnostic path unexpectedly bootstrapped the runtime"),
    )
    with pytest.raises(Exception, match="run the Astrid install line again"):
        AstridClient.open(endpoint="http://127.0.0.1:1", credential="token", realm_id="realm", actor_id="actor", client_name="test", client_version="1", protocol_version="workspace.v1")


def test_warm_acquisition_connects_before_considering_bootstrap(monkeypatch, tmp_path):
    data_root = tmp_path / "support"
    discovery = data_root / "runtime" / "discovery.json"
    discovery.parent.mkdir(parents=True)
    discovery.write_text("{}", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({
                "status": "reconnected",
                "realm_id": "realm-1",
                "endpoint": "http://127.0.0.1:1",
                "actor_id": "actor-1",
            }),
            "",
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)

    result = autobootstrap.ensure_runtime(start_pack_host=False, data_root=data_root)

    assert result["status"] == "reconnected"
    assert len(seen) == 1
    assert seen[0][:4] == ["/usr/bin/astrid-runtime", "connect", "--profile", "astrid"]


def test_stale_discovery_falls_back_to_authoritative_bootstrap(monkeypatch, tmp_path):
    data_root = tmp_path / "support"
    discovery = data_root / "runtime" / "discovery.json"
    discovery.parent.mkdir(parents=True)
    discovery.write_text(json.dumps({"pid": 999999}), encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(command, **kwargs):
        seen.append(command)
        if command[1] == "connect":
            return subprocess.CompletedProcess(
                command, 1, '{"ok":false,"error":"Runtime discovery is stale"}', ""
            )
        return subprocess.CompletedProcess(
            command,
            0,
            '{"status":"started","realm_id":"realm-1","endpoint":"http://127.0.0.1:1","actor_id":"actor-1"}',
            "",
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)

    result = autobootstrap.ensure_runtime(start_pack_host=False, data_root=data_root)

    assert result["status"] == "started"
    assert [command[1] for command in seen] == ["connect", "up"]


def test_connect_protocol_rejection_is_not_masked_by_bootstrap(monkeypatch, tmp_path):
    data_root = tmp_path / "support"
    discovery = data_root / "runtime" / "discovery.json"
    discovery.parent.mkdir(parents=True)
    discovery.write_text("{}", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(
            command, 1, '{"ok":false,"error":"Source profile is incompatible"}', ""
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)

    with pytest.raises(autobootstrap.AutoBootstrapError, match="incompatible"):
        autobootstrap.ensure_runtime(start_pack_host=False, data_root=data_root)
    assert [command[1] for command in seen] == ["connect"]


@pytest.mark.parametrize("refusal", ["Runtime checkout identity differs.", "New wording: discovery is stale."])
def test_identity_mismatch_refuses_connect_first_without_fallback_or_metadata_writes(monkeypatch, tmp_path, refusal):
    data_root = tmp_path / "support"
    runtime_support = data_root / "runtime"
    source_profiles = runtime_support / "source-profiles"
    credentials = data_root / "credentials"
    source_profiles.mkdir(parents=True)
    credentials.mkdir(parents=True)
    files = {
        runtime_support / "discovery.json": b'{"pid":123,"runtime_instance_id":"old-owner"}',
        runtime_support / "catalog.json": b'{"selected_realm_id":"realm-1"}',
        source_profiles / "astrid.json": b'{"profile":"astrid","source_checkout":"old"}',
        credentials / "astrid.json": b'{"token":"existing"}',
    }
    for path, content in files.items():
        path.write_bytes(content)
    before = {path: path.read_bytes() for path in files}
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(
            command, 1, json.dumps({"ok": False, "error": {
                "message": refusal, "details": {"code": "identity_mismatch"}}}), "",
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: True)

    with pytest.raises(autobootstrap.AutoBootstrapError) as caught:
        autobootstrap.ensure_runtime(start_pack_host=False, data_root=data_root)

    assert str(caught.value) == refusal
    assert caught.value.code == "runtime_identity_mismatch"
    assert caught.value.details["code"] == "identity_mismatch"
    assert caught.value.next_action == "astrid-runtime restart"
    assert len(seen) == 1
    assert seen[0][1] == "connect"
    assert before == {path: path.read_bytes() for path in files}


@pytest.mark.integration
def test_loopback_old_owner_refuses_agent_callers_until_explicit_dirty_restart(monkeypatch, tmp_path):
    from astrid.runtime_cli import RuntimeCLI
    from tests.helpers.runtime import runtime_checkout

    agent_root = Path(__file__).resolve().parents[2]
    runtime_root = runtime_checkout()
    runtime_python = sys.executable
    python_path = os.pathsep.join((
        str(runtime_root), str(runtime_root / "packages" / "python"), str(agent_root),
    ))
    monkeypatch.setenv("PYTHONPATH", python_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    if sys.platform == "win32":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.delenv("ASTRID_INSTALL_RELEASE", raising=False)
    monkeypatch.delenv("ASTRID_INSTALL_MANAGED_BY", raising=False)

    def git_checkout(root: Path, source_root: Path, trees: tuple[str, ...]):
        root.mkdir(parents=True)
        for tree in trees:
            target = root / tree
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                source_root / tree, target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
            )
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
        (root / "tracked.txt").write_text("baseline", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)

    source_runtime = tmp_path / "source-runtime"
    source_agent = tmp_path / "source-agent"
    git_checkout(
        source_runtime, runtime_root,
        ("runtime_protocol", "astrid_runtime", "packages/python/astrid_runtime_client"),
    )
    git_checkout(source_agent, agent_root, ("astrid",))
    manifest = tmp_path / "source-profile.json"
    manifest.write_text(json.dumps({
        "profile": "astrid",
        "runtime_checkout": str(source_runtime),
        "source_checkout": str(source_agent),
        "runtime_command": [],
        "protocol_version": "workspace.v1",
        "schema_version": "workspace-schema-v1",
    }), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(manifest))

    monkeypatch.syspath_prepend(str(runtime_root / "packages" / "python"))
    monkeypatch.syspath_prepend(str(runtime_root))
    runtime_module_names = ("astrid_runtime", "runtime_protocol")
    saved_modules = {
        name: module for name, module in sys.modules.items()
        if any(name == prefix or name.startswith(prefix + ".") for prefix in runtime_module_names)
    }
    for name in tuple(sys.modules):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in runtime_module_names):
            del sys.modules[name]
    from astrid_runtime import (
        BootstrapConfig,
        LocalRuntimeBoundary,
        RuntimePaths,
        SourceProfile,
        configure_workspace,
    )
    from astrid_runtime import bootstrap as runtime_bootstrap
    from astrid_runtime.bootstrap import restart as runtime_restart

    profile = SourceProfile(
        profile="astrid", runtime_checkout=str(source_runtime),
        source_checkout=str(source_agent), capability_digest="caps-v1",
    )
    config = BootstrapConfig(source_profile=profile)
    paths = RuntimePaths.sandbox(tmp_path / "support")
    first_boundary = LocalRuntimeBoundary()
    restart_boundary = None

    def read_authority():
        return {
            path: path.read_bytes()
            for path in (
                paths.catalog_path,
                paths.source_profiles_dir / "astrid.json",
                paths.discovery_path,
                paths.instance_lock_path,
                paths.credentials_dir / "astrid.json",
            )
        }

    try:
        configure_workspace(
            paths, first_boundary, config, mode="create",
            realm_root=paths.realms_dir / "explicit-realm", realm_id="explicit-realm",
        )
        first = runtime_bootstrap(paths, first_boundary, config)
        discovery_before = json.loads(paths.discovery_path.read_text(encoding="utf-8"))
        identity_before = discovery_before["identity"]
        assert identity_before["dirty"] is False

        (source_agent / "astrid" / "UPD-B1-test-edit.txt").write_text("edited source", encoding="utf-8")
        authority_before = read_authority()
        monkeypatch.setattr(
            autobootstrap, "_launcher_command",
            lambda: [runtime_python, "-m", "astrid_runtime"],
        )
        with pytest.raises(autobootstrap.AutoBootstrapError) as caught:
            autobootstrap.ensure_runtime(start_pack_host=False, data_root=paths.app_support)
        from astrid_runtime.identity import IDENTITY_REFUSAL_MESSAGE
        refusal = IDENTITY_REFUSAL_MESSAGE
        assert str(caught.value) == refusal
        assert caught.value.code == "runtime_identity_mismatch"
        assert authority_before == read_authority()
        assert first_boundary._process is not None and first_boundary._process.poll() is None

        runtime_cli = RuntimeCLI(command=(runtime_python, "-m", "astrid_runtime"))
        cli_result = runtime_cli.up(
            support_root=paths.app_support,
            expected_realm_id=first.realm_id,
            realm_root=paths.realms_dir / "explicit-realm",
        )
        assert not cli_result.ok
        assert cli_result.data["error"] == refusal
        assert authority_before == read_authority()
        assert first_boundary._process.poll() is None

        restart_boundary = LocalRuntimeBoundary()
        restart_boundary.prepare_restart(
            source_profile=profile,
            realm_id=first.realm_id,
            realm_root=paths.realms_dir / "explicit-realm",
            support_root=paths.runtime_support,
            pid=int(discovery_before["pid"]),
        )
        restarted = runtime_restart(paths, restart_boundary, config)
        updated = json.loads(paths.discovery_path.read_text(encoding="utf-8"))
        assert restarted.runtime_instance_id != first.runtime_instance_id
        assert updated["runtime_instance_id"] == restarted.runtime_instance_id
        assert updated["identity"]["dirty"] is True
        assert updated["identity"]["dirty_hash"] != identity_before["dirty_hash"]
        reconnected = autobootstrap.ensure_runtime(start_pack_host=False, data_root=paths.app_support)
        assert reconnected["status"] == "reconnected"
        assert reconnected["runtime_instance_id"] == restarted.runtime_instance_id
    finally:
        if restart_boundary is not None:
            process = getattr(restart_boundary, "_process", None)
            if process is not None and process.poll() is None:
                restart_boundary.stop()
        process = getattr(first_boundary, "_process", None)
        if process is not None and process.poll() is None:
            first_boundary.stop()
        for name in tuple(sys.modules):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in runtime_module_names):
                del sys.modules[name]
        sys.modules.update(saved_modules)


def test_interrupted_bootstrap_gets_one_bounded_recovery_attempt(monkeypatch, tmp_path):
    data_root = tmp_path / "support"
    seen: list[list[str]] = []

    def fake_run(command, **kwargs):
        seen.append(command)
        if len(seen) == 1:
            raise subprocess.TimeoutExpired(command, timeout=1)
        return subprocess.CompletedProcess(
            command,
            0,
            '{"status":"reconnected","realm_id":"realm-1","endpoint":"http://127.0.0.1:1","actor_id":"actor-1"}',
            "",
        )

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)

    result = autobootstrap.ensure_runtime(start_pack_host=False, data_root=data_root)

    assert result["status"] == "reconnected"
    assert len(seen) == 2
    assert all(command[1] == "up" for command in seen)


@pytest.mark.parametrize("profile", ["source", "installed"])
def test_real_cold_launch_projects_installed_startup_policy_only(monkeypatch, tmp_path, profile):
    from astrid.sdk import host_bootstrap
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", profile)
    seen = []
    original_test = test_default_runtime_connection_still_starts_pack_host
    original_setattr = monkeypatch.setattr
    def bind(target, name, value, *args, **kwargs):
        if target is host_bootstrap and name == "ensure_pack_host":
            def observe(*call_args, **call_options):
                seen.append(dict(call_options))
                return value(*call_args, **call_options)
            return original_setattr(target, name, observe, *args, **kwargs)
        return original_setattr(target, name, value, *args, **kwargs)
    monkeypatch.setattr = bind
    try:
        original_test(monkeypatch, tmp_path)
    finally:
        monkeypatch.setattr = original_setattr
    assert len(seen) == 1
    assert "startup_idle_seconds" not in seen[0]


def test_identity_refusal_text_alone_does_not_classify_as_identity_mismatch(monkeypatch, tmp_path):
    refusal = "An older Astrid is still running. Run `astrid-runtime restart`; it waits for running tasks."
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 1, json.dumps({"ok": False, "error": {
            "message": refusal, "details": {"code": "other_refusal"}}}), "")

    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/bin/astrid-runtime")
    monkeypatch.setattr(autobootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(autobootstrap, "_discovery_present", lambda _root: True)
    with pytest.raises(autobootstrap.AutoBootstrapError) as caught:
        autobootstrap.ensure_runtime(start_pack_host=False, data_root=tmp_path)
    assert caught.value.code == "runtime_connection_rejected"
    assert len(seen) == 1
