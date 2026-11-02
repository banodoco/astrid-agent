#!/usr/bin/env python3
"""Scaffold a local Remotion HtmlInCanvas effect element."""

# The canonical-entrypoint guard intentionally runs before imports.
# ruff: noqa: E402

from __future__ import annotations

from astrid.core.contracts.errors import AstridError, AstridValueError
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("rendering.html_canvas_effect")
import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.pack.personal import ensure_personal_pack, resolve_personal_packs_root

_EFFECT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates" / "card"
_PERSONAL_PACK_MANIFEST: dict[str, Any] = {
    "schema_version": 3,
    "id": "personal",
    "name": "Personal",
    "version": "0.1.0",
    "status": "active",
    "domain": "media",
    "stability": "stable",
    "support": "project",
    "visibility": "visible",
    "permissions": [
        {
            "id": "project_files",
            "reason": "Provides render-time visual element definitions consumed by the rendering pack.",
        }
    ],
    "description": "Versioned render-time visual elements and their immutable assets for Remotion timelines.",
    "keywords": ["element", "effect", "remotion", "overlay", "canvas"],
    "capabilities": [],
    "agent": {
        "purpose": "Use for project-local render-time visual elements.",
        "do_not_use_for": "This is an element pack, not an executor or renderer.",
        "normal_entrypoints": [],
    },
    "documentation": {
        "kind": "none",
        "reason": "Project-local authored elements do not require a pack skill document.",
    },
    "rendering": {},
}


def _title_from_id(effect_id: str) -> str:
    return " ".join(part.capitalize() for part in effect_id.split("-"))


def _component_source() -> str:
    return (_TEMPLATE_ROOT / "component.tsx").read_text(encoding="utf-8")


def _element_manifest(effect_id: str, label: str, description: str) -> dict:
    return {
        "schema_version": 1,
        "id": effect_id,
        "kind": "effect",
        "pack_id": "personal",
        "short_description": "DOM-to-canvas Remotion effect for post-processed cards.",
        "description": description,
        "keywords": ["html", "canvas", "remotion", "effect", "card"],
        "metadata": {
            "label": label,
            "description": description,
            "whenToUse": "Use for premium DOM-authored cards that need canvas, shader, glow, blur, or texture post-processing.",
            "render_requirements": {
                "remotion_min_version": "4.0.455",
                "uses_html_in_canvas": True,
                "recommended_gl": "angle",
                "fallback_gl": "swangle",
                "final_renderer": "rendering.render",
            },
            "limitations": [
                "Do not nest inside another HtmlInCanvas capture.",
                "Keep this as an effect element; rendering.render remains the final video renderer.",
            ],
        },
        "schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "subtitle": {"type": "string"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "background": {"type": "string"},
                "foreground": {"type": "string"},
                "accent": {"type": "string"},
                "postProcess": {
                    "type": "string",
                    "enum": ["none", "soft-blur", "glow", "vignette"],
                },
            },
        },
        "defaults": {
            "content": label,
            "subtitle": "DOM layout captured into canvas.",
            "width": 900,
            "height": 520,
            "postProcess": "glow",
        },
        "dependencies": {
            "js_packages": [],
            "python_requirements": [],
        },
    }


def _load_or_bootstrap_personal_manifest(personal_pack: Path) -> tuple[Path, dict[str, Any]]:
    """Load a v3 Personal manifest or return the minimal v3 bootstrap payload."""

    manifest_path = personal_pack / "pack.yaml"
    if not manifest_path.exists():
        return manifest_path, dict(_PERSONAL_PACK_MANIFEST)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise AstridValueError(f"personal pack manifest is not a regular file: {manifest_path}")
    try:
        payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AstridValueError(f"invalid personal pack manifest: {manifest_path}") from exc
    if not isinstance(payload, dict):
        raise AstridValueError(f"personal pack manifest must contain an object: {manifest_path}")
    if payload.get("schema_version") != 3 or payload.get("id") != "personal":
        raise AstridValueError(
            f"personal pack manifest must be a v3 manifest for pack 'personal': {manifest_path}"
        )
    if not isinstance(payload.get("rendering", {}), dict):
        raise AstridValueError(f"personal pack rendering declarations must be an object: {manifest_path}")
    return manifest_path, payload


