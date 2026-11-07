"""Review data and decision application helpers."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import copy
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.pack_api import write_json_atomic

from .artifacts import HASHES_KEY, sidecar_hashes, write_hashed_sidecar
from ...shared.items import repo_relative_path, utc_now_iso
from .state import make_initial_state, write_review_state


def write_review_data(path: str | Path, items: list[Mapping[str, Any]]) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": [dict(item) for item in items]}
    write_json_atomic(out_path, payload)
    return out_path


def write_initial_review_state(
    path: str | Path,
    *,
    run_id: str,
    writer_id: str,
    buckets: Mapping[str, int | Mapping[str, Any]] | None = None,
    config_hash: str | None = None,
    schema_version_source: str | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    state = make_initial_state(
        run_id=run_id,
        writer_id=writer_id,
        buckets=buckets,
        config_hash=config_hash,
        schema_version_source=schema_version_source,
        status="reviewing",
        now=now,
    )
    return write_review_state(path, state, now=now)


def write_human_review_final(run_dir: str | Path, payload: Mapping[str, Any]) -> Path:
    out_path = Path(run_dir) / "review_server" / "human_review.final.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def apply_review_decisions(
    items: list[Mapping[str, Any]],
    state: Mapping[str, Any],
    *,
    persist_caption_sidecars: bool = True,
    now: str | None = None,
) -> list[dict[str, Any]]:
    decisions = state.get("review_decisions") or {}
    output: list[dict[str, Any]] = []
    for item in items:
        updated = copy.deepcopy(dict(item))
        item_id = str(updated["item_id"])
        decision = dict(decisions.get(item_id) or {})
        normalized = _normalize_decision(decision.get("decision", updated.get("review_status", "pending")))
        if normalized == "accept":
            updated["review_status"] = "accepted"
        elif normalized == "reject":
            updated["review_status"] = "rejected"
        else:
            updated["review_status"] = "pending"
        if decision:
            final_decision = {
                "item_id": item_id,
                "decision": normalized,
                "reject_reason": decision.get("reject_reason"),
                "edited_caption": decision.get("edited_caption"),
                "reviewed_at": decision.get("reviewed_at") or now or utc_now_iso(),
                "state_version": int(decision.get("state_version", state.get("state_version", 0))),
            }
            if decision.get("reviewer_id"):
                final_decision["reviewer_id"] = str(decision["reviewer_id"])
            updated["review_decision"] = final_decision
        edited_caption = decision.get("edited_caption")
        if isinstance(edited_caption, str) and edited_caption:
            caption = dict(updated.get("caption") or {})
            caption["text"] = edited_caption
            caption.setdefault("schema_version", 1)
            caption.setdefault("confidence", 1.0)
            caption.setdefault("model", "human_review")
            updated["caption"] = caption
        if updated["review_status"] == "accepted" and persist_caption_sidecars:
            _persist_caption_sidecar(updated)
        output.append(updated)
    return output


def _persist_caption_sidecar(item: dict[str, Any]) -> Path:
    media_path = Path(str(item["media_path"])).expanduser()
    if not media_path.is_absolute():

        media_path = (PACK_ROOT / media_path).resolve()
    caption = dict(item.get("caption") or {})
    caption.setdefault("text", "")
    caption.setdefault("schema_version", 1)
    caption.setdefault("confidence", 0.0)
    caption.setdefault("model", "")
    sidecar = media_path.with_name(f"{item['item_id']}.caption.json")
    hashes = _caption_sidecar_hashes(item, caption, target_sidecar=sidecar)
    write_hashed_sidecar(sidecar, caption, hashes)
    item["caption_file"] = repo_relative_path(sidecar)
    return sidecar


def _caption_sidecar_hashes(item: Mapping[str, Any], caption: Mapping[str, Any], *, target_sidecar: Path) -> dict[str, str]:
    existing = _existing_caption_sidecar(item, target_sidecar=target_sidecar)
    if existing is not None and existing.is_file():
        raw = json.loads(existing.read_text(encoding="utf-8"))
        if isinstance(raw, Mapping) and isinstance(raw.get(HASHES_KEY), Mapping):
            return {str(key): str(value) for key, value in raw[HASHES_KEY].items() if value}
    return sidecar_hashes(
        prompt=str(caption.get("prompt") or item.get("caption_prompt") or ""),
        schema=caption.get("schema") or item.get("caption_schema") or caption.get("schema_version", 1),
        media=item,
        config=_caption_cache_config(item, caption),
    )


def _existing_caption_sidecar(item: Mapping[str, Any], *, target_sidecar: Path) -> Path | None:
    value = item.get("caption_file")
    if not isinstance(value, str) or not value:
        return target_sidecar if target_sidecar.is_file() else None
    path = Path(value).expanduser()
    if path.is_absolute():
        return path

    return (PACK_ROOT / path).resolve()


def _caption_cache_config(item: Mapping[str, Any], caption: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "caption_model": caption.get("model", ""),
        "caption_schema_version": caption.get("schema_version", 1),
        "caption_provider": item.get("caption_provider", ""),
    }


def _normalize_decision(value: Any) -> str:
    if value in {"accepted", "accept", True}:
        return "accept"
    if value in {"rejected", "reject", False}:
        return "reject"
    return "pending"
