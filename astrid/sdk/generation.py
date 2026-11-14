"""Generation SDK helpers.

This module stays lightweight so ``import astrid.sdk`` exposes ``generate``
without loading model catalogs or generation backend registries until a
generation method is actually invoked.
"""

from __future__ import annotations

import builtins
import importlib
import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from ._module import _sdk_module
from .exceptions import (
    AstridSDKError,
    CapabilityInvocationError,
    CapabilityPreconditionError,
    CapabilityValidationError,
)
from .results import _reconstruct_generation_result

if TYPE_CHECKING:
    from astrid.core.generation.placement import Placement

    from .execution_request import ExecutionRequest
    from .results import InvocationResult


WorkflowFile = str | Path | Mapping[str, Any]

# Accepted for explicit use while its default-route qualification is pending.
_EXPLICIT_ONLY_ENTRIES = frozenset({"hunyuan-foley-local", "audio/hunyuan_foley_v2a"})
# Default entries are data: packs declare them (generation.defaults, entries[].default_for) and the
# user pins them in the generation settings document; see astrid.core.generation.defaults.

def _local_template(entry: Any, mode: str) -> str:
    return getattr(entry.modes[mode].backends.get("local"), "template", "")


class WorkflowInvocationClient(Protocol):
    def invoke_result(self, capability_id: str, *, kind: str, **kwargs: Any) -> InvocationResult: ...


class GenerationInputError(CapabilityValidationError):
    """Keep the SDK validation taxonomy while carrying the shared envelope."""

    def __init__(self, message: str, *, entry=None):
        from astrid.core.generation.entries import public_description
        from astrid.core.generation.preflight import generation_error
        if entry is not None and 'inputs' in entry:
            entry = public_description(entry)
        error = generation_error(ValueError(message), entry=entry, code='input_rejected',
                                 details={'constraint': message})
        self.readiness = error.readiness
        self.code = 'input_rejected'
        super().__init__(message, details={'readiness': self.readiness})

    def to_envelope(self):
        return {**self.readiness, 'task_id': None}


def _declared_output_count_from_document(document: Any) -> int | None:
    """Count the sealed graph's declared final sinks, as VibeComfy's runtime does.

    VibeComfy treats an output as a declared sink when it carries a node_id.
    Returns None when the document declares no outputs list.
    """
    outputs = document.get("outputs") if isinstance(document, Mapping) else None
    if not isinstance(outputs, list):
        return None
    return sum(1 for item in outputs
               if isinstance(item, Mapping) and item.get("node_id") is not None)


