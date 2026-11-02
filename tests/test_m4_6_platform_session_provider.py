"""The fixed direct provider accepts only an exact live direct context."""

from __future__ import annotations

import inspect
import hashlib
import os
import sys
from pathlib import Path

import pytest

from scripts import native_service_adapter as native
from scripts.m4_6_platform_session_provider import (
    PLATFORM_SESSION_PROVIDER,
    TrustedPlatformContextUnavailable,
)
from tests.failure_mode_fixtures import PROVISIONAL_CANDIDATE


def test_fixed_provider_has_only_the_contract_open_signature_and_fails_closed() -> None:
    signature = inspect.signature(PLATFORM_SESSION_PROVIDER.open_authenticated_session)
    assert tuple(signature.parameters) == ("binding", "owner")
    assert "trusted_origin" not in signature.parameters

    with pytest.raises(TrustedPlatformContextUnavailable, match="exact live NativeOwner"):
        PLATFORM_SESSION_PROVIDER.open_authenticated_session(object(), object())


def _openable_linux_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if native._platform_name() != "linux":
        pytest.skip("exact live-type provider contract is platform-specific")
    paths = {}
    for name, data in {
        "product-driver": b"product driver\n",
        "service-definition": b"[Service]\nExecStart=/runtime\n",
        "service-executable": b"runtime\n",
    }.items():
        path = tmp_path / name
        path.write_bytes(data)
        paths[name] = path
    checkouts = []
    for name, sha in PROVISIONAL_CANDIDATE.items():
        if name == "manifest_sha256":
            continue
        path = tmp_path / f"checkout-{name}"
        path.mkdir()
        checkouts.append((name, sha, str(path)))
    owner = native.NativeOwner(
        os_name="linux",
        canonical_definition_path=paths["service-definition"],
        runner=native.SubprocessCommandRunner(),
        process_probe=native.LiveProcessProbe(native.SubprocessCommandRunner(), None),
        data_probe=native.DataProbe(tmp_path),
        durable_journal_probe=native.DurableJournalProbe(tmp_path),
        powershell=None,
    )
    owner.principal = "runner"
    owner.target = native.SYSTEMD_UNIT
    monkeypatch.setattr(owner, "assert_absent", lambda: None)
    executable = Path(sys.executable).resolve(strict=True)
    binding = native.PlatformSessionBinding(
        slot_id="direct-linux", mode="direct", owner_kind="native-service", os_name="linux",
        runner_image="ubuntu-24.04", shell="sh", runner_os="Linux", image_os="ubuntu24",
        os_build="Linux-test", repository_claim="banodoco/astrid-agent", workflow_claim="journey",
        job_claim="direct-linux", run_id_claim="1", run_attempt_claim="1",
        candidate=tuple(sorted(PROVISIONAL_CANDIDATE.items())), checkouts=tuple(sorted(checkouts)),
        collector_pid=os.getpid(), collector_executable=str(executable),
        collector_executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        product_protocol="astrid-m4-6-product-driver/v1", product_command=(str(paths["product-driver"]),),
        product_command_cwd=str(tmp_path),
        artifacts=tuple(
            native.PlatformArtifactBinding(name, str(path), hashlib.sha256(path.read_bytes()).hexdigest())
            for name, path in sorted(paths.items())
        ),
        owner_principal="runner", owner_target=native.SYSTEMD_UNIT,
    )
    plan = native.RemovalPlan(
        operation="product-uninstall", keep_data=True,
        remove_paths=(str(paths["service-definition"]), str(paths["service-executable"])),
        preserve_paths=(str((tmp_path / "data").resolve()),),
        expected_postconditions=(
            "owner-target-absent", "all-authorized-incarnations-absent",
            "no-owned-action-processes", "preserved-data-identity-unchanged",
        ),
    )
    (tmp_path / "data").mkdir()
    custody = native.CustodyBinding(
        slot_id="direct-linux", owner_kind="native-service", principal="runner",
        target=native.SYSTEMD_UNIT, definition_path=str(paths["service-definition"]),
        definition_sha256=hashlib.sha256(paths["service-definition"].read_bytes()).hexdigest(),
        action=(str(paths["service-executable"]),),
        action_sha256=native._sha256_bytes(native._canonical([str(paths["service-executable"])])),
        immutable_actions=(
            next(row for row in binding.artifacts if row.role == "service-definition"),
            next(row for row in binding.artifacts if row.role == "service-executable"),
        ),
        resolved_action_identity_sha256=native._sha256_bytes(
            native._canonical([str(paths["service-executable"])])
        ),
        removal_plan=plan,
    )
    return binding, owner, custody, paths


def test_direct_provider_binds_exact_live_context_once_and_never_returns_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, owner, custody, _paths = _openable_linux_context(tmp_path, monkeypatch)
    session = PLATFORM_SESSION_PROVIDER.open_authenticated_session(binding, owner)
    assert session.bind_exact_custody(custody, owner) is None
    with pytest.raises(TrustedPlatformContextUnavailable, match="custody-bound, not opened"):
        session.bind_exact_custody(custody, owner)
    with pytest.raises(TrustedPlatformContextUnavailable, match="foreign"):
        session.authorize_exact_removal(object(), object())
    assert session.abort("controlled") is None
    with pytest.raises(TrustedPlatformContextUnavailable, match="already closed"):
        session.abort("again")


def test_direct_provider_refuses_changed_immutable_artifact_before_custody_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding, owner, custody, paths = _openable_linux_context(tmp_path, monkeypatch)
    session = PLATFORM_SESSION_PROVIDER.open_authenticated_session(binding, owner)
    paths["service-executable"].write_bytes(b"changed")
    with pytest.raises(TrustedPlatformContextUnavailable, match="changed, is unsafe, or is absent"):
        session.bind_exact_custody(custody, owner)
