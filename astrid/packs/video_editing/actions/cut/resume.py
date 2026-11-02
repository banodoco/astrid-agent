"""Resume-mode helpers for the hype cut executor.

Extracted from ``run.py``. Handles the
``--timeline`` resume path where an existing hype.timeline.json is
re-processed for re-render or asset rebasing.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.pack_api import write_manifest
from astrid.pack_api import AstridError
from astrid.pack_api import METADATA_VERSION, AssetRegistry, PipelineMetadata, TimelineConfig, canonical_timeline_config, load_metadata, load_registry, load_timeline, materialize_output, save_metadata, save_registry, save_timeline
from astrid.pack_api import utc_now_seconds

from .timeline_build import _register_cut_outputs


@dataclass(frozen=True)
class ResumeModeResult:
    source_timeline_path: Path
    source_assets_path: Path
    out_dir: Path
    timeline_path: Path
    assets_path: Path
    metadata_path: Path
    registry: AssetRegistry
    config: TimelineConfig
    rendered_path: Path | None = None


def ensure_resume_mode_args(args: argparse.Namespace) -> None:
    conflicts: list[tuple[str, Any]] = [
        ("--scenes", args.scenes),
        ("--video", args.video),
        ("--shots", args.shots),
        ("--transcript", args.transcript),
        ("--primary-asset", args.primary_asset),
        ("--asset", args.asset),
    ]
    for flag, value in conflicts:
        if value not in (None, []):
            raise AstridError(f"--timeline cannot be combined with {flag}")


def build_resume_metadata(
    config: TimelineConfig,
    prior_meta: PipelineMetadata | None,
    *,
    render: bool,
    renderer: str,
) -> PipelineMetadata:
    clip_ids = [clip["id"] for clip in config["clips"]]
    prior_clips = prior_meta.get("clips", {}) if isinstance(prior_meta, dict) else {}
    clips: dict[str, dict[str, Any]] = {}
    if isinstance(prior_clips, dict):
        for clip_id in clip_ids:
            clip_meta = prior_clips.get(clip_id)
            if isinstance(clip_meta, dict):
                clips[clip_id] = dict(clip_meta)
    prior_sources = prior_meta.get("sources", {}) if isinstance(prior_meta, dict) else {}
    sources = {key: dict(value) for key, value in prior_sources.items()} if isinstance(prior_sources, dict) else {}
    return {
        "version": METADATA_VERSION,
        "generated_at": utc_now_seconds(),
        "pipeline": {
            "steps_run": ["cut"],
            "tool_versions": {"cut.py": "sprint3"},
            "config_snapshot": {
                "mode": "timeline_resume",
                "render": render,
                "renderer": renderer,
            },
        },
        "clips": clips,
        "sources": sources,
    }


def _require_materialized_registry(registry: AssetRegistry) -> None:
    """Fail closed unless every resumed asset is an attempt-local file.

    Resume is an offline operation.  It must never turn a URL, cache record,
    or relative locator into a fetch/read through another authority.
    """
    for key, entry in registry.get("assets", {}).items():
        if not isinstance(entry, dict):
            raise AstridError(f"Resume asset registry entry {key!r} is invalid")
        unsupported = {"url", "uri", "path", "source_path", "locator", "cache"}.intersection(entry)
        if unsupported:
            raise AstridError(
                f"Resume asset {key!r} contains unsupported locator fields: "
                f"{', '.join(sorted(unsupported))}",
                recovery_command="Supply the runtime-materialized asset registry for this attempt",
            )
        file_value = entry.get("file")
        if not isinstance(file_value, str) or not file_value or not Path(file_value).is_absolute():
            raise AstridError(
                f"Resume asset {key!r} is not runtime-materialized",
                recovery_command="Supply an absolute attempt-local materialized file in hype.assets.json",
            )
        materialized = Path(file_value).resolve()
        if materialized.is_symlink() or not materialized.is_file():
            raise AstridError(
                f"Resume materialized asset {key!r} is missing: {materialized}",
                recovery_command="Re-run the task so the runtime host can materialize the asset",
            )


def execute_resume_mode(args: argparse.Namespace) -> ResumeModeResult:
    ensure_resume_mode_args(args)

    timeline_path = args.timeline.resolve()
    if not timeline_path.is_file():
        raise AstridError(f"Timeline file not found: {timeline_path}")
    source_dir = timeline_path.parent
    assets_path_in = args.assets.resolve() if args.assets is not None else source_dir / "hype.assets.json"
    if not assets_path_in.is_file():
        raise AstridError(f"Assets file not found: {assets_path_in}")

    config = load_timeline(timeline_path)
    registry = load_registry(assets_path_in)
    _require_materialized_registry(registry)

    # Backfill an old attempt artifact without consulting a workspace theme
    # hierarchy. Runtime-owned style documents must be materialized explicitly.
    if "output" not in config:
        config["output"] = materialize_output(
            config,
            {"visual": {"canvas": {"width": 1920, "height": 1080, "fps": 30}}},
        )

    missing_assets = sorted(
        {
            asset
            for clip in config["clips"]
            for asset in [clip.get("asset")]
            if asset is not None and asset not in registry["assets"]
        }
    )
    if missing_assets:
        quoted = ", ".join(repr(asset) for asset in missing_assets)
        raise AstridError(f"Timeline references assets missing from registry: {quoted}")

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    timeline_path_out = out_dir / "hype.timeline.json"
    assets_path_out = out_dir / "hype.assets.json"
    metadata_path_out = out_dir / "hype.metadata.json"
    canonical_timeline_config(config)
    save_timeline(config, timeline_path_out)
    saved_registry = registry
    if out_dir == assets_path_in.parent.resolve():
        save_registry(saved_registry, assets_path_out)
    else:
        save_registry(saved_registry, assets_path_out)

    prior_meta_path = source_dir / "hype.metadata.json"
    prior_meta = load_metadata(prior_meta_path) if prior_meta_path.exists() else None
    save_metadata(
        build_resume_metadata(config, prior_meta, render=False, renderer=args.renderer),
        metadata_path_out,
    )

    rendered_path: Path | None = None
    _register_cut_outputs(
        out_dir=out_dir,
        stage="cut.resume",
        metadata={"mode": "timeline_resume", "renderer": args.renderer},
        rendered_path=rendered_path,
    )
    # --- universal result manifest -------------------
    manifest_outputs = [
        {"path": "hype.timeline.json", "type": "file"},
        {"path": "hype.assets.json", "type": "file"},
        {"path": "hype.metadata.json", "type": "file"},
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "kind": "cut",
        "inputs": {
            "timeline": str(timeline_path),
        },
        "outputs": manifest_outputs,
        "created": datetime.now(timezone.utc).isoformat(),
        "warnings": [],
    }
    write_manifest(out_dir / "manifest.json", manifest)
    return ResumeModeResult(
        source_timeline_path=timeline_path,
        source_assets_path=assets_path_in,
        out_dir=out_dir,
        timeline_path=timeline_path_out,
        assets_path=assets_path_out,
        metadata_path=metadata_path_out,
        registry=saved_registry,
        config=config,
        rendered_path=rendered_path,
    )


def run_resume_mode(args: argparse.Namespace) -> int:
    result = execute_resume_mode(args)
    summary = (
        f"timeline={result.timeline_path} assets={result.assets_path} metadata={result.metadata_path}"
    )
    if result.rendered_path is not None:
        summary = f"{summary} hype={result.rendered_path}"
    print(f"wrote {summary}")
    return 0
