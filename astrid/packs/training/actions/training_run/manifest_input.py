"""Training-run manifest input normalization."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from astrid.pack_api import write_json_atomic
from astrid.packs.training.shared.manifest import validate_schema
from astrid.packs.training.shared.manifest_adapters.ai_toolkit_ltx import (
    AiToolkitLtxAdapter,
)


class TrainingManifestError(ValueError):
    """Raised when an input manifest cannot be normalized for training."""


@dataclass(frozen=True)
class NormalizedManifest:
    source_manifest_path: Path
    normalized_manifest_path: Path
    source_format: str
    state: dict[str, str]


def normalize_ai_toolkit_manifest(
    manifest_path: str | Path,
    training_run_dir: str | Path,
    *,
    repo_root: Path = PACK_ROOT,
    allow_inferred_caption_sidecars: bool = True,
) -> NormalizedManifest:
    """Normalize canonical or flat training manifests into the training run."""
    source = Path(manifest_path).expanduser().resolve()
    run_dir = Path(training_run_dir).expanduser().resolve()
    if not source.is_file():
        raise TrainingManifestError(f"manifest not found: {source}")

    data = _load_json_object(source)
    normalized = run_dir / "manifests" / "ai-toolkit-ltx" / "manifest.json"

    if isinstance(data.get("items"), list):
        source_format = "canonical-final"
        flat = _adapt_canonical_manifest(data, source, normalized, repo_root=repo_root)
    elif isinstance(data.get("clips"), list):
        source_format = "ai-toolkit-ltx-flat"
        flat = _normalize_flat_manifest(
            data,
            source,
            repo_root=repo_root,
            allow_inferred_caption_sidecars=allow_inferred_caption_sidecars,
        )
    else:
        raise TrainingManifestError("manifest must contain either canonical items[] or flat clips[]")

    validate_schema(flat, "ai-toolkit-adapter-manifest.schema.json")
    _validate_flat_files(flat, source.parent, repo_root=repo_root)
    write_json_atomic(normalized, flat)

    state = {
        "source_manifest_path": str(source),
        "normalized_manifest_path": str(normalized),
        "source_format": source_format,
    }
    write_json_atomic(normalized.parent / "manifest_state.json", state)
    return NormalizedManifest(source, normalized, source_format, state)


def compatibility_manifest_path(dataset_run_dir: str | Path) -> Path:
    """Return the dataset-builder flat adapter path used as compatibility source."""
    return Path(dataset_run_dir).expanduser().resolve() / "ai-toolkit-ltx.manifest.json"


def seed_from_dataset_run(
    dataset_run_dir: str | Path,
    training_run_dir: str | Path,
    *,
    repo_root: Path = PACK_ROOT,
) -> NormalizedManifest:
    """Normalize the dataset-builder compatibility flat manifest into a training run."""
    return normalize_ai_toolkit_manifest(compatibility_manifest_path(dataset_run_dir), training_run_dir, repo_root=repo_root)


def _load_json_object(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TrainingManifestError(f"invalid JSON in manifest {path}: {exc.msg}") from exc
    if not isinstance(data, Mapping):
        raise TrainingManifestError("manifest must be a JSON object")
    return dict(data)


def _adapt_canonical_manifest(
    data: Mapping[str, Any],
    source: Path,
    normalized_path: Path,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for index, raw in enumerate(data.get("items", [])):
        if not isinstance(raw, Mapping):
            raise TrainingManifestError(f"items[{index}] is not an object")
        item = dict(raw)
        item_id = str(item.get("item_id") or "")
        if not item_id:
            raise TrainingManifestError(f"items[{index}] missing item_id")
        for key in ("media_path", "caption_file"):
            if not item.get(key):
                raise TrainingManifestError(f"{item_id}: canonical item missing {key}")
            item[key] = str(_resolve_existing_path(str(item[key]), source.parent, repo_root, label=key, item_id=item_id))
        items.append(item)

    adapter = AiToolkitLtxAdapter(
        out_path=normalized_path,
        source_manifest=source,
        repo_root=repo_root,
    )
    errors = adapter.validate(items)
    if errors:
        raise TrainingManifestError("; ".join(errors))
    return json.loads(adapter.export(items).read_text(encoding="utf-8"))


def _normalize_flat_manifest(
    data: Mapping[str, Any],
    source: Path,
    *,
    repo_root: Path,
    allow_inferred_caption_sidecars: bool,
) -> dict[str, Any]:
    flat = dict(data)
    clips = flat.get("clips")
    if not isinstance(clips, list) or not clips:
        raise TrainingManifestError("flat manifest must contain at least one clip")

    normalized_clips: list[dict[str, Any]] = []
    for index, raw in enumerate(clips):
        if not isinstance(raw, Mapping):
            raise TrainingManifestError(f"clips[{index}] is not an object")
        clip = dict(raw)
        clip_file = str(clip.get("clip_file") or clip.get("path") or "")
        if not clip_file:
            raise TrainingManifestError(f"clips[{index}] missing clip_file/path")
        clip_id = str(clip.get("clip_id") or Path(clip_file).stem)
        clip["clip_id"] = clip_id
        clip_path = _resolve_existing_path(clip_file, source.parent, repo_root, label="clip_file", item_id=clip_id)
        clip["clip_file"] = _manifest_path_string(clip_path, repo_root=repo_root)
        clip["path"] = clip["clip_file"]

        caption_value = str(clip.get("caption_file") or "")
        if caption_value:
            caption_path = _resolve_existing_path(caption_value, source.parent, repo_root, label="caption_file", item_id=clip_id)
        elif allow_inferred_caption_sidecars:
            caption_path = clip_path.with_name(f"{clip_id}.caption.json")
            if not caption_path.is_file():
                raise TrainingManifestError(f"{clip_id}: inferred caption sidecar missing: {caption_path}")
        else:
            raise TrainingManifestError(f"{clip_id}: caption_file is required")
        clip["caption_file"] = _manifest_path_string(caption_path, repo_root=repo_root)
        normalized_clips.append(clip)

    flat["clips"] = normalized_clips
    return flat


def _validate_flat_files(flat: Mapping[str, Any], manifest_dir: Path, *, repo_root: Path) -> None:
    missing: list[str] = []
    for index, clip in enumerate(flat.get("clips", [])):
        if not isinstance(clip, Mapping):
            missing.append(f"clips[{index}] is not an object")
            continue
        clip_id = str(clip.get("clip_id") or f"clips[{index}]")
        for key in ("clip_file", "caption_file"):
            value = str(clip.get(key) or "")
            if not value:
                missing.append(f"{clip_id}: missing {key}")
                continue
            try:
                _resolve_existing_path(value, manifest_dir, repo_root, label=key, item_id=clip_id)
            except TrainingManifestError as exc:
                missing.append(str(exc))
    if missing:
        raise TrainingManifestError("; ".join(missing[:10]))


def _resolve_existing_path(value: str, manifest_dir: Path, repo_root: Path, *, label: str, item_id: str) -> Path:
    candidates = _path_candidates(value, manifest_dir, repo_root)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise TrainingManifestError(f"{item_id}: {label} missing: {candidates[-1]}")


def _path_candidates(value: str, manifest_dir: Path, repo_root: Path) -> list[Path]:
    path = Path(value).expanduser()
    if path.is_absolute():
        return [path.resolve()]
    repo_candidate = (repo_root / path).resolve()
    manifest_candidate = (manifest_dir / path).resolve()
    if repo_candidate == manifest_candidate:
        return [repo_candidate]
    return [repo_candidate, manifest_candidate]


def _manifest_path_string(path: Path, *, repo_root: Path) -> str:
    resolved = path.expanduser().resolve()
    try:
        return resolved.relative_to(repo_root.expanduser().resolve()).as_posix()
    except ValueError:
        return str(resolved)
