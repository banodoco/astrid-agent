from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from astrid.sdk import autobootstrap
from astrid.sdk.client import AstridClient
from astrid.sdk.remote import RemoteAstridClient, RemoteTimelines
from astrid.sdk.workspace_client import (
    WorkspaceClient,
    WorkspaceClientError,
    resolve_runtime_connection,
)


def test_open_requires_explicit_context_and_never_bootstraps(monkeypatch):
    monkeypatch.setattr(
        "astrid.sdk.autobootstrap.ensure_runtime",
        lambda: pytest.fail("ordinary SDK open invoked the launcher"),
    )
    with pytest.raises(Exception, match="run the Astrid install line again"):
        AstridClient.open()


def test_connection_resolution_does_not_guess_a_checkout_or_catalog(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", "http://wrong")
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(tmp_path / "wrong-token"))
    with pytest.raises(WorkspaceClientError):
        resolve_runtime_connection("", "")
    assert resolve_runtime_connection("https://127.0.0.1:8443/", "Bearer token") == (
        "https://127.0.0.1:8443",
        "token",
    )


def test_connection_resolution_accepts_host_injected_runtime_context(tmp_path: Path, monkeypatch):
    credential = tmp_path / "owner.token"
    credential.write_text("child-token\n", encoding="utf-8")
    credential.chmod(0o600)
    monkeypatch.setenv("ASTRID_RUNTIME_ENDPOINT", "http://127.0.0.1:8443/")
    monkeypatch.setenv("ASTRID_RUNTIME_CREDENTIAL", str(credential))

    assert resolve_runtime_connection() == ("http://127.0.0.1:8443", "child-token")


def test_generated_failure_keeps_request_correlation(monkeypatch):
    class GeneratedFailure(RuntimeError):
        status = 503
        code = "registration_unavailable"
        message = "registration unavailable"
        request_id = "request-sdk-1"
        details = {"retryable": False}

    class Generated:
        def __init__(self, *_args, **_kwargs):
            pass

        def health(self):
            raise GeneratedFailure()

    monkeypatch.setattr("astrid.sdk.workspace_client.GeneratedWorkspaceClient", Generated)
    client = WorkspaceClient("http://127.0.0.1:8443", "token")

    with pytest.raises(WorkspaceClientError) as caught:
        client.health()

    assert caught.value.code == "registration_unavailable"
    assert caught.value.request_id == "request-sdk-1"
    assert caught.value.details == {"retryable": False}


def test_workspace_client_gives_runtime_calls_a_timeout_long_enough_for_large_realms(monkeypatch):
    from astrid.sdk import workspace_client

    constructed = {}

    class Generated:
        def __init__(self, endpoint, token=None, **kwargs):
            constructed.update(endpoint=endpoint, token=token, kwargs=kwargs)

    monkeypatch.setattr("astrid.sdk.workspace_client.GeneratedWorkspaceClient", Generated)
    WorkspaceClient("http://127.0.0.1:8443", "token")

    assert constructed["kwargs"] == {"timeout": workspace_client.WORKSPACE_REQUEST_TIMEOUT_SECONDS}
    assert workspace_client.WORKSPACE_REQUEST_TIMEOUT_SECONDS == 300.0


def test_timeline_document_create_is_retired_and_never_reaches_generated_route():
    class Client:
        def create_timeline_document(self, *args, **kwargs):
            raise AssertionError("retired timeline create must not reach the generated route")

    remote = RemoteTimelines(Client())
    result = remote.create(
        project="project-1",
        config={"tracks": []},
        registry={"assets": {}},
        slug="main",
        idempotency_key="timeline-1",
    )
    assert not result.ok
    assert result.error.code == "retired_route"
    assert result.error.details["replacement"] == "timelines.inspect/open_composition"
    assert result.idempotency_key == "timeline-1"
    assert not hasattr(remote, "create_timeline")


def test_remote_has_no_retired_alias_surface():
    names = {
        "selected_project_ref",
        "read_events",
        "subscribe_events",
        "render",
        "close",
    }
    assert names.isdisjoint(vars(RemoteAstridClient))


def test_top_level_sdk_has_no_event_aliases():
    import astrid

    assert not hasattr(astrid, "read_events")
    assert not hasattr(astrid, "subscribe_events")


def test_launcher_uses_installed_command_and_explicit_manifest(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "source-manifest.json"
    manifest.write_text(json.dumps({"profile": "astrid"}), encoding="utf-8")
    monkeypatch.setenv("ASTRID_RUNTIME_SOURCE_MANIFEST", str(manifest))
    monkeypatch.setenv("ASTRID_RUNTIME_LAUNCHER", "/usr/local/bin/astrid-runtime")
    seen: dict[str, object] = {}

    def run(command, **kwargs):
        seen["command"] = command
        assert "env" not in kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps({"status": "started", "realm_id": "realm", "endpoint": "http://127.0.0.1:1", "actor_id": "actor"}),
            "",
        )

    monkeypatch.setattr(autobootstrap.subprocess, "run", run)
    result = autobootstrap.ensure_runtime()
    assert result["status"] == "started"
    assert seen["command"][:5] == ["/usr/local/bin/astrid-runtime", "up", "--profile", "astrid", "--source-manifest"]


def test_public_open_signature_has_no_auto_bootstrap_keyword():
    assert "auto_bootstrap" not in inspect.signature(AstridClient.open).parameters


def test_sdk_boundary_has_no_checkout_or_dynamic_import_injection():
    root = Path(__file__).resolve().parents[2] / "astrid" / "sdk"
    workspace_source = (root / "workspace_client.py").read_text(encoding="utf-8")
    bootstrap_source = (root / "autobootstrap.py").read_text(encoding="utf-8")
    assert "sys.path" not in workspace_source
    assert "importlib" not in workspace_source
    assert "ASTRID_RUNTIME_CHECKOUT" not in workspace_source
    # The launcher owns source selection; the SDK only carries its reported field through.
    assert "source_checkout" in bootstrap_source
    assert "result[field] = value[field]" in bootstrap_source
    assert "PYTHONPATH" not in bootstrap_source
