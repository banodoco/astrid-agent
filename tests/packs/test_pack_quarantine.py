"""Pack quarantine reporting, capability approval, census lock, and typed unavailable (WP-Q).

Ported from the freeze. Not carried: lenient discovery (valid neighbours still load) and
the generic host skipping a quarantined pack's executors; the combined port keeps source
admission strict and exposes quarantine only through the non-strict ``scan_packs`` seam.

An invalid source pack is quarantined: excluded from discovery, reported with its
manifest error and fix, and its capabilities return ``unavailable`` instead of
``not found``. ``_core`` and runtime-required packs still fail closed. Capabilities
with no approved matrix row are unavailable per capability, not a whole-host failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.execution import capability_ledger
from astrid.core.execution.generic_host import (
    GenericPackHost,
    HostError,
    _preflight_unavailable_reason,
    _unapproved_reason,
)
from astrid.core.pack import PackValidationError, discover_packs
from astrid.core.pack import loader
from astrid.core.pack.loader import pack_quarantine_section, quarantined_pack_for_capability
from astrid.core.pack.loader import scan_packs as _scan_packs


def scan_packs(root):
    # Codex keeps admission strict; the freeze's quarantining scan is the explicit non-strict seam.
    return _scan_packs(root, strict=False)

from astrid.core.pack.discovery import discover_pack_metadata
from astrid.core.pack.validate import validate_pack
from astrid.sdk.discovery import _resolve_capability
from astrid.sdk.exceptions import CapabilityNotFoundError, CapabilityUnavailableError

GOOD = "schema_version: 2\nid: goodpack\nname: Good\nversion: 1.0.0\ncapabilities: [echo]\n"
BAD = (
    "schema_version: 2\nid: badpack\nname: Bad\nversion: 1.0.0\n"
    "keywords: [pixel, 'pixel art']\ncapabilities: [snap]\n"
)


def _pack(root: Path, folder: str, text: str) -> Path:
    pack_dir = root / folder
    pack_dir.mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(text, encoding="utf-8")
    return pack_dir


def _executor(root: Path, executor_id: str) -> None:
    root.mkdir(parents=True)
    (root / "executor.yaml").write_text(
        json.dumps({
            "schema_version": 1,
            "id": executor_id,
            "name": executor_id,
            "kind": "external",
            "version": "1.0",
            "command": {"argv": ["{python_exec}", "-c", "pass"]},
            "outputs": [{"name": "answer", "type": "file", "path_template": "{out}/answer.txt", "artifact_type": "text/plain"}],
            "metadata": {"resource_keys": ["cpu"], "estimated_scratch_bytes": 1},
        }),
        encoding="utf-8",
    )


def _matrix_row(capability_id: str) -> dict:
    return {
        "id": capability_id,
        "disposition": "optional",
        "evidence_reason": "test fixture",
        "adapter_family": "cpu",
        "resource_keys": ["cpu"],
        "required_env": [],
        "required_binaries": [],
        "required_packages": [],
    }


class _EmptyRegistry:
    alias_resolver = None

    def list(self):
        return ()

    def get(self, key):
        raise KeyError(key)

    def as_mapping(self):
        return {}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    loader._LOGGED_QUARANTINES.clear()


@pytest.fixture
def roots(tmp_path) -> Path:
    root = tmp_path / "packs"
    _pack(root, "goodpack", GOOD)
    _pack(root, "badpack", BAD)
    return root


def test_invalid_keyword_quarantines_only_that_pack(roots):
    scan = scan_packs(roots)

    assert [pack.id for pack in scan.packs] == ["goodpack"]
    [record] = scan.quarantined
    assert record.pack_id == "badpack"
    assert record.manifest_path == roots / "badpack" / "pack.yaml"
    assert "keywords.1" in record.error and "'pixel art'" in record.error
    assert record.fix == f"run the pack validator: python3 -m astrid.core.pack.cli validate {roots / 'badpack'}"
    # Codex's port keeps source admission strict: discovery never skips an invalid pack.
    with pytest.raises(PackValidationError, match="keywords.1"):
        discover_packs(roots)


def test_core_manifest_is_fail_closed(tmp_path):
    root = tmp_path / "packs"
    _pack(root, "_core", BAD)

    with pytest.raises(PackValidationError, match="_core"):
        scan_packs(root)
    with pytest.raises(PackValidationError):
        discover_packs(root)


def test_invoking_a_quarantined_capability_is_typed_unavailable(roots, monkeypatch):
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(roots))

    with pytest.raises(CapabilityUnavailableError) as info:
        _resolve_capability(
            "badpack.snap",
            kind=None,
            element_kind=None,
            executor_registry=_EmptyRegistry(),
            orchestrator_registry=_EmptyRegistry(),
            element_registry=None,
        )

    details = info.value.details
    assert details["state"] == "unavailable"
    assert details["reason"] == "pack_quarantined"
    assert details["pack_id"] == "badpack"
    assert "keywords.1" in details["error"]
    assert details["fix"].startswith("run the pack validator:")


def test_unknown_capability_is_still_not_found(roots, monkeypatch):
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(roots))

    with pytest.raises(CapabilityNotFoundError):
        _resolve_capability(
            "nothing.here",
            kind=None,
            element_kind=None,
            executor_registry=_EmptyRegistry(),
            orchestrator_registry=_EmptyRegistry(),
            element_registry=None,
        )


def test_quarantine_owner_matches_declared_bare_labels(roots):
    assert quarantined_pack_for_capability("snap", roots=(roots,)).pack_id == "badpack"
    assert quarantined_pack_for_capability("badpack.snap", roots=(roots,)).pack_id == "badpack"
    assert quarantined_pack_for_capability("goodpack.echo", roots=(roots,)) is None


def test_doctor_section_lists_quarantine_with_error_and_fix(roots, monkeypatch):
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(roots))

    section = pack_quarantine_section()

    assert section["fail_closed_error"] is None
    [record] = [row for row in section["quarantined"] if row["pack_id"] == "badpack"]
    assert record["state"] == "quarantined"
    assert "keywords.1" in record["error"]
    assert record["fix"].startswith("run the pack validator:")


def test_doctor_section_reports_fail_closed_error_without_raising(tmp_path, monkeypatch):
    root = tmp_path / "packs"
    _pack(root, "_core", BAD)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(root))

    section = pack_quarantine_section()

    assert section["quarantined"] == []
    assert "_core" in section["fail_closed_error"]


def test_unapproved_capability_is_unavailable_without_stopping_host(tmp_path):
    root = tmp_path / "packs"
    _pack(root, "goodpack", GOOD)
    _executor(root / "goodpack" / "executors" / "echo", "goodpack.echo")
    _executor(root / "goodpack" / "executors" / "extra", "goodpack.extra")
    matrix = tmp_path / "config" / "matrix.json"
    matrix.parent.mkdir()
    matrix.write_text(
        json.dumps({
            "schema_version": 1,
            "capabilities": [_matrix_row("goodpack.echo"), _matrix_row("ghost.gone")],
        }),
        encoding="utf-8",
    )

    host = GenericPackHost(pack_roots=[root], capability_matrix=matrix)
    records = {record.id: record for record in host.discover()}

    assert set(records) == {"goodpack.echo", "goodpack.extra"}
    assert records["goodpack.echo"].approval_reason is None
    assert "goodpack.extra" in records["goodpack.extra"].approval_reason
    assert host.stale_matrix_entries == ("ghost.gone",)
    [extra] = host.preflight("goodpack.extra")
    assert extra.ready is False
    assert extra.preflight["approval"]["ok"] is False
    assert "no approved capability-matrix row" in _preflight_unavailable_reason(extra)
    with pytest.raises(HostError, match="no approved capability-matrix row"):
        host.admit("executor", "goodpack.extra")


def test_census_check_classifies_ok_degraded_and_drift():
    check = capability_ledger._census_check

    assert check({"a", "b"}, ["a", "b"], set())["status"] == "ok"
    drift = check({"a", "new"}, ["a"], set())
    assert drift["status"] == "drift" and drift["added"] == ["new"] and drift["complete"] is False
    degraded = check({"a"}, ["a", "b"], {"b"})
    assert degraded["status"] == "degraded" and degraded["quarantined"] == ["b"] and degraded["complete"] is True
    missing = check({"a"}, ["a", "b"], set())
    assert missing["status"] == "drift" and missing["missing"] == ["b"]
    assert check({"a"}, None, set())["added"] == ["a"]


def test_unapproved_capability_guidance_does_not_point_into_install_versions():
    reason = _unapproved_reason(
        "goodpack.extra", Path(r"C:\Astrid\versions\1.2.3\config\astrid-beta-capabilities.json")
    )

    assert "source capability matrix" in reason
    assert "versions\\" not in reason
    assert "restart the host" in reason


def test_approve_regenerates_lock_and_keeps_quarantined_approvals(tmp_path):
    checkout = tmp_path / "checkout"
    _pack(checkout / "astrid" / "packs", "goodpack", GOOD)
    _pack(checkout / "astrid" / "packs", "badpack", BAD)
    lock = checkout / capability_ledger.CENSUS_LOCK_RELATIVE
    lock.parent.mkdir(parents=True)
    lock.write_text(
        json.dumps({
            "schema_version": 1,
            "source_labels": ["badpack.snap"],
            "historical_source_labels": [],
            "executor_inventory": [],
        }),
        encoding="utf-8",
    )

    result = capability_ledger.write_census_lock(checkout)

    written = json.loads(lock.read_text(encoding="utf-8"))
    assert written["source_labels"] == ["badpack.snap", "goodpack.echo"]
    assert result["diff"]["source_labels"] == {"added": ["goodpack.echo"], "removed": []}


def test_host_bootstrap_surfaces_pack_host_bootstrap_error_line(tmp_path):
    from astrid.sdk.host_bootstrap import _host_failure_cause

    log = tmp_path / "generic-host.log"
    log.write_text(
        "starting\nTraceback (most recent call last):\n"
        "astrid.sdk.host_bootstrap.PackHostBootstrapError: capability matrix does not exactly cover\n",
        encoding="utf-8",
    )

    assert _host_failure_cause(log) == (
        "astrid.sdk.host_bootstrap.PackHostBootstrapError: capability matrix does not exactly cover"
    )
    assert _host_failure_cause(tmp_path / "missing.log") == ""


def test_host_bootstrap_ignores_arbitrary_tracebacks_and_keeps_safe_startup_context(tmp_path):
    from astrid.sdk.host_bootstrap import _host_failure_cause

    log = tmp_path / "generic-host.log"
    safe_context = f"field=capability_matrix path={tmp_path / 'matrix.json'}"
    log.write_text(
        f"generic host CLI exiting after failure: error_type=HostError safe_context={safe_context}\n"
        "Traceback (most recent call last):\n"
        "RuntimeError: private-token-should-not-appear\n"
        "ValueError: RuntimeError: generic host CLI exiting after failure: safe_context=spoofed-secret\n",
        encoding="utf-8",
    )

    assert _host_failure_cause(log) == safe_context
    assert "private-token-should-not-appear" not in _host_failure_cause(log)
    assert "spoofed-secret" not in _host_failure_cause(log)

    log.write_text(
        f"generic host CLI exiting after failure: error_type=HostError safe_context={safe_context}\n"
        "generic host CLI exiting after failure: error_type=RuntimeError\n"
        "RuntimeError: newest failure has no marked context\n",
        encoding="utf-8",
    )
    assert _host_failure_cause(log) == ""
