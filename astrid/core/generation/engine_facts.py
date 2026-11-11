"""Engine facts handed to a ``vibecomfy.workflows`` hook that asks for them.

A contribution's modules must not import Astrid (an AST test pins this for the audiovisual pack). What
they need from the engine side (the pinned ComfyUI commit, the model lock, model-root
binding, the managed-assets manifest, atomic JSON writes, the pre-run source checks)
arrives as one ``EngineFacts`` object: the engine passes it to a hook that declares an
``engine`` parameter, and the legacy ``actions/*/run.py`` shells install the same object.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

PLATFORM = "linux-x86_64-cu130"


@dataclass(frozen=True)
class EngineFacts:
    comfy_commit: Callable[[], str]
    locked_models: Callable[[], Mapping[str, Any]]
    bind_model_root: Callable[[Mapping[str, Any]], Any]
    build_asset_manifest: Callable[..., dict[str, Any]]
    read_archive: Callable[[Any], Any]
    write_json_atomic: Callable[[Any, Any], None]
    assert_no_unresolved_placeholders: Callable[[Any], None]
    measure_source_av: Callable[[Any], Mapping[str, Any]]
    astrid_error: Callable[[Exception], Any]


def comfyui_facts() -> EngineFacts:
    """The facts of the comfyui engine on the RunPod platform."""
    def comfy_commit() -> str:
        from astrid.core.generation.engine_recipe import engine_source_revision
        return engine_source_revision("comfyui", PLATFORM, "comfyui")

    def locked_models() -> Mapping[str, Any]:
        from astrid.core.generation.engine_recipe import load_recipe_model_lock
        return {model.destination: model for model in load_recipe_model_lock("comfyui", PLATFORM).models}

    def bind_model_root(model_root: Mapping[str, Any]) -> Any:
        from astrid.core.generation.model_root import ModelRootBindingError, validate_model_root_binding
        try:
            return validate_model_root_binding(model_root, verify_files=True)
        except ModelRootBindingError as exc:
            raise ValueError(str(exc)) from exc

    def build_asset_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest as build
        return build(*args, **kwargs)

    def read_archive(path: Any) -> Any:
        from astrid.packs.vibecomfy.asset_manifest import read_archive as read
        return read(path)

    def write_json_atomic(path: Any, value: Any) -> None:
        from astrid.core._shared.result_manifest import write_json_atomic as write
        write(path, value)

    def assert_no_unresolved_placeholders(bindings: Any) -> None:
        from astrid.packs.vibecomfy.invocation_preflight import _assert_no_unresolved_placeholders as check
        check(bindings)

    def measure_source_av(path: Any) -> Mapping[str, Any]:
        from astrid.packs.vibecomfy.invocation_preflight import measure_source_av as measure
        return measure(path).to_dict()

    def astrid_error(error: Exception) -> Any:
        from astrid.core.contracts.errors import coerce_astrid_error
        return coerce_astrid_error(error)

    return EngineFacts(comfy_commit, locked_models, bind_model_root, build_asset_manifest, read_archive,
                       write_json_atomic, assert_no_unresolved_placeholders, measure_source_av, astrid_error)