def _write_personal_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def scaffold(
    *,
    effect_id: str,
    label: str | None,
    description: str | None,
    project_root: Path,
    out_path: Path,
    timeline_path: Path | None = None,
    assets_path: Path | None = None,
    force: bool = False,
) -> dict:
    if not _EFFECT_ID_RE.match(effect_id):
        raise AstridError(
            "effect id must be kebab-case, e.g. glass-product-card",
            valid_options=["glass-product-card", "simple-fade", "hero-banner"],
            recovery_command="pass a valid kebab-case effect id via --effect-id, e.g. glass-product-card",
        )

    resolved_label = label or _title_from_id(effect_id)
    resolved_description = description or (
        f"{resolved_label} uses Remotion HtmlInCanvas to capture DOM content and post-process it as a canvas texture."
    )
    packs_root = resolve_personal_packs_root()
    if packs_root is None:
        raise AstridValueError("no absolute personal packs root is configured")
    personal_pack = ensure_personal_pack(packs_root)
    element_root = personal_pack / "rendering" / "elements" / "effects" / effect_id
    personal_pack_manifest, pack_payload = _load_or_bootstrap_personal_manifest(personal_pack)
    declaration_key = f"effects/{effect_id}"
    expected_manifest_path = f"rendering/elements/effects/{effect_id}/element.yaml"
    expected_component_path = f"rendering/elements/effects/{effect_id}/component.tsx"
    rendering = pack_payload.setdefault("rendering", {})
    existing_declaration = rendering.get(declaration_key)
    if existing_declaration is not None:
        if not isinstance(existing_declaration, dict) or existing_declaration.get("type") != "element":
            raise AstridValueError(f"personal pack declaration is not an element: {declaration_key}")
        if existing_declaration.get("path") not in {None, expected_manifest_path}:
            raise AstridValueError(
                f"refusing to move an existing declaration outside its selected closure: {declaration_key}"
            )
    element_exists = element_root.exists() or element_root.is_symlink()
    if element_exists:
        if not force:
            raise FileExistsError(
                f"personal effect already exists: {element_root}; pass --force to overwrite"
            )
        if element_root.is_symlink() or not element_root.is_dir():
            raise OSError(f"refusing to force-overwrite non-directory personal effect: {element_root}")
        shutil.rmtree(element_root)

    element_root.mkdir(parents=True, exist_ok=False)
    component_path = element_root / "component.tsx"
    manifest_path = element_root / "element.yaml"
    component_path.write_text(_component_source(), encoding="utf-8")
    manifest_path.write_text(
        json.dumps(
            _element_manifest(effect_id, resolved_label, resolved_description),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    rendering[declaration_key] = {
        "type": "element",
        "path": expected_manifest_path,
        "resources": [
            {"kind": "implementation", "path": expected_component_path},
        ],
    }
    _write_personal_manifest(personal_pack_manifest, pack_payload)

    timeline_clip = {
        "id": f"{effect_id}-sample",
        "at": 0,
        "track": "overlay",
        "clipType": effect_id,
        "hold": 3,
        "params": {
            "content": resolved_label,
            "subtitle": "Generated by rendering.html_canvas_effect",
            "postProcess": "glow",
        },
    }
    preview_timeline = {
        "theme": "banodoco-default",
        "theme_overrides": {
            "visual": {
                "canvas": {"width": 1280, "height": 720, "fps": 30},
                "color": {"bg": "#050814", "fg": "#f8fbff", "accent": "#67e8f9"},
            }
        },
        "tracks": [{"id": "overlay", "kind": "visual", "label": "HTML Canvas Effect"}],
        "clips": [timeline_clip],
    }
    preview_assets = {"assets": {}}
    resolved_timeline_path = timeline_path or (out_path.parent / "timeline.json")
    resolved_assets_path = assets_path or (out_path.parent / "assets.json")
    resolved_timeline_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_assets_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_timeline_path.write_text(
        json.dumps(preview_timeline, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    resolved_assets_path.write_text(
        json.dumps(preview_assets, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = {
        "ok": True,
        "effect_id": effect_id,
        "element_root": str(element_root),
        "component": str(component_path),
        "manifest": str(manifest_path),
        "personal_pack_manifest": str(personal_pack_manifest),
        "declaration": declaration_key,
        "timeline_clip": timeline_clip,
        "preview_timeline": str(resolved_timeline_path),
        "preview_assets": str(resolved_assets_path),
        "template": str(_TEMPLATE_ROOT),
        "render_requirements": {
            "final_renderer": "rendering.render",
            "remotion_min_version": "4.0.455",
            "uses_html_in_canvas": True,
            "recommended_render_flag": "--gl=angle",
            "software_render_flag": "--gl=swangle",
        },
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scaffold a local Remotion HtmlInCanvas effect element."
    )
    parser.add_argument(
        "--effect-id", required=True, help="Bare kebab-case effect id, e.g. glass-product-card."
    )
    parser.add_argument("--label", help="Human-readable effect label.")
    parser.add_argument("--description", help="Short effect description.")
    parser.add_argument("--project-root", type=Path, default=REPO_ROOT, help="Astrid project root.")
    parser.add_argument("--out", type=Path, required=True, help="Report JSON output path.")
    parser.add_argument("--timeline", type=Path, help="Preview timeline JSON output path.")
    parser.add_argument("--assets", type=Path, help="Preview assets JSON output path.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing local element.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = scaffold(
            effect_id=args.effect_id,
            label=args.label,
            description=args.description,
            project_root=args.project_root.resolve(),
            out_path=args.out,
            timeline_path=args.timeline,
            assets_path=args.assets,
            force=args.force,
        )
    except (FileExistsError, OSError, ValueError) as exc:
        raise AstridError(
            str(exc),
            recovery_command="check the effect id, file paths, and permissions, then rerun",
        ) from exc

    # --- universal result manifest -----------------------
    out_file = args.out.resolve()
    manifest_path = out_file.parent / "manifest.json"
    manifest_dir = manifest_path.parent
    manifest = build_manifest(
        kind="html_canvas_effect",
        inputs={
            "effect_id": args.effect_id,
            "label": args.label,
            "description": args.description,
            "project_root": str(args.project_root.resolve()),
        },
        outputs=[
            {"path": out_file.name, "type": "file"},
            {
                "path": os.path.relpath(Path(report["element_root"]).resolve(), manifest_dir),
                "type": "directory",
            },
            {
                "path": os.path.relpath(Path(report["preview_timeline"]).resolve(), manifest_dir),
                "type": "file",
            },
            {
                "path": os.path.relpath(Path(report["preview_assets"]).resolve(), manifest_dir),
                "type": "file",
            },
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------

    print(f"html-canvas-effect: wrote {report['element_root']}")
    print(f"html-canvas-effect: report {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_pack_main("rendering.html_canvas_effect", lambda: main()))
