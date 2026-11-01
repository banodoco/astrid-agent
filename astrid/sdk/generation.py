"""Generation SDK helpers.

This module stays lightweight so ``import astrid.sdk`` exposes ``generate``
without loading model catalogs or generation backend registries until a
generation method is actually invoked.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._module import _sdk_module
from .exceptions import (
    CapabilityPreconditionError,
    CapabilityValidationError,
)
from .results import _reconstruct_generation_result


def _load_model_registry(
    *,
    project_root: str | Path | None = None,
    extra_pack_roots: tuple[str, ...] = (),
) -> Any:
    """Lazily load the generation model registry."""
    from astrid.core.model_catalog.registry import ModelRegistry

    return ModelRegistry.load_default(
        **_sdk_module()._registry_load_kwargs(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        ),
    )


def _infer_image_mode(
    explicit_mode: str | None,
    inputs: dict[str, Any],
) -> str:
    """Infer the image generation mode."""
    if explicit_mode is not None:
        return explicit_mode

    if inputs.get("image_ref"):
        return "i2i"
    return "t2i"


_EXPLICIT_ONLY_IMAGE_MODES: frozenset[str] = frozenset(
    {"edit", "inpaint", "outpaint", "upscale"}
)


def _reject_retired_generation_inputs(inputs: dict[str, Any]) -> None:
    """Reject removed public spellings instead of silently forwarding them."""
    if "backend" in inputs:
        raise TypeError("generate.* no longer accepts 'backend'; use execution")


_AUDIO_TTS_PASSTHROUGH_INPUTS = frozenset(
    {"voice", "rate", "volume", "pitch", "provider"}
)


def _speech_inputs_from_audio_request(inputs: dict[str, Any]) -> dict[str, Any]:
    """Map typed ``audio(mode='tts')`` inputs to exact-text speech inputs.

    ``prompt`` is accepted as the audio facade's historical main-text alias;
    the speech action itself receives only ``text`` and speech settings.
    Model and execution are explicit facade arguments and are intentionally not
    forwarded because they select the music backend, not speech authority.
    """
    unsupported = sorted(
        set(inputs) - _AUDIO_TTS_PASSTHROUGH_INPUTS - {"text", "prompt"}
    )
    if unsupported:
        raise CapabilityValidationError(
            "generate.audio(mode='tts') accepts text, voice, rate, volume, pitch and provider; "
            f"unsupported input(s): {', '.join(unsupported)}"
        )
    text = inputs.get("text")
    prompt = inputs.get("prompt")
    if text is not None and prompt is not None and text != prompt:
        raise CapabilityValidationError(
            "mode='tts' received conflicting text and prompt; pass only text"
        )
    spoken = text if text is not None else prompt
    if not isinstance(spoken, str) or not spoken.strip():
        raise CapabilityValidationError(
            "mode='tts' requires non-empty text (the exact words to speak)"
        )
    speech: dict[str, Any] = {"text": spoken}
    for key in _AUDIO_TTS_PASSTHROUGH_INPUTS:
        if key in inputs:
            speech[key] = inputs[key]
    return speech


def _infer_video_mode(
    explicit_mode: str | None,
    inputs: dict[str, Any],
) -> str:
    """Infer the video generation mode."""
    if explicit_mode is not None:
        return explicit_mode

    has_image_ref = bool(inputs.get("image_ref"))
    has_image_end_ref = bool(inputs.get("image_end_ref"))

    if has_image_ref and has_image_end_ref:
        return "flf"
    if has_image_ref:
        return "i2v"
    return "t2v"


def _resolve_execution(
    model_entry: Any,
    mode: str,
    explicit_execution: str | None,
    *,
    model: str,
) -> str:
    """Validate or infer the execution backend for *(model, mode)*."""
    mode_spec = model_entry.modes.get(mode)
    if mode_spec is None:
        available_modes = ", ".join(sorted(model_entry.modes))
        raise CapabilityValidationError(
            f"Model {model!r} does not support mode {mode!r}. "
            f"Available modes: {available_modes}"
        )

    backend_ids = list(mode_spec.backends.keys())
    if not backend_ids:
        raise CapabilityValidationError(
            f"Model {model!r} mode {mode!r} has no configured backends"
        )

    if explicit_execution is not None:
        if explicit_execution not in backend_ids:
            raise CapabilityValidationError(
                f"Execution {explicit_execution!r} is not available for "
                f"model {model!r} mode {mode!r}. "
                f"Available: {', '.join(sorted(backend_ids))}"
            )
        return explicit_execution

    inference_backend_ids = [
        backend_id for backend_id in backend_ids if backend_id != "codex"
    ] or backend_ids
    if len(inference_backend_ids) == 1:
        return inference_backend_ids[0]

    raise CapabilityValidationError(
        f"Ambiguous execution for model {model!r} mode {mode!r}. "
        f"Available backends: {', '.join(sorted(backend_ids))}. "
        f"Please specify one explicitly via the 'execution' parameter."
    )


def _resolve_invoke_destination(
    *,
    out: Path | str | None,
    project: str | None,
    project_root: str | Path | None,
    client: Any | None = None,
) -> tuple[Path | str | None, str | None]:
    del project_root  # project roots discover packs; they never select ownership.
    from astrid.core.project.guidance import (
        format_project_required_guidance,
        selected_project,
    )

    selected, _source = selected_project(project)
    if selected is None:
        from astrid.sdk.invocation import _runtime_selected_project

        selected = _runtime_selected_project(client) if client is not None else _runtime_selected_project()
    if selected is None:
        raise CapabilityPreconditionError(
            format_project_required_guidance(operation="generation")
        )
    return out, selected


@dataclass(frozen=True)
class GenerationFacade:
    """Public typed entrypoint for generation executors."""

    sdk_module_name: str = "astrid.sdk"

    def _invoke(self, capability_id: str, /, **kwargs: Any) -> Any:
        sdk_module = importlib.import_module(self.sdk_module_name)
        # GenerationResult reconstruction requires terminal task readback;
        # the low-level invoke() default remains admission-only for callers
        # that intentionally manage waiting themselves.
        kwargs.setdefault("wait", True)
        return sdk_module.invoke(capability_id, **kwargs)

    def __getattr__(self, name: str) -> Any:
        """Resolve a plugin-registered generation verb by *name*."""
        from astrid.core.generation.verbs import (
            get_verb,
            list_verbs,
            load_generation_verb_plugins,
        )

        load_generation_verb_plugins()

        try:
            return get_verb(name)
        except KeyError:
            known = list_verbs()
            hint = f" Available plugin verbs: {', '.join(known)}." if known else ""
            raise AttributeError(
                f"'GenerationFacade' has no attribute {name!r}. "
                f"Built-in methods: image, audio, video.{hint}"
            ) from None

    def image(
        self,
        *,
        model: str,
        mode: str | None = None,
        execution: str | None = None,
        out: Path | str | None = None,
        project: str | None = None,
        client: Any | None = None,
        project_root: str | Path | None = None,
        extra_pack_roots: tuple[str, ...] = (),
        banodoco_config: Any | None = None,
        active_theme: str | Path | None = None,
        include_missing_roots: bool = False,
        brief: Path | str | None = None,
        dry_run: bool = False,
        check_binaries: bool = False,
        python_exec: str | None = None,
        verbose: bool = False,
        argv: tuple[str, ...] = (),
        **inputs: Any,
    ) -> Any:
        _reject_retired_generation_inputs(inputs)
        if execution == "openai":
            raise CapabilityPreconditionError(
                "astrid.generate.image does not support execution='openai'; use executor "
                "'generation.generate_image_openai' directly"
            )

        sdk_module = importlib.import_module(self.sdk_module_name)
        resolved_mode = sdk_module._infer_image_mode(mode, inputs)
        registry = sdk_module._load_model_registry(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        )
        try:
            model_entry = registry.get(model)
        except KeyError as exc:
            raise CapabilityValidationError(str(exc)) from exc

        if mode is None and resolved_mode in sdk_module._EXPLICIT_ONLY_IMAGE_MODES:
            raise CapabilityValidationError(
                f"Mode {resolved_mode!r} requires an explicit 'mode' argument"
            )

        resolved_execution = sdk_module._resolve_execution(
            model_entry,
            resolved_mode,
            execution,
            model=model,
        )
        from astrid.core.generation.preflight import validate_generation_request

        recipe = inputs.get("shot_generation_recipe")
        if recipe is not None:
            from astrid.packs.generation.actions.generate_image.task_adapter import (
                GenerateImageAdapterError,
                validate_shot_generation_recipe,
            )

            try:
                validate_shot_generation_recipe(
                    recipe,
                    model=model,
                    mode=resolved_mode,
                    execution=resolved_execution,
                    resolved_settings=inputs,
                )
            except GenerateImageAdapterError as exc:
                raise CapabilityValidationError(str(exc)) from exc

        validate_generation_request(
            registry,
            model=model,
            mode=resolved_mode,
            execution=resolved_execution,
            inputs={"model": model, "mode": resolved_mode, **inputs},
            modality="image",
            # Keep the image facade's historic deferred prompt validation:
            # executor-owned prompt semantics (including prompt-less upscale)
            # remain compatible while matrix/readiness checks happen here.
            required_features=(),
        )
        if resolved_execution == "local":
            from astrid.core.generation.preflight import (
                require_local_generation_readiness,
            )

            # This read-only check must happen before destination resolution
            # and invoke/admission.  It reports missing local installation
            # prerequisites without creating a run, task, or staging tree.
            require_local_generation_readiness(
                model_entry,
                resolved_mode,
                python_executable=python_exec,
            )
        invoke_out, invoke_project = _resolve_invoke_destination(
            client=client,
            out=out,
            project=project,
            project_root=project_root,
        )
        result = self._invoke(
            "generation.generate_image",
            kind="executor",
            client=client,
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
            banodoco_config=banodoco_config,
            active_theme=active_theme,
            include_missing_roots=include_missing_roots,
            out=invoke_out,
            project=invoke_project,
            inputs={
                "model": model,
                "mode": resolved_mode,
                "execution": resolved_execution,
                **inputs,
            },
            brief=brief,
            dry_run=dry_run,
            check_binaries=check_binaries,
            python_exec=python_exec,
            verbose=verbose,
            argv=argv,
        )
        if dry_run:
            return result
        return _reconstruct_generation_result(result)

    def video(
        self,
        *,
        model: str,
        mode: str | None = None,
        execution: str | None = None,
        out: Path | str | None = None,
        project: str | None = None,
        client: Any | None = None,
        project_root: str | Path | None = None,
        extra_pack_roots: tuple[str, ...] = (),
        banodoco_config: Any | None = None,
        active_theme: str | Path | None = None,
        include_missing_roots: bool = False,
        brief: Path | str | None = None,
        dry_run: bool = False,
        check_binaries: bool = False,
        python_exec: str | None = None,
        verbose: bool = False,
        argv: tuple[str, ...] = (),
        **inputs: Any,
    ) -> Any:
        _reject_retired_generation_inputs(inputs)
        sdk_module = importlib.import_module(self.sdk_module_name)
        resolved_mode = sdk_module._infer_video_mode(mode, inputs)
        registry = sdk_module._load_model_registry(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        )
        try:
            model_entry = registry.get(model)
        except KeyError as exc:
            raise CapabilityValidationError(str(exc)) from exc

        resolved_execution = sdk_module._resolve_execution(
            model_entry,
            resolved_mode,
            execution,
            model=model,
        )
        from astrid.core.generation.preflight import (
            require_local_generation_readiness,
            validate_generation_request,
        )

        # FLF's defining frame pair must be rejected before dry-run/live
        # invocation.  The prompt remains executor-owned for compatibility
        # with existing facade callers that only exercise routing.
        flf_required = None
        if resolved_mode == "flf" and (
            inputs.get("image_ref") is not None
            or inputs.get("image_end_ref") is not None
        ):
            flf_required = ("image_ref", "image_end_ref")
        validate_generation_request(
            registry,
            model=model,
            mode=resolved_mode,
            execution=resolved_execution,
            inputs={"model": model, "mode": resolved_mode, **inputs},
            modality="video",
            # The typed facade historically defers prompt validation to the
            # executor; when FLF refs are supplied, however, enforce the
            # defining frame pair before dry-run/live admission.
            required_features=flf_required or (),
        )
        if resolved_execution == "local":
            # Keep local video on the same actionable, read-only prerequisite
            # gate as image generation. No cloud fallback is attempted.
            require_local_generation_readiness(
                model_entry,
                resolved_mode,
                python_executable=python_exec,
            )
        invoke_out, invoke_project = _resolve_invoke_destination(
            client=client,
            out=out,
            project=project,
            project_root=project_root,
        )
        result = self._invoke(
            "generation.generate_video",
            kind="executor",
            client=client,
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
            banodoco_config=banodoco_config,
            active_theme=active_theme,
            include_missing_roots=include_missing_roots,
            out=invoke_out,
            project=invoke_project,
            inputs={
                "model": model,
                "mode": resolved_mode,
                "execution": resolved_execution,
                **inputs,
            },
            brief=brief,
            dry_run=dry_run,
            check_binaries=check_binaries,
            python_exec=python_exec,
            verbose=verbose,
            argv=argv,
        )
        if dry_run:
            return result
        return _reconstruct_generation_result(result)

    def audio(
        self,
        *,
        model: str,
        mode: str | None = None,
        execution: str | None = None,
        out: Path | str | None = None,
        project: str | None = None,
        client: Any | None = None,
        project_root: str | Path | None = None,
        extra_pack_roots: tuple[str, ...] = (),
        banodoco_config: Any | None = None,
        active_theme: str | Path | None = None,
        include_missing_roots: bool = False,
        brief: Path | str | None = None,
        dry_run: bool = False,
        check_binaries: bool = False,
        python_exec: str | None = None,
        verbose: bool = False,
        argv: tuple[str, ...] = (),
        **inputs: Any,
    ) -> Any:
        _reject_retired_generation_inputs(inputs)
        if mode == "tts":
            speech_inputs = _speech_inputs_from_audio_request(inputs)
            invoke_out, invoke_project = _resolve_invoke_destination(
                client=client,
                out=out,
                project=project,
                project_root=project_root,
            )
            result = self._invoke(
                "generation.generate_speech",
                kind="executor",
                client=client,
                project_root=project_root,
                extra_pack_roots=extra_pack_roots,
                banodoco_config=banodoco_config,
                active_theme=active_theme,
                include_missing_roots=include_missing_roots,
                out=invoke_out,
                project=invoke_project,
                inputs=speech_inputs,
                brief=brief,
                dry_run=dry_run,
                check_binaries=check_binaries,
                python_exec=python_exec,
                verbose=verbose,
                argv=argv,
            )
            if dry_run:
                return result
            return _reconstruct_generation_result(result)
        """Generate audio through the same typed preflight as image/video."""

        sdk_module = importlib.import_module(self.sdk_module_name)
        registry = sdk_module._load_model_registry(
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
        )
        try:
            model_entry = registry.get(model)
        except KeyError as exc:
            raise CapabilityValidationError(str(exc)) from exc

        # Audio models currently expose one canonical mode (music). Infer it
        # when omitted so the facade remains ergonomic while still validating
        # the resolved model → mode → backend cell.
        resolved_mode = mode
        if resolved_mode is None:
            if len(model_entry.modes) == 1:
                resolved_mode = next(iter(model_entry.modes))
            else:
                available_modes = ", ".join(sorted(model_entry.modes))
                raise CapabilityValidationError(
                    f"Ambiguous audio mode for model {model!r}. "
                    f"Available modes: {available_modes}. Please specify mode."
                )

        resolved_execution = sdk_module._resolve_execution(
            model_entry,
            resolved_mode,
            execution,
            model=model,
        )
        from astrid.core.generation.preflight import (
            require_local_generation_readiness,
            validate_generation_request,
        )

        validate_generation_request(
            registry,
            model=model,
            mode=resolved_mode,
            execution=resolved_execution,
            inputs={"model": model, "mode": resolved_mode, **inputs},
            modality="audio",
        )
        if resolved_execution == "local":
            require_local_generation_readiness(
                model_entry,
                resolved_mode,
                python_executable=python_exec,
            )

        invoke_out, invoke_project = _resolve_invoke_destination(
            client=client,
            out=out,
            project=project,
            project_root=project_root,
        )
        result = self._invoke(
            "generation.generate_audio",
            kind="executor",
            client=client,
            project_root=project_root,
            extra_pack_roots=extra_pack_roots,
            banodoco_config=banodoco_config,
            active_theme=active_theme,
            include_missing_roots=include_missing_roots,
            out=invoke_out,
            project=invoke_project,
            inputs={
                "model": model,
                "mode": resolved_mode,
                "execution": resolved_execution,
                **inputs,
            },
            brief=brief,
            dry_run=dry_run,
            check_binaries=check_binaries,
            python_exec=python_exec,
            verbose=verbose,
            argv=argv,
        )
        if dry_run:
            return result
        return _reconstruct_generation_result(result)


generate = GenerationFacade()
