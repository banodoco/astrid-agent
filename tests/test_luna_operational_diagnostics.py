from __future__ import annotations

from pathlib import Path

import pytest

from astrid import omp_agent
from astrid.core.util.credential_store import list_credential_names, main as credential_main
from astrid.runtime_cli import RuntimeCLIError, validate_started_workspace
from astrid.sdk.client import AstridClient, RUNTIME_PAIRING_FIX
from astrid.sdk.exceptions import CapabilityUnavailableError, ServiceUnavailableError
from astrid_runtime_client.contract_metadata import COMPONENT_MANIFEST_SHA256, SCHEMA_DIGEST


def test_quarantined_capability_error_exposes_stable_state() -> None:
    error = CapabilityUnavailableError("pack is quarantined", details={"pack_id": "badpack"})

    assert error.state == "unavailable"
    assert error.reason == "pack_quarantined"
    assert error.details == {"pack_id": "badpack"}


def test_started_workspace_accepts_omitted_realm_root_but_fences_support_paths(tmp_path: Path) -> None:
    support = tmp_path / "support"
    realm = tmp_path / "realm"
    validate_started_workspace(
        {"realm_id": "realm-1"},
        expected_realm_id="realm-1",
        expected_realm_root=realm,
        expected_support_root=support,
    )

    with pytest.raises(RuntimeCLIError, match="outside the selected support root"):
        validate_started_workspace(
            {"realm_id": "realm-1", "credential_file": str(tmp_path / "elsewhere" / "credential")},
            expected_realm_id="realm-1",
            expected_realm_root=realm,
            expected_support_root=support,
        )


def test_storage_hint_names_non_destructive_alternative(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ASTRID_RUNTIME_DATA_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy = tmp_path / "Library" / "Application Support" / "Astrid Runtime" / "runtime" / "catalog.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"selected_realm_id":"realm-1"}', encoding="utf-8")

    from astrid.sdk.storage_root import ensure_no_unmigrated_runtime

    with pytest.raises(ValueError) as caught:
        ensure_no_unmigrated_runtime(tmp_path / "new" / ".astrid-data")
    message = str(caught.value)
    assert "astrid-upgrade" in message
    assert "ASTRID_RUNTIME_DATA_ROOT" in message
    assert "is not touched" in message


def test_credential_listing_is_names_only_and_does_not_create_store(tmp_path: Path, monkeypatch, capsys) -> None:
    target = tmp_path / "astrid.env"
    target.write_text("RUNPOD_API_KEY='never-print-this'\nEMPTY=\nOPENAI_API_KEY=another-secret\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_ENV_FILE", str(target))

    assert list_credential_names() == ["OPENAI_API_KEY", "RUNPOD_API_KEY"]
    assert credential_main(["list"]) == 0
    output = capsys.readouterr()
    assert output.out == "OPENAI_API_KEY\nRUNPOD_API_KEY\n"
    assert "never-print-this" not in output.out + output.err
    assert "another-secret" not in output.out + output.err

    missing = tmp_path / "missing" / "astrid.env"
    monkeypatch.setenv("ASTRID_ENV_FILE", str(missing))
    assert credential_main(["list"]) == 0
    assert not missing.exists()


def test_missing_named_launcher_guidance_does_not_recommend_stock_omp(monkeypatch, capsys) -> None:
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: None)
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent.shutil, "which", lambda name: "/tmp/omp" if name == "omp" else None)

    assert omp_agent.main(["--agent", "scout", "say hello"]) == 1
    message = capsys.readouterr().err
    assert "@oh-my-pi/pi-coding-agent" in message
    assert "ASTRID_AGENT_LAUNCHER" in message
    assert "no `agent run` command" in message


def test_client_reports_redacted_schema_pairing_facts(monkeypatch) -> None:
    old_digest = "sha256:" + "0" * 64

    class Workspace:
        def health(self):
            return {
                "status": "ok",
                "protocol": "workspace.v1",
                "schema_digest": old_digest,
                "runtime_epoch": 1,
                "runtime_instance_id": "instance-1",
                "runtime_session_id": "session-1",
            }

    monkeypatch.setattr("astrid.sdk.workspace_client.WorkspaceClient", lambda *_args: Workspace())
    with pytest.raises(ServiceUnavailableError) as caught:
        AstridClient.open(
            endpoint="http://127.0.0.1:1",
            credential="token",
            realm_id="realm-1",
            actor_id="actor-1",
            client_name="test",
            client_version="1",
            protocol_version="workspace.v1",
        )

    details = caught.value.details
    assert details["actual_schema_digest"] == old_digest
    assert details["expected_schema_digest"] == SCHEMA_DIGEST
    assert details["expected_component_manifest"] == COMPONENT_MANIFEST_SHA256
    assert details["pairing_hint"] == RUNTIME_PAIRING_FIX
    assert details["operation"] == "health"
    assert details["cause_class"] == "WorkspaceClientError"
    assert "token" not in str(caught.value).lower()
