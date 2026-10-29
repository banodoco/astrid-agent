"""Registry-backed generation inventory and conservative, read-only readiness."""
from __future__ import annotations

import argparse
import json
import os
from typing import Any

from astrid.core.generation.availability import availability_declaration
from astrid.core.generation.backends.comfy_target import (
    ComfyModelMissing, ComfyTarget, ComfyTargetClient, MAX_JSON_BYTES,
    resolve_comfy_models,
)
from astrid.core.model_catalog.registry import ModelRegistry
from astrid.packs.vibecomfy.direct_image.compiler import CANONICAL_CAPABILITIES
from astrid.sdk.invocation import discover


def catalog_rows() -> list[dict[str, Any]]:
    """Static inventory shared by the CLI and the generated capability index.

    The caller controls pack discovery scope; this function never probes a host
    or examines credentials. Backend membership is not a readiness claim.
    """
    rows: list[dict[str, Any]] = []
    def add(id: str, kind: str, mode: str = "", backend: str = "", template: str = "", source: str = "", availability: str = "available", reason: str = "") -> None:
        rows.append(dict(id=id, kind=kind, mode=mode, backend=backend, template=template,
                         source=source, availability=availability, reason=reason))

    registry = ModelRegistry.load_default()
    for entry in registry.list_all(include_closed=True):
        for mode, spec in entry.modes.items():
            for backend, target in spec.backends.items():
                add(entry.id, entry.modality, mode, backend, target.template, "catalog")
    for spec in CANONICAL_CAPABILITIES.values():
        if spec.model_id == "z-image-turbo":
            # Public direct-image spelling of the catalog's z-image identity.
            # Inventory follows G23's declaration, not the older leaf binding.
            _, mode = registry.get_by_mode("z-image", spec.mode)
            add(spec.model_id, "image", spec.mode, "local", mode.backends["local"].template, "catalog-alias")
            rows[-1]["registry_id"] = "z-image"
        else:
            if any((row["id"], row["mode"], row["backend"], row["template"]) ==
                   (spec.model_id, spec.mode, "local", spec.template_id) for row in rows):
                continue
            add(spec.model_id, "image", spec.mode, "local", spec.template_id, "direct-image")
    declaration = availability_declaration()
    actions = declaration.get("actions", {})
    for capability in discover().actions:
        if not capability.id.startswith("generation.") and capability.id not in actions:
            continue
        name = capability.id.lower()
        kind = "image" if "image" in name or "thumbnail" in name else "video" if "video" in name else "audio" if "audio" in name or "speech" in name else "generation"
        record = actions.get(capability.id, {})
        unavailable = record.get("available") is False
        error = record.get("error", {})
        add(capability.id, kind, source="action", availability="unavailable" if unavailable else "available", reason=error.get("message", "") if unavailable else "")
    existing = {row["id"] for row in rows if row["source"] == "action"}
    for action, record in actions.items():
        if action not in existing:
            add(action, "image" if "thumbnail" in action or "sampler" in action else "generation", source="action", availability="unavailable" if record.get("available") is False else "available", reason=record.get("error", {}).get("message", ""))
    for route, fixture in declaration.get("fixture_only_routes", {}).items():
        if fixture:
            add(route, "fixture", source="fixture", availability="fixture-only", reason="fixture-only route")
    return sorted(rows, key=lambda row: (row["id"], row["kind"], row["mode"], row["backend"], row["source"], row["template"]))


def _targets() -> list[tuple[ComfyTarget, dict[str, Any] | None]]:
    from astrid.packs.vibecomfy.local import scan
    targets = {}
    for server in scan([8188, 8000]).get("servers", []):
        try:
            target = ComfyTarget(server["url"], "attached", facts=server)
        except (KeyError, TypeError, ValueError):
            continue
        if target.url in targets:
            continue
        try:
            schema = ComfyTargetClient(target).request_json("/object_info", timeout=2, response_limit=MAX_JSON_BYTES)
            if not isinstance(schema, dict):
                schema = None
        except Exception:  # Untrusted endpoint failures are data, never diagnostics.
            schema = None
        targets[target.url] = (target, schema)
    return [targets[url] for url in sorted(targets)]


class _InventoryClient(ComfyTargetClient):
    """Resolve every row against one bounded, read-only target capture."""

    def __init__(self, target: ComfyTarget, schema: dict[str, Any]):
        super().__init__(target)
        self._inventory = schema

    def object_info(self) -> dict[str, Any]:
        return self._inventory


