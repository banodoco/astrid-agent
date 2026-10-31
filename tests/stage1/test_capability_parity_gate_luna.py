"""Machine-readable Stage 1 capability census and adapter-family gate.

The Stage 1 contract requires an explicit disposition for every discovered
capability.  It does not require 59 copies of the host protocol test: the
generic host is deliberately shared by adapter families.  This gate therefore
records the family representative that proves the shared route, while keeping
the canonical Remotion compositor as a direct-proof exception whenever it is
actually ready.

The resulting report is written to ``tmp_path`` by the test so CI can retain it
without making generated evidence part of the source checkout.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from tests.helpers.runtime import runtime_checkout

ROOT = Path(__file__).resolve().parents[2]
PACKS = ROOT / "astrid" / "packs"
MATRIX = ROOT / "config" / "astrid-beta-capabilities.json"

# These selectors are the durable proofs for the shared host boundary.  A
# family proof is intentionally not claimed as business-level proof for every
# pack: each pack remains in the exact discovered census and its digest is
# emitted in the report.  The direct rule below prevents the canonical
# Remotion compositor from being hidden behind the offline render helpers.
FAMILY_PROOFS: dict[str, dict[str, str]] = {
    "cpu": {
        "kind": "family_representative",
        "selector": "tests/stage1/test_runtime_client_cutover.py::test_generic_host_cpu_route_claims_settles_and_fences_stale_attempt_across_restart",
        "rationale": "the cutover test proves generic CPU discovery, registration, claim, subprocess execution, CAS settlement, restart, and stale-attempt fencing on a real daemon",
    },
    "provider": {
        "kind": "family_representative",
        "selector": "tests/integrations/test_generic_host_external_provider.py::test_external_pack_command_imports_from_its_admitted_pack_root",
        "rationale": "provider family executes a real host-managed loopback route through the subprocess host and settles output in the runtime",
    },
    "render": {
        "kind": "family_representative",
        "selector": "tests/stage1/test_runtime_client_cutover.py::test_generic_host_render_route_runs_real_ffmpeg_and_settles_boot_manifest_provenance",
        "rationale": "the cutover test proves the shared render-family host route with real FFmpeg and boot-manifest provenance on a real daemon; canonical Remotion rendering is direct-proof gated below",
    },
    "local_generation": {
        "kind": "family_representative",
        "selector": "tests/integrations/test_generic_host_runtime_control2.py::test_provider_fixture_without_credentials_settles_offline",
        "rationale": "the shared host admission and settlement route is proven while local-generation readiness remains dependency-gated",
    },
}


def _selector_path(selector: str) -> Path:
    return ROOT / selector.split("::", 1)[0]


def _report(host: GenericPackHost) -> dict[str, Any]:
    records = []
    for record in host.preflight():
        disposition = str(record.matrix.get("disposition", "unclassified"))
        family = record.adapter.family
        proof = FAMILY_PROOFS.get(family)
        # The final compositor is the one render capability whose manifest
        # explicitly requires Remotion.  A family FFmpeg proof must never
        # silently certify it if its full dependency closure is ready.
        direct_required = record.id == "rendering.render"
        if direct_required and record.ready:
            proof = {
                "kind": "direct",
                "selector": "tests/integrations/test_generic_host_remotion_render.py::test_generic_host_remotion_register_claim_execute_settle_and_cas",
                "rationale": "canonical Astrid rendering pack must execute through Node/Remotion/FFmpeg when advertised ready",
            }
        records.append(
            {
                "id": record.id,
                "adapter_family": family,
                "disposition": disposition,
                "ready": record.ready,
                "capability_digest": record.capability_digest,
                "source_digest": record.source_digest,
                "dependency_digest": record.dependency_digest,
                "preflight": dict(record.preflight),
                "proof": proof,
                "direct_proof_required": direct_required,
            }
        )
    return {
        "schema": "astrid.stage1.capability_parity.v1",
        "matrix": str(MATRIX.relative_to(ROOT)),
        "discovered_count": len(records),
        "ready_count": sum(bool(row["ready"]) for row in records),
        "records": records,
    }


def _run_proofs(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Execute each unique proof selector needed by a ready capability."""
    selectors = {
        str(row["proof"]["selector"])
        for row in report["records"]
        if row["ready"] and isinstance(row.get("proof"), dict)
    }
    results: dict[str, dict[str, Any]] = {}
    runtime = runtime_checkout()
    proof_env = os.environ.copy()
    proof_env["ASTRID_RUNTIME_CHECKOUT"] = str(runtime)
    proof_env["PYTHONPATH"] = os.pathsep.join(
        part
        for part in (str(ROOT), str(runtime), str(runtime / "packages" / "python"), proof_env.get("PYTHONPATH", ""))
        if part
    )
    for selector in sorted(selectors):
        completed = subprocess.run(
            # The representatives live in the integration lane; -m "" keeps addopts from deselecting them.
            [sys.executable, "-m", "pytest", "-q", "-m", "", selector],
            cwd=ROOT,
            env=proof_env,
            text=True,
            capture_output=True,
            check=False,
            timeout=240,
        )
        output = f"{completed.stdout}\n{completed.stderr}"
        # A successful pytest exit is not sufficient: a skipped representative
        # is not execution evidence for a capability advertised as ready.
        skipped = bool(re.search(r"\b\d+\s+skipped\b", output))
        passed = bool(re.search(r"\b\d+\s+passed\b", output))
        results[selector] = {
            "returncode": completed.returncode,
            "status": "pass" if completed.returncode == 0 and passed and not skipped else "fail",
            "output_tail": output[-2000:],
        }
    return results


