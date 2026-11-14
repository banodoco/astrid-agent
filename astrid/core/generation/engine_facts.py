"""Engine facts handed to a ``vibecomfy.workflows`` hook that asks for them.

A contribution's modules must not import Astrid (an AST test pins this for the audiovisual
pack). The host supplies its pinned ComfyUI commit, model lock, model-root binding,
atomic JSON writes, errors and run logs here. The engine pack extends these facts with
its own callables, then passes the complete object to a hook that declares an ``engine``
parameter. The legacy ``actions/*/run.py`` shells install the same complete object.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import ModuleType
from typing import Any, Callable, Mapping

PLATFORM = "linux-x86_64-cu130"


@dataclass(frozen=True)
class EngineFacts:
    comfy_commit: Callable[[], str]
    locked_models: Callable[[], Mapping[str, Any]]
    bind_model_root: Callable[[Mapping[str, Any]], Any]
    write_json_atomic: Callable[[Any, Any], None]
    astrid_error: Callable[[Exception], Any]
    run_logs: ModuleType


def comfyui_facts() -> EngineFacts:
    """Core-owned facts of the comfyui engine on the RunPod platform."""
    from astrid.core._shared import run_logs

    def comfy_commit() -> str:
        from astrid.core.generation.engine_recipe import engine_source_revision
        return engine_source_revision("comfyui", PLATFORM, "comfyui")

    def locked_models() -> Mapping[str, Any]:
        from astrid.core.generation.engine_recipe import load_recipe_model_lock
        return {model.destination: model for model in load_recipe_model_lock("comfyui", PLATFORM).models}

    def bind_model_root(model_root: Mapping[str, Any]) -> Any:
        from astrid.core.generation.model_root import (
            ModelRootBindingError,
            validate_model_root_binding,
        )
        try:
            return validate_model_root_binding(model_root, verify_files=True)
        except ModelRootBindingError as exc:
            raise ValueError(str(exc)) from exc

    def write_json_atomic(path: Any, value: Any) -> None:
        from astrid.core._shared.result_manifest import write_json_atomic as write
        write(path, value)

    def astrid_error(error: Exception) -> Any:
        from astrid.core.contracts.errors import coerce_astrid_error
        return coerce_astrid_error(error)

    return EngineFacts(comfy_commit, locked_models, bind_model_root, write_json_atomic, astrid_error, run_logs)
