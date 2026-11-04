"""Scene-only answers through the managed filmstrip artifact."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from astrid.core._shared.result_manifest import build_manifest, write_manifest


def _scene_only_label_unavailable() -> dict[str, object]:
    """Use the existing revision-label shape when Scene is advisory unavailable."""
    return {
        "status": "unavailable",
        "label": "revision unavailable",
        "actual_revision_id": None,
        "base_revision_id": None,
    }


def execute_scene_only(
    args,
    authority: Mapping[str, Any],
    *,
    label_snapshot: Callable[..., dict[str, object]],
) -> dict[str, Any]:
    """Return public Scene answers through the ordinary filmstrip artifact."""
    from astrid.core.foundation.paths import REPO_ROOT
    from astrid.core.rendering import scene_answers
    from astrid.core.rendering.remotion_runtime import default_composition_id
    from astrid.core.rendering.verified_source import inherited_source

    snapshot = authority.get("capture_snapshot") or authority.get("filmstrip_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("project_slug") != args.project_slug:
        raise ValueError("Scene-only authority does not match the project.")
    timeline_path = getattr(args, "timeline", None)
    assets_path = getattr(args, "assets_registry", None)
    if timeline_path is None or assets_path is None:
        raise ValueError("Scene-only authority did not receive the materialized timeline and registry.")
    duration_frames = snapshot.get("duration_frames")
    if type(duration_frames) is not int or duration_frames <= 0:
        raise ValueError("Scene-only authority has no positive canonical frame extent.")
    frames = range(duration_frames)
    answers: list[dict[str, object]] = []
    scene_clips_output: dict[str, object] = {}
    receipt_provenance: dict[str, str] | None = None
    label = _scene_only_label_unavailable()
    out_root = args.out.expanduser().resolve()
    try:
        source = inherited_source(timeline_path, assets_path)
        if source is None:
            raise scene_answers.SceneQueryUnavailable("exact retained Runtime source required")
        source.validate_snapshot(snapshot)
        bound = source.read()
        bound_snapshot = bound.get("snapshot") if isinstance(bound, Mapping) else None
        if not isinstance(bound_snapshot, Mapping):
            raise scene_answers.SceneQueryUnavailable("verified source has no capture snapshot")
        if bound_snapshot.get("project_slug") != args.project_slug:
            raise scene_answers.SceneQueryUnavailable("verified source does not match the project")
        bound_duration_frames = bound_snapshot.get("duration_frames")
        if (type(bound_duration_frames) is not int or bound_duration_frames <= 0
                or bound_duration_frames != duration_frames):
            raise scene_answers.SceneQueryUnavailable("verified source frame extent differs from authority")
        config = bound_snapshot.get("config")
        config_clips = config.get("clips") if isinstance(config, Mapping) else None
        if (not isinstance(config_clips, list)
                or any(not isinstance(clip, Mapping) or type(clip.get("id")) is not str
                       or not clip["id"] for clip in config_clips)):
            raise scene_answers.SceneQueryUnavailable("verified source has missing or malformed config.clips")
        config_ids = [clip["id"] for clip in config_clips]
        if len(set(config_ids)) != len(config_ids):
            raise scene_answers.SceneQueryUnavailable("verified source has duplicate compiled clip IDs")
        bound_metadata = bound_snapshot.get("metadata")
        scene_clips = bound_metadata.get("scene_clips") if isinstance(bound_metadata, Mapping) else None
        if (not isinstance(scene_clips, Mapping) or set(scene_clips) != set(config_ids)
                or any(not isinstance(value, Mapping)
                       or type(value.get("address")) is not str or not value["address"]
                       for value in scene_clips.values())):
            raise scene_answers.SceneQueryUnavailable("verified source has no exact compiled scene address map")
        capture_identity = (
            bound_metadata.get("capture_identity")
            if isinstance(bound_metadata, Mapping)
            else None
        )
        if (not isinstance(capture_identity, str)
                or len(capture_identity) != 71
                or not capture_identity.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in capture_identity[7:])
                or bound_snapshot.get("video_digest") != capture_identity):
            raise scene_answers.SceneQueryUnavailable("verified source has no exact capture identity")
        source_timeline_id = bound_snapshot.get("timeline_id")
        if not isinstance(source_timeline_id, str) or not source_timeline_id:
            raise scene_answers.SceneQueryUnavailable("verified source has no exact timeline identity")
        out_root.mkdir(parents=True, exist_ok=True)
        project_dir = Path(
            os.environ.get("ASTRID_REMOTION_PROJECT_DIR") or (REPO_ROOT / "remotion")
        ).resolve()
        with tempfile.TemporaryDirectory(prefix=".scene-only-", dir=str(out_root)) as temporary:
            answers = scene_answers._prepared_host_frames_at(
                Path(timeline_path),
                Path(assets_path),
                frames,
                staged_video=out_root / ".scene-only-unused.mp4",
                provenance_out_path=out_root / ".scene-only-provenance.json",
                project_dir=project_dir,
                composition_id=default_composition_id(),
                theme_path=getattr(args, "theme_path", None),
                min_free_gb=None,
                frame_output_dir=Path(temporary),
                materialized_root=getattr(args, "materialized_root", None),
                materialized_objects=getattr(args, "materialized_objects", None),
            )
        expected_frames = range(duration_frames)
        if (not isinstance(answers, list) or len(answers) != duration_frames
                or any(not isinstance(answer, Mapping)
                       or type(answer.get("frame")) is not int
                       or answer["frame"] != frame
                       for answer, frame in zip(answers, expected_frames))):
            raise scene_answers.SceneQueryUnavailable("public Scene returned an incomplete or misordered frame extent")
        if any(
            not isinstance(answer.get("layers"), list)
            or any(not isinstance(layer, Mapping) or layer.get("clipId") not in scene_clips
                   for layer in answer["layers"])
            for answer in answers
        ):
            raise scene_answers.SceneQueryUnavailable("public Scene returned an unaddressed compiled ID")
        source.validate_snapshot(snapshot)
        # Publish labels and maps only after Root succeeded against the
        # unchanged host-owned binding; never copy them from outer metadata.
        label = label_snapshot(bound_snapshot, source=source)
        scene_clips_output = dict(scene_clips)
        receipt_provenance = {
            "video_digest": capture_identity,
            "timeline_id": source_timeline_id,
        }
    except (scene_answers.SceneQueryUnavailable, OSError, TypeError, ValueError, KeyError):
        answers = []
        scene_clips_output = {}
        label = _scene_only_label_unavailable()

    out_root.mkdir(parents=True, exist_ok=True)
    pack_root = out_root / "filmstrip-view"
    pack_root.mkdir(parents=True, exist_ok=True)
    scene_path = pack_root / "scene-answers.json"
    scene_path.write_text(
        json.dumps(
            {
                "answers": answers,
                "scene_clips": scene_clips_output,
                "snapshot_label": label,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    nested_manifest = build_manifest(
        kind="timeline_filmstrip",
        created="1970-01-01T00:00:00Z",
        inputs={
            "timeline_id": snapshot.get("timeline_id"),
            "render_run_id": snapshot.get("render_run_id"),
            "scene_only": True,
        },
        outputs=[
            {
                "path": scene_path.name,
                "type": "file",
                "role": "result",
                "is_primary": True,
                "label": scene_path.name,
            }
        ],
        entrypoints={"scene": scene_path.name},
        timeline_ids=[snapshot.get("timeline_id")],
    )
    nested_manifest_path = pack_root / "manifest.json"
    write_manifest(nested_manifest_path, nested_manifest)
    bundle = out_root / "filmstrip-bundle.zip"
    from .filmstrip_execution import _write_zip_atomic

    _write_zip_atomic(bundle, pack_root)

    def receipt_entry(
        name: str,
        path: Path,
        *,
        primary: bool = False,
        provenance: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "name": name,
            "path": path.relative_to(out_root).as_posix(),
            "content_hash": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "role": "result" if primary else "auxiliary",
            "is_primary": primary,
            "durability": "durable" if primary else "temporary",
        }
        if provenance is not None:
            entry["provenance"] = dict(provenance)
        return entry

    host_manifest_path = out_root / "manifest.json"
    write_manifest(
        host_manifest_path,
        build_manifest(
            kind="timeline_filmstrip_result",
            created="1970-01-01T00:00:00Z",
            inputs={
                "timeline_id": snapshot.get("timeline_id"),
                "render_run_id": snapshot.get("render_run_id"),
                "scene_only": True,
            },
            outputs=[
                receipt_entry("filmstrip_manifest", nested_manifest_path),
                receipt_entry(
                    "filmstrip_bundle", bundle, primary=True,
                    provenance=receipt_provenance,
                ),
            ],
            entrypoints={
                "manifest": "filmstrip-view/manifest.json",
                "scene": "filmstrip-view/scene-answers.json",
                "bundle": "filmstrip-bundle.zip",
            },
        ),
    )
    manifest_digest = "sha256:" + hashlib.sha256(nested_manifest_path.read_bytes()).hexdigest()
    bundle_digest = "sha256:" + hashlib.sha256(bundle.read_bytes()).hexdigest()
    entrypoints = {
        "manifest": "filmstrip-view/manifest.json",
        "scene": "filmstrip-view/scene-answers.json",
        "bundle": "filmstrip-bundle.zip",
    }
    return {
        "returncode": 0,
        "run_root": str(out_root),
        "manifest_path": str(nested_manifest_path),
        "timeline_ids": [snapshot.get("timeline_id")],
        "snapshot_label": label,
        "cas": {"filmstrip_manifest": manifest_digest, "filmstrip_bundle": bundle_digest},
        "entrypoints": entrypoints,
        "outputs": {
            "pack_root": str(pack_root),
            "manifest_path": str(nested_manifest_path),
            "scene_answers": str(scene_path),
            "filmstrip_bundle": str(bundle),
            "entrypoints": entrypoints,
            "snapshot_label": label,
        },
    }
