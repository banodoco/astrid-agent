"""Complete ComfyUI facts supplied by the pack that implements the engine."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from astrid.core.generation.engine_facts import EngineFacts, comfyui_facts
from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest, read_archive
from astrid.packs.vibecomfy.invocation_preflight import (
    _assert_no_unresolved_placeholders,
    measure_source_av,
)


@dataclass(frozen=True)
class ComfyUIEngineFacts(EngineFacts):
    build_asset_manifest: Callable[..., dict[str, Any]]
    read_archive: Callable[[Any], Any]
    assert_no_unresolved_placeholders: Callable[[Any], None]
    measure_source_av: Callable[[Any], Mapping[str, Any]]


def comfyui_engine_facts() -> ComfyUIEngineFacts:
    """Combine the host's facts with this pack's engine implementation."""
    core = comfyui_facts()

    def source_av(path: Any) -> Mapping[str, Any]:
        return measure_source_av(path).to_dict()

    return ComfyUIEngineFacts(
        comfy_commit=core.comfy_commit,
        locked_models=core.locked_models,
        bind_model_root=core.bind_model_root,
        write_json_atomic=core.write_json_atomic,
        astrid_error=core.astrid_error,
        run_logs=core.run_logs,
        build_asset_manifest=build_asset_manifest,
        read_archive=read_archive,
        assert_no_unresolved_placeholders=_assert_no_unresolved_placeholders,
        measure_source_av=source_av,
    )
