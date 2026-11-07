"""AI Toolkit LTX flat manifest adapter."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any


from ..items import repo_relative_path, utc_now_iso
from ..manifest import validate_schema


class AiToolkitLtxAdapter:
    format_id = "ai-toolkit-ltx"

    def __init__(
        self,
        *,
        out_path: str | Path = "ai-toolkit-ltx.manifest.json",
        source_manifest: str | Path | None = None,
        vocabulary_path: str | Path | None = None,
        repo_root: Path = PACK_ROOT,
    ) -> None:
        self.out_path = Path(out_path)
        self.source_manifest = Path(source_manifest) if source_manifest is not None else None
        self.vocabulary_path = Path(vocabulary_path) if vocabulary_path is not None else None
        self.repo_root = repo_root

    def validate(self, items: list[dict[str, Any]]) -> list[str]:
        errors: list[str] = []
        if not items:
            return ["ai-toolkit-ltx export requires at least one accepted item"]
        for item in items:
            item_id = str(item.get("item_id") or "")
            media_path = _resolve_repo_path(item.get("media_path"), self.repo_root)
            if media_path is None or not media_path.is_file():
                errors.append(f"{item_id}: clip path missing: {item.get('media_path')}")
                continue
            caption_path = _resolve_repo_path(item.get("caption_file"), self.repo_root)
            expected_caption = media_path.with_name(f"{item_id}.caption.json")
            if caption_path is None or not caption_path.is_file():
                errors.append(f"{item_id}: caption sidecar missing: {item.get('caption_file')}")
            elif caption_path.resolve() != expected_caption.resolve():
                errors.append(f"{item_id}: caption sidecar must be sibling {expected_caption.name}")
        return errors

    def export(self, accepted_items: list[dict[str, Any]]) -> Path:
        errors = self.validate(accepted_items)
        if errors:
            raise ValueError("; ".join(errors))
        manifest = {
            "clips": [_clip_entry(item, self.repo_root) for item in accepted_items],
            "generated_at": utc_now_iso(),
        }
        if self.source_manifest is not None:
            manifest["source_manifest"] = repo_relative_path(_resolve_output_path(self.source_manifest, self.repo_root), repo_root=self.repo_root)
        if self.vocabulary_path is not None:
            manifest["vocabulary_path"] = repo_relative_path(_resolve_output_path(self.vocabulary_path, self.repo_root), repo_root=self.repo_root)
        validate_schema(manifest, "ai-toolkit-adapter-manifest.schema.json")
        out_path = _resolve_output_path(self.out_path, self.repo_root)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return out_path


def _clip_entry(item: Mapping[str, Any], repo_root: Path) -> dict[str, Any]:
    media_path = _resolve_repo_path(item.get("media_path"), repo_root)
    caption_path = _resolve_repo_path(item.get("caption_file"), repo_root)
    assert media_path is not None
    assert caption_path is not None
    rights = item.get("rights") if isinstance(item.get("rights"), Mapping) else {}
    entry = {
        "clip_id": str(item.get("item_id")),
        "clip_file": repo_relative_path(media_path, repo_root=repo_root),
        "path": repo_relative_path(media_path, repo_root=repo_root),
        "caption_file": repo_relative_path(caption_path, repo_root=repo_root),
        "source_url": str(item.get("source_url", "")),
        "content_hash": str(item.get("content_hash", "")),
        "rights_status": str(rights.get("rights_status", "unknown")),
    }
    if item.get("bucket") is not None:
        entry["bucket"] = str(item["bucket"])
    if item.get("duration_s") is not None:
        entry["duration_s"] = float(item["duration_s"])
    return entry


def _resolve_repo_path(value: Any, repo_root: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()


def _resolve_output_path(path: Path, repo_root: Path) -> Path:
    path = path.expanduser()
    return path.resolve() if path.is_absolute() else (repo_root / path).resolve()
