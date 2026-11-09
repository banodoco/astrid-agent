"""The CPU stages the ``vibecomfy.workflows`` build runs: prepare and compile from a file map.

``build`` runs these once; ``after_outputs`` reads what ``build`` saved and never calls them
(``save_build_state`` / ``load_build_state``). The module imports nothing from Astrid.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Mapping

from .engine_facts import facts
from .errors import PreparationError
from .request import H3Request, _asset_ids, normalize_request
from .request_v2 import branch_for

STATE_NAME = "build-state.json"
_OUTPUT_CONTRACTS = {"muxed_av_full_timeline", "separate_av_full_timeline"}


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _authoritative_source_asset_id(request: Any) -> str | None:
    """Return the source baseline asset without projecting v1 keys onto v2."""

    value = request.value
    if value.get("version") != 2:
        source = value["source"]
        return None if source is None else str(source["asset"])
    branch = branch_for(request)
    if branch in {"source_free", "audio_only"}:
        return None
    videos = [
        item
        for item in value["media"]
        if item["role"] == "timeline" and item.get("modality") == "video"
    ]
    return str(videos[0]["asset"]) if len(videos) == 1 else None


def _generation_intent(compilation: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the child publication declaration from the sealed compilation."""

    capabilities = compilation.get("capabilities")
    public = capabilities.get("public_generation") if isinstance(capabilities, Mapping) else None
    if not isinstance(public, Mapping):
        raise ValueError("sealed H3 compilation is missing public_generation")
    selectors = public.get("selectors")
    modality = public.get("modality", "video")
    if not isinstance(selectors, list) or not selectors:
        raise ValueError("sealed H3 compilation declares no public generation selectors")
    if not all(isinstance(selector, Mapping) for selector in selectors):
        raise ValueError("sealed H3 generation selectors must be objects")
    return {
        "version": 1,
        "modality": modality,
        "partial_success_policy": "reject",
        "groups": [{"group_key": "main", "selectors": [dict(selector) for selector in selectors]}],
        "metadata": {"compiled_generation_contract": True},
    }


def _sealed_plan(compilation: Mapping[str, Any]) -> dict[str, Any]:
    capabilities = compilation.get("capabilities", {})
    contract = capabilities.get("output_contract")
    if contract not in _OUTPUT_CONTRACTS:
        raise ValueError("unsupported H3 parent output mapping")
    intent = _generation_intent(compilation)
    if (intent["modality"] != "video" or len(intent["groups"]) != 1
            or len(intent["groups"][0]["selectors"]) != 1):
        raise ValueError("tracked H3 requires the sealed single video selector")
    digest = compilation.get("compilation_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("H3 compilation has no sealed identity")
    return {"compilation_digest": digest, "output_contract": contract, "intent": intent,
            "workflow_members": {name: row["sha256"] for name, row in compilation["workflow"].items()},
            "workflow_inputs_sha256": stable_digest(compilation["workflow_inputs"])}


def materialize_file_map(request: H3Request, files: Mapping[str, Any], destination: Path,
                         ) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Copy exactly the request's declared assets from ``{id: path}`` into ``destination``."""
    destination = destination.resolve()
    paths: dict[str, str] = {}
    identities: list[dict[str, Any]] = []
    for asset_id in _asset_ids(request):
        value = files.get(asset_id)
        if not isinstance(value, str) or not value:
            raise PreparationError(f"asset {asset_id!r} needs a file in asset_map before task submission")
        source = Path(value).expanduser().resolve(strict=True)
        if not source.is_file():
            raise PreparationError(f"asset {asset_id!r} is not a regular file: {source}")
        member = f"assets/{hashlib.sha256(asset_id.encode()).hexdigest()[:16]}{source.suffix}"
        target = (destination / member).resolve()
        if not target.is_relative_to(destination):
            raise PreparationError("asset member escapes its attempt directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        paths[asset_id] = str(target)
        identities.append({"asset": asset_id, "member": member, "sha256": digest, "size": target.stat().st_size,
                           "kind": "file_map_member", "status": "resolved", "path": str(target)})
    return paths, identities


def compile_stages(request: H3Request, files: Mapping[str, Any], destination: Path):
    """Prepare and compile one request from its file map: CPU only. Returns (paths, preparation, compilation)."""
    from .compile import compile_preparation
    from .prepare import prepare_request

    paths, identities = materialize_file_map(request, files, destination / "assets")
    preparation = prepare_request(request, asset_map=paths)
    preparation["assets"] = identities
    compilation = compile_preparation(preparation, out_dir=destination / "compile")
    return paths, preparation, compilation


# The name the planner and the bundle-based transform used for the same stage.
_compile_stages = compile_stages


def save_build_state(directory: Path, *, request: H3Request, document: Mapping[str, Any], preparation: Mapping[str, Any],
                     source: str | None, plan: Mapping[str, Any]) -> Path:
    """What ``after_outputs`` needs, written once by ``build`` into the shared job directory."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_NAME
    path.write_text(json.dumps({"request": document, "request_digest": request.digest,
                                "preparation": preparation, "source": source, "plan": plan},
                               sort_keys=True), encoding="utf-8")
    return path


def load_build_state(directory: Path) -> dict[str, Any]:
    path = Path(directory) / STATE_NAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"h3 build state is missing or unreadable ({path}); after_outputs runs after build "
                         "in the same job directory") from exc
    request = normalize_request(state["request"])
    if request.digest != state["request_digest"]:
        raise ValueError("h3 build state does not match its request digest")
    return {**state, "request_object": request}
