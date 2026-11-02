"""Focused I-08 gateway, C2 diagnostic, and public-surface checks."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from astrid.core.gateway import main
from astrid.core.gateway.diagnostics import (
    C2_DEADLINE_MS,
    collect_diagnostic,
    validate_diagnostic,
)
from astrid.runtime_cli import RuntimeResult


@dataclass
class _Observer:
    inspect_data: dict[str, object] | None = None
    observe_data: dict[str, object] | None = None
    inspect_calls: int = 0
    observe_calls: int = 0

    def inspect(self, *, support_root: str, timeout: float = 5.0) -> RuntimeResult:
        self.inspect_calls += 1
        data = self.inspect_data or {"ok": True, "realm_id": "selected"}
        return RuntimeResult(("runtime", "workspace", "inspect"), 0 if data.get("ok") else 1, data)

    def observe(self, command: str, *, support_root: str, timeout: float = 5.0) -> RuntimeResult:
        self.observe_calls += 1
        data = self.observe_data or {"ok": True, "state": "ready"}
        return RuntimeResult(("runtime", command), 0 if data.get("ok") else 1, data)


@pytest.mark.parametrize(
    ("problem_code", "failure_boundary"),
    [
        ("runtime_unavailable", "runtime-contact"),
        ("observation_stale", "runtime-contact"),
        ("workspace_identity_mismatch", "workspace-selection"),
        ("permission_limited", "runtime-permission"),
    ],
)
def test_c2_bounded_failure_cases_have_paired_semantics(problem_code: str, failure_boundary: str) -> None:
    runtime = _Observer(
        inspect_data={"ok": False, "problem_code": problem_code},
        observe_data={"ok": False, "problem_code": problem_code},
    )
    report, _, _ = collect_diagnostic(runtime, support_root="/Users/private/support")

    assert validate_diagnostic(report) == []
    assert report["problemCode"] == problem_code
    assert report["failureBoundary"] == failure_boundary
    assert report["actionsExecuted"] == []
    assert type(report["timing"]["elapsedMs"]) is int and report["timing"]["elapsedMs"] >= 0


def test_c2_timeout_is_paired_and_does_not_start_a_second_observer() -> None:
    class TimeoutObserver:
        def inspect(self, *, support_root: str, timeout: float = 5.0) -> RuntimeResult:
            return RuntimeResult(("runtime", "workspace", "inspect"), 0, {"ok": True})

        def observe(self, command: str, *, support_root: str, timeout: float = 5.0) -> RuntimeResult:
            raise TimeoutError("fixture timeout")

    runtime = TimeoutObserver()
    report, _, _ = collect_diagnostic(runtime, support_root="/Users/private/support")

    assert validate_diagnostic(report) == []
    assert report["problemCode"] == "observation_timeout"
    assert report["failureBoundary"] == "diagnostic-observation"
    assert type(report["timing"]["elapsedMs"]) is int and report["timing"]["elapsedMs"] >= 0


def test_runtime_cli_timeout_maps_to_c2_timeout() -> None:
    from astrid.runtime_cli import RuntimeCLI

    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    report, _, _ = collect_diagnostic(
        RuntimeCLI(command=("astrid-runtime",), runner=runner),
        support_root="/Users/private/support",
        command="doctor",
    )

    assert validate_diagnostic(report) == []
    assert report["problemCode"] == "observation_timeout"
    assert report["failureBoundary"] == "diagnostic-observation"


def test_total_deadline_stops_after_first_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from astrid.core.gateway import diagnostics

    ticks = iter((0.0, 0.0, 5.001, 5.001))
    monkeypatch.setattr(diagnostics.time, "monotonic", lambda: next(ticks))
    runtime = _Observer()
    report, _, _ = collect_diagnostic(runtime, support_root="/Users/private/support")

    assert runtime.inspect_calls == 1
    assert runtime.observe_calls == 0
    assert report["problemCode"] == "observation_timeout"
    assert report["failureBoundary"] == "diagnostic-observation"


def test_public_help_auth_and_status_diagnostic_routes(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    assert main(["help"]) == 0
    help_text = capsys.readouterr().out
    assert "Family census (exactly nine families)" in help_text
    assert "Reserved workspace commands" in help_text
    assert "auth        [hivemind] login/status/logout/revoke" in help_text

    auth_calls: list[list[str]] = []
    monkeypatch.setattr("astrid.core.auth.run_auth", lambda argv: auth_calls.append(list(argv or [])) or 0)
    assert main(["auth", "status"]) == 0
    assert auth_calls == [["status"]]

    assert main(["tasks", "--help"]) == 0
    assert "follow" in capsys.readouterr().out

    observer = _Observer(
        inspect_data={"ok": False, "problem_code": "workspace_missing", "support_root": str(tmp_path / "private")},
        observe_data={"ok": False, "problem_code": "runtime_unavailable", "error": "/Users/private/runtime.log"},
    )
    monkeypatch.setattr("astrid.runtime_cli.RuntimeCLI", lambda: observer)
    monkeypatch.setattr("astrid.sdk.storage_root.resolve_runtime_data_root", lambda: tmp_path / "private")
    assert main(["status", "--diagnostic", "--shared"]) == 1
    diagnostic = json.loads(capsys.readouterr().out)
    assert validate_diagnostic(diagnostic) == []
    assert diagnostic["mode"] == "shared"
    assert "/Users/private" not in json.dumps(diagnostic)
    assert diagnostic["nextActions"][0]["executable"] is False


def test_public_doctor_permission_diagnostic_is_observer_only(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    observer = _Observer(
        observe_data={"ok": False, "problem_code": "permission_limited", "error": "/Users/private/doctor.log"}
    )
    monkeypatch.setattr("astrid.runtime_cli.RuntimeCLI", lambda: observer)
    monkeypatch.setattr("astrid.sdk.storage_root.resolve_runtime_data_root", lambda: tmp_path / "private")

    assert main(["doctor", "--diagnostic", "--shared"]) == 1
    diagnostic = json.loads(capsys.readouterr().out)
    assert validate_diagnostic(diagnostic) == []
    assert diagnostic["problemCode"] == "permission_limited"
    assert diagnostic["failureBoundary"] == "runtime-permission"
    assert observer.observe_calls == 1


def test_doctor_consumes_product_observations_and_shared_redaction(monkeypatch, tmp_path):
    import astrid.core.pack.loader as loader
    import astrid.core.element.registry as registry
    monkeypatch.setattr(loader, "pack_quarantine_section", lambda: {"count": 1, "quarantined": [{"pack_id": "optional", "manifest_path": "/Users/private/pack.yaml"}], "fail_closed_error": None})
    monkeypatch.setattr(registry, "element_skip_section", lambda: {"count": 1, "skipped": [{"element_id": "bad", "path": "/Users/private/component.tsx"}], "error": None})
    from datetime import datetime, timezone
    from astrid.core.execution.guards import write_evidence_status, EVIDENCE_STATUS_NAME
    import astrid.sdk.host_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, "_host_birth_identity", lambda pid: "birth")
    root = tmp_path / "runtime"; root.mkdir()
    identity = {"pid": 17, "process_birth_id": "birth", "runtime_instance_id": "instance", "source_closure_digest": "closure"}
    (root / "generic-host.ready.json").write_text(json.dumps(identity))
    write_evidence_status(root / EVIDENCE_STATUS_NAME, {**identity, "charged_bytes": 23, "cap_bytes": 42, "live_attempts": 1,
                         "observation_kind": "measured_live_evidence_budget", "updated_at": datetime.now(timezone.utc).isoformat()})
    runtime = _Observer(observe_data={"ok": True, "state": "ready", "compatibility": {"compatible": True}, "execution_guards": {"evidence_budget": {"run_observed_bytes": 999, "cap_bytes": 999}}})
    report, _, observed = collect_diagnostic(runtime, support_root=str(tmp_path), mode="shared", command="doctor")
    assert validate_diagnostic(report) == []
    assert report["facts"]["computeWorker"]["value"] == "observed"
    facts = observed.data["astrid_observations"]
    assert facts["runtimeClientCompatibility"]["value"]["compatible"]
    assert facts["liveEvidenceBudget"]["value"]["run_observed_bytes"] == 23
    assert facts["packQuarantine"]["count"] == 1
    assert facts["elementSkips"]["count"] == 1
    assert "/Users/private" not in json.dumps(report)
    assert "/Users/private" not in json.dumps(facts)
    assert report["actionsExecuted"] == []



def test_doctor_compares_existing_status_health_to_generated_client(monkeypatch, tmp_path):
    import astrid.core.pack.loader as loader
    import astrid.core.element.registry as registry
    from astrid_runtime_client.contract_metadata import PROTOCOL, SCHEMA_DIGEST
    monkeypatch.setattr(loader, "pack_quarantine_section", lambda: {"count": 0, "quarantined": [], "fail_closed_error": None})
    monkeypatch.setattr(registry, "element_skip_section", lambda: {"count": 0, "skipped": [], "error": None})
    calls = []
    schema = [SCHEMA_DIGEST]
    class Observer:
        def observe(self, command, **kwargs):
            calls.append((command, kwargs["timeout"]))
            data = {"ok": True, "healthy": True} if command == "doctor" else {"ok": True, "health": {"status": "ok", "protocol": PROTOCOL, "schema_digest": schema[0], "runtime_instance_id": "instance"}}
            return RuntimeResult((command,), 0, data)
    diagnostic, _, observed = collect_diagnostic(Observer(), support_root=str(tmp_path), command="doctor")
    assert [row[0] for row in calls] == ["doctor", "status"]
    assert all(0 < row[1] <= 5 for row in calls)
    assert diagnostic["problemCode"] is None
    compatibility = observed.data["astrid_observations"]["runtimeClientCompatibility"]
    assert compatibility["observed"] and compatibility["value"]["compatible"]
    assert observed.data["astrid_observations"]["liveEvidenceBudget"] == {"observed": False, "value": None}
    schema[0] = "sha256:" + "f"*64
    diagnostic, _, observed = collect_diagnostic(Observer(), support_root=str(tmp_path), command="doctor")
    assert diagnostic["problemCode"] == "capability_unavailable"
    assert diagnostic["failureBoundary"] == "runtime-schema"


@pytest.mark.parametrize("invalid", ["stale", "future", "foreign", "reused_birth", "public_mode"])
def test_doctor_live_budget_rejects_stale_or_foreign_observation(monkeypatch, tmp_path, invalid):
    from datetime import datetime, timezone, timedelta
    from astrid.core.execution.guards import write_evidence_status, EVIDENCE_STATUS_NAME
    from astrid.core.gateway.diagnostics import collect_product_observations
    import astrid.core.pack.loader as loader
    import astrid.core.element.registry as registry
    import astrid.sdk.host_bootstrap as bootstrap
    monkeypatch.setattr(loader, "pack_quarantine_section", lambda: {"count": 0})
    monkeypatch.setattr(registry, "element_skip_section", lambda: {"count": 0})
    monkeypatch.setattr(bootstrap, "_host_birth_identity", lambda pid: "different" if invalid == "reused_birth" else "birth")
    root = tmp_path / "runtime"; root.mkdir()
    identity = {"pid": 17, "process_birth_id": "birth", "runtime_instance_id": "instance", "source_closure_digest": "closure"}
    (root / "generic-host.ready.json").write_text(json.dumps(identity))
    stamp = datetime.now(timezone.utc) + timedelta(seconds=-6 if invalid == "stale" else 60 if invalid == "future" else 0)
    record = {**identity, "charged_bytes": 1, "cap_bytes": 10, "live_attempts": 1,
              "observation_kind": "measured_live_evidence_budget", "updated_at": stamp.isoformat()}
    if invalid == "foreign": record["runtime_instance_id"] = "foreign"
    path = root / EVIDENCE_STATUS_NAME
    write_evidence_status(path, record)
    if invalid == "public_mode": path.chmod(0o644)
    value = collect_product_observations(str(tmp_path), {"execution_guards": {"evidence_budget": {"run_observed_bytes": 123}}})
    assert value["liveEvidenceBudget"] == {"observed": False, "value": None}


def test_public_doctor_json_consumes_bound_live_budget_without_mutation(monkeypatch, tmp_path, capsys):
    from datetime import datetime, timezone
    from astrid.core.execution.guards import EVIDENCE_STATUS_NAME, write_evidence_status
    import astrid.sdk.host_bootstrap as bootstrap
    import astrid.core.pack.loader as loader
    import astrid.core.element.registry as registry
    monkeypatch.setattr(loader, "pack_quarantine_section", lambda: {"count": 0, "quarantined": [], "fail_closed_error": None})
    monkeypatch.setattr(registry, "element_skip_section", lambda: {"count": 0, "skipped": [], "error": None})
    monkeypatch.setattr(bootstrap, "_host_birth_identity", lambda pid: "birth")
    root = tmp_path / "runtime"; root.mkdir()
    identity = {"pid": 17, "process_birth_id": "birth", "runtime_instance_id": "instance", "source_closure_digest": "closure"}
    ready = root / "generic-host.ready.json"; ready.write_text(json.dumps(identity))
    observation = root / EVIDENCE_STATUS_NAME
    write_evidence_status(observation, {**identity, "charged_bytes": 23, "cap_bytes": 42, "live_attempts": 1,
                          "observation_kind": "measured_live_evidence_budget", "updated_at": datetime.now(timezone.utc).isoformat()})
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    observer = _Observer(observe_data={"ok": True, "compatibility": {"compatible": True}})
    monkeypatch.setattr("astrid.runtime_cli.RuntimeCLI", lambda: observer)
    monkeypatch.setattr("astrid.sdk.storage_root.resolve_runtime_data_root", lambda: tmp_path)
    assert main(["doctor", "--json", "--shared"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["astrid_observations"]["liveEvidenceBudget"]["value"]["run_observed_bytes"] == 23
    assert payload["diagnostic"]["facts"]["computeWorker"]["value"] == "observed"
    assert validate_diagnostic(payload["diagnostic"]) == []
    assert payload["effects"] == ["observe"] and payload["diagnostic"]["actionsExecuted"] == []
    assert before == {path.name: path.read_bytes() for path in root.iterdir()}


@pytest.mark.parametrize("nomination", ["selected", "outside", "alias", "missing", "foreign"])
def test_doctor_live_budget_binds_exact_c06_ready_path(monkeypatch, tmp_path, nomination):
    from datetime import datetime, timezone
    from astrid.core.execution.guards import EVIDENCE_STATUS_NAME, write_evidence_status
    from astrid.core.gateway.diagnostics import collect_product_observations
    import astrid.sdk.host_bootstrap as bootstrap
    import astrid.core.pack.loader as loader
    import astrid.core.element.registry as registry
    monkeypatch.setattr(loader, "pack_quarantine_section", lambda: {"count": 0})
    monkeypatch.setattr(registry, "element_skip_section", lambda: {"count": 0})
    monkeypatch.setattr(bootstrap, "_host_birth_identity", lambda _: "birth")
    root = tmp_path / "runtime"; root.mkdir()
    identity = {"pid": 17, "process_birth_id": "birth", "runtime_instance_id": "instance", "source_closure_digest": "closure"}
    # A matching legacy marker must not mask a bad explicit nomination.
    (root / "generic-host.ready.json").write_text(json.dumps(identity))
    ready = root / "c06-operation.ready.json"
    if nomination == "outside": ready = tmp_path / "foreign.ready.json"
    ready.write_text(json.dumps({**identity, "source_closure_digest": "foreign"} if nomination == "foreign" else identity))
    if nomination == "alias":
        alias = root / "alias.ready.json"; alias.symlink_to(ready); ready = alias
    if nomination == "missing": ready = root / "missing.ready.json"
    hold = {"holding": "fixture.hold", "leases": 0, "idle_seconds": 600.0,
            "expires_at": "2026-10-19T12:10:00+00:00"}
    write_evidence_status(root / EVIDENCE_STATUS_NAME, {**identity, "ready_file": str(ready),
        "charged_bytes": 23, "cap_bytes": 42, "live_attempts": 1, "warm_holds": [hold],
        "observation_kind": "measured_live_evidence_budget", "updated_at": datetime.now(timezone.utc).isoformat()})
    observations = collect_product_observations(str(tmp_path), {})
    observed = observations["liveEvidenceBudget"]
    assert observed["observed"] is (nomination == "selected")
    if nomination == "selected": assert observed["value"]["run_observed_bytes"] == 23
    else: assert observed["value"] is None
    # Only the bound host's warm holds are reported: what each keeps warm and when it expires.
    assert observations["warmHolds"] == ({"observed": True, "value": [hold]} if nomination == "selected"
                                         else {"observed": False, "value": None})
