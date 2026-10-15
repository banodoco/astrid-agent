"""Canonical reconciliation of Astrid capability source projections.

Pack manifests and the result-contract snapshot describe the capability
surface. This module joins those projections into one JSON-shaped, read-only
ledger consumed before host readiness is evaluated.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Mapping

from astrid.core.execution.provider_route_grant import ProjectGrantAuthority
from astrid.core.pack.loader import _load_manifest_payload

_LOGGER = logging.getLogger(__name__)

CENSUS_LOCK_RELATIVE = Path("config") / "astrid-capability-census.lock.json"
CENSUS_SECTIONS: tuple[str, ...] = (
    "source_labels",
    "historical_source_labels",
    "executor_inventory",
    "legacy_ids",
)
APPROVE_COMMAND = "python -m astrid.core.execution.capability_ledger approve"


class CapabilityLedgerError(ValueError):
    """Raised when a capability source cannot be reconciled safely."""


_EXECUTABLE_DISPOSITIONS = frozenset({"required", "optional"})
_NON_EXECUTABLE_DISPOSITIONS = frozenset({"unsupported", "retired"})


def _validate_matrix_capabilities(capabilities: list[Any]) -> None:
    """Validate the canonical matrix before any host can consume it."""
    seen: set[str] = set()
    for index, row in enumerate(capabilities):
        if not isinstance(row, Mapping):
            raise CapabilityLedgerError(f"capabilities[{index}] must be an object")
        capability_id = row.get("id")
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise CapabilityLedgerError(f"capabilities[{index}].id must be a non-empty string")
        if capability_id in seen:
            raise CapabilityLedgerError(f"duplicate capability id {capability_id!r}")
        seen.add(capability_id)
        disposition = row.get("disposition")
        if disposition not in _EXECUTABLE_DISPOSITIONS | _NON_EXECUTABLE_DISPOSITIONS:
            raise CapabilityLedgerError(
                f"capabilities[{index}].disposition is not an approved state: {disposition!r}"
            )
        if disposition in _EXECUTABLE_DISPOSITIONS:
            reason = row.get("evidence_reason")
            if not isinstance(reason, str) or not reason.strip():
                raise CapabilityLedgerError(
                    f"capabilities[{index}].evidence_reason is required for {disposition} capability"
                )


def project_capability_execution_admission(
    ledger: Mapping[str, Any], capability_id: str, *,
    authority: ProjectGrantAuthority | None, grant: object,
    realm: str, browser_session: str, actor_id: str, project_id: str,
) -> dict[str, Any]:
    """Intersect a host-owned project policy with current capability admission.

    This policy seam is not connected to a browser transport: callers must first
    have a trusted session binding. An arbitrary policy/decision dictionary is
    not an authority. Source admission is still distinct from runtime readiness.
    Runtime's timeline.update operation is not an executor capability and must
    not be admitted through this matrix or aliased to a rendering capability.
    """
    denied = {"capability_id": capability_id, "executable": False, "status": "denied"}
    if not isinstance(authority, ProjectGrantAuthority):
        return {**denied, "code": "project_grant_authority_missing"}
    decision = authority.decide(
        grant, realm=realm, browser_session=browser_session, actor_id=actor_id,
        project_id=project_id, operation=capability_id,
    )
    if decision["allowed"] is not True:
        return {**denied, "code": decision["code"], "project_decision": decision}
    admission = capability_execution_admission(ledger, capability_id)
    return {**admission, "code": "project_capability_admitted" if admission["executable"]
            else "project_capability_unavailable", "project_decision": decision}


def capability_execution_admission(
    ledger: Mapping[str, Any],
    capability_id: str,
) -> dict[str, Any]:
    """Return a fail-closed execution decision for one canonical capability.

    This is an integration seam for the host/gateway owner.  It does not run
    a capability or perform runtime readiness checks; it only prevents an
    undeclared, withdrawn, or unreconciled capability from being admitted.
    """
    denied = {
        "capability_id": capability_id,
        "executable": False,
        "status": "denied",
        "reason": "capability is not admitted",
    }
    if not isinstance(capability_id, str) or not capability_id.strip():
        denied["reason"] = "capability id must be a non-empty string"
        return denied
    capabilities = ledger.get("capabilities")
    if not isinstance(capabilities, list):
        denied["reason"] = "canonical capability matrix is missing"
        return denied
    row = next(
        (item for item in capabilities if isinstance(item, Mapping) and item.get("id") == capability_id),
        None,
    )
    if row is None:
        denied["reason"] = "capability is not declared in the canonical matrix"
        return denied
    disposition = row.get("disposition")
    denied["disposition"] = disposition
    if disposition in _NON_EXECUTABLE_DISPOSITIONS:
        denied["reason"] = f"capability disposition is {disposition}"
        return denied
    if disposition not in _EXECUTABLE_DISPOSITIONS:
        denied["reason"] = "capability disposition is not executable"
        return denied

    sources = ledger.get("sources")
    coverage = sources.get("coverage") if isinstance(sources, Mapping) else None
    if not isinstance(coverage, Mapping) or not coverage or any(
        not isinstance(section, Mapping) or section.get("complete") is not True
        for section in coverage.values()
    ):
        denied["reason"] = "capability source reconciliation is incomplete"
        return denied
    inventory = sources.get("executor_inventory") if isinstance(sources, Mapping) else ()
    if isinstance(inventory, list):
        executor_row = next(
            (item for item in inventory if isinstance(item, Mapping) and item.get("id") == capability_id),
            None,
        )
        if isinstance(executor_row, Mapping) and executor_row.get("executable") is False:
            denied["reason"] = "executor inventory marks capability non-executable"
            return denied

    return {
        "capability_id": capability_id,
        "executable": True,
        "status": "admitted",
        "disposition": disposition,
        "reason": "canonical matrix and reconciled source census admit capability",
        "requires_runtime_readiness": True,
    }


# These rows are deliberately data, not importable capability definitions.  The
# source manifests and the Reigh admission registry were retired, but their
# historical occurrences remain part of the B9 census and must not disappear
# merely because the executable routes were removed.
_HISTORICAL_SOURCE_LABELS: tuple[dict[str, Any], ...] = (
    {
        "pack": "iteration",
        "label": "collect_thread_provenance",
        "source": "historical: astrid/packs/iteration/pack.yaml",
        "canonical_id": "iteration.collect_runtime_provenance",
        "disposition": "replaced",
        "equivalent_to": "iteration.collect_runtime_provenance",
        "executable": False,
        "reason": "Replaced by runtime-owned provenance collection.",
    },
    {
        "pack": "iteration",
        "label": "prepare_iteration",
        "source": "historical: astrid/packs/iteration/pack.yaml",
        "canonical_id": None,
        "disposition": "retired",
        "equivalent_to": None,
        "executable": False,
        "reason": "Retired with the thread/sidecar iteration preparation authority.",
    },
    {
        "pack": "reigh",
        "label": "build_spatial_audio_page",
        "source": "historical: astrid/packs/reigh/pack.yaml",
        "canonical_id": None,
        "disposition": "unsupported",
        "equivalent_to": None,
        "executable": False,
        "reason": "Reigh integration pack is not shipped in the current checkout.",
    },
    {
        "pack": "reigh",
        "label": "fetch_reigh_data",
        "source": "historical: astrid/packs/reigh/pack.yaml",
        "canonical_id": None,
        "disposition": "unsupported",
        "equivalent_to": None,
        "executable": False,
        "reason": "Reigh integration pack is not shipped in the current checkout.",
    },
    {
        "pack": "reigh",
        "label": "open_in_reigh",
        "source": "historical: astrid/packs/reigh/pack.yaml",
        "canonical_id": None,
        "disposition": "unsupported",
        "equivalent_to": None,
        "executable": False,
        "reason": "Reigh bridge authority was retired and is not shipped.",
    },
    {
        "pack": "reigh",
        "label": "publish_timeline",
        "source": "historical: astrid/packs/reigh/pack.yaml",
        "canonical_id": None,
        "disposition": "unsupported",
        "equivalent_to": None,
        "executable": False,
        "reason": "Reigh publishing authority was retired and is not shipped.",
    },
    {
        "pack": "training",
        "label": "manage_asset_cache",
        "source": "historical: astrid/packs/training/pack.yaml",
        "canonical_id": None,
        "disposition": "retired",
        "equivalent_to": None,
        "executable": False,
        "reason": "Retired with the persistent URL asset-cache authority.",
    },
    {
        "pack": "typed_timeline",
        "label": "typed_timeline.render",
        "source": "historical: astrid/packs/typed_timeline/pack.yaml",
        "canonical_id": None,
        "disposition": "retired",
        "equivalent_to": None,
        "executable": False,
        "reason": "Retired typed-timeline render route; rendering is runtime-owned.",
    },
)

# These personal adapters are opt-in project packs.  They may be present in
# an editable checkout, but they are not part of the canonical source census;
# their executor contracts remain represented by the optional matrix rows.
_OPTIONAL_PROJECT_PACK_IDS = frozenset({"discord_local", "seedance_local"})

_HISTORICAL_EXECUTOR_ROWS: tuple[dict[str, Any], ...] = (
    {
        "id": "iteration.prepare",
        "result_contract": "manifest",
        "disposition": "retired",
        "discovery_status": "historical_only",
        "executable": False,
        "reason": "Retired thread/sidecar iteration preparation executor.",
        "source": "historical: astrid/core/contracts/output_result_exemptions.json",
    },
    {
        "id": "reigh.open_in_reigh",
        "result_contract": "exempted",
        "disposition": "unsupported",
        "discovery_status": "historical_only",
        "executable": False,
        "reason": "Retired Reigh bridge executor; external integration is not shipped.",
        "source": "historical: astrid/core/contracts/output_result_exemptions.json",
    },
    {
        "id": "reigh.publish",
        "result_contract": "exempted",
        "disposition": "unsupported",
        "discovery_status": "historical_only",
        "executable": False,
        "reason": "Retired Reigh publishing executor; external integration is not shipped.",
        "source": "historical: astrid/core/contracts/output_result_exemptions.json",
    },
    {
        "id": "reigh.reigh_data",
        "result_contract": "exempted",
        "disposition": "unsupported",
        "discovery_status": "historical_only",
        "executable": False,
        "reason": "Retired Reigh data executor; external integration is not shipped.",
        "source": "historical: astrid/core/contracts/output_result_exemptions.json",
    },
    {
        "id": "reigh.spatial_audio_page",
        "result_contract": "manifest",
        "disposition": "unsupported",
        "discovery_status": "historical_only",
        "executable": False,
        "reason": "Retired Reigh spatial-audio executor; external integration is not shipped.",
        "source": "historical: astrid/core/contracts/output_result_exemptions.json",
    },
    {
        "id": "training.asset_cache",
        "result_contract": "exempted",
        "disposition": "retired",
        "discovery_status": "historical_only",
        "executable": False,
        "reason": "Retired persistent URL asset-cache executor.",
        "source": "historical: astrid/core/contracts/output_result_exemptions.json",
    },
)

_LEGACY_REIGH_IDS: tuple[tuple[str, str], ...] = (
    ("reigh.wan_2_2_t2i", "wgp"),
    ("reigh.qwen_image", "vibecomfy"),
    ("reigh.qwen_image_style", "vibecomfy"),
    ("reigh.qwen_image_2512", "vibecomfy"),
    ("reigh.z_image_turbo", "vibecomfy"),
    ("reigh.image_upscale", "vibecomfy"),
    ("reigh.individual_travel_segment", "wgp"),
    ("reigh.join_clips_orchestrator", "wgp"),
    ("reigh.video_enhance", "vibecomfy"),
    ("reigh.z_image_turbo_i2i", "vibecomfy"),
    ("reigh.qwen_image_edit", "vibecomfy"),
    ("reigh.image_inpaint", "vibecomfy"),
    ("reigh.annotated_image_edit", "vibecomfy"),
    ("reigh.travel_orchestrator", "wgp"),
    ("reigh.wan_2_2_i2v", "wgp"),
    ("reigh.travel_stitch", "wgp"),
    ("reigh.edit_video_orchestrator", "wgp"),
    ("reigh.animate_character", "vibecomfy"),
    ("reigh.flux_klein_edit", "vibecomfy"),
)


def _repo_root_for_matrix(path: Path) -> Path | None:
    candidate = path.expanduser().resolve().parent.parent
    return candidate if (candidate / "astrid" / "packs").is_dir() else None


def _quarantine_snapshot(repo_root: Path) -> tuple[dict[str, tuple[str, ...]], list[dict[str, Any]]]:
    """Return quarantined pack folders with their declared labels, and their records.

    A quarantined manifest never moves the admitted census. Its declared labels
    are kept so the census can tell "a new half-written pack" apart from "a
    reviewed pack that turned invalid" (see :func:`_census_check`).
    """
    from astrid.core.pack.loader import _raw_declared_capabilities, scan_packs

    scan = scan_packs(repo_root / "astrid" / "packs", strict=False)
    labels = {record.pack_id: _raw_declared_capabilities(record.manifest_path) for record in scan.quarantined}
    return labels, [record.to_dict() for record in scan.quarantined]


def _census_lock(repo_root: Path) -> dict[str, list[str]] | None:
    """Return the approved census lock, or ``None`` when no lock is committed yet.

    The lock is generated by ``approve`` and reviewed as a diff. It lists the
    approved identifiers themselves, not counts, so a swap is visible.
    """
    path = repo_root / CENSUS_LOCK_RELATIVE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityLedgerError(f"cannot read census lock {path}: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise CapabilityLedgerError(f"census lock {path} requires schema_version 1")
    lock: dict[str, list[str]] = {}
    for section in CENSUS_SECTIONS:
        values = payload.get(section)
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise CapabilityLedgerError(f"census lock {path} section {section!r} must be a list of strings")
        lock[section] = sorted(values)
    return lock


def _census_check(current: set[str], approved: list[str] | None, quarantined: set[str]) -> dict[str, Any]:
    """Compare the admitted census with the approved lock. Never raises.

    * ``ok``: the admitted set equals the approved set.
    * ``degraded``: the only difference is approved entries whose manifest is
      quarantined. A previously reviewed pack turned invalid.
    * ``drift``: an admitted entry is not approved, or an approved entry is
      missing without quarantine. Drift is reported with names, not counts.

    Drift does not stop the host. An unapproved capability is kept out of
    executable admission per capability (see ``generic_host``), so the blast
    radius of an unreviewed change is that one capability.
    """
    approved_set = set(approved or ())
    added = sorted(current - approved_set)
    removed = approved_set - current
    explained = sorted(removed & quarantined)
    unexplained = sorted(removed - quarantined)
    if added or unexplained:
        status = "drift"
    elif explained:
        status = "degraded"
    else:
        status = "ok"
    return {
        "approved": len(approved_set),
        "ledger": len(current),
        "added": added,
        "missing": unexplained,
        "quarantined": explained,
        "status": status,
        "complete": status != "drift",
        "degraded": status == "degraded",
    }


def _source_labels(repo_root: Path, *, excluded: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest in sorted((repo_root / "astrid" / "packs").glob("*/pack.yaml")):
        if manifest.parent.name in _OPTIONAL_PROJECT_PACK_IDS or manifest.parent.name in excluded:
            continue
        raw = _load_manifest_payload(manifest)
        labels = raw.get("capabilities", []) if isinstance(raw, Mapping) else []
        if not isinstance(labels, list):
            raise CapabilityLedgerError(f"{manifest}: capabilities must be a list")
        for label in labels:
            if not isinstance(label, str) or not label.strip():
                raise CapabilityLedgerError(f"{manifest}: capability labels must be non-empty strings")
            rows.append({"pack": manifest.parent.name, "label": label, "source": str(manifest.relative_to(repo_root))})
    return rows


def _aliases(repo_root: Path, *, excluded: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest in sorted((repo_root / "astrid" / "packs").glob("*/pack.yaml")):
        if manifest.parent.name in excluded:
            continue
        raw = _load_manifest_payload(manifest)
        aliases = raw.get("aliases", []) if isinstance(raw, Mapping) else []
        if not isinstance(aliases, list):
            raise CapabilityLedgerError(f"{manifest}: aliases must be a list")
        for alias in aliases:
            if not isinstance(alias, Mapping) or not {"alias", "canonical_id"} <= set(alias):
                raise CapabilityLedgerError(f"{manifest}: alias entry missing alias/canonical_id")
            rows.append({
                "pack": manifest.parent.name,
                "kind": str(alias.get("kind", "executor")),
                "alias": str(alias["alias"]),
                "canonical_id": str(alias["canonical_id"]),
                "deprecated": bool(alias.get("deprecated", False)),
                "deprecation_message": str(alias.get("deprecation_message", "")),
                "source": str(manifest.relative_to(repo_root)),
            })
    return rows


def _executor_inventory(repo_root: Path) -> list[dict[str, Any]]:
    source = repo_root / "astrid" / "core" / "contracts" / "output_result_exemptions.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    non_exempt = payload.get("non_exempt", [])
    exemptions = payload.get("exemptions", {})
    if not isinstance(non_exempt, list) or not isinstance(exemptions, Mapping):
        raise CapabilityLedgerError(f"{source}: invalid executor snapshot")
    rows: list[dict[str, Any]] = []
    for capability_id in sorted(set(str(value) for value in non_exempt)):
        rows.append({"id": capability_id, "result_contract": "manifest", "disposition": "historical", "source": str(source.relative_to(repo_root))})
    for capability_id, detail in sorted(exemptions.items()):
        detail = detail if isinstance(detail, Mapping) else {}
        rows.append({
            "id": str(capability_id),
            "result_contract": "exempted",
            "disposition": "historical",
            "reason": str(detail.get("note", "")),
            "source": str(source.relative_to(repo_root)),
        })
    rows.extend(dict(row) for row in _HISTORICAL_EXECUTOR_ROWS if row["id"] not in {item["id"] for item in rows})
    return rows


def _legacy_ids(repo_root: Path) -> list[dict[str, Any]]:
    """Return the exact pre-cutover Reigh registry as inert historical data."""
    source = "historical: astrid/core/integrations/reigh/capabilities.py"
    return [
        {
            "id": capability_id,
            "binding": binding,
            "disposition": "retired",
            "discovery_status": "historical_only",
            "executable": False,
            "source": source,
            "reason": "Removed legacy Reigh registry entry; retained for census only.",
        }
        for capability_id, binding in _LEGACY_REIGH_IDS
    ]


def _model_inventory(repo_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    source = repo_root / "astrid" / "core" / "model_catalog" / "models.yaml"
    raw = _load_manifest_payload(source)
    models = raw.get("models", []) if isinstance(raw, Mapping) else []
    rows: list[dict[str, Any]] = []
    backends: set[str] = set()
    for model in models:
        if not isinstance(model, Mapping) or not model.get("id"):
            continue
        model_backends: set[str] = set()
        for mode in (model.get("modes", {}) or {}).values():
            if isinstance(mode, Mapping) and isinstance(mode.get("backends", {}), Mapping):
                model_backends.update(str(value) for value in mode["backends"])
        backends.update(model_backends)
        rows.append({"id": str(model["id"]), "backends": sorted(model_backends), "source": str(source.relative_to(repo_root))})
    return rows, sorted(backends)


def _render_backend_inventory(repo_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    root = repo_root / "astrid" / "packs" / "rendering" / "rendering" / "renderers"
    for source in sorted(root.glob("*/renderer.yaml")):
        raw = _load_manifest_payload(source)
        if isinstance(raw, Mapping) and raw.get("id"):
            rows.append({"id": str(raw["id"]), "required_binaries": [str(value) for value in (raw.get("required_binaries") or [])], "source": str(source.relative_to(repo_root))})
    return rows


def _provider_inventory(capabilities: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    providers: dict[str, set[str]] = {}
    for row in capabilities:
        if row.get("adapter_family") != "provider":
            continue
        for name in row.get("required_env", ()) or ():
            providers.setdefault(str(name), set()).add(str(row.get("id", "")))
    return [{"credential": key, "capabilities": sorted(value)} for key, value in sorted(providers.items())]


def _historical_projection(row: Mapping[str, Any]) -> bool:
    # Keep current manifests distinct from the historical projection.  The
    # latter includes retired source occurrences, while Fal's two current
    # labels remain outside the non-Fal historical census.
    return row["pack"] != "fal" and not (
        row["pack"] == "iteration" and row["label"] == "collect_runtime_provenance"
    )


def _census_sets(repo_root: Path) -> dict[str, Any]:
    """Collect the admitted census identifiers and the quarantined subset of each.

    Shared by :func:`_reconcile_sources` (check) and :func:`write_census_lock`
    (approve), so both see exactly the same inputs.
    """
    quarantined_by_pack, quarantined_records = _quarantine_snapshot(repo_root)
    excluded = frozenset(quarantined_by_pack)
    labels = _source_labels(repo_root, excluded=excluded)
    historical_labels = [row for row in labels if _historical_projection(row)]
    historical_labels.extend(dict(row) for row in _HISTORICAL_SOURCE_LABELS)
    historical_labels.sort(key=lambda row: (row["pack"], row["label"]))
    aliases = _aliases(repo_root, excluded=excluded)
    executors = _executor_inventory(repo_root)
    legacy = _legacy_ids(repo_root)
    quarantined_rows = [
        {"pack": pack, "label": label}
        for pack, declared in sorted(quarantined_by_pack.items())
        for label in declared
    ]
    current = {
        "source_labels": {f"{row['pack']}.{row['label']}" for row in labels},
        "historical_source_labels": {f"{row['pack']}.{row['label']}" for row in historical_labels},
        "executor_inventory": {str(row["id"]) for row in executors},
        "legacy_ids": {str(row["id"]) for row in legacy},
    }
    quarantined = {
        "source_labels": {f"{row['pack']}.{row['label']}" for row in quarantined_rows},
        "historical_source_labels": {
            f"{row['pack']}.{row['label']}" for row in quarantined_rows if _historical_projection(row)
        },
        "executor_inventory": set(),
        "legacy_ids": set(),
    }
    return {
        "labels": labels,
        "historical_labels": historical_labels,
        "aliases": aliases,
        "executors": executors,
        "legacy": legacy,
        "current": current,
        "quarantined": quarantined,
        "quarantined_records": quarantined_records,
    }


def _reconcile_sources(repo_root: Path, capabilities: list[Mapping[str, Any]]) -> dict[str, Any]:
    census = _census_sets(repo_root)
    labels = census["labels"]
    historical_labels = census["historical_labels"]
    quarantined_records = census["quarantined_records"]
    aliases = census["aliases"]
    executors = census["executors"]
    legacy = census["legacy"]
    models, model_backends = _model_inventory(repo_root)
    rendering_backends = _render_backend_inventory(repo_root)
    current_ids = {str(row.get("id")) for row in capabilities}
    for row in labels:
        candidate = f"{row['pack']}.{row['label']}"
        row["canonical_id"] = candidate if candidate in current_ids else None
        row["disposition"] = "advertised" if row["canonical_id"] else "unmapped_source_label"
    for row in executors:
        if row["id"] in current_ids:
            row["disposition"] = "advertised"
            row["discovery_status"] = "discovered"
        elif row["id"].startswith(("hivemind.", "discord_local.", "seedance_local.")):
            # These IDs remain in the result-contract inventory. If their
            # shipped/explicit pack is absent, keep them visible without
            # making an unavailable external route look executable.
            row["disposition"] = "unavailable_external"
            row["discovery_status"] = "not_installed"
            row["reason"] = "optional external pack is not installed in this checkout"
        elif row["id"] in current_ids:
            row["disposition"] = "advertised"
            row["discovery_status"] = "discovered"
        elif row["disposition"] == "historical":
            row["reason"] = row.get("reason") or "retained in historical executor snapshot; no current executor manifest"
            row["discovery_status"] = "historical_only"
    for row in executors:
        if row.get("discovery_status") != "discovered":
            row.setdefault("executable", False)
    expected_hivemind = sorted(row["id"] for row in executors if row["id"].startswith("hivemind."))
    # Blessed census baseline. Hivemind remains represented by the optional
    # external contract below; it is deliberately absent from the in-tree
    # source-label census. Optional project packs remain represented by their
    # matrix contracts, but do not advance the canonical source counts.
    lock = _census_lock(repo_root)
    coverage = {
        section: _census_check(census["current"][section], (lock or {}).get(section), census["quarantined"][section])
        for section in CENSUS_SECTIONS
    }
    drifted = {section: row for section, row in coverage.items() if row["status"] == "drift"}
    if drifted:
        _LOGGER.warning(
            "capability census drift (host keeps running; unapproved capabilities are unavailable): %s; "
            "review the diff and run: %s",
            {section: {"added": row["added"], "missing": row["missing"]} for section, row in drifted.items()},
            APPROVE_COMMAND,
        )
    return {
        "pack_labels": labels,
        "quarantined_packs": quarantined_records,
        "historical_pack_labels": historical_labels,
        "aliases": aliases,
        "executor_inventory": executors,
        "legacy_ids": legacy,
        "providers": _provider_inventory(capabilities),
        "models": models,
        "generation_backends": model_backends,
        "rendering_backends": rendering_backends,
        "hivemind": {
            "disposition": "optional_external",
            "executor_ids": expected_hivemind,
            "external_census": {
                "declared_count": 7,
                "installed_count": 0,
                "unresolved": True,
                "note": "Seven Hivemind executors are declared by the optional external pack; installation is proven by the managed source inventory."
                if len(expected_hivemind) == 7
                else "Historical Hivemind contract is incomplete; no additional ID is guessed.",
            },
        },
        "coverage": coverage,
        "counts": {"pack_labels": len(labels), "historical_pack_labels": len(historical_labels), "executor_inventory": len(executors), "legacy_ids": len(legacy), "aliases": len(aliases), "models": len(models), "rendering_backends": len(rendering_backends)},
    }


def write_census_lock(repo_root: Path) -> dict[str, Any]:
    """Regenerate the census lock from the admitted tree and return the diff.

    Quarantined entries that were already approved are kept, so approving while
    a reviewed pack is temporarily invalid does not erase its approval.
    """
    census = _census_sets(repo_root)
    previous = _census_lock(repo_root) or {}
    payload: dict[str, Any] = {"schema_version": 1, "generated_by": APPROVE_COMMAND}
    diff: dict[str, dict[str, list[str]]] = {}
    for section in CENSUS_SECTIONS:
        current = census["current"][section]
        kept = set(previous.get(section, ())) & census["quarantined"][section]
        approved = sorted(current | kept)
        payload[section] = approved
        before = set(previous.get(section, ()))
        diff[section] = {"added": sorted(set(approved) - before), "removed": sorted(before - set(approved))}
    path = repo_root / CENSUS_LOCK_RELATIVE
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(path), "diff": diff}


def main(argv: list[str] | None = None) -> int:
    """``approve``: regenerate the reviewable census lock for this checkout."""
    parser = argparse.ArgumentParser(prog="python -m astrid.core.execution.capability_ledger")
    subcommands = parser.add_subparsers(dest="command", required=True)
    approve = subcommands.add_parser("approve", help="regenerate config/astrid-capability-census.lock.json")
    approve.add_argument("--checkout", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args(argv)
    result = write_census_lock(args.checkout.resolve())
    changed = {section: row for section, row in result["diff"].items() if row["added"] or row["removed"]}
    print(f"wrote {result['path']}")
    print(json.dumps(changed, indent=2, sort_keys=True) if changed else "no census changes")
    return 0


def load_capability_ledger(matrix_path: str | Path) -> dict[str, Any]:
    """Load the readiness matrix and reconcile all shipped capability sources."""
    path = Path(matrix_path).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityLedgerError(f"cannot read capability ledger {path}: {exc}") from exc
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1 or not isinstance(payload.get("capabilities"), list):
        raise CapabilityLedgerError("capability ledger requires schema_version 1 and a capabilities list")
    _validate_matrix_capabilities(payload["capabilities"])
    result = dict(payload)
    repo_root = _repo_root_for_matrix(path)
    result["sources"] = _reconcile_sources(repo_root, payload["capabilities"]) if repo_root else {"counts": {}, "coverage": {}}
    return result


__all__ = [
    "CapabilityLedgerError",
    "capability_execution_admission",
    "project_capability_execution_admission",
    "load_capability_ledger",
]


if __name__ == "__main__":
    sys.exit(main())
