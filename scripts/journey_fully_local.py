#!/usr/bin/env python3
"""Provisional, receipt-first contract for the fully-local editor update journey.

This module deliberately owns no Pinokio, Runtime, Git, or process-control code.
``FullyLocalAdapter`` is the seam for the later Linux adapter.  The included
adapter is a deterministic source/fake fixture: it writes no checkout, starts
no service, and must never be presented as a Linux receipt.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Protocol

SCHEMA_VERSION = 1
RECEIPT_KIND = "m4-4b-fully-local-editor-journey"
MODE_BYTES = '{"schema_version":1,"mode":"local"}'
PINNED_TUPLE = {
    "base": "3cc84be5d8c565f1a4a500dcf713478498e34885",
    "runtime": "a3e3f2ed95d6363e7f5b463b521da94221cf6f00",
    "app": "33f1de3f33dfb1e9799ca745c5c3b8437173e06e",
    "pinokio": "c5b42701641889ecb916c60cde09a717f83d62ff",
}

ROUTE_ORDER = (
    "runtime_preflight_stop",
    "installer_lock_release",
    "pinokio_launcher_fast_forward",
    "local_port_preflight",
    "editor_checkout_validation",
    "editor_fast_forward_pull",
    "installer_update_manifest",
    "ref_build_boundary",
    "temporary_runtime_healthy",
    "editor_build",
    "temporary_runtime_down",
    "restore_editor_checkout",
    "restore_runtime_state",
    "post_restore_start",
    "post_restore_status",
    "post_restore_workspace",
    "post_restore_http",
    "post_restore_ready",
    "controlled_stop",
)


class ReceiptValidationError(ValueError):
    """Raised when a journey receipt cannot prove the local contract."""


class FullyLocalAdapter(Protocol):
    """Small adapter surface for a later real Pinokio/Linux implementation."""

    evidence_class: str

    def collect_receipt(self, *, failure_step: str) -> Mapping[str, Any]:
        """Exercise the route and return a receipt without hiding its evidence."""


def _missing(path: str) -> ReceiptValidationError:
    return ReceiptValidationError(f"missing or ambiguous receipt field: {path}")


def _field(value: Any, path: str, kind: type | tuple[type, ...] | None = None) -> Any:
    if value is None:
        raise _missing(path)
    if kind is not None and not isinstance(value, kind):
        raise ReceiptValidationError(f"{path}: expected {kind}, got {type(value).__name__}")
    return value


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    return _field(value, path, Mapping)


def _equal(left: Any, right: Any, path: str) -> None:
    if left != right:
        raise ReceiptValidationError(f"{path}: invariant changed")


def _validate_provenance(receipt: Mapping[str, Any], require_linux: bool) -> None:
    evidence_class = _field(receipt.get("evidence_class"), "evidence_class", str)
    platform = _mapping(receipt.get("platform"), "platform")
    if evidence_class not in {"source_fake", "linux_receipt"}:
        raise ReceiptValidationError("evidence_class must be source_fake or linux_receipt")
    if evidence_class == "source_fake":
        if platform.get("linux_receipt") is not False:
            raise ReceiptValidationError("source/fake evidence must explicitly set linux_receipt=false")
        if require_linux:
            raise ReceiptValidationError("source/fake evidence cannot satisfy a Linux receipt gate")
        _equal(platform.get("kind"), "source_fake", "platform.kind")
        _field(receipt.get("evidence_disclaimer"), "evidence_disclaimer", str)
        if "linux" in receipt["evidence_disclaimer"].lower() and "not" not in receipt["evidence_disclaimer"].lower():
            raise ReceiptValidationError("source/fake disclaimer must not imply Linux evidence")
        return
    if not require_linux:
        raise ReceiptValidationError("Linux evidence requires the explicit Linux receipt gate")
    if platform.get("linux_receipt") is not True or platform.get("kind") != "linux":
        raise ReceiptValidationError("linux_receipt evidence must identify a real Linux platform")
    for path in ("captured_at", "command", "pinokio_version", "runner_identity"):
        _field(receipt.get(path), path)


def _validate_route(receipt: Mapping[str, Any]) -> None:
    route = _mapping(receipt.get("route"), "route")
    _equal(route.get("route_id"), "pinokio.update.local-editor", "route.route_id")
    _equal(route.get("mode"), "local", "route.mode")
    if route.get("backend") is not False or route.get("foreground_service") is not False:
        raise ReceiptValidationError("backend and foreground-service routes are forbidden")
    for key in ("pre_update_sha", "target_sha", "journal_id", "temporary_runtime_id", "controller_id"):
        _field(route.get(key), f"route.{key}", str)
    events = _field(route.get("events"), "route.events", list)
    names = [event.get("name") if isinstance(event, Mapping) else None for event in events]
    if names != list(ROUTE_ORDER):
        raise ReceiptValidationError(f"route order mismatch: expected {list(ROUTE_ORDER)!r}, got {names!r}")
    for sequence, event in enumerate(events, start=1):
        if not isinstance(event, Mapping) or type(event.get("sequence")) is not int or event.get("sequence") != sequence:
            raise ReceiptValidationError("route events must have unambiguous contiguous sequence numbers")
        _field(event.get("status"), f"route.events[{sequence}].status", str)
    ready_positions = [index for index, event in enumerate(events) if event.get("ready") is True]
    local_set_positions = [index for index, event in enumerate(events) if event.get("local_set") is True]
    if ready_positions != [ROUTE_ORDER.index("post_restore_ready")]:
        raise ReceiptValidationError("ready was missing or published before post-restore checks")
    if local_set_positions != [ROUTE_ORDER.index("post_restore_ready")]:
        raise ReceiptValidationError("local.set was missing or published before post-restore checks")
    if any(event.get("ready") not in (None, False, True) for event in events):
        raise ReceiptValidationError("ready fields must be explicit booleans when present")
    if any(event.get("local_set") not in (None, False, True) for event in events):
        raise ReceiptValidationError("local_set fields must be explicit booleans when present")


def _validate_failure(receipt: Mapping[str, Any]) -> None:
    failure = _mapping(receipt.get("failure"), "failure")
    if failure.get("injected") is not True or failure.get("step") != "editor_build":
        raise ReceiptValidationError("the build failure must be deterministic and injected at editor_build")
    command = _field(failure.get("command"), "failure.command", list)
    if "--build" not in command or "local" not in command:
        raise ReceiptValidationError("failure.command is not the local editor build step")
    status = _field(failure.get("exit_status"), "failure.exit_status", int)
    if type(status) is not int or status <= 0:
        raise ReceiptValidationError("failure.exit_status must be nonzero")
    if failure.get("pinokio_break") is not True or not failure.get("ASTRID_PINOKIO_FAILURE"):
        raise ReceiptValidationError("Pinokio break evidence is missing")
    _field(failure.get("raw_action_log"), "failure.raw_action_log", str)
    if failure.get("ready_event_seen") is not False or failure.get("local_set_seen") is not False:
        raise ReceiptValidationError("failed build must not publish ready or local.set")
    boundary = _mapping(receipt.get("boundary"), "boundary")
    if boundary.get("mode_published") is not False or boundary.get("successful_build") is not False:
        raise ReceiptValidationError("ref/build boundary was not captured before mode/build success")
    if boundary.get("before", {}).get("checkout_sha") == boundary.get("after", {}).get("checkout_sha"):
        raise ReceiptValidationError("boundary does not prove the editor ref advanced")


def _validate_checkout(receipt: Mapping[str, Any]) -> None:
    checkout = _mapping(receipt.get("checkout"), "checkout")
    before = _mapping(checkout.get("before"), "checkout.before")
    after = _mapping(checkout.get("after"), "checkout.after")
    for name, snapshot in (("before", before), ("after", after)):
        for key in ("head", "branch", "origin", "porcelain"):
            _field(snapshot.get(key), f"checkout.{name}.{key}", str)
    _equal(after, before, "checkout.after")
    if checkout.get("changed_during_update") is not True:
        raise ReceiptValidationError("checkout receipt does not prove an update occurred")
    if checkout.get("restored") is not True:
        raise ReceiptValidationError("editor checkout was not restored")
    if checkout.get("restoration_source") not in {"updater", "updater_journal", "fake_updater_restore"}:
        raise ReceiptValidationError("checkout restoration is not attributed to the updater")
    recovery = _mapping(checkout.get("recovery"), "checkout.recovery")
    if recovery.get("journal_state") not in {"rolled_back", "recovered"}:
        raise ReceiptValidationError("update journal is not durably recovered")
    _field(recovery.get("journal_id"), "checkout.recovery.journal_id", str)
    _equal(recovery.get("restored_head"), before["head"], "checkout.recovery.restored_head")


def _validate_install_and_mode(receipt: Mapping[str, Any]) -> None:
    installed = _mapping(receipt.get("installed_runtime"), "installed_runtime")
    before = _mapping(installed.get("before"), "installed_runtime.before")
    after = _mapping(installed.get("after"), "installed_runtime.after")
    _equal(after, before, "installed_runtime.after")
    for key in ("install", "current", "selected_version", "managed_by", "owner", "profile_source_manifest", "data_root"):
        _field(after.get(key), f"installed_runtime.after.{key}")
    _equal(after.get("managed_by"), "pinokio", "installed_runtime.after.managed_by")
    _equal(after.get("owner"), "haus.astrid.runtime", "installed_runtime.after.owner")
    mode = _mapping(receipt.get("mode"), "mode")
    _equal(mode.get("before_bytes"), MODE_BYTES, "mode.before_bytes")
    _equal(mode.get("after_bytes"), MODE_BYTES, "mode.after_bytes")
    _equal(mode.get("before"), {"schema_version": 1, "mode": "local"}, "mode.before")
    _equal(mode.get("after"), {"schema_version": 1, "mode": "local"}, "mode.after")
    if mode.get("unchanged") is not True:
        raise ReceiptValidationError("local mode was not proven unchanged")


def _validate_data(receipt: Mapping[str, Any]) -> None:
    data = _mapping(receipt.get("data"), "data")
    before = _mapping(data.get("before"), "data.before")
    after = _mapping(data.get("after"), "data.after")
    for key in ("realpath", "selected_realm", "catalog_digest", "inventory_digest", "inventory"):
        _field(before.get(key), f"data.before.{key}")
        _field(after.get(key), f"data.after.{key}")
    _equal(after, before, "data.after")
    if data.get("unchanged") is not True or data.get("contains_secrets") is not False:
        raise ReceiptValidationError("data inventory is missing unchanged/non-secret proof")


def _validate_cleanup(receipt: Mapping[str, Any]) -> None:
    cleanup = _mapping(receipt.get("cleanup"), "cleanup")
    lock = _mapping(cleanup.get("lock"), "cleanup.lock")
    if lock.get("released") is not True or lock.get("probe_exit_status") != 0:
        raise ReceiptValidationError("installer lock was not released and probed successfully")
    if lock.get("removed") is not False:
        raise ReceiptValidationError("lock deletion is not a valid release proof")
    ports = _mapping(cleanup.get("ports"), "cleanup.ports")
    for port in ("17340", "5180"):
        state = _mapping(ports.get(port), f"cleanup.ports.{port}")
        if state.get("occupied") is not False or state.get("owner") != "none":
            raise ReceiptValidationError(f"owned port {port} was left occupied")
    processes = _mapping(cleanup.get("processes"), "cleanup.processes")
    if (processes.get("no_live_owned_descendants") is not True
            or processes.get("after") != [] or processes.get("forced_tree_kill") is not False):
        raise ReceiptValidationError("owned Runtime/controller/build descendants were not cleanly drained")


def _validate_readiness(receipt: Mapping[str, Any]) -> None:
    installed = _mapping(receipt["installed_runtime"]["after"], "installed_runtime.after")
    readiness = _mapping(receipt.get("readiness"), "readiness")
    status = _mapping(readiness.get("status"), "readiness.status")
    if status.get("exit_status") != 0 or status.get("support_healthy") is not True:
        raise ReceiptValidationError("post-restore Runtime status is not healthy")
    if status.get("health_status") != "ok" or status.get("stale_discovery") is not False:
        raise ReceiptValidationError("post-restore status has stale or unhealthy Runtime evidence")
    _equal(status.get("realm_id"), installed.get("selected_realm"), "readiness.status.realm_id")
    workspace = _mapping(readiness.get("workspace"), "readiness.workspace")
    if workspace.get("http_status") != 200 or workspace.get("schemaVersion") != 1:
        raise ReceiptValidationError("workspace readiness response is missing or invalid")
    _equal(workspace.get("realm_id"), status.get("realm_id"), "readiness.workspace.realm_id")
    _equal(workspace.get("runtime_instance_id"), status.get("runtime_instance_id"),
           "readiness.workspace.runtime_instance_id")
    http = _mapping(readiness.get("http"), "readiness.http")
    if http.get("status") != 200 or http.get("url") != "http://127.0.0.1:5180/tools/video-editor":
        raise ReceiptValidationError("local editor HTTP readiness was not proven")
    ready = _mapping(readiness.get("ready"), "readiness.ready")
    if (ready.get("published_url") != http["url"] or ready.get("hosted_door") is not False
            or ready.get("checks_complete") is not True):
        raise ReceiptValidationError("Pinokio readiness was not local and post-check")
    stop = _mapping(readiness.get("controlled_stop"), "readiness.controlled_stop")
    if stop.get("status") != "stopped" or stop.get("exit_status") != 0 or stop.get("lock_released") is not True:
        raise ReceiptValidationError("post-restore Start did not use controlled stop cleanup")


def validate_receipt(receipt: Mapping[str, Any], *, require_linux: bool = False) -> None:
    """Validate the complete contract, raising on the first unsafe ambiguity."""
    if not isinstance(receipt, Mapping):
        raise ReceiptValidationError("receipt must be an object")
    _equal(receipt.get("schema_version"), SCHEMA_VERSION, "schema_version")
    _equal(receipt.get("receipt_kind"), RECEIPT_KIND, "receipt_kind")
    _equal(receipt.get("status"), "pass", "status")
    candidate = _mapping(receipt.get("candidate"), "candidate")
    _equal(dict(candidate), PINNED_TUPLE, "candidate")
    _validate_provenance(receipt, require_linux)
    _validate_route(receipt)
    _validate_failure(receipt)
    _validate_checkout(receipt)
    _validate_install_and_mode(receipt)
    _validate_data(receipt)
    _validate_cleanup(receipt)
    _validate_readiness(receipt)


class DeterministicFakeAdapter:
    """Source/fake adapter used by tests; it does not touch the real checkout."""

    evidence_class = "source_fake"

    def collect_receipt(self, *, failure_step: str) -> Mapping[str, Any]:
        if failure_step != "editor_build":
            raise ValueError("the provisional journey only injects editor_build failure")
        return _fake_receipt()


def run_journey(adapter: FullyLocalAdapter | None = None, *, require_linux: bool = False) -> dict[str, Any]:
    """Collect and validate one receipt through an explicitly supplied adapter."""
    selected = adapter or DeterministicFakeAdapter()
    receipt = dict(selected.collect_receipt(failure_step="editor_build"))
    if receipt.get("evidence_class") != selected.evidence_class:
        raise ReceiptValidationError("adapter evidence class and receipt evidence class disagree")
    validate_receipt(receipt, require_linux=require_linux)
    return receipt


def _fake_receipt() -> dict[str, Any]:
    """Build deterministic source evidence for the full route/restore contract."""
    baseline_head = "1" * 40
    target_head = "2" * 40
    runtime_identity = {
        "owner": "haus.astrid.runtime",
        "managed_by": "pinokio",
        "profile": "astrid",
        "data_root": "/tmp/m4-4b-fully-local-data",
        "release": 17,
    }
    installed = {
        "install": "install.json",
        "current": "versions/prior",
        "selected_version": "versions/prior/version.json",
        "managed_by": "pinokio",
        "owner": "haus.astrid.runtime",
        "profile_source_manifest": "profile.json:sha256:prior",
        "data_root": runtime_identity["data_root"],
        "selected_realm": "realm-1",
        "runtime_identity": runtime_identity,
    }
    data = {
        "realpath": runtime_identity["data_root"],
        "selected_realm": "realm-1",
        "catalog_digest": "sha256:" + "a" * 64,
        "inventory_digest": "sha256:" + "b" * 64,
        "inventory": [
            {"path": "projects/demo/project.json", "sha256": "c" * 64},
            {"path": "runtime/catalog.json", "sha256": "d" * 64},
        ],
    }
    events = []
    for sequence, name in enumerate(ROUTE_ORDER, start=1):
        event: dict[str, Any] = {"sequence": sequence, "name": name, "status": "pass"}
        if name == "editor_build":
            event.update(status="failed", ready=False, local_set=False, exit_status=23)
        elif name == "post_restore_ready":
            event.update(ready=True, local_set=True)
        elif name in {"post_restore_start", "post_restore_status", "post_restore_workspace", "post_restore_http"}:
            event.update(ready=False, local_set=False)
        events.append(event)
    return {
        "schema_version": SCHEMA_VERSION,
        "receipt_kind": RECEIPT_KIND,
        "status": "pass",
        "evidence_class": "source_fake",
        "evidence_disclaimer": "Source/fake implementation evidence only; not a Linux platform receipt or release acceptance.",
        "platform": {"kind": "source_fake", "linux_receipt": False, "os": "source-test"},
        "candidate": copy.deepcopy(PINNED_TUPLE),
        "route": {
            "route_id": "pinokio.update.local-editor",
            "mode": "local",
            "backend": False,
            "foreground_service": False,
            "pre_update_sha": baseline_head,
            "target_sha": target_head,
            "journal_id": "m4-4b-fake-journal-1",
            "temporary_runtime_id": "runtime-build-1",
            "controller_id": "controller-build-1",
            "events": events,
        },
        "boundary": {
            "before": {"checkout_sha": baseline_head, "mode_bytes": MODE_BYTES},
            "after": {"checkout_sha": target_head, "mode_bytes": MODE_BYTES},
            "mode_published": False,
            "successful_build": False,
        },
        "failure": {
            "injected": True,
            "step": "editor_build",
            "reason": "deterministic test-only build failure",
            "command": ["node", "scripts/dev-local-workspace.mjs", "--build", "session.mjs", "local"],
            "exit_status": 23,
            "ASTRID_PINOKIO_FAILURE": "injected-editor-build-failure",
            "raw_action_log": "source/fake Pinokio action log: injected editor build failure",
            "pinokio_break": True,
            "ready_event_seen": False,
            "local_set_seen": False,
        },
        "checkout": {
            "before": {"head": baseline_head, "branch": "main", "origin": "origin-url", "porcelain": ""},
            "after": {"head": baseline_head, "branch": "main", "origin": "origin-url", "porcelain": ""},
            "changed_during_update": True,
            "restored": True,
            "restoration_source": "fake_updater_restore",
            "recovery": {"journal_id": "m4-4b-fake-journal-1", "journal_state": "rolled_back",
                          "restored_head": baseline_head},
        },
        "installed_runtime": {"before": copy.deepcopy(installed), "after": copy.deepcopy(installed)},
        "mode": {"before_bytes": MODE_BYTES, "after_bytes": MODE_BYTES,
                 "before": {"schema_version": 1, "mode": "local"},
                 "after": {"schema_version": 1, "mode": "local"}, "unchanged": True},
        "data": {"before": copy.deepcopy(data), "after": copy.deepcopy(data),
                 "unchanged": True, "contains_secrets": False},
        "cleanup": {
            "lock": {"released": True, "probe_exit_status": 0, "removed": False},
            "ports": {"17340": {"occupied": False, "owner": "none"},
                      "5180": {"occupied": False, "owner": "none"}},
            "processes": {"before": ["runtime:fake", "controller:fake", "build:fake"],
                          "after": [], "no_live_owned_descendants": True, "forced_tree_kill": False},
        },
        "readiness": {
            "status": {"exit_status": 0, "support_healthy": True, "health_status": "ok",
                        "stale_discovery": False, "runtime_instance_id": "runtime-post-1", "realm_id": "realm-1"},
            "workspace": {"http_status": 200, "schemaVersion": 1, "realm_id": "realm-1",
                          "runtime_instance_id": "runtime-post-1"},
            "http": {"url": "http://127.0.0.1:5180/tools/video-editor", "status": 200},
            "ready": {"published_url": "http://127.0.0.1:5180/tools/video-editor",
                      "hosted_door": False, "checks_complete": True},
            "controlled_stop": {"status": "stopped", "exit_status": 0, "lock_released": True},
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="optional receipt path; omitted means stdout only")
    parser.add_argument("--require-linux", action="store_true",
                        help="apply the later Linux gate (the built-in fake will fail closed)")
    args = parser.parse_args(argv)
    try:
        receipt = run_journey(require_linux=args.require_linux)
    except (ReceiptValidationError, ValueError) as exc:
        print(f"fully-local journey rejected: {exc}", file=sys.stderr)
        return 1
    encoded = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
