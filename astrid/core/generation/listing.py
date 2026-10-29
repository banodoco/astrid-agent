"""Registry-backed generation inventory and conservative, read-only readiness."""
from __future__ import annotations

import argparse
import json
import os
from functools import lru_cache
from typing import Any

from astrid.core.generation.availability import availability_declaration
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

    for entry in ModelRegistry.load_default().list_all(include_closed=True):
        for mode, spec in entry.modes.items():
            for backend, target in spec.backends.items():
                add(entry.id, entry.modality, mode, backend, target.template, "catalog")
    for spec in CANONICAL_CAPABILITIES.values():
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


@lru_cache(maxsize=128)
def _template_requirements(template: str) -> tuple[tuple[str, str, str], ...] | None:
    """Use VibeComfy's graph resolver; absent or unresolved evidence is unknown."""
    try:
        from vibecomfy.cli_loader import load_workflow_any
        from vibecomfy.model_assets import resolve_referenced_assets
        resolved, unresolved = resolve_referenced_assets(load_workflow_any(template))
        if unresolved or not resolved:
            return None
        requirements = tuple(sorted((item["class_type"], item["field"], item["value"]) for item in resolved))
        if not all(all(isinstance(part, str) and part for part in item) for item in requirements):
            return None
        return requirements
    except (ImportError, KeyError, OSError, ValueError, TypeError, AttributeError):
        return None


def _model_inventory(schema: Any, requirements: tuple[tuple[str, str, str], ...] | None) -> tuple[bool | None, list[str]]:
    if requirements is None or not isinstance(schema, dict):
        return None, []
    present = []
    for node, field, name in requirements:
        try:
            choices = schema[node]["input"]["required"][field][0]
        except (KeyError, TypeError, IndexError):
            return None, sorted(present)
        if not isinstance(choices, list):
            return None, sorted(present)
        if name in choices:
            present.append(name)
    return len(present) == len(requirements), sorted(present)


def _targets() -> list[tuple[str, Any]]:
    from astrid.packs.vibecomfy.local import scan
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient, MAX_JSON_BYTES

    targets = []
    for server in scan([8188, 8000]).get("servers", []):
        try:
            target = ComfyTarget(server["url"], "attached")
            schema = ComfyTargetClient(target).request_json("/object_info", timeout=2, response_limit=MAX_JSON_BYTES)
            targets.append((target.url, schema))
        except Exception:  # Untrusted endpoint failures are data, never diagnostics.
            # Preserve the discovered target even if the model probe fails.
            if isinstance(server, dict) and isinstance(server.get("url"), str):
                targets.append((server["url"], None))
    return sorted(targets, key=lambda pair: pair[0])


def generation_rows(kind: str | None = None) -> list[dict[str, Any]]:
    rows = [row for row in catalog_rows() if kind is None or row["kind"] == kind]
    targets = _targets() if any(row["backend"] == "local" for row in rows) else []
    output = []
    for row in rows:
        base = dict(row)
        if row["availability"] != "available":
            output.append({**base, "target": "declaration", "ready": False, "reason": row["reason"], "fix": "Use an implemented generation route; see the availability declaration.", "models_present": []})
        elif row["backend"] == "local":
            for target, schema in targets or [("attached", None)]:
                complete, present = _model_inventory(schema, _template_requirements(row["template"]))
                if schema is None:
                    reason, fix = "no attached ComfyUI target or model probe unavailable", "Start ComfyUI on loopback and make /object_info available."
                elif complete is None:
                    reason, fix = "model requirements or target inventory unavailable", "Install VibeComfy and inspect the template and target model inventory."
                elif not complete:
                    reason, fix = "required models missing", "Install the template's required models in the attached ComfyUI."
                elif row["id"] in {"z-image", "z-image-turbo"}:
                    reason, fix = "Z-Image Turbo route references the Base checkpoint", "Use a verified Turbo workflow after the template is corrected."
                else:
                    reason, fix = "attached generation route not yet available", "Use the existing vibecomfy.run local_server route or wait for attached generation support."
                output.append({**base, "target": target, "ready": False, "reason": reason, "fix": fix, "models_present": present})
        elif row["backend"] == "cloud":
            has_key = any(name == "FAL_KEY" for name in os.environ)
            output.append({**base, "target": "cloud", "ready": has_key, "reason": "key present" if has_key else "no key", "fix": "" if has_key else "Set FAL_KEY in the environment.", "models_present": []})
        elif row["backend"] == "codex":
            from astrid.core.generation.backends.codex import codex_unavailable_reason
            reason = codex_unavailable_reason()
            output.append({**base, "target": "codex", "ready": reason is None, "reason": reason or "available", "fix": "Install and sign in to Codex." if reason else "", "models_present": []})
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
            status = "ready" if row["ready"] else row["reason"]
            fix = f"; fix: {row['fix']}" if row["fix"] else ""
            print(f"{row['id']}  {row['kind']}  {row['mode'] or '-'}  {row['target']}  {status}{fix}")
    return 0