def _declared_output_count(companion: Path) -> int | None:
    import json

    try:
        document = json.loads(companion.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _declared_output_count_from_document(document)


def _require_single_declared_output(*, workflow: Any, companion: Any) -> None:
    """Refuse before any staging or admission unless exactly one declared sink exists."""
    from astrid.core.generation.preflight import generation_error

    if companion is not None:
        if isinstance(companion, Mapping):
            declared = _declared_output_count_from_document(companion)
        elif isinstance(companion, (str, Path)):
            declared = _declared_output_count(Path(companion))
        else:
            declared = None
    elif isinstance(workflow, (str, Path)):
        original = Path(workflow).expanduser().resolve()
        if original.is_dir():
            original = original / "workflow.py"
        sealed = original.with_suffix(".vibe.json")
        if not sealed.exists():
            # Companion-less workflows (for example audio or raw-target) are not
            # VibeComfy-declared and pass unchanged; any extra output still fails
            # loudly at host harvest.
            return
        declared = _declared_output_count(sealed)
    else:
        return
    # Refuse only when more than one final sink is declared. Zero or undeclared
    # keeps VibeComfy's legacy full-output map, and extra undeclared outputs still
    # fail loudly at host harvest.
    if declared is not None and declared > 1:
        raise generation_error(
            ValueError(f"this workflow produces {declared} outputs; Phase 1 supports exactly one"),
            code="unsupported_output_cardinality", details={"field": "workflow"},
        )


def _import_generation_member(path: Path, controls: dict[str, Any]) -> dict[str, Any]:
    import hashlib

    from astrid.core.execution.managed_inputs import managed_file_digest
    client = controls.get('client')
    importer = getattr(getattr(client, 'media', None), 'import_file', None)
    if not callable(importer):
        raise CapabilityPreconditionError(
            'generate with placement requires a connected Runtime client with '
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
    """Freeze a canonical bundle, including its declared source evidence."""
    from tempfile import TemporaryDirectory
    original = path.expanduser().resolve()
    if original.is_dir():
        original = original / 'workflow.py'
    with TemporaryDirectory(prefix='astrid-workflow-source-') as scratch:
        snapshot = Path(scratch) / original.name
        try:
            if original.suffix.lower() == '.py':
                from vibecomfy.security.provenance import Provenance
                from vibecomfy.workflow_bundle import _atomic_publish_pair, load_bundle

                bundle = load_bundle(original, trust=Provenance.USER_CONFIRMED)
                bundle.require_canonical_authority('snapshot generation workflow')
                archive = original.with_name('source.json')
                declared = None
                if not archive.is_file():
                    metadata = bundle.workflow.metadata
                    declared = (metadata.get('source_workflow_path') or metadata.get('source_path')
                                or metadata.get('source_workflow'))
                    if declared:
                        archive = Path(declared)
                        if not archive.is_absolute():
                            if archive.parts[0] == 'ready_templates':
                                from vibecomfy.registry.ready import repo_ready_template_root
                                archive = repo_ready_template_root().parent / archive
                            else:
                                archive = original.parent / archive
                # A declared member is required evidence, not an optional
                # adjacent convenience file.
                members = ({snapshot.with_name('source.json'): archive.read_bytes()}
                           if declared or archive.is_file() else {})
                _atomic_publish_pair(snapshot, original.read_bytes(), bundle.ui_sidecar,
                                     expected=bundle, extra_members=members)
            else:
                snapshot.write_bytes(original.read_bytes())
        except (OSError, ValueError) as exc:
            raise GenerationInputError(f'canonical workflow bundle could not be read: {original}: {exc}') from exc
        yield snapshot


def _public_input_values(values):
    from astrid.core.generation.entries import merge_input_values
    values = dict(values)
    supplied = values.pop('inputs', {})
    try:
        return merge_input_values(values, supplied)
    except ValueError as exc:
        raise GenerationInputError(str(exc)) from exc


def _prompt_only_video(*, model, entry, inputs):
    """Identify an unselected video request containing only a prompt."""
    return model is None and entry is None and set(inputs) == {"prompt"}


_ALIGN_MODES = ('align', 'check', 'trust')


def _align_mode(align):
    if align not in _ALIGN_MODES:
        raise GenerationInputError(f"align must be one of {', '.join(_ALIGN_MODES)}")
    return align


_CLAIM_CONTROLS = frozenset({
    'max_usd', 'max_hourly_usd', 'deadline', 'offer_digest',
    'preparation_seconds', 'execution_seconds', 'idle_seconds',
    'capacity_timeout_seconds', 'wait_for_capacity', 'gpu', 'gpu_types', 'datacenter',
})


def _generation_controls(*, engine, consent):
    from astrid.core.generation.targets import canonical_engine
    if engine is not None:
        try:
            selected = canonical_engine(engine)
        except ValueError as exc:
            raise GenerationInputError(str(exc)) from exc
        if selected != 'comfyui':
            raise GenerationInputError('Phase 1 target generation supports only comfyui')
    if not isinstance(consent, (tuple, list)) or any(scope != 'user_env' for scope in consent):
        raise GenerationInputError('consent must contain only user_env; rent with `astrid machines claim`')
    return {name: value for name, value in
            (('engine', engine), ('consent', tuple(consent)))
            if value is not None and value != ()}


def _runpod_preferences(client):
    """The saved RunPod placement policy, read from the caller's Runtime connection when it has one."""
    from astrid.core.generation import settings
    from astrid.packs.runpod.target_selection import read_runpod_preferences

    # Only a connection that serves app-state can hold the settings document.
    if hasattr(settings.transport_of(client), "get_user_app_state"):
        return read_runpod_preferences(client=client).as_mapping()
    return read_runpod_preferences().as_mapping()


def _runpod_job_preferences(preferences, datacenter):
    """Apply a per-job datacenter to a copied preference snapshot."""
    if datacenter is None:
        return preferences
    from astrid.packs.runpod.target_selection import PaidSelectionError, RunPodPreferences

    try:
        candidate = dict(preferences)
        selected = RunPodPreferences.from_mapping(candidate)
        datacenter = datacenter.strip() if isinstance(datacenter, str) else datacenter
        if (selected.volume_id is not None and selected.datacenter_id is not None
                and selected.datacenter_id != datacenter):
            raise GenerationInputError(
                f"--datacenter {datacenter!r} conflicts with the selected network volume's "
                f"datacenter {selected.datacenter_id!r}"
            )
        candidate["datacenter_id"] = datacenter
        return RunPodPreferences.from_mapping(candidate).as_mapping()
    except PaidSelectionError as exc:
        raise GenerationInputError(str(exc)) from exc


_NO_MACHINE = 'no machine: run `astrid machines claim`'


def _require_claimed_machine(kind, machine=None):
    """Read local custody only; the owning host rechecks it before running."""
    import time

    from astrid.core.generation.placement import _runpod_leases

    now = time.time()
    live = [row for row in _runpod_leases(kind)
            if row.get('live') and row.get('state', 'idle') in {'idle', 'active', 'preparing'}
            and (row.get('release_at') is None or row['release_at'] > now)]
    if machine is not None:
        live = [row for row in live if machine in {row.get('machine'), row.get('id')}]
    if not live:
        raise GenerationInputError(_NO_MACHINE)


def _runpod_placement_policy(*, engine, machine=None, consent=(), idle_timeout_s=None):
    """A job may shorten its warm hold; the claim owns its paid bounds."""
    return {'kind': 'runpod', 'engine': engine,
            **({'machine': machine} if machine else {}),
            **({'consent': list(consent)} if consent else {}),
            **({'idle_timeout_s': idle_timeout_s} if idle_timeout_s is not None else {})}


def _require_runpod_claim_host(client):
    """Refuse manual acquisition without a ready Runtime host registration."""
    from astrid.core.generation.settings import transport_of
    from astrid.sdk.capability_selection import select_capability_row
    from astrid.sdk.pagination import paged_rows

    transport = transport_of(client)
    reader = getattr(transport, "list_capabilities", None)
    rows = paged_rows(reader) if callable(reader) else None
    row = select_capability_row(rows or (), "runpod.claim")
    if row is None or row.get("status") != "ready":
        raise GenerationInputError("claim requires a running Astrid host; start Astrid and retry `astrid machines`")


def _runpod_operation(operation, *, client, wait, include_quote=False, preferences=None, machine_id=None,
                      consent=None, until_release=False, requested_idle_seconds=None, timeout_seconds=None, fresh_claim=False):
    """Submit an ordinary task to the retained host's target owner."""

    if operation == "claim" or fresh_claim:
        _require_runpod_claim_host(client)
    policy = {"kind": "runpod", "engine": "comfyui", "preferences": preferences if preferences is not None else _runpod_preferences(client)}
    if fresh_claim:
        if operation != "status":
            raise GenerationInputError("fresh claim disclosure requires status")
        policy["fresh_claim"] = True
    if machine_id is not None:
        if operation not in {"release", "keep"} or not isinstance(machine_id, str) or not machine_id.strip():
            raise GenerationInputError("exact machine operation requires a nonempty machine id")
        policy["machine"] = machine_id
    if operation in {"keep", "claim"}:
        from astrid.packs.runpod.target_selection import PaidRunConsent

        if operation == "keep" and (machine_id is None or type(until_release) is not bool):
            raise GenerationInputError("keep requires an exact machine and a boolean until-release selection")
        if isinstance(consent, PaidRunConsent):
            consent = asdict(consent)
        if not isinstance(consent, Mapping):
            raise GenerationInputError(f"{operation} requires fresh paid consent")
        policy["paid_consent"] = dict(consent)
        if operation == "keep":
            policy["until_release"] = until_release
        else:
            from astrid.packs.runpod.target_selection import _number
            policy["requested_idle_seconds"] = _number(requested_idle_seconds, "claim retention seconds")
    elif consent is not None or until_release:
        raise GenerationInputError("paid keep consent requires the keep operation")
    inputs = {"placement_policy": json.dumps(policy, sort_keys=True, separators=(',', ':'), allow_nan=False)}
    if operation == 'status':
        inputs['include_quote'] = include_quote
    sdk_module = importlib.import_module('astrid.sdk')
    invoke = client.invoke_result if client is not None else sdk_module.invoke_result
    return invoke('runpod.' + operation, kind='action', inputs=inputs, client=client,
                  wait=wait, read_managed_outputs=False, **(
                      {"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}))


def _runpod_status(*, client, include_quote, preferences=None, fresh_claim=False):
    result = _runpod_operation('status', client=client, wait=True, include_quote=include_quote,
                               preferences=preferences, fresh_claim=fresh_claim)
    if not result.ok:
        message = 'target status task failed'
        if fresh_claim:
            error = result.error
            message += ': ' + str(error.get('message', error) if isinstance(error, Mapping) else error)
        raise CapabilityInvocationError(message, details={'error': result.error})
    settled = result.raw_result.get('result', {})
    payload = settled.get('payload', settled) if isinstance(settled, Mapping) else {}
    status = payload.get('target_operation') if isinstance(payload, Mapping) else None
    if not isinstance(status, Mapping):
        raise CapabilityInvocationError('target status task omitted its owner evidence')
    return dict(status)


def _modality_controls(*, engine, consent, align) -> dict[str, Any]:
    """The controls image, video and audio build from their public keywords."""
    controls = _generation_controls(engine=engine, consent=consent)
    if align is not None:
        controls['align'] = _align_mode(align)
    return controls


def _generation_boundary(method):
    """Project legacy SDK failures without changing their exception taxonomy."""
    @wraps(method)
    def call(*args, **kwargs):
        from astrid.core.contracts.errors import AstridError
        from astrid.core.generation.preflight import generation_error
        from astrid.core.pack.discovery import pack_discovery_scope
        try:
            with pack_discovery_scope():
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


def _reject_claim_controls(inputs):
    if not isinstance(inputs, Mapping):
        return
    removed = sorted(_CLAIM_CONTROLS.intersection(inputs))
    if removed:
        raise GenerationInputError(
            f"generate no longer accepts {', '.join(removed)}; use `astrid machines claim`")


def _reject_retired_generation_inputs(inputs: dict[str, Any]) -> None:
    """Reject removed public spellings instead of silently forwarding them."""
    if "target" in inputs:
        raise TypeError("generate.* no longer accepts 'target'; use placement=")
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
    if inputs.get("video_ref"):
        return "v2v"
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
    if "local" in inference_backend_ids:
        return "local"
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
    import sys

    from astrid.core.project.guidance import (
        FIRST_PROJECT_NAME,
        FIRST_PROJECT_SLUG,
        format_first_project_notice,
        format_project_required_guidance,
        selected_project,
    )

    selected, _source = selected_project(project)
    if selected is None:
        from astrid.sdk.invocation import _runtime_selected_project

        selected = _runtime_selected_project(client) if client is not None else _runtime_selected_project()
    if selected is None and client is not None:
        from astrid.sdk.pagination import paged_rows

        projects = getattr(client, "projects", None)
        if all(callable(getattr(projects, name, None)) for name in ("list", "create", "select")):
            rows = paged_rows(projects.list)
            if rows is None:
                raise CapabilityPreconditionError(
                    format_project_required_guidance(operation="generation")
                )
            first = next((
                row for row in rows
                if isinstance(row, dict)
                and (row.get("name") == FIRST_PROJECT_NAME or row.get("slug") == FIRST_PROJECT_SLUG)
            ), None)
            created = first is None
            if first is None:
                made = projects.create(slug=FIRST_PROJECT_SLUG, name=FIRST_PROJECT_NAME)
                if not made.ok:
                    raise CapabilityPreconditionError(made.error.message)
                first = made.data.get("project", made.data) if isinstance(made.data, dict) else {}
            ref = first.get("slug") or first.get("project_id") or first.get("id") or FIRST_PROJECT_SLUG
            chosen = projects.select(ref)
            if not chosen.ok:
                raise CapabilityPreconditionError(chosen.error.message)
            selected = first.get("project_id") or first.get("id") or ref
            print(format_first_project_notice(created=created), file=sys.stderr)
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
                                     defer_target: bool = False, align: str = "check"):
    """Shared installed-catalog resolution and binding, without admission."""
    from astrid.core.generation.preflight import require_generation_target_readiness
    if not defer_target:
        require_generation_target_readiness(candidate, mode, target_client)
    translation = _validate_catalog_target_request(
        registry=registry, candidate=candidate, mode=mode, modality=modality, inputs=inputs)
    workflow, plan, schema = backend.prepare_catalog_workflow(
        candidate, mode, inputs, resolve_target=target_client is not None,
        **({'retain_source': retain_source} if retain_source is not None else {}),
        **({'align': 'trust'} if align == 'trust' else {}),
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

    from vibecomfy.model_assets import _referenced_model_values
    from vibecomfy.workflow_bundle import emit_bundle

    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
    from astrid.core.generation.features import backend_descriptors_from_pack
    from astrid.core.generation.model_root import (
        ModelRootBinding,
        validate_model_root_binding,
    )
    from astrid.core.model_catalog import registry as catalog
    from astrid.core.model_catalog.schema import validate_registry_with_backends
    from astrid.core.model_catalog.taxonomy import GenerationTaxonomyRegistry
    from astrid.core.pack import load_pack_manifest

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
                  if "t2i" in entry.modes and _local_template(entry, "t2i")]
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


def _no_prompt_check(**_: Any) -> None:
    return None


def _video_prompt_check(*, model: Any, entry: Any, inputs: dict[str, Any]) -> None:
    if _prompt_only_video(model=model, entry=entry, inputs=inputs):
        raise GenerationInputError(
            "Prompt-only video requires a reference. "
            "Use --image-ref for a video reference, or see 'generate list --kind video'."
        )


def _image_resolve_mode(sdk_module: Any, *, model: Any, mode: Any, execution: Any, inputs: dict[str, Any],
                        **_: Any) -> tuple[Any, bool]:
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
    return resolved_mode, explicit_openai


def _video_resolve_mode(sdk_module: Any, *, mode: Any, inputs: dict[str, Any], resume: bool,
                        recovery_path: Any, placement: Any, **_: Any) -> tuple[Any, bool]:
    from astrid.core.generation.placement import is_runpod_placement

    if resume or recovery_path is not None and not is_runpod_placement(placement):
        raise CapabilityValidationError("video recovery is supported only for RunPod jobs")
    return sdk_module._infer_video_mode(mode, inputs), False


def _audio_resolve_mode(sdk_module: Any, *, mode: Any, **_: Any) -> tuple[Any, bool]:
    # The single model mode is resolved after the model lookup (see _audio_finish_mode).
    return mode, False


def _image_finish_mode(sdk_module: Any, *, mode: Any, resolved_mode: Any, explicit_openai: bool,
                       **_: Any) -> Any:
    if mode is None and resolved_mode in sdk_module._EXPLICIT_ONLY_IMAGE_MODES and not explicit_openai:
        raise CapabilityValidationError(
            f"Mode {resolved_mode!r} requires an explicit 'mode' argument"
        )
    return resolved_mode


def _audio_finish_mode(sdk_module: Any, *, model: Any, model_entry: Any, mode: Any, **_: Any) -> Any:
    if mode is not None:
        return mode
    # Audio models currently expose one canonical mode (music). Infer it
    # when omitted so the facade remains ergonomic while still validating
    # the resolved model -> mode -> backend cell.
    if len(model_entry.modes) == 1:
        return next(iter(model_entry.modes))
    available_modes = ", ".join(sorted(model_entry.modes))
    raise CapabilityValidationError(
        f"Ambiguous audio mode for model {model!r}. "
        f"Available modes: {available_modes}. Please specify mode."
    )


def _no_target_mode_check(**_: Any) -> None:
    return None


def _audio_target_mode_check(*, mode: Any, **_: Any) -> None:
    if mode is None:
        raise CapabilityValidationError('mode is required for audio generation with placement')


def _no_explicit_target_check(**_: Any) -> None:
    return None


def _audio_explicit_target_check(*, recovery_path: Any, resume: bool, wait: bool, **_: Any) -> None:
    if recovery_path is not None or resume or not wait:
        raise CapabilityValidationError('audio recovery and no-wait require an explicit placement')


def _no_recipe_check(**_: Any) -> None:
    return None


def _image_recipe_check(*, inputs: dict[str, Any], model: Any, resolved_mode: Any, resolved_execution: Any,
                        **_: Any) -> None:
    recipe = inputs.get("shot_generation_recipe")
    if recipe is None:
        return
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


def _image_required_features(*, resolved_mode: Any, inputs: dict[str, Any]) -> tuple[str, ...]:
    # Image keeps its historic deferred prompt validation (executor-owned prompt
    # semantics, including prompt-less upscale), so it requires no features.
    return ()


def _video_required_features(*, resolved_mode: Any, inputs: dict[str, Any]) -> tuple[str, ...]:
    # FLF's defining frame pair is rejected before dry-run/live invocation.
    if resolved_mode == "flf" and (inputs.get("image_ref") is not None or inputs.get("image_end_ref") is not None):
        return ("image_ref", "image_end_ref")
    return ()


def _audio_required_features(*, resolved_mode: Any, inputs: dict[str, Any]) -> None:
    # None lets validation use the mode's own declared requirements.
    return None


def _speech_invoke(facade: Any, *, inputs: dict[str, Any], client: Any, out: Any, project: Any,
                   project_root: Any, extra_pack_roots: Any, banodoco_config: Any, active_theme: Any,
                   include_missing_roots: bool, brief: Any, dry_run: bool, check_binaries: bool,
                   python_exec: Any, verbose: bool, argv: Any) -> Any:
    speech_inputs = _speech_inputs_from_audio_request(inputs)
    invoke_out, invoke_project = _resolve_invoke_destination(
        client=client,
        out=out,
        project=project,
        project_root=project_root,
    )
    result = facade._invoke(
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


@dataclass(frozen=True)
class _MediaKind:
    """What differs between image, video and audio. The one media body only reads these rules."""

    capability: str
    retire_inputs_first: bool
    check_prompt: Any
    resolve_mode: Any
    finish_mode: Any
    check_target_mode: Any
    check_explicit_target: Any
    speech_mode: str | None
    check_recipe: Any
    required_features: Any


_MEDIA_KINDS: dict[str, _MediaKind] = {
    "image": _MediaKind(
        capability="generation.generate_image", retire_inputs_first=False,
        check_prompt=_no_prompt_check, resolve_mode=_image_resolve_mode, finish_mode=_image_finish_mode,
        check_target_mode=_no_target_mode_check, check_explicit_target=_no_explicit_target_check,
        speech_mode=None, check_recipe=_image_recipe_check, required_features=_image_required_features,
    ),
    "video": _MediaKind(
        capability="generation.generate_video", retire_inputs_first=False,
        check_prompt=_video_prompt_check, resolve_mode=_video_resolve_mode,
        finish_mode=lambda sdk_module, *, resolved_mode, **_: resolved_mode,
        check_target_mode=_no_target_mode_check, check_explicit_target=_no_explicit_target_check,
        speech_mode=None, check_recipe=_no_recipe_check, required_features=_video_required_features,
    ),
    "audio": _MediaKind(
        capability="generation.generate_audio", retire_inputs_first=True,
        check_prompt=_no_prompt_check, resolve_mode=_audio_resolve_mode, finish_mode=_audio_finish_mode,
        check_target_mode=_audio_target_mode_check, check_explicit_target=_audio_explicit_target_check,
        speech_mode="tts", check_recipe=_no_recipe_check, required_features=_audio_required_features,
    ),
}


@dataclass(frozen=True)
class GenerationFacade:
    """Public typed entrypoint for generation executors."""

    sdk_module_name: str = "astrid.sdk"

    def list(self, *, kind=None):
        from astrid.core.generation.listing import generation_rows
        return generation_rows(kind)

    @_generation_boundary
    def launch(self, *, placement='auto', engine='comfyui', consent=(), timeout=10.0,
               project_root=None):
        """Launch/re-probe one selected human ComfyUI with fresh user_env consent.

        Return the ordinary readiness envelope, including manual-start guidance
        on failure. Never save consent or health, discover a fallback, or admit
        a generation task. URL/env overrides resolve without prior launch facts.
        """
        from astrid.core.generation import targets
        from astrid.core.generation.target_launch import launch_target

        _generation_controls(engine=engine, consent=consent)
        from astrid.core.generation.placement import is_runpod_placement
        if is_runpod_placement(placement):
            raise GenerationInputError('launch requires a selected local human ComfyUI')
        try:
            selection = targets.resolve_target(placement, repo=project_root)
        except (ValueError, OSError) as exc:
            raise GenerationInputError(str(exc)) from exc
        return launch_target(selection, consent=consent, timeout=timeout).readiness

    @_generation_boundary
    def targets(self, *, placement='auto', engine='comfyui', save=False, inputs=None,
                project_root=None, client=None):
        """Inspect selection without probing; persist only an explicit save."""
        from astrid.core.generation import targets
        from astrid.core.generation.entries import merge_input_values
        from astrid.core.generation.preflight import readiness_envelope
        try:
            if targets.canonical_engine(engine) != 'comfyui':
                raise ValueError('Phase 1 targets support only comfyui')
            updates = merge_input_values(inputs if inputs is not None else {})
            from astrid.core.generation.placement import is_runpod_placement
            if is_runpod_placement(placement):
                if save:
                    allowed = {'gpu_types', 'datacenter_id', 'volume', 'image', 'idle_timeout_seconds',
                               'provider_account_ref', 'credential_ref'}
                    if updates.keys() - allowed:
                        raise ValueError('unsupported RunPod preference: ' + ', '.join(sorted(updates.keys() - allowed)))
                    from astrid.core.generation import settings as generation_settings

                    def write(document):
                        preferences = document['machines'].setdefault('runpod', {})
                        for name, value in updates.items():
                            if value is None:
                                preferences.pop(name, None)
                            else:
                                preferences[name] = value
                    generation_settings.update(write, client)
                elif updates:
                    raise ValueError('provider inputs require --save')
                payload = readiness_envelope()
                from astrid.core.generation import settings as generation_settings
                payload['preferences'] = generation_settings.machine_policy('runpod', client) or {}
                if not save and client is not None:
                    payload['target'] = _runpod_status(client=client, include_quote=False)
                return payload
            if updates:
                raise ValueError('local target input updates are not supported; select an explicit URL')
            selection = targets.resolve_target(placement, repo=project_root)
            record = selection.record
            if record is None and placement == 'managed':
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
    def defaults(self, *, client=None):
        """Every ``(kind, mode)`` slot with its resolved entry, its source and the other candidates."""
        from astrid.core.generation import defaults as generation_defaults
        return generation_defaults.listing(client)

    @_generation_boundary
    def set_default(self, kind, mode, entry, *, pack=None, client=None):
        """Pin ``entry`` as the default for ``kind``/``mode``; refuses an entry that cannot fill the slot."""
        from astrid.core.generation import defaults as generation_defaults
        generation_defaults.set_default(kind, mode, entry, pack=pack, client=client)
        return generation_defaults.listing(client)

    @_generation_boundary
    def unset_default(self, kind, mode, *, client=None):
        """Remove the user's pin for ``kind``/``mode``; the highest-priority pack default applies again."""
        from astrid.core.generation import defaults as generation_defaults
        generation_defaults.unset_default(kind, mode, client=client)
        return generation_defaults.listing(client)

    @_generation_boundary
    def describe(self, entry: str | None = None, *, workflow=None, mode=None,
                 placement=None, engine='comfyui', client=None, max_hourly_usd=None,
                 datacenter=None, fresh_claim=False):
        from astrid.core.generation.entries import describe_entry, describe_workflow
        if placement is not None:
            _generation_controls(engine=engine, consent=())
            from astrid.core.generation import placement as placements
            resolved = placements.validate_placement(placements.resolve_placement(
                placements.request_from_selector(placement), client=client))
            from astrid.core.generation.placement import is_runpod_placement
            if not is_runpod_placement(resolved) or entry is not None or workflow is not None:
                raise GenerationInputError('target describe requires only placement=runpod')
            preferences = _runpod_job_preferences(_runpod_preferences(client), datacenter)
            if resolved.policy.gpu_types:
                preferences['gpu_types'] = list(resolved.policy.gpu_types)
            from astrid.packs.runpod.target_selection import (
                RunPodPreferences,
                _number,
                policy_consent_digest,
            )

            if max_hourly_usd is not None:
                max_hourly_usd = _number(max_hourly_usd, "hourly USD ceiling")
            description = _runpod_status(client=client, include_quote=True, preferences=preferences, fresh_claim=fresh_claim)
            description["waiting_policy"] = {
                "preferences": preferences, "max_hourly_usd": max_hourly_usd,
                "policy_digest": (policy_consent_digest(RunPodPreferences.from_mapping(preferences), max_hourly_usd)
                                  if max_hourly_usd is not None else None),
                "claim_command": "astrid machines claim",
            }
            return description
        if bool(entry) == bool(workflow):
            raise GenerationInputError('describe requires an entry or a workflow')
        return describe_entry(entry, mode=mode) if entry else describe_workflow(Path(workflow), mode=mode)

    @_generation_boundary
    def claim(self, *, consent, requested_idle_seconds, placement='runpod', datacenter=None, client=None, wait=True):
        """Claim one machine through the running host, using saved storage."""
        from astrid.core.generation import placement as placements
        resolved = placements.validate_placement(placements.resolve_placement(
            placements.request_from_selector(placement), client=client))
        if resolved.kind != 'runpod':
            raise GenerationInputError('claim requires RunPod placement')
        preferences = _runpod_job_preferences(_runpod_preferences(client), datacenter)
        if resolved.policy.gpu_types:
            preferences['gpu_types'] = list(resolved.policy.gpu_types)
        return _runpod_operation('claim', client=client, wait=wait, preferences=preferences,
                                 consent=consent, requested_idle_seconds=requested_idle_seconds)

    @_generation_boundary
    def keep(self, *, machine_id, consent, until_release=False, client=None, wait=False,
             timeout_seconds=300.0):
        """Keep the exact rented RunPod machine with fresh paid consent."""
        return _runpod_operation('keep', client=client, wait=wait, machine_id=machine_id,
                                 consent=consent, until_release=until_release,
                                 timeout_seconds=timeout_seconds)

    @_generation_boundary
    def release(self, *, engine: str, placement: str, machine_id=None, client=None, wait=False):
        """Release a managed engine through its verified owner; never HTTP free an unowned target.

        Attached or unmanaged targets are refused and never receive an HTTP free.
        The owned managed engine is verified as the registered loopback owner, then
        released through the existing native unload path (CheckoutServerAdapter.release).
        """
        from astrid.core.generation.preflight import generation_error
        from astrid.core.generation.targets import canonical_engine
        if canonical_engine(engine) != 'comfyui':
            raise GenerationInputError('Phase 1 release supports only comfyui')
        from astrid.core.generation.placement import is_runpod_placement
        if is_runpod_placement(placement):
            return _runpod_operation('release', client=client, wait=wait, machine_id=machine_id)
        if machine_id is not None:
            raise GenerationInputError('machine id release requires runpod placement')
        bridge = getattr(client, 'release_generation', None)
        if not callable(bridge):
            raise generation_error(ValueError('The bound host release owner is unavailable'),
                code='missing_dependency', details={'field': 'target'},
                fixes=[{'argv': ['astrid', 'generate', 'targets', '--placement', placement],
                        'scope': 'read_only', 'details': {}}])
        result = bridge(engine='comfyui', target=placement, wait=wait)
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
        placement: str | None = None,
        inputs: Mapping[str, Any] | None = None,
        mode: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
        placement_policy: Mapping[str, Any] | None = None,
        python: WorkflowFile | None = None,
        companion: WorkflowFile | None = None,
        source: WorkflowFile | None = None,
        source_video: WorkflowFile | None = None,
        source_video_node: str | None = None,
        source_video_widget: str | None = None,
        managed_assets: WorkflowFile | None = None,
        entry_inputs: WorkflowFile | None = None,
        workflow_inputs: str | None = None,
        after_outputs_job: str | None = None,
        comfy_url: str | None = None,
        comfy_output_dir: str | None = None,
        generation_intent: Mapping[str, Any] | None = None,
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
        align: str | None = None,
    ) -> InvocationResult:
        """Invoke the existing workflow action through the supplied client.

        Omitted domain values stay omitted so the action owns its defaults.
        """
        controls = _generation_controls(engine=engine, consent=consent)
        _reject_claim_controls(inputs or {})
        _require_single_declared_output(workflow=workflow, companion=companion)
        if placement is not None:
            from astrid.core.generation.entries import resolve_workflow_inputs
            if align is not None:
                controls['align'] = _align_mode(align)
            if not isinstance(workflow, (str, Path)) or python is not None or companion is not None:
                raise GenerationInputError('target workflow requires one local workflow path')
            if inputs is not None and not isinstance(inputs, Mapping):
                raise GenerationInputError('workflow inputs must be an object')
            if any(value is not None for value in (
                    execution_request, source, source_video, source_video_node,
                    source_video_widget, managed_assets, entry_inputs, workflow_inputs, comfy_url,
                    comfy_output_dir, generation_intent, placement_policy)):
                raise GenerationInputError('a target workflow cannot replace explicit prepared binding or managed source controls')
            source_controls: dict[str, Any] = {'client': client, 'project': project, 'out': out,
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
                    mode=description['mode'], modality=description['kind'], placement=placement,
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
                "managed_assets": managed_assets, "entry_inputs": entry_inputs, "workflow_inputs": workflow_inputs,
                "after_outputs_job": after_outputs_job,
                "comfy_url": comfy_url, "comfy_output_dir": comfy_output_dir,
                "placement_policy": (json.dumps(dict(placement_policy), sort_keys=True, separators=(',', ':'), allow_nan=False)
                                  if placement_policy is not None else None),
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

    def _generate_on_target(self, **kwargs: Any) -> Any:
        """Run ``_generate_on_target_impl``, then remove any hook job directories it made."""
        import shutil
        job_dirs: list[Path] = []
        try:
            return self._generate_on_target_impl(_job_dirs=job_dirs, **kwargs)
        finally:
            for directory in job_dirs:
                shutil.rmtree(directory, ignore_errors=True)

    def _generate_on_target_impl(
        self,
        *,
        _job_dirs: builtins.list[Path],
        registry: Any,
        model: str | None,
        mode: str,
        modality: str,
        placement: str | Mapping[str, Any] | Placement | None,
        execution: str | None,
        inputs: dict[str, Any],
        dry_run: bool,
        workflow_entry: dict[str, Any] | None = None,
        retained_source: dict[str, Any] | None = None,
        **controls: Any,
    ) -> Any:
        """Bind a catalog template and admit it through the workflow facade.

        Qualify candidates before placement discovery. An attached server can
        be used without installing or owning a managed ComfyUI.
        """
        import json
        from tempfile import TemporaryDirectory

        from astrid.core.contracts.errors import AstridError
        from astrid.core.generation import placement as placements
        from astrid.core.generation.backends.vibecomfy import VibeComfyBackend
        from astrid.core.generation.preflight import (
            compare_versions,
            generation_error,
            readiness_envelope,
            require_generation_target_readiness,
            select_generation_target,
        )

        requested_align = controls.pop('align', None)  # None: not asked for; RunPod then aligns, local checks
        align = _align_mode(requested_align or 'check')

        if execution not in {None, "local"}:
            raise CapabilityValidationError(
                "placement selects a local ComfyUI workflow; use execution='local'"
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
        # Catalog priority is explicit, stable and per modality/mode. Unknown
        # catalog entries and user entries remain eligible in lexical order.
        from astrid.core.generation import defaults as generation_defaults
        from astrid.core.generation import entries as workflow_entries
        from astrid.core.generation import settings as generation_settings
        from astrid.packs.vibecomfy.production_engine import ProductionEngineError
        settings_snapshot = generation_settings.read(controls.get('client'))
        pinned = (generation_defaults.pinned_entry(modality, mode, settings_snapshot)
                  if model is None and workflow_entry is None else None)
        preferred = generation_defaults.ordered_ids(modality, mode, settings_snapshot)
        def rank(candidate: Any) -> tuple[int, str]:
            identifier = candidate[1].id if candidate[0] == "catalog" else candidate[1]["id"]
            return (preferred.index(identifier) if identifier in preferred else len(preferred), identifier)

        candidates: builtins.list[tuple[str, Any]]
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
                          if (entry.id not in _EXPLICIT_ONLY_ENTRIES or entry.id in preferred)
                          and mode in entry.modes and _local_template(entry, mode)]
            candidates += [("workflow", entry) for entry in
                           [*({**entry, 'source': 'user-entry'} for entry in workflow_entries.user_entries()),
                            *({**entry, 'source': 'ready-template'} for entry in workflow_entries.ready_entries())]
                           if entry["kind"] == modality and mode in entry["modes"]
                           and (entry["id"] not in _EXPLICIT_ONLY_ENTRIES or entry["id"] in preferred)
                           and (not entry.get("registry_id") or entry["id"] in preferred)]
            if pinned is not None and not any(
                    (c[1].id if c[0] == "catalog" else c[1]["id"]) == pinned for c in candidates):
                found = workflow_entries.find_entry(pinned)
                if found is not None:
                    candidates.append(("workflow", found))
            candidates.sort(key=rank)
        from astrid.core.generation.availability import (
            require_entry_available,
            require_model_available,
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
        default_id = (candidates[0][1].id if candidates[0][0] == 'catalog'
                      else candidates[0][1]['id']) if candidates else None
        # Qualify every candidate before the placement resolver can probe discovery.
        placement = placements.validate_placement(placements.resolve_placement(
            placements.request_from_selector(placement), client=controls.get('client'),
            repo=controls.get('project_root')))
        is_managed_target = placement.kind == 'local'
        is_remote_target = placement.kind not in placements.BUILTIN_KINDS
        if is_remote_target and not dry_run:
            try:
                _require_claimed_machine(placement.kind, placement.machine)
            except GenerationInputError:
                # Keep local entry admission ahead of the missing-machine
                # refusal, just as the owning host does before attaching a job.
                entry_id = workflow_entry.get('id') if workflow_entry is not None else model
                if entry_id is not None:
                    from astrid.core.execution.machine_run import resolve_machine

                    machine = resolve_machine(placement.kind)
                    entry_refusal = getattr(machine, 'entry_refusal', None)
                    reason = entry_refusal(entry_id) if callable(entry_refusal) else None
                    if reason is not None:
                        raise GenerationInputError(reason) from None
                raise
        placement_policy = (_runpod_placement_policy(engine=None, machine=placement.machine,
                            consent=controls.get('consent', ()),
                            idle_timeout_s=placement.policy.requested_idle_timeout_s)
                            if is_remote_target and not dry_run else
                            {"kind": "local", "device": placement.device} if is_managed_target else None)
        runpod_engine = pending_target = None
        for name in ('engine', 'consent'):
            controls.pop(name, None)
        # Managed and remote workflows are frozen from declarations. The host
        # validates that exact selection after alignment and engine readiness.
        target_client = (None if is_remote_target else select_generation_target(
            placements.comfy_selector(placement), repo=controls.get("project_root"),
            entry_id=model or (workflow_entry or {}).get('registry_id') or (workflow_entry or {}).get('id') or default_id,
            mode=mode, defer_managed=is_managed_target))
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
            if automatic and identifier != pinned and source == "workflow" and candidate.get('hooks_declared'):
                # A contributed default may require a different public input shape. Only
                # skip it for missing required inputs; a chosen candidate's build errors
                # and explicitly pinned entries remain fail-closed.
                description = workflow_entries.describe_entry(identifier, mode=mode, _native=True)
                names = {name for port in description['inputs']
                         for name in (port['name'], *port.get('aliases', []))}
                try:
                    workflow_entries.validate_inputs(description, {k: v for k, v in inputs.items() if k in names})
                except workflow_entries.MissingRequiredEntryInputsError as exc:
                    failures.append(f"{identifier}: {exc}")
                    continue
            try:
                assets = {}
                if is_remote_target:
                    template = (None if source == "workflow" else _local_template(candidate, mode))
                    if source == "workflow" and candidate.get('hooks_declared'):
                        import tempfile
                        _job_dirs.append(Path(tempfile.mkdtemp(prefix='astrid-job-')))
                        candidate = workflow_entries.attach_hooks(candidate, inputs, _job_dirs[-1])
                    runpod_engine = workflow_entries.runpod_engine(candidate if source == "workflow" else
                        workflow_entries.find_entry(template) if template else None)
                    pending_target = {'engine': runpod_engine, 'placement': 'runpod', 'state': 'pending_host_readiness'}
                if source == "catalog":
                    if is_managed_target and not dry_run:
                        require_generation_target_readiness(candidate, mode, target_client)
                    workflow, plan, schema, assets = _prepare_catalog_target_workflow(
                        registry=registry, candidate=candidate, mode=mode, modality=modality,
                        inputs=inputs, target_client=target_client, backend=backend,
                        retain_source=None if dry_run else retain_source,
                        defer_target=is_remote_target or (is_managed_target and not dry_run), align=align)
                else:
                    from types import SimpleNamespace
                    prerequisite = SimpleNamespace(id=identifier, modes={mode: SimpleNamespace(
                        backends={"local": SimpleNamespace(template=str(candidate["bundle"]))})})
                    if not is_remote_target:
                        require_generation_target_readiness(prerequisite, mode, target_client)
                    if candidate["kind"] != modality or mode not in candidate["modes"]:
                        raise ValueError(f"entry {identifier!r} does not support {modality}/{mode}")
                    # The selected workflow owns preparation. Its catalog association
                    # supplies discovery/qualification metadata, not another selector.
                    if candidate.get('hooks_declared') and 'build' in workflow_entries.entry_hooks(candidate):
                        # A contribution with a build hook owns its graph: build (CPU, deterministic) picks it
                        # from the request; after_outputs shares this job directory. Plan 32 T6b-3.
                        if controls.get('wait') is False and not dry_run and not is_remote_target:
                            # after_outputs reads the build state in this call's job directory, which is
                            # removed when the call returns; a queued task would find it gone.
                            raise CapabilityValidationError(
                                f"entry {identifier!r} runs a post-generation step on this machine; "
                                "it cannot run with --no-wait (the build state lives only for this call)")
                        import tempfile
                        job_dir = candidate.get('job_dir') or Path(tempfile.mkdtemp(prefix='astrid-job-'))
                        if not candidate.get('job_dir'):
                            _job_dirs.append(Path(job_dir))
                        # Discovery checks only required ports. The selected hook must
                        # honor the same declared-controls contract as an explicit entry.
                        build_inputs = dict(inputs)
                        if build_inputs.get('count') == 1 and not any(
                                'count' in (port['name'], *port.get('aliases', []))
                                for port in candidate.get('inputs', [])):
                            build_inputs.pop('count')  # single-output admission was checked above
                        build_inputs = workflow_entries.validate_inputs(candidate, build_inputs)
                        built = workflow_entries.build_entry(
                            candidate, build_inputs, job_dir, check_runtime=not dry_run and not is_remote_target)
                        workflow, assets = built['workflow'], dict(built.get('assets') or {})
                        plan = {'kind': candidate['kind'], 'unbound': [], 'bindings': dict(built['bindings']),
                                'build_plan': built.get('plan'), 'job_dir': str(job_dir)}
                        if not dry_run and source_ref is None:
                            retained = Path(built['bundle']) / 'source.json'
                            retain_source(retained.name, retained.read_bytes())
                        schema = workflow_entries.target_schema(candidate, workflow, target_client, engine=runpod_engine,
                                                                pending_target=pending_target, align=align)
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
                        schema = workflow_entries.target_schema(candidate, workflow, target_client, engine=runpod_engine,
                                                                pending_target=pending_target, align=align)
                from vibecomfy.workflow_bundle import load_bundle
                if source == "workflow":
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
                if (model is not None or workflow_entry is not None or identifier in preferred
                        or getattr(exc, "code", None) in {"COMFY_TARGET_NOT_READY", "engine_not_found"}):
                    # A preferred default owns its failure. Never silently
                    # replace it with another model after preparation fails.
                    if isinstance(exc, AstridError):
                        raise generation_error(exc) from exc
                    if isinstance(exc, CapabilityValidationError):
                        raise
                    if align == 'check' and 'not present locally' in str(exc):
                        if target_client is not None and target_client.target.kind == 'attached':
                            remedy = 'put the missing model files in their folders on your ComfyUI, then retry'
                        else:
                            remedy = (f"run: astrid generate align --apply --consent user_env"
                                      f" --entry {identifier}")
                        exc = ValueError(
                            f"{exc}. Fix: pass --align trust to accept the workflow's declared models as present "
                            f"on the target (unverified), or {remedy}")
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
        if is_remote_target and placement_policy is not None:
            placement_policy["engine"] = runpod_engine
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
        if source == 'workflow' and candidate.get('license_notice'):
            description['license_notice'] = candidate['license_notice']
        readiness = readiness_envelope(entry=description, target=(
            pending_target if is_remote_target else target_record),
                                      issues=compare_versions(recorded, observed))
        if dry_run:
            preview = dict(plan or {"model": entry_id, "mode": mode})
            preview.update(model=entry_id, mode=mode, reason=("explicitly selected workflow entry" if model is not None or workflow_entry is not None else "highest-ranked ready local entry for the requested kind and mode on the selected target"))
            # Name the VibeComfy workflow the selected entry resolved to.
            ready_template = (getattr(workflow, 'metadata', None) or {}).get('ready_template')
            if isinstance(ready_template, str) and ready_template:
                preview.setdefault('workflow', ready_template)
            if approval is not None:
                preview["approval"] = {"status": "approved", "api_digest": approval.api_digest}
            # Only managed placements run declaration preparation on the execution host.
            if is_managed_target or is_remote_target:
                from vibecomfy.workflow_bundle import emit_bundle

                from astrid.core.generation.managed_preparation import judge_declarations
                from astrid.packs.vibecomfy.production_engine import (
                    _canonical_bundle_value,
                    load_workflow_path,
                )
                with TemporaryDirectory(prefix="astrid-dry-declarations-") as scratch:
                    root = Path(scratch)
                    capsule = root / "workflow.py"
                    emit_bundle(workflow, capsule, {"operation": "authored"})
                    loaded = load_workflow_path(capsule, root / "load")
                    canonical = _canonical_bundle_value(loaded.resolved).workflow
                    manifest, _ = judge_declarations(canonical, root / "engine",
                                                     workflow_entries.engine_table()['comfyui'])
                if placement.kind == 'local':
                    from astrid.core.generation.managed_preparation import configured_models_root
                    manifest = {**manifest, 'models_root': configured_models_root()}
                readiness['entry']['requirements'] = manifest
            readiness['entry'].update(preview)
            if align == 'trust':
                readiness['alignment'] = {'mode': 'trust', 'verified': False}
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
            if is_remote_target and entry_id != 'workflow':
                from astrid.core.generation.job_inputs import write_archive
                # A requested filename_prefix is bound to the workflow's declared output
                # (workflow_inputs). The shipped archive carries it only when the entry
                # declares it as an input; an entry without that port must not refuse it.
                archive_inputs = {name: value for name, value in inputs.items() if name != 'filename_prefix'}
                public, public_values = workflow_entries.resolve_entry_inputs(entry_id, archive_inputs, mode=mode)
                if 'filename_prefix' in inputs and any(
                        'filename_prefix' in {port['name'], *port.get('aliases', [])} for port in public['inputs']):
                    public, public_values = workflow_entries.resolve_entry_inputs(entry_id, inputs, mode=mode)
                if 'seed' in {port['name'] for port in public['inputs']} and 'seed' in (plan or {}).get('bindings', {}):
                    public_values['seed'] = plan['bindings']['seed']
                archive = write_archive(root / 'entry-inputs.zip', public_values, public['inputs'])
                asset_arguments['entry_inputs'] = import_member(archive)
            hook_job = None
            if source == 'workflow' and not is_remote_target and plan and plan.get('job_dir') and 'after_outputs' in (candidate.get('hooks') or workflow_entries.entry_hooks(candidate)):
                # The hook runs where the job runs, after the outputs are written and before they are sealed.
                # On a rented machine the pod-side `astrid generate` rebuilds and runs it from the shipped inputs;
                # this controller's job directory is not shipped.
                job = workflow_entries.entry_job(candidate, inputs, plan['job_dir'], hooks={})
                hook_job = json.dumps({'entry': candidate['id'], 'job_dir': plan['job_dir'],
                                       'inputs': job['inputs'], 'files': job['files']}, sort_keys=True)
            result = self.workflow(
                **({'after_outputs_job': hook_job} if hook_job else {}),
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
                # The managed client is for preparation and inventory. Passing its
                # loopback URL as an attached endpoint bypasses managed alignment
                # and lifecycle ownership in the action.
                comfy_url=(target_client.target.url if placement.kind == 'attached' and target_client is not None else None),
                placement_policy=placement_policy,
                generation_intent={
                    "version": 1, "modality": modality, "partial_success_policy": "reject",
                    "groups": [{"group_key": "main", "selectors": [{
                        "selector": "main-0", "ordinal": 0,
                        "variant_key": "original", "required": True,
                    }]}],
                    "metadata": {"model": entry_id, "mode": mode, **({"align": requested_align} if requested_align is not None else {})},
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
        from astrid.core.generation.entries import describe_entry, resolve_entry_inputs

        if model is not None and model != entry:
            raise CapabilityValidationError("model and entry select different generation entries")
        try:
            description = describe_entry(entry, mode=mode, registry=registry, _native=True)
            description, values = resolve_entry_inputs(
                entry, dict(inputs), mode=mode, kind=modality, registry=registry,
            )
        except (ValueError, KeyError) as exc:
            raise GenerationInputError(str(exc), entry=locals().get('description')) from exc
        from astrid.core.generation.availability import require_entry_available
        require_entry_available(description, description['mode'])
        if description.get("action"):
            raise CapabilityPreconditionError(
                "action-backed generation requires a Runtime-admitted parent; "
                "the tracked parent interface is not yet available"
            )
        return description, values

    def generate(self, entry: str | None = None, inputs: Mapping[str, Any] | None = None,
                 placement: str | None = None, **options: Any) -> Any:
        """The one generation front door: ``generate(entry, inputs, placement)``.

        The kind (image, video or audio) is read from the entry's own declaration, so this is the
        one place that refuses an unknown entry or malformed inputs. ``image``, ``video`` and
        ``audio`` remain as the typed aliases that pin a kind.
        """
        if entry is None:
            raise CapabilityValidationError("generate requires an entry")
        if inputs is not None and not isinstance(inputs, Mapping):
            raise CapabilityValidationError("generate inputs must be an object")
        from astrid.core.generation.entries import describe_entry

        sdk_module = importlib.import_module(self.sdk_module_name)
        try:
            kind = describe_entry(
                entry, mode=options.get("mode"),
                registry=sdk_module._load_model_registry(
                    project_root=options.get("project_root"),
                    extra_pack_roots=options.get("extra_pack_roots", ()),
                ),
                _native=True,
            )["kind"]
        except (KeyError, ValueError) as exc:
            raise CapabilityValidationError(str(exc)) from exc
        return getattr(self, kind)(entry=entry, placement=placement, **dict(inputs or {}), **options)

    @_generation_boundary
    def image(
        self,
        *,
        model: str | None = None,
        entry: str | None = None,
        placement: str | None = None,
        mode: str | None = None,
        execution: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
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
        align: str | None = None,
        **inputs: Any,
    ) -> Any:
        arguments = {key: value for key, value in locals().items() if key not in {"self", "inputs"}}
        return self._media_request("image", **arguments, **inputs)

    def _media_request(
        self,
        modality: str,
        *,
        model: str | None = None,
        request: str | Mapping[str, Any] | None = None,
        asset_map: str | Mapping[str, Any] | None = None,
        resume: bool = False,
        entry: str | None = None,
        placement: str | None = None,
        mode: str | None = None,
        execution: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
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
        align: str | None = None,
        **inputs: Any,
    ) -> Any:
        kind = _MEDIA_KINDS[modality]
        if kind.retire_inputs_first:
            _reject_retired_generation_inputs(inputs)
        _reject_claim_controls(inputs)
        inputs = _public_input_values(inputs)
        _reject_claim_controls(inputs)
        controls = _modality_controls(engine=engine, consent=consent, align=align)
        if not kind.retire_inputs_first:
            _reject_retired_generation_inputs(inputs)
        inputs.update({name: value for name, value in
                       (("request", request), ("asset_map", asset_map)) if value is not None})
        kind.check_prompt(model=model, entry=entry, inputs=inputs)
        sdk_module = importlib.import_module(self.sdk_module_name)
        registry = None
        entry_source = None
        if entry is None and model is not None:
            from astrid.core.generation.entries import find_entry

            # Preserve the workflow lookup's user precedence for either selector.
            manifest = find_entry(model)
            if manifest is not None and manifest.get('source') == 'user-entry':
                entry_source = 'workflow'
        if entry is not None:
            registry = sdk_module._load_model_registry(
                project_root=project_root, extra_pack_roots=extra_pack_roots,
            )
            description, inputs = self._resolve_public_entry(
                entry=entry, model=model, mode=mode, modality=modality,
                inputs=inputs, registry=registry,
            )
            entry_source = description["source"]
            model, mode = entry, description["mode"]
        if entry_source == "workflow" and placement is None:
            placement = "auto"
        if registry is None:
            registry = sdk_module._load_model_registry(
                project_root=project_root,
                extra_pack_roots=extra_pack_roots,
            )
        # Engine selects the ComfyUI target resolver; establish that selector
        # before resolving a catalog execution so provider entries can consume
        # ``auto`` without it being reintroduced as a target afterward.
        if engine is not None and placement is None:
            placement = "auto"
        declared_entry = None
        if model is not None and entry_source != "workflow":
            try:
                declared_entry = registry.get(model)
            except KeyError:
                pass
        resolved_mode, explicit_openai = kind.resolve_mode(
            sdk_module, model=model, mode=mode, execution=execution, inputs=inputs,
            resume=resume, recovery_path=recovery_path, placement=placement,
        )
        # Catalog entries declare their execution in the selected mode. Resolve
        # that declaration before interpreting placement so provider entries
        # keep their existing SDK route when placement is omitted or ``auto``.
        # A concrete placement only applies to the local ComfyUI route.
        resolved_execution = None
        if declared_entry is not None:
            resolved_mode = kind.finish_mode(
                sdk_module, model=model, model_entry=declared_entry, mode=mode,
                resolved_mode=resolved_mode, explicit_openai=explicit_openai,
            )
            resolved_execution = sdk_module._resolve_execution(
                declared_entry, resolved_mode, execution, model=model,
            )
            if resolved_execution != "local":
                if placement not in (None, "auto"):
                    raise CapabilityValidationError(
                        f"entry {model!r} declares execution {resolved_execution!r}; "
                        "valid placements: omitted or 'auto'",
                        details={"entry": model, "execution": resolved_execution,
                                 "valid_placements": [None, "auto"]},
                    )
                execution = resolved_execution
                placement = None
            elif placement is None:
                # Omitted placement and ``auto`` enter the same resolver below.
                placement = "auto"
        if placement is not None:
            kind.check_target_mode(mode=resolved_mode)
            return self._generate_on_target(
                registry=registry, model=model, mode=resolved_mode, modality=modality,
                placement=placement, execution=execution, inputs=inputs, dry_run=dry_run,
                client=client, out=out, project=project, project_root=project_root,
                extra_pack_roots=extra_pack_roots, banodoco_config=banodoco_config,
                active_theme=active_theme, include_missing_roots=include_missing_roots,
                brief=brief, check_binaries=check_binaries, python_exec=python_exec,
                verbose=verbose, argv=argv,
                **controls, **({'wait': False} if not wait else {}),
                **({'recovery_path': recovery_path} if recovery_path is not None else {}),
                **({'resume': True} if resume else {}),
            )
        kind.check_explicit_target(recovery_path=recovery_path, resume=resume, wait=wait)
        if kind.speech_mode is not None and resolved_mode == kind.speech_mode:
            return _speech_invoke(
                self, inputs=inputs, client=client, out=out, project=project,
                project_root=project_root, extra_pack_roots=extra_pack_roots,
                banodoco_config=banodoco_config, active_theme=active_theme,
                include_missing_roots=include_missing_roots, brief=brief, dry_run=dry_run,
                check_binaries=check_binaries, python_exec=python_exec, verbose=verbose, argv=argv,
            )
        if model is None:
            raise CapabilityValidationError("model is required when target is omitted")
        model_entry = declared_entry
        if model_entry is None:
            try:
                model_entry = registry.get(model)
            except KeyError as exc:
                raise CapabilityValidationError(str(exc)) from exc
        if resolved_mode is None:
            resolved_mode = kind.finish_mode(
                sdk_module, model=model, model_entry=model_entry, mode=mode,
                resolved_mode=resolved_mode, explicit_openai=explicit_openai,
            )
        if resolved_execution is None:
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

        kind.check_recipe(inputs=inputs, model=model, resolved_mode=resolved_mode,
                          resolved_execution=resolved_execution)
        validate_generation_request(
            registry,
            model=model,
            mode=resolved_mode,
            execution=resolved_execution,
            inputs={"model": model, "mode": resolved_mode, **inputs},
            modality=modality,
            required_features=kind.required_features(resolved_mode=resolved_mode, inputs=inputs),
        )
        if resolved_execution == "local":
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
            kind.capability,
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

    @_generation_boundary
    def video(
        self,
        *,
        model: str | None = None,
        entry: str | None = None,
        placement: str | None = None,
        mode: str | None = None,
        execution: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
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
        align: str | None = None,
        **inputs: Any,
    ) -> Any:
        arguments = {key: value for key, value in locals().items() if key not in {"self", "inputs"}}
        return self._media_request("video", **arguments, **inputs)

    @_generation_boundary
    def audio(
        self,
        *,
        model: str | None = None,
        entry: str | None = None,
        placement: str | None = None,
        mode: str | None = None,
        execution: str | None = None,
        engine: str | None = None,
        consent: tuple[str, ...] = (),
        wait: bool = True,
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
        align: str | None = None,
        **inputs: Any,
    ) -> Any:
        arguments = {key: value for key, value in locals().items() if key not in {"self", "inputs"}}
        return self._media_request("audio", **arguments, **inputs)


generate = GenerationFacade()


def defaults(*, client=None):
    """Every ``(kind, mode)`` slot with its resolved entry, where it came from, and the other candidates."""
    return generate.defaults(client=client)


def set_default(kind, mode, entry, *, pack=None, client=None):
    """Pin ``entry`` as the user's default for ``kind``/``mode``."""
    return generate.set_default(kind, mode, entry, pack=pack, client=client)


def unset_default(kind, mode, *, client=None):
    """Remove the user's pin for ``kind``/``mode``."""
    return generate.unset_default(kind, mode, client=client)

