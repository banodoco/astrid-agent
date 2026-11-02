"""Generation SDK helpers.

This module stays lightweight so ``import astrid.sdk`` exposes ``generate``
without loading model catalogs or generation backend registries until a
generation method is actually invoked.
"""

from __future__ import annotations

import importlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from ._module import _sdk_module
from .exceptions import (
    AstridSDKError,
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


class GenerationInputError(CapabilityValidationError):
    """Keep the SDK validation taxonomy while carrying the shared envelope."""

    def __init__(self, message: str, *, entry=None):
        from astrid.core.generation.preflight import generation_error
        from astrid.core.generation.entries import public_description
        if entry is not None and 'inputs' in entry:
            entry = public_description(entry)
        error = generation_error(ValueError(message), entry=entry,
                                 details={'constraint': message})
        self.readiness = error.readiness
        self.code = 'input_rejected'
        super().__init__(message, details={'readiness': self.readiness})

    def to_envelope(self):
        return {**self.readiness, 'task_id': None}


def _import_generation_member(path: Path, controls: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    from astrid.core.execution.managed_inputs import managed_file_digest
    client = controls.get('client')
    importer = getattr(getattr(client, 'media', None), 'import_file', None)
    if not callable(importer):
        raise CapabilityPreconditionError(
            'generate with target requires a connected Runtime client with '
            'media.import_file; use astrid generate or pass client= explicitly')
    invoke_out, invoke_project = _resolve_invoke_destination(
        client=client, out=controls.get('out'), project=controls.get('project'),
        project_root=controls.get('project_root'))
    controls.update(out=invoke_out, project=invoke_project)
    data = path.read_bytes()
    sha256 = hashlib.sha256(data).hexdigest()
    imported = importer(project=invoke_project, path=path)
    stored = getattr(imported, 'data', None) if getattr(imported, 'ok', False) else None
    if not isinstance(stored, Mapping):
        error = getattr(imported, 'error', None)
        raise CapabilityInvocationError(
            f'could not import generated {path.name}: '
            f"{getattr(error, 'message', None) or error or 'no result'}")
    try:
        actual = managed_file_digest(stored, path.name)
    except ValueError as exc:
        raise CapabilityInvocationError(str(exc)) from exc
    if actual != 'sha256:' + sha256:
        raise CapabilityInvocationError(f'imported generated {path.name} does not match its digest')
    return {'digest': actual, 'sha256': sha256, 'bytes': len(data), 'filename': path.name}


def _retain_generation_source(path: Path, controls: dict[str, Any]) -> dict[str, Any]:
    from tempfile import TemporaryDirectory
    data = path.read_bytes()
    with TemporaryDirectory(prefix="astrid-generation-source-") as scratch:
        snapshot = Path(scratch) / path.name
        snapshot.write_bytes(data)
        return _import_generation_member(snapshot, controls)


@contextmanager
def _snapshot_generation_workflow(path: Path):
    """Read source and canonical companions once for validation and execution."""
    from tempfile import TemporaryDirectory
    original = path.expanduser().resolve()
    if original.is_dir():
        original = original / 'workflow.py'
    with TemporaryDirectory(prefix='astrid-workflow-source-') as scratch:
        snapshot = Path(scratch) / original.name
        try:
            snapshot.write_bytes(original.read_bytes())
            if original.suffix.lower() == '.py':
                for member in (original.with_suffix('.vibe.json'), original.with_name('source.json')):
                    if member.exists():
                        (snapshot.parent / member.name).write_bytes(member.read_bytes())
        except OSError as exc:
            raise GenerationInputError(f'workflow could not be read: {original}: {exc}') from exc
        yield snapshot


def _public_input_values(values):
    from astrid.core.generation.entries import merge_input_values
    values = dict(values)
    supplied = values.pop('inputs', {})
    try:
        return merge_input_values(values, supplied)
    except ValueError as exc:
        raise GenerationInputError(str(exc)) from exc


def _generation_controls(*, engine, consent, max_usd, deadline):
    from astrid.core.generation.targets import canonical_engine
    if engine is not None:
        try:
            selected = canonical_engine(engine)
        except ValueError as exc:
            raise GenerationInputError(str(exc)) from exc
        if selected != 'comfyui':
            raise GenerationInputError('Phase 1 target generation supports only comfyui')
    if max_usd is not None:
        import math
        if type(max_usd) not in (float, int) or not math.isfinite(max_usd) or max_usd <= 0:
            raise GenerationInputError('max_usd must be a finite positive USD cap')
    if deadline is not None:
        from datetime import datetime, timedelta
        try:
            stamp = datetime.fromisoformat(deadline)
            if stamp.utcoffset() != timedelta(0):
                raise ValueError
        except (TypeError, ValueError):
            raise GenerationInputError('deadline must be an absolute UTC timestamp') from None
    if not isinstance(consent, (tuple, list)) or any(scope not in {'user_env', 'paid'} for scope in consent):
        raise GenerationInputError('consent must contain user_env and/or paid scopes')
    return {name: value for name, value in
            (('engine', engine), ('consent', tuple(consent)), ('max_usd', max_usd), ('deadline', deadline))
            if value is not None and value != ()}


def _runpod_paid_controls(controls):
    """Per-call authority only; saved preferences never supply consent/bounds."""
    from datetime import datetime, timezone
    from astrid.core.generation.preflight import generation_error
    consent = tuple(controls.get('consent', ()))
    cap, deadline = controls.get('max_usd'), controls.get('deadline')
    _generation_controls(engine=controls.get('engine'), consent=consent,
                         max_usd=cap, deadline=deadline)
    if ('paid' not in consent or cap is None or deadline is None
            or datetime.fromisoformat(deadline) <= datetime.now(timezone.utc)):
        raise generation_error(ValueError('RunPod requires fresh paid consent, a positive max_usd and a future UTC deadline'),
                               code='input_rejected', details={'field': 'consent'})
    return dict(consent=consent, max_usd=cap, deadline=deadline)


def _runpod_target_policy(controls, *, engine="comfyui"):
    """Snapshot the user's disclosed offer and preferences into the task."""
    from astrid.packs.runpod.target_selection import read_runpod_preferences

    paid = _runpod_paid_controls(controls)
    names = ("offer_digest", "preparation_seconds", "execution_seconds", "idle_seconds")
    if any(controls.get(name) is None for name in names):
        raise GenerationInputError("RunPod requires the disclosed offer digest and preparation, execution and idle bounds")
    return {"kind": "runpod", "engine": engine, "preferences": read_runpod_preferences().as_mapping(),
            **{name: controls[name] for name in names}, **paid, "consent": list(paid["consent"])}


def _runpod_operation(operation, *, client, wait, include_quote=False):
    """Submit an ordinary task to the retained host's target owner."""
    from astrid.packs.runpod.target_selection import read_runpod_preferences

    policy = {"kind": "runpod", "engine": "comfyui", "preferences": read_runpod_preferences().as_mapping()}
    inputs = {"target_policy": json.dumps(policy, sort_keys=True, separators=(',', ':'), allow_nan=False)}
    if operation == 'status':
        inputs['include_quote'] = include_quote
    sdk_module = importlib.import_module('astrid.sdk')
    invoke = client.invoke_result if client is not None else sdk_module.invoke_result
    return invoke('runpod.' + operation, kind='action', inputs=inputs, client=client,
                  wait=wait, read_managed_outputs=False)


def _runpod_status(*, client, include_quote):
    result = _runpod_operation('status', client=client, wait=True, include_quote=include_quote)
    if not result.ok:
        raise CapabilityInvocationError('target status task failed', details={'error': result.error})
    settled = result.raw_result.get('result', {})
    payload = settled.get('payload', settled) if isinstance(settled, Mapping) else {}
    status = payload.get('target_operation') if isinstance(payload, Mapping) else None
    if not isinstance(status, Mapping):
        raise CapabilityInvocationError('target status task omitted its owner evidence')
    return dict(status)


def _bind_runpod_preparation(prepared, *, client, owner, recovery_path, capability_id, h3_endpoint=None):
    from astrid.core.generation.preflight import generation_error
    from .invocation import retain_prepared_owner
    if not prepared.selection_path or not callable(prepared.endpoint_checkout):
        raise generation_error(ValueError('RunPod preparation omitted attested endpoint custody'),
                               code='missing_dependency')
    request = prepared.selection.execution_request(None)
    if request.get('target', {}).get('kind') != 'runpod' or request.get('remote_activation_required') is not True:
        raise generation_error(ValueError('RunPod preparation omitted its exact activation request'),
                               code='input_rejected')
    if h3_endpoint is not None:
        from astrid.core.generation.preflight import freeze_h3_endpoint
        selected = h3_endpoint['target']
        if prepared.h3_warm_lease_seconds != 1800:
            raise generation_error(ValueError('H3 endpoint owner must retain one 1800 second warm lease'),
                                   code='input_rejected')
        if (freeze_h3_endpoint(prepared.h3_target) != selected
                or any(request['target'].get(key) != selected[key]
                       for key in ('pod_id', 'provider_account_ref'))):
            raise generation_error(ValueError('H3 prepared endpoint substituted the selected endpoint'),
                                   code='input_rejected')
    retain_prepared_owner(client, recovery_path=recovery_path, capability_id=capability_id,
                          monitor=prepared.owner_tick, owner=owner,
                          endpoint_checkout=prepared.endpoint_checkout,
                          execution_request=request if capability_id == 'vibecomfy.run' else None,
                          h3_target=prepared.h3_target if h3_endpoint is not None else None,
                          h3_endpoint=h3_endpoint)
    return request


def _generation_boundary(method):
    """Project legacy SDK failures without changing their exception taxonomy."""
    @wraps(method)
    def call(*args, **kwargs):
        from astrid.core.generation.preflight import generation_error
        from astrid.core.contracts.errors import AstridError
        try:
            result = method(*args, **kwargs)
        except AstridSDKError as exc:
            if not callable(getattr(exc, 'to_envelope', None)):
                canonical = generation_error(exc)
                exc.readiness = canonical.readiness
                exc.task_id = canonical.task_id
                exc.to_envelope = canonical.to_envelope
            raise
        except AstridError as exc:
            canonical = generation_error(exc)
            if canonical is exc:
                raise
            raise canonical from exc
        from .results import InvocationResult
        if isinstance(result, InvocationResult) and not result.ok and result.error is not None:
            from dataclasses import replace
            return replace(result, error=generation_error(result.error or {
                'code': 'generation_failed', 'message': 'Generation did not succeed.'},
                task_id=result.kernel_task_id).to_envelope())
        return result
    return call


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


def _validate_catalog_target_request(*, registry, candidate, mode, modality, inputs):
    """Keep matrix and input validation ahead of qualification and target probes."""
    from astrid.core.generation.preflight import validate_generation_request
    mode_spec = candidate.modes.get(mode)
    spec = mode_spec.backends.get('local') if mode_spec is not None else None
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
    translation = getattr(spec, 'param_map', None) or VibeComfyBackend.DEFAULT_PARAM_MAP.get(mode, {})
    required_inputs = {**inputs}
    for canonical, public in translation.items():
        if canonical not in required_inputs and public in inputs:
            required_inputs[canonical] = inputs[public]
    validate_generation_request(registry, model=candidate.id, mode=mode,
                                execution="local", inputs=required_inputs, modality=modality)
    return translation


def _prepare_catalog_target_workflow(*, registry: Any, candidate: Any, mode: str,
                                     modality: str, inputs: dict[str, Any],
                                     target_client: Any, backend: Any, retain_source=None,
                                     defer_target: bool = False):
    """Shared installed-catalog resolution and binding, without admission."""
    from astrid.core.generation.preflight import require_generation_target_readiness
    if not defer_target:
        require_generation_target_readiness(candidate, mode, target_client)
    translation = _validate_catalog_target_request(
        registry=registry, candidate=candidate, mode=mode, modality=modality, inputs=inputs)
    workflow, plan, schema = backend.prepare_catalog_workflow(
        candidate, mode, inputs, resolve_target=target_client is not None,
        **({'retain_source': retain_source} if retain_source is not None else {}),
    )
    from astrid.core.generation.entries import apply_workflow_inputs
    workflow, bindings, assets = apply_workflow_inputs(workflow, mode=mode, values=inputs,
        param_map=translation)
    if bindings['unbound']:
        raise ValueError('workflow has no binding for requested controls: ' + ', '.join(bindings['unbound']))
    return workflow, {**(plan or {}), **bindings}, schema, assets


def _prepare_verified_image_recipe(*, inputs: dict[str, Any], target_client: Any,
                                   model_root: Any) -> bytes:
    """Freeze the installed image producer for its host's private control receiver.

    This is an implementation helper, not an authority entrypoint. Its target
    client and model root must come from the receiving host's retained state;
    neither is decoded from creative inputs. It never invokes the facade.
    """
    import base64
    import hashlib
    import json
    import math
    import secrets
    from tempfile import TemporaryDirectory

    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
    from astrid.core.generation.model_root import (
        ModelRootBinding, validate_model_root_binding,
    )
    from astrid.core.model_catalog import registry as catalog
    from astrid.core.model_catalog.schema import validate_registry_with_backends
    from astrid.core.model_catalog.taxonomy import GenerationTaxonomyRegistry
    from astrid.core.generation.features import backend_descriptors_from_pack
    from astrid.core.pack import load_pack_manifest
    from vibecomfy.model_assets import _referenced_model_values
    from vibecomfy.workflow_bundle import emit_bundle

    def canonical(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")

    allowed = {"prompt", "negative_prompt", "seed", "count", "size", "steps", "guidance_scale"}
    if type(inputs) is not dict or set(inputs) - allowed:
        raise CapabilityValidationError("private image preparation accepts only creative scalar controls")
    if not isinstance(inputs.get("prompt"), str) or not inputs["prompt"].strip():
        raise CapabilityValidationError("private image preparation requires a nonblank prompt")
    if any(not isinstance(inputs[key], str) for key in ("prompt", "negative_prompt", "size") if key in inputs):
        raise CapabilityValidationError("private image text controls must be strings")
    if any(len(inputs[key].encode("utf-8")) > 65536 for key in ("prompt", "negative_prompt", "size") if key in inputs):
        raise CapabilityValidationError("private image text control exceeds its bound")
    if "size" in inputs:
        import re
        if re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", inputs["size"]) is None:
            raise CapabilityValidationError("private image size must be an explicit positive WxH")
    for key in ("seed", "count", "steps"):
        if key in inputs and type(inputs[key]) is not int:
            raise CapabilityValidationError(f"private image {key} must be an integer")
    if inputs.get("count", 1) != 1 or not 1 <= inputs.get("steps", 1) <= 1000:
        raise CapabilityValidationError("private image preparation requires count=1 and bounded positive steps")
    if "guidance_scale" in inputs and (
        type(inputs["guidance_scale"]) not in (int, float)
        or not math.isfinite(inputs["guidance_scale"])
        or not 0 <= inputs["guidance_scale"] <= 1000
    ):
        raise CapabilityValidationError("private image guidance must be finite and bounded")
    seed = inputs.get("seed", -1)
    if seed < -1 or seed >= 2**64:
        raise CapabilityValidationError("private image seed must be -1 or an unsigned 64-bit integer")
    effective = dict(inputs, seed=secrets.randbits(64) if seed == -1 else seed, count=1)
    if type(target_client) is not ComfyTargetClient or type(model_root) is not ModelRootBinding:
        raise CapabilityValidationError("private image preparation requires retained host target and model root")
    bound_root = validate_model_root_binding(model_root.as_dict(), verify_files=True)
    # Read only shipped metadata; do not discover user/project pack extensions.
    shipped_generation = load_pack_manifest(
        Path(__file__).resolve().parents[1] / "packs" / "generation" / "pack.yaml"
    )
    taxonomy = GenerationTaxonomyRegistry(
        backend_descriptors=backend_descriptors_from_pack(shipped_generation)
    )
    registry = catalog.ModelRegistry(validate_registry_with_backends(catalog._load_yaml(
        Path(catalog.__file__).with_name("models.yaml"),
    ), taxonomy_registry=taxonomy))
    preferred = ("qwen-image-2.1-turbo", "z-image", "qwen-image-2512")
    candidates = [entry for entry in registry.list_by_modality("image")
                  if "t2i" in entry.modes and getattr(entry.modes["t2i"].backends.get("local"), "template", "")]
    candidates.sort(key=lambda entry: (preferred.index(entry.id) if entry.id in preferred else len(preferred), entry.id))
    if not candidates:
        raise CapabilityValidationError("installed catalog has no local image producer")
    selected = candidates[0]  # Preparation errors never select another candidate.
    workflow, plan, schema, _assets = _prepare_catalog_target_workflow(
        registry=registry, candidate=selected, mode="t2i", modality="image",
        inputs=effective, target_client=target_client,
        backend=VibeComfyBackend(target_client=target_client),
    )
    if schema is None or not isinstance(plan, dict) or not plan.get("roles"):
        raise CapabilityValidationError("private image producer has no resolved target model authority")
    metadata = getattr(workflow, "metadata", {})
    declared_root = metadata.get("models_root")
    if declared_root is not None and str(declared_root) != str(bound_root.path):
        raise CapabilityValidationError("producer workflow redirects the retained model root")
    inventory = {(item["subdir"], item["name"]): item for item in bound_root.inventory}
    model_references = []
    for reference in _referenced_model_values(workflow):
        value, subdir = reference["value"], reference["subdir"]
        path = Path(subdir) / value
        if ("\\" in value or "\\" in subdir or path.is_absolute()
                or path.as_posix() != (subdir + "/" if subdir else "") + value
                or any(part in {".", ".."} for part in path.parts)):
            raise CapabilityValidationError("producer model reference is not an exact relative inventory path")
        record = inventory.get((path.parent.as_posix() if path.parent != Path(".") else "", path.name))
        if record is None:
            raise CapabilityValidationError("producer model reference is absent from the retained model-root inventory")
        model_references.append({**reference, "inventory": dict(record)})
    if not model_references:
        raise CapabilityValidationError("producer has no verifiable model references")
    spec = selected.modes["t2i"].backends["local"]
    for role, declaration in spec.model_roles.items():
        matches = [ref for ref in model_references if ref["class_type"] == declaration.node_class
                   and ref["field"] == declaration.input and ref["value"] == plan["roles"].get(role)]
        if len(matches) != 1:
            raise CapabilityValidationError("resolved producer role is absent or ambiguous in the frozen workflow")
    workflow_inputs = {name: {"node_id": item.node_id, "field": item.field, "value": item.value}
                       for name, item in sorted(workflow.inputs.items())}
    param_map = dict(spec.param_map or VibeComfyBackend.DEFAULT_PARAM_MAP["t2i"])
    control_bindings = {key: (["width", "height"] if key == "size" else [param_map[key]])
                        for key in effective if key != "count" and key in param_map}
    with TemporaryDirectory(prefix="astrid-private-image-recipe-") as scratch:
        root = Path(scratch)
        bundle = emit_bundle(workflow, root / "workflow.py", {"operation": "authored"})
        bundle.require_canonical_authority("private verified image preparation")
        approval = bundle.compile(schema_provider=schema, models_root=bound_root.path)
        detached = approval.to_dict()
        member_bytes = {"workflow.py": (root / "workflow.py").read_bytes(),
                        "workflow.vibe.json": (root / "workflow.vibe.json").read_bytes(),
                        "source.json": canonical(detached["ui_projection"])}
        result = {"version": "astrid.private-image-recipe/v1", "entry": selected.id,
                  "modality": "image", "mode": "t2i", "template": spec.template,
                  "template_hash": spec.template_hash,
                  "effective_inputs": effective, "workflow_inputs": workflow_inputs,
                  "control_bindings": control_bindings, "resolved_randomness": {"seed": effective["seed"]},
                  "resolution": plan, "model_root": bound_root.as_dict(),
                  "model_references": model_references, "approval": detached,
                  "members": {name: {"sha256": "sha256:" + hashlib.sha256(data).hexdigest(),
                                      "size": len(data), "bytes_base64": base64.b64encode(data).decode("ascii")}
                              for name, data in member_bytes.items()}}
        frozen = canonical(result)
    if len(frozen) > 768 * 1024:
        raise CapabilityValidationError("private image recipe exceeds its response bound")
    return frozen


@dataclass(frozen=True)
class GenerationFacade:
    """Public typed entrypoint for generation executors."""

    sdk_module_name: str = "astrid.sdk"

    def list(self, *, kind=None):
        from astrid.core.generation.listing import generation_rows
        return generation_rows(kind)

    @_generation_boundary
    def launch(self, *, target='auto', engine='comfyui', consent=(), timeout=10.0,
               project_root=None):
        """Launch/re-probe one selected human ComfyUI with fresh user_env consent.

        Return the ordinary readiness envelope, including manual-start guidance
        on failure. Never save consent or health, discover a fallback, or admit
        a generation task. URL/env overrides resolve without prior launch facts.
        """
        from astrid.core.generation import targets
        from astrid.core.generation.target_launch import launch_target

        _generation_controls(engine=engine, consent=consent, max_usd=None, deadline=None)
        if target == 'runpod':
            raise GenerationInputError('launch requires a selected local human ComfyUI')
        try:
            selection = targets.resolve_target(target, repo=project_root)
        except (ValueError, OSError) as exc:
            raise GenerationInputError(str(exc)) from exc
        return launch_target(selection, consent=consent, timeout=timeout).readiness

    @_generation_boundary
    def targets(self, *, target='auto', engine='comfyui', save=False, inputs=None,
                project_root=None, client=None):
        """Inspect selection without probing; persist only an explicit save."""
        from astrid.core.generation import targets
        from astrid.core.generation.entries import merge_input_values
        from astrid.core.generation.preflight import readiness_envelope
        try:
            if targets.canonical_engine(engine) != 'comfyui':
                raise ValueError('Phase 1 targets support only comfyui')
            updates = merge_input_values(inputs if inputs is not None else {})
            if target == 'runpod':
                if save:
                    allowed = {'gpu_types', 'datacenter_id', 'volume', 'image', 'idle_timeout_seconds',
                               'provider_account_ref', 'credential_ref'}
                    if updates.keys() - allowed:
                        raise ValueError('unsupported RunPod preference: ' + ', '.join(sorted(updates.keys() - allowed)))
                    def write(document):
                        preferences = document.setdefault('runpod', {})
                        if not isinstance(preferences, dict):
                            raise ValueError('runpod preferences must be a table')
                        for name, value in updates.items():
                            if value is None:
                                preferences.pop(name, None)
                            else:
                                preferences[name] = value
                    targets.update_config(write)
                elif updates:
                    raise ValueError('provider inputs require --save')
                payload = readiness_envelope()
                payload['preferences'] = targets.read_config().get('runpod', {})
                if not save and client is not None:
                    payload['target'] = _runpod_status(client=client, include_quote=False)
                return payload
            if updates:
                raise ValueError('local target input updates are not supported; select an explicit URL')
            selection = targets.resolve_target(target, repo=project_root)
            record = selection.record
            if record is None and target == 'managed':
                record = targets.TargetRecord(owner='astrid')
            if save:
                if not selection.bound:
                    raise ValueError('select an explicit target before saving discovered defaults')
                if record is None:
                    raise ValueError('select an explicit target before saving')
                targets.save_target(record)
            return readiness_envelope(target=record)
        except (ValueError, OSError) as exc:
            raise GenerationInputError(str(exc)) from exc

    @_generation_boundary
    def describe(self, entry: str | None = None, *, workflow=None, mode=None,
                 target=None, engine='comfyui', client=None):
        from astrid.core.generation.entries import describe_entry, describe_workflow
        if target is not None:
            _generation_controls(engine=engine, consent=(), max_usd=None, deadline=None)
            if target != 'runpod' or entry is not None or workflow is not None:
                raise GenerationInputError('target describe requires only target=runpod')
            return _runpod_status(client=client, include_quote=True)
        if bool(entry) == bool(workflow):
            raise GenerationInputError('describe requires an entry or a workflow')
        return describe_entry(entry, mode=mode) if entry else describe_workflow(Path(workflow), mode=mode)

    @_generation_boundary
    def release(self, *, engine: str, target: str, client=None, wait=False):
        """Release a managed engine through its verified owner; never HTTP free an unowned target.

        Attached or unmanaged targets are refused and never receive an HTTP free.
        The owned managed engine is verified as the registered loopback owner, then
        released through the existing native unload path (CheckoutServerAdapter.release).
        """
        from astrid.core.generation.preflight import generation_error
        from astrid.core.generation.targets import canonical_engine
        if canonical_engine(engine) != 'comfyui':
            raise GenerationInputError('Phase 1 release supports only comfyui')
        if target == 'runpod':
            return _runpod_operation('release', client=client, wait=wait)
        bridge = getattr(client, 'release_generation', None)
        if not callable(bridge):
            raise generation_error(ValueError('The bound host release owner is unavailable'),
                code='missing_dependency', details={'field': 'target'},
                fixes=[{'argv': ['astrid', 'generate', 'targets', '--target', target],
                        'scope': 'read_only', 'details': {}}])
        result = bridge(engine='comfyui', target=target, wait=wait)
        if isinstance(result, dict) and result.get('ok') is False:
            raise generation_error(ValueError(str(result.get('reason') or 'release was not proven')),
                code='release_not_proven', details={'field': 'target'})
        return result

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

    @_generation_boundary
    def workflow(
        self, *,
        workflow: WorkflowFile | None = None,
        target: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        mode: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
        max_usd: float | None = None,
        deadline: str | None = None,
        offer_digest: str | None = None,
        preparation_seconds: float | None = None,
        execution_seconds: float | None = None,
        idle_seconds: float | None = None,
        target_policy: Mapping[str, Any] | None = None,
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
        if target is not None:
            from astrid.core.generation.entries import resolve_workflow_inputs
            controls = _generation_controls(engine=engine, consent=consent, max_usd=max_usd, deadline=deadline)
            controls.update(offer_digest=offer_digest, preparation_seconds=preparation_seconds,
                            execution_seconds=execution_seconds, idle_seconds=idle_seconds)
            if not isinstance(workflow, (str, Path)) or python is not None or companion is not None:
                raise GenerationInputError('target workflow requires one local workflow path')
            if inputs is not None and not isinstance(inputs, Mapping):
                raise GenerationInputError('workflow inputs must be an object')
            if any(value is not None for value in (
                    execution_request, placement, source, source_video, source_video_node,
                    source_video_widget, managed_assets, workflow_inputs, comfy_url,
                    comfy_output_dir, generation_intent, target_policy)):
                raise GenerationInputError('a target workflow cannot replace explicit prepared binding or managed source controls')
            source_controls = {'client': client, 'project': project, 'out': out,
                               'project_root': project_root}
            with _snapshot_generation_workflow(Path(workflow)) as frozen:
                from astrid.core.generation.availability import require_bundled_workflow_available
                original = Path(workflow).expanduser().resolve()
                if original.is_dir():
                    original = original / 'workflow.py'
                require_bundled_workflow_available(frozen, source_path=original)
                retained_source = None
                if not dry_run:
                    original_source = frozen.with_name('source.json') if frozen.suffix == '.py' else frozen
                    if not original_source.is_file():
                        original_source = frozen
                    retained_source = _retain_generation_source(original_source, source_controls)
                    project, out = source_controls['project'], source_controls['out']
                try:
                    description, values = resolve_workflow_inputs(frozen, inputs or {}, mode=mode)
                except ImportError as exc:
                    from astrid.core.generation.preflight import generation_error
                    raise generation_error(exc, code='missing_dependency',
                        details={'field': 'workflow', 'module': exc.name}) from exc
                except (ValueError, RuntimeError, OSError) as exc:
                    raise GenerationInputError(str(exc)) from exc
                workflow_entry = {'id': description['id'], 'kind': description['kind'],
                    'modes': [description['mode']], 'bundle': str(frozen),
                    'binds': {}, 'defaults': {}, 'needs': []}
                return self._generate_on_target(registry=_load_model_registry(
                    project_root=project_root, extra_pack_roots=extra_pack_roots), model=None,
                    mode=description['mode'], modality=description['kind'], target=target,
                    execution=None, inputs=values, dry_run=dry_run, workflow_entry=workflow_entry,
                    retained_source=retained_source,
                    client=client, project=project, out=out, wait=wait,
                    project_root=project_root, extra_pack_roots=extra_pack_roots,
                    banodoco_config=banodoco_config, active_theme=active_theme,
                    include_missing_roots=include_missing_roots, brief=brief,
                    idempotency_context=idempotency_context, idempotency_key=idempotency_key,
                    recovery_path=recovery_path, resume=resume, read_managed_outputs=read_managed_outputs,
                    timeout_seconds=timeout_seconds, poll_seconds=poll_seconds,
                    check_binaries=check_binaries, python_exec=python_exec,
                    verbose=verbose, argv=argv, **controls)
        if inputs is not None:
            raise GenerationInputError('workflow inputs require an explicit target')
        from astrid.core.generation.backends.registry import (
            GenerationBackendRegistry, resolve_workflow_descriptor,
        )

        resolve_workflow_descriptor(
            GenerationBackendRegistry(), placement=placement,
            execution_request=execution_request,
        )
        from astrid.core.generation.availability import require_bundled_workflow_available
        for member in (workflow, python):
            if isinstance(member, (str, Path)):
                require_bundled_workflow_available(member)
        inputs = {
            name: value for name, value in {
                "workflow": workflow, "python": python, "companion": companion,
                "source": source, "source_video": source_video,
                "source_video_node": source_video_node,
                "source_video_widget": source_video_widget,
                "managed_assets": managed_assets, "workflow_inputs": workflow_inputs,
                "comfy_url": comfy_url, "comfy_output_dir": comfy_output_dir,
                "target_policy": (json.dumps(dict(target_policy), sort_keys=True, separators=(',', ':'), allow_nan=False)
                                  if target_policy is not None else None),
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
        workflow_entry: dict[str, Any] | None = None,
        retained_source: dict[str, Any] | None = None,
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
            generation_error, readiness_envelope, compare_versions,
        )

        is_remote_target = target == 'runpod'
        if is_remote_target:
            _runpod_paid_controls(controls)
        target_policy = _runpod_target_policy(controls) if is_remote_target and not dry_run else None
        for name in ('engine', 'consent', 'max_usd', 'deadline', 'offer_digest', 'preparation_seconds',
                     'execution_seconds', 'idle_seconds'):
            controls.pop(name, None)

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
        from astrid.core.generation import entries as workflow_entries
        from astrid.packs.vibecomfy.production_engine import ProductionEngineError

        # Catalog priority is explicit, stable and per modality/mode. Unknown
        # catalog entries and user entries remain eligible in lexical order.
        preferred = {
            ("image", "t2i"): ("qwen-image-2.1-turbo", "z-image", "qwen-image-2512"),
            ("image", "edit"): ("qwen-image-2.1-turbo-edit", "z-image", "qwen-image-2512"),
        }.get((modality, mode), ())
        def rank(candidate: Any) -> tuple[int, str]:
            identifier = candidate[1].id if candidate[0] == "catalog" else candidate[1]["id"]
            return (preferred.index(identifier) if identifier in preferred else len(preferred), identifier)

        if workflow_entry is not None:
            candidates = [("workflow", workflow_entry)]
        elif model is not None:
            manifest = workflow_entries.find_entry(model)
            if manifest is not None:
                candidates = [("workflow", manifest)]
            else:
                try:
                    candidates = [("catalog", registry.get(model))]
                except KeyError as exc:
                    raise CapabilityValidationError(str(exc)) from exc
        else:
            candidates = [("catalog", entry) for entry in registry.list_by_modality(modality)
                          if mode in entry.modes and getattr(entry.modes[mode].backends.get("local"), "template", "")]
            candidates += [("workflow", entry) for entry in
                           [*({**entry, 'source': 'user-entry'} for entry in workflow_entries.user_entries()),
                            *({**entry, 'source': 'ready-template'} for entry in workflow_entries.ready_entries())]
                           if entry["kind"] == modality and mode in entry["modes"]
                           and not entry.get("registry_id")]
            candidates.sort(key=rank)
        from astrid.core.generation.availability import (
            require_model_available, require_entry_available,
        )
        automatic = model is None and workflow_entry is None
        qualified = []
        filtered = []
        for source, candidate in candidates:
            identifier = candidate.id if source == 'catalog' else candidate['id']
            try:
                if source == 'catalog':
                    if automatic:
                        require_model_available(identifier, mode, 'local')
                    else:
                        _validate_catalog_target_request(registry=registry, candidate=candidate,
                            mode=mode, modality=modality, inputs=inputs)
                else:
                    if not automatic and (candidate['kind'] != modality or mode not in candidate['modes']):
                        raise CapabilityValidationError(f"entry {identifier!r} does not support {modality}/{mode}")
                    require_entry_available(candidate, mode)
            except AstridError as exc:
                if not automatic or exc.code != 'generation_route_unqualified':
                    raise
                filtered.append((identifier, exc))
                continue
            qualified.append((source, candidate))
        if automatic and filtered and not qualified:
            raise AstridError(
                'No qualified generation candidate remains; filtered routes: '
                + '; '.join(f'{identifier}: {exc}' for identifier, exc in filtered),
                code='generation_route_unqualified', recovery_command=filtered[0][1].fix)
        candidates = qualified
        target_client = (None if is_remote_target else select_generation_target(
            target, repo=controls.get("project_root"),
            entry_id=model or (workflow_entry or {}).get('registry_id') or (workflow_entry or {}).get('id')))
        backend = VibeComfyBackend(target_client=target_client)

        def import_member(path: Path) -> dict[str, Any]:
            return _import_generation_member(path, controls)

        source_ref = retained_source

        def retain_source(filename: str, data: bytes) -> None:
            nonlocal source_ref
            with TemporaryDirectory(prefix='astrid-generation-source-') as scratch:
                snapshot = Path(scratch) / filename
                snapshot.write_bytes(data)
                source_ref = import_member(snapshot)

        selected = None
        failures: list[str] = []
        for source, candidate in candidates:
            source_ref = retained_source
            identifier = candidate.id if source == "catalog" else candidate["id"]
            try:
                assets = {}
                if source == "catalog":
                    workflow, plan, schema, assets = _prepare_catalog_target_workflow(
                        registry=registry, candidate=candidate, mode=mode, modality=modality,
                        inputs=inputs, target_client=target_client, backend=backend,
                        retain_source=None if dry_run else retain_source, defer_target=is_remote_target)
                else:
                    from types import SimpleNamespace
                    prerequisite = SimpleNamespace(id=identifier, modes={mode: SimpleNamespace(
                        backends={"local": SimpleNamespace(template=str(candidate["bundle"]))})})
                    if not is_remote_target:
                        require_generation_target_readiness(prerequisite, mode, target_client)
                    if candidate["kind"] != modality or mode not in candidate["modes"]:
                        raise ValueError(f"entry {identifier!r} does not support {modality}/{mode}")
                    registry_id = candidate.get("registry_id")
                    if registry_id:
                        catalog_entry = registry.get(registry_id)
                        if not is_remote_target:
                            require_generation_target_readiness(catalog_entry, mode, target_client)
                        validate_generation_request(registry, model=registry_id, mode=mode,
                                                    execution="local", inputs=inputs, modality=modality)
                        workflow, plan, schema, assets = _prepare_catalog_target_workflow(
                            registry=registry, candidate=catalog_entry, mode=mode,
                            modality=modality, inputs=inputs, target_client=target_client, backend=backend,
                            retain_source=None if dry_run else retain_source, defer_target=is_remote_target)
                    else:
                        with _snapshot_generation_workflow(workflow_entries.bundle_path(candidate)) as frozen:
                            if not dry_run and source_ref is None:
                                original_path = frozen
                                retained = frozen.with_name('source.json')
                                if retained.is_file():
                                    original_path = retained
                                retain_source(original_path.name, original_path.read_bytes())
                            workflow, plan, assets = workflow_entries.prepare(
                                frozen, target_client=target_client, mode=mode,
                                binds=candidate.get("binds"), defaults=candidate.get("defaults"), values=inputs)
                        if plan["kind"] != modality:
                            raise ValueError("entry kind does not match workflow output")
                        if plan["unbound"]:
                            raise ValueError("workflow has no binding for requested controls: " + ", ".join(plan["unbound"]))
                        if workflow_entry is not None:
                            from vibecomfy.entries import draft_entry
                            needs = draft_entry(workflow, identifier)['needs']
                            # A raw audio workflow may run without being saved
                            # as one of the catalog's named audio modes.
                            needs = [need for need in needs if need.get('ambiguity') !=
                                     'audio mode is unknown; choose tts, music, or sfx']
                            needs.extend(need for need in candidate.get('needs', [])
                                         if need.get('ambiguity') and need not in needs)
                            candidate = {**candidate, 'needs': needs}
                        schema = workflow_entries.target_schema(candidate, workflow, target_client)
                from vibecomfy.workflow_bundle import load_bundle
                if source == "workflow" and not candidate.get("registry_id"):
                    # Re-emit the edited canonical workflow before approval so
                    # the input bindings are represented by a fresh revision.
                    from vibecomfy.workflow_bundle import emit_bundle
                    with TemporaryDirectory(prefix="astrid-entry-approval-") as scratch:
                        bundle = emit_bundle(workflow, Path(scratch) / "workflow.py", {"operation": "authored"})
                        bundle.require_canonical_authority("generate on target")
                        approval = bundle.compile(schema_provider=schema) if schema is not None and not assets else None
                else:
                    bundle = load_bundle(workflow)
                    bundle.require_canonical_authority("generate on target")
                    approval = bundle.compile(schema_provider=schema) if schema is not None and not assets else None
            except (AstridError, ValueError, KeyError, OSError, CapabilityValidationError, ProductionEngineError) as exc:
                if (model is not None or workflow_entry is not None
                        or getattr(exc, "code", None) in {"COMFY_TARGET_NOT_READY", "engine_not_found"}):
                    # Package/runtime prerequisites apply to every local
                    # candidate; retain their existing error and guidance.
                    if isinstance(exc, AstridError):
                        raise generation_error(exc) from exc
                    if isinstance(exc, CapabilityValidationError):
                        raise
                    raise generation_error(exc, entry={'id': identifier, 'engine': 'comfyui', 'mode': mode},
                        target=getattr(target_client, 'generation_target', None),
                        code='missing_dependency' if 'missing' in str(exc).lower() else 'input_rejected') from exc
                failures.append(f"{identifier}: {exc}")
                continue
            selected = (identifier, workflow, bundle, plan, schema, approval, assets)
            break
        if selected is None:
            raise AstridError(
                f"No ready local {modality} model supports mode {mode!r} on the selected target"
                + (": " + "; ".join(failures) if failures else ""),
                code="COMFY_TARGET_NO_MODEL", field="model",
                fix="Use an attached ComfyUI with the required nodes and models, or an installed managed runtime",
            )
        entry_id, workflow, bundle, plan, schema, approval, assets = selected
        target_record = getattr(target_client, 'generation_target', None)
        from astrid.core.generation.targets import TargetRecord
        if target_record is None:
            target_record = TargetRecord(owner='human' if target_client else 'astrid',
                endpoint=target_client.target.url if target_client else None)
        # Recorded versions are provenance; only known differences warn.
        requirements = getattr(workflow, 'requirements', None)
        recorded = getattr(workflow, 'recorded_versions', None) or {}
        runtime = getattr(requirements, 'runtime', None)
        if not recorded and runtime is not None:
            recorded = {'comfyui': runtime.comfy_version, 'python': runtime.python_version}
        observed = {**(target_record.facts.get('versions') or {})}
        system = target_record.facts.get('system') or {}
        observed.setdefault('comfyui', system.get('comfyui_version'))
        observed.setdefault('python', system.get('python_version'))
        if not isinstance(recorded, dict):
            recorded = {}
        inventory_entry = candidate if source == 'workflow' else {'binds': {}, 'defaults': {}}
        inventory = workflow_entries.input_inventory(workflow_entries._workflow_inputs(
            inventory_entry, mode, workflow=workflow))
        effective_values = {row['name']: workflow.inputs[row['name']].value for row in inventory
                            if row['name'] in workflow.inputs}
        effective_values.update((plan or {}).get('bindings', inputs))
        description = {'id': entry_id, 'engine': 'comfyui', 'mode': mode, 'kind': modality,
                       'inputs': inventory,
                       'effective_values': effective_values,
                       'accepted_inputs': sorted(inputs), 'rejected_inputs': []}
        readiness = readiness_envelope(entry=description, target=(
            {'engine': 'comfyui', 'placement': 'runpod', 'state': 'pending_host_readiness'} if is_remote_target else target_record),
                                      issues=compare_versions(recorded, observed))
        if dry_run:
            preview = dict(plan or {"model": entry_id, "mode": mode})
            preview.update(model=entry_id, mode=mode, reason=("explicitly selected workflow entry" if model is not None or workflow_entry is not None else "highest-ranked ready local entry for the requested kind and mode on the selected target"))
            if approval is not None:
                preview["approval"] = {"status": "approved", "api_digest": approval.api_digest}
            readiness['entry'].update(preview)
            return readiness

        from vibecomfy.workflow_bundle import emit_bundle
        if source_ref is None:
            raise CapabilityInvocationError('generation did not retain its original source')

        with TemporaryDirectory(prefix="astrid-generate-target-") as scratch:
            root = Path(scratch)
            emit_bundle(workflow, root / "workflow.py", {"operation": "authored"})
            asset_arguments: dict[str, Any] = {}
            if assets:
                import zipfile
                from astrid.packs.vibecomfy.asset_manifest import build_asset_manifest
                manifest = build_asset_manifest(assets)
                archive = root / "assets.zip"
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
                    zipped.writestr("manifest.json", json.dumps(manifest))
                    for record in manifest["assets"]:
                        zipped.write(assets[record["binding"]], record["member"])
                asset_arguments["managed_assets"] = import_member(archive)
            result = self.workflow(
                python=import_member(root / "workflow.py"),
                companion=import_member(root / "workflow.vibe.json"),
                source=source_ref,
                # Carry explicit native bindings through the executor as well
                # as the sealed graph. The attached runner chooses a fresh
                # seed when no seed binding is supplied. Media bindings are
                # supplied separately by the staged asset manifest.
                workflow_inputs=json.dumps({name: value for name, value in
                    (plan or {}).get('bindings', {}).items() if name not in assets},
                    sort_keys=True, allow_nan=False),
                comfy_url=(target_client.target.url if target_client is not None else None),
                target_policy=target_policy,
                generation_intent={
                    "version": 1, "modality": modality, "partial_success_policy": "reject",
                    "groups": [{"group_key": "main", "selectors": [{
                        "selector": "main-0", "ordinal": 0,
                        "variant_key": "original", "required": True,
                    }]}],
                    "metadata": {"model": entry_id, "mode": mode},
                },
                **asset_arguments, **controls,
            )
        if controls.get('wait') is False:
            return result
        return _reconstruct_generation_result(result)

    @staticmethod
    def _resolve_public_entry(*, entry: str, model: str | None,
                              mode: str | None, modality: str,
                              inputs: Mapping[str, Any], registry: Any):
        from astrid.core.generation.entries import describe_entry, resolve_entry_inputs, validate_inputs

        if model is not None and model != entry:
            raise CapabilityValidationError("model and entry select different generation entries")
        try:
            description = describe_entry(entry, mode=mode, registry=registry, _native=True)
            ports = {port["name"]: port for port in description["inputs"]}
            producer = (description.get("action") == "h3_av.transform"
                        and all(ports.get(name, {}).get("file_custody") == "producer_local"
                                for name in ("request", "asset_map")))
            if producer:
                # The H3 admission adapter alone reads these owner controls.
                # Keep its explicit producer boundary while validating each
                # declared control's type, without changing execution metadata.
                from astrid.packs.h3_av.actions.transform.run import PUBLIC_CONTROLS
                controls = {key: value for key, value in inputs.items() if key in PUBLIC_CONTROLS}
                description, values = resolve_entry_inputs(
                    entry, {key: value for key, value in inputs.items() if key not in PUBLIC_CONTROLS},
                    mode=mode, kind=modality, registry=registry,
                )
                control_schema = {**description, "inputs": [
                    {**port, **({"type": "object"} if port["name"] in {"execution_request", "cleanup_receipt"}
                                and isinstance(controls.get(port["name"]), Mapping) else
                               {"file_custody": "producer_local"} if port["type"] == "file" else {})}
                    for port in description["inputs"] if port["name"] in PUBLIC_CONTROLS]}
                values.update(validate_inputs(control_schema, controls, required=False))
            else:
                description, values = resolve_entry_inputs(
                    entry, dict(inputs), mode=mode, kind=modality, registry=registry,
                )
        except (ValueError, KeyError) as exc:
            raise GenerationInputError(str(exc), entry=locals().get('description')) from exc
        from astrid.core.generation.availability import require_entry_available
        require_entry_available(description, description['mode'])
        if description.get("action") and (not producer or description.get("runtime_parent_required")):
            raise CapabilityPreconditionError(
                "action-backed generation requires a Runtime-admitted parent; "
                "the tracked parent interface is not yet available"
            )
        return description, values

    @_generation_boundary
    def image(
        self,
        *,
        model: str | None = None,
        entry: str | None = None,
        target: str | None = None,
        mode: str | None = None,
        execution: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
        max_usd: float | None = None,
        deadline: str | None = None,
        offer_digest: str | None = None,
        preparation_seconds: float | None = None,
        execution_seconds: float | None = None,
        idle_seconds: float | None = None,
        wait: bool = True,
        out: Path | str | None = None,
        project: str | None = None,
        client: Any | None = None,
        recovery_path: Path | str | None = None,
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
        inputs = _public_input_values(inputs)
        controls = _generation_controls(engine=engine, consent=consent, max_usd=max_usd, deadline=deadline)
        controls.update(offer_digest=offer_digest, preparation_seconds=preparation_seconds,
                        execution_seconds=execution_seconds, idle_seconds=idle_seconds)
        _reject_retired_generation_inputs(inputs)
        sdk_module = importlib.import_module(self.sdk_module_name)
        registry = None
        if entry is not None:
            registry = sdk_module._load_model_registry(
                project_root=project_root, extra_pack_roots=extra_pack_roots,
            )
            description, inputs = self._resolve_public_entry(
                entry=entry, model=model, mode=mode, modality="image",
                inputs=inputs, registry=registry,
            )
            model, mode = entry, description["mode"]
            if description["source"] == "workflow" and target is None:
                target = "auto"
        resolved_mode = sdk_module._infer_image_mode(mode, inputs)
        explicit_openai = execution == "openai" or (
            isinstance(model, str) and model.startswith("gpt-image")
        )
        if explicit_openai and mode is None and inputs.get("image_ref"):
            resolved_mode = "edit"
        if model is not None and mode is None and inputs.get("image_ref"):
            from astrid.core.generation.entries import find_entry
            manifest = find_entry(model)
            if manifest is not None and "edit" in manifest["modes"] and "i2i" not in manifest["modes"]:
                resolved_mode = "edit"
        if registry is None:
            registry = sdk_module._load_model_registry(
                project_root=project_root,
                extra_pack_roots=extra_pack_roots,
            )
        if engine is not None and target is None:
            target = 'auto'
        if target is not None:
            return self._generate_on_target(
                registry=registry, model=model, mode=resolved_mode, modality="image",
                target=target, execution=execution, inputs=inputs, dry_run=dry_run,
                client=client, out=out, project=project, project_root=project_root,
                extra_pack_roots=extra_pack_roots, banodoco_config=banodoco_config,
                active_theme=active_theme, include_missing_roots=include_missing_roots,
                brief=brief, check_binaries=check_binaries, python_exec=python_exec,
                verbose=verbose, argv=argv,
                **controls, **({'wait': False} if not wait else {}),
                **({'recovery_path': recovery_path} if recovery_path is not None else {}),
            )
        if model is None:
            raise CapabilityValidationError("model is required when target is omitted")
        try:
            model_entry = registry.get(model)
        except KeyError as exc:
            raise CapabilityValidationError(str(exc)) from exc

        if (mode is None and resolved_mode in sdk_module._EXPLICIT_ONLY_IMAGE_MODES
                and not explicit_openai):
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
            **({'wait': False} if not wait else {}),
            **({'recovery_path': recovery_path} if recovery_path is not None else {}),
        )
        if dry_run or not wait:
            return result
        return _reconstruct_generation_result(result)

    @staticmethod
    def _validate_action_entry(entry: dict[str, Any], *, modality: str,
                               mode: str | None, inputs: Mapping[str, Any]) -> None:
        """Check declared bindings without initiating the untracked action route."""
        if entry["kind"] != modality or (mode is not None and mode not in entry["modes"]):
            raise CapabilityValidationError(f"entry {entry['id']!r} does not support {modality}/{mode}")
        if mode is None and len(entry["modes"]) != 1:
            raise CapabilityValidationError(f"entry {entry['id']!r} requires an explicit mode")
        from astrid.core.generation.entries import validate_inputs
        try:
            validate_inputs({**entry, "source": "action"}, dict(inputs))
        except ValueError as exc:
            raise CapabilityValidationError(str(exc)) from exc

    @_generation_boundary
    def video(
        self,
        *,
        model: str | None = None,
        entry: str | None = None,
        target: str | None = None,
        mode: str | None = None,
        execution: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
        max_usd: float | None = None,
        deadline: str | None = None,
        offer_digest: str | None = None,
        preparation_seconds: float | None = None,
        execution_seconds: float | None = None,
        idle_seconds: float | None = None,
        wait: bool = True,
        request: str | Mapping[str, Any] | None = None,
        asset_map: str | Mapping[str, Any] | None = None,
        out: Path | str | None = None,
        project: str | None = None,
        client: Any | None = None,
        recovery_path: Path | str | None = None,
        resume: bool = False,
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
        inputs = _public_input_values(inputs)
        controls = _generation_controls(engine=engine, consent=consent, max_usd=max_usd, deadline=deadline)
        controls.update(offer_digest=offer_digest, preparation_seconds=preparation_seconds,
                        execution_seconds=execution_seconds, idle_seconds=idle_seconds)
        _reject_retired_generation_inputs(inputs)
        inputs.update({name: value for name, value in
                       (("request", request), ("asset_map", asset_map)) if value is not None})
        sdk_module = importlib.import_module(self.sdk_module_name)
        registry = None
        action_entry = None
        if entry is not None:
            registry = sdk_module._load_model_registry(
                project_root=project_root, extra_pack_roots=extra_pack_roots,
            )
            description, inputs = self._resolve_public_entry(
                entry=entry, model=model, mode=mode, modality="video",
                inputs=inputs, registry=registry,
            )
            model, mode = entry, description["mode"]
            action_entry = description if description.get("action") else None
            if description["source"] == "workflow" and target is None:
                target = "auto"
        elif model is not None:
            from astrid.core.generation.entries import find_entry
            manifest = find_entry(model)
            if manifest is not None and "action" in manifest:
                registry = sdk_module._load_model_registry(
                    project_root=project_root, extra_pack_roots=extra_pack_roots,
                )
                action_entry, inputs = self._resolve_public_entry(
                    entry=model, model=model, mode=mode, modality="video", inputs=inputs, registry=registry,
                )
        if action_entry is not None:
            if action_entry["action"] != "h3_av.transform":
                raise CapabilityPreconditionError("action-backed generation requires a Runtime-admitted parent")
            if target not in (None, "auto", "managed", "runpod") or execution not in (None, "local"):
                raise CapabilityValidationError("tracked H3 requires the default Runtime target")
            if not inputs.get("request") or not inputs.get("asset_map"):
                raise CapabilityValidationError("H3 requires request and asset_map")
            if target == 'runpod':
                _runpod_paid_controls(controls)
                if not dry_run:
                    inputs['target_policy'] = _runpod_target_policy(controls, engine='vibecomfy-h3')
            controls = dict(kind="action", inputs=inputs, project=project, out=out,
                project_root=project_root, extra_pack_roots=extra_pack_roots,
                banodoco_config=banodoco_config, include_missing_roots=include_missing_roots,
                dry_run=dry_run, wait=wait, read_managed_outputs=True,
                recovery_path=recovery_path, resume=resume)
            if client is None and not dry_run:
                # Reuse the ordinary owner launcher connection; the admitted
                # parent, including prepared custody, stays in this context.
                with sdk_module.AstridClient.open_from_launcher(start_pack_host=False) as owner:
                    return owner.invoke_result("h3_av.transform", **controls)
            invoke = client.invoke_result if client is not None else sdk_module.invoke_result
            return invoke("h3_av.transform", **controls)
        if resume or recovery_path is not None and target != 'runpod':
            raise CapabilityValidationError("video recovery is supported for the tracked H3 action")
        resolved_mode = sdk_module._infer_video_mode(mode, inputs)
        if registry is None:
            registry = sdk_module._load_model_registry(
                project_root=project_root,
                extra_pack_roots=extra_pack_roots,
            )
        if engine is not None and target is None:
            target = 'auto'
        if target is not None:
            return self._generate_on_target(
                registry=registry, model=model, mode=resolved_mode, modality="video",
                target=target, execution=execution, inputs=inputs, dry_run=dry_run,
                client=client, out=out, project=project, project_root=project_root,
                extra_pack_roots=extra_pack_roots, banodoco_config=banodoco_config,
                active_theme=active_theme, include_missing_roots=include_missing_roots,
                brief=brief, check_binaries=check_binaries, python_exec=python_exec,
                verbose=verbose, argv=argv,
                **controls, **({'wait': False} if not wait else {}),
                **({'recovery_path': recovery_path} if recovery_path is not None else {}),
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
            **({'wait': False} if not wait else {}),
        )
        if dry_run or not wait:
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