def _local_readiness(row: dict[str, Any], registry: ModelRegistry,
                     target: ComfyTarget | None, schema: dict[str, Any] | None) -> dict[str, Any]:
    result = {"target": target.url if target else "attached", "target_kind": "attached",
              "ready": False, "readiness_scope": "target_inventory", "models_present": []}
    if target is None or schema is None:
        return {**result, "reason": "no attached ComfyUI target or model probe unavailable",
                "fix": "Start ComfyUI on loopback and make /object_info available."}
    try:
        model_id = row.get("registry_id", row["id"])
        _, mode = registry.get_by_mode(model_id, row["mode"])
        spec = mode.backends.get("local")
        if not spec or not spec.model_roles or spec.template != row["template"]:
            raise KeyError("no matching catalog role declaration")
    except KeyError:
        return {**result, "reason": "model requirements unavailable in catalog",
                "fix": "Inspect the workflow; add exact loader roles to its model catalog declaration."}
    try:
        plan = resolve_comfy_models(_InventoryClient(target, schema), registry, model_id, row["mode"])
    except ComfyModelMissing as exc:
        roles = sorted(exc.failures)
        requirements = [f"{role} ({' or '.join(spec.model_roles[role].names)})" for role in roles]
        return {**result, "reason": "required models missing or ambiguous", "missing_roles": roles,
                "fix": "Provide exactly one declared choice per role in the attached ComfyUI: " + "; ".join(requirements) + "."}
    except Exception:  # Do not expose endpoint or resolver exception contents.
        return {**result, "reason": "target model probe unavailable",
                "fix": "Check the attached ComfyUI /object_info model inventory."}
    return {**result, "ready": True, "reason": "required model roles present (model bytes unverified)",
            "fix": "", "models_present": sorted(plan["roles"].values()),
            "model_roles": plan["roles"], "model_bytes": plan["model_bytes"]}


def generation_rows(kind: str | None = None) -> list[dict[str, Any]]:
    rows = [row for row in catalog_rows() if kind is None or row["kind"] == kind]
    targets = _targets() if any(row["backend"] == "local" for row in rows) else []
    registry = ModelRegistry.load_default() if any(row["backend"] == "local" for row in rows) else None
    output = []
    for row in rows:
        base = dict(row)
        if row["availability"] != "available":
            output.append({**base, "target": "declaration", "ready": False, "reason": row["reason"], "fix": "Use an implemented generation route; see the availability declaration.", "models_present": []})
        elif row["backend"] == "local":
            assert registry is not None
            for target, schema in targets or [(None, None)]:
                output.append({**base, **_local_readiness(row, registry, target, schema)})
        elif row["backend"] in {"cloud", "wavespeed"}:
            key_name = "FAL_KEY" if row["backend"] == "cloud" else "WAVESPEED_API_KEY"
            # os._Environ.__contains__ can fetch a value via __getitem__.
            # Iteration inspects names only, including for an empty value.
            has_key = any(name == key_name for name in os.environ)
            output.append({**base, "target": row["backend"], "ready": has_key, "readiness_scope": "key_presence", "reason": "key present" if has_key else "no key", "fix": "" if has_key else f"Set {key_name} in the environment.", "models_present": []})
        elif row["backend"] == "codex":
            from astrid.core.generation.backends.codex import codex_unavailable_reason
            reason = codex_unavailable_reason()
            output.append({**base, "target": "codex", "ready": reason is None, "readiness_scope": "installation_presence", "reason": reason or "binary and auth file present", "fix": "Install and sign in to Codex." if reason else "", "models_present": []})
        else:
            output.append({**base, "target": row["backend"] or "action", "ready": False, "reason": "readiness not established", "fix": "Inspect the capability's requirements before invocation.", "models_present": []})
    return sorted(output, key=lambda row: (row["id"], row["kind"], row["mode"], row["backend"], row["source"], row["target"], row["template"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astrid generate", description="Discover generation capabilities and readiness.")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list generation capabilities")
    listing.add_argument("--kind", choices=("image", "video", "audio", "generation", "fixture"))
    listing.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    rows = generation_rows(args.kind)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
    else:
        for row in rows:
            status = row["reason"]
            if row["ready"] and row.get("readiness_scope") == "target_inventory":
                status = f"ready: {status}"
            fix = f"; fix: {row['fix']}" if row["fix"] else ""
            print(f"{row['id']}  {row['kind']}  {row['mode'] or '-'}  {row['target']}  {status}{fix}")
    return 0
