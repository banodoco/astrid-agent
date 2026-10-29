"""Generation SDK helpers.

This module stays lightweight so ``import astrid.sdk`` exposes ``generate``
without loading model catalogs or generation backend registries until a
generation method is actually invoked.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from ._module import _sdk_module
from .exceptions import (
    CapabilityPreconditionError,
    CapabilityInvocationError,
    CapabilityValidationError,
)
from .results import _reconstruct_generation_result

if TYPE_CHECKING:
    from .execution_request import ExecutionRequest
    from .results import InvocationResult
    from astrid.core.generation.engine_contracts import Placement


WorkflowFile = str | Path | Mapping[str, Any]


class WorkflowInvocationClient(Protocol):
    def invoke_result(self, capability_id: str, *, kind: str, **kwargs: Any) -> InvocationResult: ...


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
                f"Built-in methods: image, audio, video, workflow.{hint}"
            ) from None

    def workflow(
        self, *,
        workflow: WorkflowFile | None = None,
        python: WorkflowFile | None = None,
        companion: WorkflowFile | None = None,
        source: WorkflowFile | None = None,
        source_video: WorkflowFile | None = None,
        source_video_node: str | None = None,
        source_video_widget: str | None = None,
        managed_assets: WorkflowFile | None = None,
        workflow_inputs: str | None = None,
        comfy_url: str | None = None,
        comfy_output_dir: str | None = None,
        generation_intent: Mapping[str, Any] | None = None,
        placement: Placement | None = None,
        out: Path | str | None = None,
        project: str | None = None,
        client: WorkflowInvocationClient | None = None,
        execution_request: ExecutionRequest | Mapping[str, Any] | None = None,
        idempotency_context: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        recovery_path: Path | str | None = None,
        resume: bool = False,
        wait: bool = True,
        read_managed_outputs: bool = True,
        timeout_seconds: float = 3600.0,
        poll_seconds: float = 1.0,
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
    ) -> InvocationResult:
        """Invoke the existing workflow action through the supplied client.

        Placement validates scheduling intent; it is never a creative input.
        Omitted domain values stay omitted so the action owns its defaults.
        """
        from astrid.core.generation.backends.registry import (
            GenerationBackendRegistry, resolve_workflow_descriptor,
        )

        resolve_workflow_descriptor(
            GenerationBackendRegistry(), placement=placement,
            execution_request=execution_request,
        )
        inputs = {
            name: value for name, value in {
                "workflow": workflow, "python": python, "companion": companion,
                "source": source, "source_video": source_video,
                "source_video_node": source_video_node,
                "source_video_widget": source_video_widget,
                "managed_assets": managed_assets, "workflow_inputs": workflow_inputs,
                "comfy_url": comfy_url, "comfy_output_dir": comfy_output_dir,
                "generation_intent": generation_intent,
            }.items() if value is not None
        }
        invoke_out, invoke_project = _resolve_invoke_destination(
            client=client, out=out, project=project, project_root=project_root,
        )
        sdk_module = importlib.import_module(self.sdk_module_name)
        invoke_result = client.invoke_result if client is not None else sdk_module.invoke_result
        return invoke_result(
            "vibecomfy.run", kind="executor", inputs=inputs, client=client,
            out=invoke_out, project=invoke_project, execution_request=execution_request,
            idempotency_context=idempotency_context, idempotency_key=idempotency_key,
            recovery_path=recovery_path, resume=resume, wait=wait,
            read_managed_outputs=read_managed_outputs, timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds, project_root=project_root,
            extra_pack_roots=extra_pack_roots, banodoco_config=banodoco_config,
            active_theme=active_theme, include_missing_roots=include_missing_roots,
            brief=brief, dry_run=dry_run, check_binaries=check_binaries,
            python_exec=python_exec, verbose=verbose, argv=argv,
        )

    def _generate_on_target(
        self,
        *,
        registry: Any,
        model: str | None,
        mode: str,
        modality: str,
        target: str,
        execution: str | None,
        inputs: dict[str, Any],
        dry_run: bool,
        **controls: Any,
    ) -> Any:
        """Bind a catalog template and admit it through the workflow facade.

        Target selection precedes package/runtime prerequisites so an attached
        server can be used without installing or owning a managed ComfyUI.
        """
        import json
        from tempfile import TemporaryDirectory

        from astrid.core.contracts.errors import AstridError
        from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
        from astrid.core.generation.preflight import (
            require_generation_target_readiness,
            select_generation_target,
            validate_generation_request,
        )

        if execution not in {None, "local"}:
            raise CapabilityValidationError(
                "target selects a local ComfyUI workflow; use execution='local'"
            )
        # This route submits one canonical workflow. Count belongs to the
        # executor loop on the older typed action and must not be ignored here.
        if inputs.get("count", 1) != 1:
            raise CapabilityValidationError(
                "generate with target currently requires count=1"
            )
        if inputs.get("shot_generation_recipe") is not None:
            raise CapabilityValidationError(
                "shot_generation_recipe requires the generation image action"
            )
        target_client = select_generation_target(target)
        if model is not None:
            try:
                candidates = [registry.get(model)]
            except KeyError as exc:
                raise CapabilityValidationError(str(exc)) from exc
        else:
            candidates = [
                entry for entry in registry.list_by_modality(modality)
                if mode in entry.modes
                and getattr(entry.modes[mode].backends.get("local"), "template", "")
            ]
        backend = VibeComfyBackend(target_client=target_client)
        selected = None
        failures: list[str] = []
        if candidates:
            # Every default candidate has a local template. The remaining
            # package/runtime prerequisites are shared across all candidates.
            require_generation_target_readiness(candidates[0], mode, target_client)
        for entry in candidates:
            try:
                validate_generation_request(
                    registry, model=entry.id, mode=mode, execution="local",
                    inputs=inputs, modality=modality,
                )
                workflow, plan, schema = backend.prepare_catalog_workflow(
                    entry, mode, inputs, resolve_target=target_client is not None,
                )
                from vibecomfy.workflow_bundle import load_bundle

                bundle = load_bundle(workflow)
                bundle.require_canonical_authority("generate on target")
                approval = bundle.compile(schema_provider=schema) if schema is not None else None
            except (AstridError, ValueError, KeyError, OSError, CapabilityValidationError) as exc:
                if model is not None:
                    raise
                failures.append(f"{entry.id}: {exc}")
                continue
            selected = (entry, workflow, bundle, plan, schema, approval)
            break
        if selected is None:
            raise AstridError(
                f"No ready local {modality} model supports mode {mode!r} on the selected target"
                + (": " + "; ".join(failures) if failures else ""),
                code="COMFY_TARGET_NO_MODEL", field="model",
                fix=("Use --target http://127.0.0.1:8188 with a ComfyUI server containing "
                     "a catalog model, or --target managed with an installed runtime"),
            )
        entry, workflow, bundle, plan, schema, approval = selected
        if dry_run:
            preview = dict(plan or {"model": entry.id, "mode": mode})
            preview["target"] = target_client.target.url if target_client is not None else "managed"
            if approval is not None:
                preview["approval"] = {"status": "approved", "api_digest": approval.api_digest}
            return preview

        # Generated source files cross the same public managed-file boundary
        # as authored workflow bundles. Workers never read caller-local paths.
        client = controls.get("client")
        importer = getattr(getattr(client, "media", None), "import_file", None)
        if not callable(importer):
            raise CapabilityPreconditionError(
                "generate with target requires a connected Runtime client with "
                "media.import_file; use astrid generate or pass client= explicitly"
            )
        invoke_out, invoke_project = _resolve_invoke_destination(
            client=client, out=controls.get("out"), project=controls.get("project"),
            project_root=controls.get("project_root"),
        )
        controls.update(out=invoke_out, project=invoke_project)
        import hashlib

        from astrid.core.execution.managed_inputs import managed_file_digest
        from vibecomfy.workflow_bundle import emit_bundle

        def import_member(path: Path) -> dict[str, Any]:
            expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            imported = importer(project=invoke_project, path=path)
            stored = getattr(imported, "data", None) if getattr(imported, "ok", False) else None
            if not isinstance(stored, Mapping):
                error = getattr(imported, "error", None)
                raise CapabilityInvocationError(
                    f"could not import generated {path.name}: "
                    f"{getattr(error, 'message', None) or error or 'no result'}"
                )
            try:
                actual = managed_file_digest(stored, path.name)
            except ValueError as exc:
                raise CapabilityInvocationError(str(exc)) from exc
            if actual != expected:
                raise CapabilityInvocationError(
                    f"imported generated {path.name} does not match its digest"
                )
            return {"digest": actual, "filename": path.name}

        with TemporaryDirectory(prefix="astrid-generate-target-") as scratch:
            root = Path(scratch)
            emitted = emit_bundle(workflow, root / "workflow.py", {"operation": "authored"})
            source = root / "source.json"
            source.write_text(json.dumps(emitted.materialize_ui(schema_provider=schema)), encoding="utf-8")
            result = self.workflow(
                python=import_member(root / "workflow.py"),
                companion=import_member(root / "workflow.vibe.json"),
                source=import_member(source),
                comfy_url=target_client.target.url if target_client is not None else None,
                generation_intent={
                    "version": 1, "modality": modality, "partial_success_policy": "reject",
                    "groups": [{"group_key": "main", "selectors": [{
                        "selector": "main-0", "ordinal": 0,
                        "variant_key": "original", "required": True,
                    }]}],
                    "metadata": {"model": entry.id, "mode": mode},
                },
                **controls,
            )
        return _reconstruct_generation_result(result)

    def image(
        self,
        *,
        model: str | None = None,
        target: str | None = None,
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
        if target is not None:
            return self._generate_on_target(
                registry=registry, model=model, mode=resolved_mode, modality="image",
                target=target, execution=execution, inputs=inputs, dry_run=dry_run,
                client=client, out=out, project=project, project_root=project_root,
                extra_pack_roots=extra_pack_roots, banodoco_config=banodoco_config,
                active_theme=active_theme, include_missing_roots=include_missing_roots,
                brief=brief, check_binaries=check_binaries, python_exec=python_exec,
                verbose=verbose, argv=argv,
            )
        if model is None:
            raise CapabilityValidationError("model is required when target is omitted")
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
        model: str | None = None,
        target: str | None = None,
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
        if target is not None:
            return self._generate_on_target(
                registry=registry, model=model, mode=resolved_mode, modality="video",
                target=target, execution=execution, inputs=inputs, dry_run=dry_run,
                client=client, out=out, project=project, project_root=project_root,
                extra_pack_roots=extra_pack_roots, banodoco_config=banodoco_config,
                active_theme=active_theme, include_missing_roots=include_missing_roots,
                brief=brief, check_binaries=check_binaries, python_exec=python_exec,
                verbose=verbose, argv=argv,
            )
        if model is None:
            raise CapabilityValidationError("model is required when target is omitted")
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