def test_stage1_capability_parity_is_explicit_and_family_proven(tmp_path: Path) -> None:
    host = GenericPackHost(pack_roots=[PACKS], capability_matrix=MATRIX, credential_source={})
    records = host.discover()
    report = _report(host)
    report_path = tmp_path / "astrid-capability-parity.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert report["schema"] == "astrid.stage1.capability_parity.v1"
    assert report["discovered_count"] == 104
    discovered_ids = {record.id for record in records}
    assert discovered_ids <= set(host.matrix)
    assert len(set(host.matrix) - discovered_ids) == 8

    allowed = {"required", "optional", "unsupported", "retired"}
    for record in records:
        disposition = record.matrix.get("disposition")
        assert disposition in allowed, f"{record.id}: unclassified disposition {disposition!r}"
        assert str(record.matrix.get("evidence_reason", "")).strip(), f"{record.id}: missing evidence reason"
        assert record.adapter.family in FAMILY_PROOFS, f"{record.id}: no adapter-family proof"
        row = next(item for item in report["records"] if item["id"] == record.id)
        proof = row["proof"]
        assert isinstance(proof, dict), f"{record.id}: missing proof record"
        selector = str(proof.get("selector", ""))
        assert selector and _selector_path(selector).is_file(), f"{record.id}: proof selector is not in checkout"
        if row["ready"]:
            assert proof["kind"] in {"family_representative", "direct"}, f"{record.id}: ready without executable proof"
        if row["direct_proof_required"] and row["ready"]:
            assert proof["kind"] == "direct", "rendering.render cannot be certified by an offline render-family helper"

    # Historical/external rows are not discovered executor routes, but the
    # reconciled ledger must keep them explicit rather than silently dropping
    # them from the source census.
    sources = host.ledger["sources"]
    assert all(section["complete"] for section in sources["coverage"].values())
    assert not sources["coverage"]["executor_inventory"]["missing"]
    assert report_path.is_file()
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert persisted["ready_count"] == report["ready_count"]


@pytest.mark.integration
def test_every_ready_capability_has_a_passing_family_proof() -> None:
    """Runs each ready capability's representative (real daemon, real FFmpeg), so it is integration-lane."""
    host = GenericPackHost(pack_roots=[PACKS], capability_matrix=MATRIX, credential_source={})
    host.discover()
    report = _report(host)
    proof_runs = _run_proofs(report)
    for row in report["records"]:
        if row["ready"] and isinstance(row.get("proof"), dict):
            proof_run = proof_runs.get(str(row["proof"]["selector"]))
            assert proof_run and proof_run["status"] == "pass", (
                f"{row['id']}: proof did not execute cleanly: {proof_run}"
            )
