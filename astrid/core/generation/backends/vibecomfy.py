"""VibeComfyBackend — local generation via vibecomfy ready templates.

The backend drives the template's declared ``bind_input`` contract through
``wf.set_input()``.  Template graph inspection is intentionally not part of
the runtime API: a template that does not declare a requested input is an
invalid template, not an invitation to infer a node target.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib import request as urllib_request

from runtime_protocol import platform

from astrid.core.generation.backends.base import (
    BackendAdapter,
    GenerationResult,
    derive_frames_from_duration,
    parse_dimension_pair,
    split_feature_support,
)
from astrid.core.generation.backends.comfy_target import (
    ComfyTarget,
    ComfyTargetClient,
    _capture_model_resolution,
    open_comfy_http,
)
from astrid.core.generation.backends.comfy_target import (
    validate_managed_url as _validate_engine_url,
)
from astrid.core.generation.vibecomfy_dependency import (
    VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV,
    VIBECOMFY_ATTESTED_REVISION_ENV,
)
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry

logger = logging.getLogger(__name__)


COMFYUI_VERSION = "0.26.0"
VIBECOMFY_WARMTH_HINT_ENV = "ASTRID_VIBECOMFY_WARMTH_HINT"


def _session_registry_binding(session_dir: Path) -> dict[str, Any]:
    """Read the owner-written process binding, failing closed on ambiguity."""
    required = ("pid", "comfy_pid", "url", "launch", "config", "comfy_process_start_identity")
    paths = {name: session_dir / ("launch.json" if name == "launch" else f"{name}.json" if name == "config" else name) for name in required}
    if any(path.is_symlink() or not path.is_file() for path in paths.values()):
        raise ValueError("vibecomfy engine session registry is incomplete")
    try:
        marker = json.loads(paths["launch"].read_text(encoding="utf-8"))
        if not isinstance(marker, Mapping):
            raise ValueError("vibecomfy engine launch marker is malformed")
        return {
            "pid": int(paths["pid"].read_text(encoding="utf-8").strip()),
            "comfy_pid": int(paths["comfy_pid"].read_text(encoding="utf-8").strip()),
            "server_url": _validate_engine_url(paths["url"].read_text(encoding="utf-8").strip()),
            "launch_token": str(marker.get("launch_token") or ""),
            "process_birth_id": str(marker.get("process_start_identity") or ""),
            "comfy_process_birth_id": str(paths["comfy_process_start_identity"].read_text(encoding="utf-8").strip()),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("vibecomfy engine session registry is unreadable") from exc


def _owner_session_command(
    session_dir: Path, action: str, *, config: Mapping[str, Any] | None = None
) -> list[str]:
    if action not in {"start", "stop"}:
        raise ValueError("unsupported VibeComfy owner action")
    runtime_root = session_dir.resolve().parents[2]
    session_id = session_dir.name
    command = [
        sys.executable,
        "-m",
        "vibecomfy.cli",
        "--quiet",
        "session",
        action,
    ]
    command += ["--id", session_id] if action == "start" else [session_id]
    command += ["--runtime-root", str(runtime_root)]
    if action == "start":
        if config is None:
            try:
                config = json.loads((session_dir / "config.json").read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("vibecomfy engine owner config is unreadable") from exc
            if not isinstance(config, Mapping):
                raise ValueError("vibecomfy engine owner config is malformed")
        for key, flag in (("port", "--port"), ("reserve_vram_gb", "--reserve-vram-gb"), ("ready_timeout_sec", "--ready-timeout-sec"), ("memory_profile", "--memory-profile")):
            if config.get(key) is not None:
                command += [flag, str(config[key])]
        for key, flag in (("vram_policy", "--vram-policy"), ("cache_policy", "--cache-policy"), ("warm_policy", "--warm-policy"), ("input_directory", "--input-directory"), ("output_directory", "--output-directory"), ("temp_directory", "--temp-directory")):
            if config.get(key) is not None:
                command += [flag, str(config[key])]
        if config.get("disable_smart_memory"):
            command.append("--disable-smart-memory")
        for launch_flag in config.get("launch_flags", ()) if isinstance(config.get("launch_flags", ()), (list, tuple)) else ():
            command.append("--launch-flag=" + str(launch_flag))
    return command


def _assert_process_incarnation_gone(pid: int, expected_identity: str) -> None:
    """Require an OS-confirmed absence; unknown probes are not success."""
    try:
        if platform.process_identity(pid) is None:
            raise ProcessLookupError(pid)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise RuntimeError("vibecomfy engine cannot verify old process termination") from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise RuntimeError("vibecomfy engine process-absence probe failed") from exc
    try:
        if sys.platform == "darwin":
            from astrid.core.execution.vibecomfy_engine_adapter import _native_process_start_identity
            current_identity = _native_process_start_identity(pid)
        else:
            from vibecomfy.runtime.session import _process_start_identity
            current_identity = _process_start_identity(pid)
    except (ImportError, AttributeError):
        current_identity = None
    if current_identity is None:
        raise RuntimeError("vibecomfy engine cannot identify a still-live old process")
    if current_identity == expected_identity:
        raise RuntimeError("vibecomfy engine old process incarnation did not terminate")
    raise RuntimeError("vibecomfy engine old process pid was reused before termination was proven")


def _open_checkout_http(request: urllib_request.Request, *, timeout: float) -> Any:
    """Compatibility seam for the shared checked HTTP opener."""
    return open_comfy_http(request, timeout=timeout)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _verify_owned_vibe_session(session_dir: Path, pid: int) -> None:
    """Verify the daemon, Comfy child, and listener as one owned composite."""
    try:
        import vibecomfy.runtime.session as session_module
        _session_composite_ownership_verified = session_module._session_composite_ownership_verified
    except (ImportError, AttributeError) as exc:
        raise ValueError("vibecomfy engine cannot load VibeComfy ownership verifier") from exc
    from astrid.core.execution.vibecomfy_engine_adapter import EngineAdapterError
    try:
        if sys.platform == "darwin":
            from astrid.core.execution.vibecomfy_engine_adapter import SelectedCompositeObserver
            owned = SelectedCompositeObserver(session_module).verify(session_dir, pid)
        else:
            owned = _session_composite_ownership_verified(session_dir, pid)
    except (OSError, TypeError, ValueError, EngineAdapterError) as exc:
        raise ValueError("vibecomfy engine session ownership verification failed") from exc
    if owned is not True:
        raise ValueError("vibecomfy engine session is not manager-owned")

# ---------------------------------------------------------------------------
# Size / resolution parsing helpers
# ---------------------------------------------------------------------------


def _parse_size(size: str) -> tuple[int, int]:
    """Parse ``WxH``, ``W*H``, ``W,H``, or a single integer into ``(width, height)``.

    Returns ``(1024, 1024)`` if *size* is empty or unparseable.
    """
    return parse_dimension_pair(size, allow_single=True) or (1024, 1024)


def _parse_resolution(res: str) -> tuple[int, int] | None:
    """Parse a resolution string like ``"1280x720"`` into ``(width, height)``.

    Accepted separators: ``x``, ``X``, ``*``, ``,``.  Returns ``None`` if
    *res* is empty or unparseable.
    """
    return parse_dimension_pair(res)


# ---------------------------------------------------------------------------
# VibeComfyBackend
# ---------------------------------------------------------------------------


if TYPE_CHECKING:
    from astrid.packs.vibecomfy.production_engine import ProductionRunResult


def _bind_target_model_roles(workflow: Any, spec: BackendSpec, roles: Mapping[str, str]) -> None:
    """Bind declared public picker inputs without reusing stale asset provenance."""
    replacements: dict[str, str] = {}
    for role, declaration in spec.model_roles.items():
        public_input = workflow.inputs.get(declaration.binding)
        node = workflow.nodes.get(public_input.node_id) if public_input is not None else None
        if (node is None or node.class_type != declaration.node_class
                or public_input.field != declaration.input):
            raise ValueError(f"catalog role {role!r} does not match its pinned template input")
        previous = public_input.value
        selected = roles[role]
        workflow.set_input(declaration.binding, selected)
        if isinstance(previous, str) and previous != selected:
            replacements[previous] = selected
    if not replacements:
        return
    # Changing a picker does not establish byte/hash/download provenance for
    # the new target filename. Drop only the superseded picker declarations;
    # unrelated authored model requirements remain subject to approval.
    def name(item: Any) -> Any:
        return item.get("name", item.get("filename")) if isinstance(item, Mapping) else item

    workflow.requirements.models = [
        replacements[name(item)] if name(item) in replacements else item
        for item in workflow.requirements.models
    ]
    assets = workflow.metadata.get("model_assets")
    if isinstance(assets, list):
        workflow.metadata["model_assets"] = [item for item in assets if name(item) not in replacements]


def _load_pinned_catalog_workflow(spec: BackendSpec, *, retain_source=None) -> Any:
    """Use the canonical repository template loader and catalog source pin."""
    from vibecomfy.registry.ready import (
        repo_ready_template_discovery, resolve_ready_template, workflow_from_ready,
    )

    template_id = spec.template
    discovery = repo_ready_template_discovery()
    record = resolve_ready_template(template_id, discovery)

    # The catalog value and resolved record must both be canonical
    # ``category/template_id`` identifiers from the repository corpus.
    requested_id = str(template_id)
    requested_parts = requested_id.split("/")
    canonical_id = str(getattr(record, "template_id", ""))
    id_parts = canonical_id.split("/")
    if (
        requested_id != canonical_id
        or len(requested_parts) != 2
        or len(id_parts) != 2
        or any(not part or part in {".", ".."} for part in requested_parts)
        or any(not part or part in {".", ".."} for part in id_parts)
        or getattr(record, "source_scope", None) != "repo"
    ):
        raise ValueError(
            f"VibeComfy template {template_id!r} is not a canonical repo template"
        )
    template_path = Path(getattr(record, "path", ""))
    template_root = Path(getattr(record, "root", ""))
    try:
        template_path_resolved = template_path.resolve(strict=True)
        template_root_resolved = template_root.resolve(strict=True)
        template_path_resolved.relative_to(template_root_resolved)
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"VibeComfy template {canonical_id!r} is outside its repo root"
        ) from exc
    expected_hash = str(spec.template_hash)
    if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", expected_hash):
        raise ValueError(
            f"VibeComfy template {canonical_id!r} has no valid sha256 pin"
        )
    original = template_path_resolved.read_bytes()
    actual_hash = hashlib.sha256(original).hexdigest()
    if actual_hash.lower() != expected_hash[7:].lower():
        raise ValueError(
            f"VibeComfy template {canonical_id!r} failed its sha256 pin"
        )
    if retain_source is not None:
        retain_source(template_path_resolved.name, original)
    workflow = workflow_from_ready(canonical_id, _discovery=discovery)
    if canonical_id == 'edit/qwen_image_2_1_turbo_edit':
        # Keep the pinned source intact. Some materialized edit graphs omit
        # this required native loader widget; default is the authored value.
        for node in workflow.runtime_nodes().values():
            if node.class_type == 'UNETLoader':
                node.inputs.setdefault('weight_dtype', 'default')
    return workflow


def _catalog_alternative_is_executable(spec: BackendSpec, roles: Mapping[str, str],
                                       object_info: dict[str, Any], server_url: str) -> bool:
    """Only advertise a role match whose pinned graph crosses canonical approval."""
    try:
        from .comfy_schema import TargetSchemaProvider
        from vibecomfy.workflow_bundle import load_bundle

        workflow = _load_pinned_catalog_workflow(spec)
        _bind_target_model_roles(workflow, spec, roles)
        bundle = load_bundle(workflow)
        bundle.require_canonical_authority("catalog model alternative")
        bundle.compile(schema_provider=TargetSchemaProvider.from_object_info(
            object_info, server_url=server_url, model_availability="target",
        ))
    except Exception:
        # Missing template, bad pin/binding, absent nodes and approval failures
        # cannot be presented as executable model alternatives.
        return False
    return True


class VibeComfyBackend(BackendAdapter):
    """Local generation backend via pinned VibeComfy ready templates.

    The base adapter has no transport endpoint.  ``CheckoutServerAdapter`` is
    the explicit remote-only subclass and owns all remote HTTP behavior.
    """

    def __init__(self, *, target_client: ComfyTargetClient | None = None) -> None:
        self._target_client = target_client

    def _model_resolution_client(self) -> ComfyTargetClient | None:
        return getattr(self, "_target_client", None)

    def workflow(
        self, workflow_path: Path, out_dir: Path, *, task_identity: str,
        attempt_identity: str | None = None,
        expected_execution_identity: str | None = None,
        workflow_input_bindings: Mapping[str, Any] | None = None,
        profile_id: str = "local_server",
        hc03_profile: Mapping[str, Any] | None = None,
        comfy_url: str = "", comfy_output_dir: str = "",
    ) -> ProductionRunResult:
        """Run authored workflows through the existing production path."""
        from astrid.packs.vibecomfy.production_engine import run_workflow_result_path

        kwargs = dict(
            task_identity=task_identity, profile_id=profile_id,
            hc03_profile=hc03_profile,
            expected_execution_identity=expected_execution_identity,
        )
        if attempt_identity is not None:
            kwargs["attempt_identity"] = attempt_identity
        if workflow_input_bindings is not None:
            kwargs["workflow_input_bindings"] = workflow_input_bindings
        if comfy_url:
            kwargs["comfy_url"] = comfy_url
        if comfy_output_dir:
            kwargs["comfy_output_dir"] = comfy_output_dir
        from astrid.core.generation.producer_result import (
            complete_producer_result, preserve_result_on_error, source_input_ref, workflow_transport,
        )
        source = source_input_ref(workflow_path, out_dir)
        transport = workflow_transport(profile_id, comfy_url)
        with preserve_result_on_error(out_dir, task_id=task_identity, attempt_id=attempt_identity,
                                      source=source, workflow_path=workflow_path, transport=transport):
            result = run_workflow_result_path(workflow_path, out_dir, **kwargs)
            return complete_producer_result(result, out_dir, task_id=task_identity,
                                            attempt_id=attempt_identity, source=source, transport=transport)

    def _run_workflow(self, workflow: Any, *, schema_provider: Any | None = None) -> Any:
        """Run a workflow through the local embedded VibeComfy runtime."""
        if schema_provider is not None:
            raise ValueError("target-backed execution requires the selected checkout adapter and output custody")
        from vibecomfy.runtime.run import run_sync

        # Production VibeComfy runtime admission is typed: it accepts the
        # approved projection together with its canonical workflow bundle.
        # Keep the one-argument call only for the lightweight test doubles
        # used by the backend unit suite; a real VibeWorkflow must cross the
        # reviewed bundle/compiler boundary before launch.
        try:
            from vibecomfy.workflow import VibeWorkflow
            from vibecomfy.workflow_bundle import load_bundle
        except ImportError:
            VibeWorkflow = None  # type: ignore[assignment,misc]
            load_bundle = None  # type: ignore[assignment]
        if VibeWorkflow is not None and isinstance(workflow, VibeWorkflow):
            if load_bundle is None:
                raise RuntimeError("VibeComfy canonical workflow bundle support is unavailable")
            bundle = load_bundle(workflow)
            bundle.require_canonical_authority("runtime execution")
            kwargs = {"schema_provider": schema_provider} if schema_provider is not None else {}
            return run_sync(bundle.compile(**kwargs), bundle)
        return run_sync(workflow)

    def _collect_outputs(self, result: Any, out_dir: Path) -> list[Path]:
        """Preserve the embedded runtime's local path-copy semantics."""
        image_paths: list[Path] = []
        for output_path_str in result.outputs:
            src = Path(output_path_str)
            if not src.is_file():
                raise ValueError(f"VibeComfy output not found: {src}")
            dst = out_dir / src.name
            # If dst already exists (e.g. from a prior iteration), add a suffix
            if dst.exists():
                stem = src.stem
                suffix = src.suffix
                counter = 1
                while dst.exists():
                    dst = out_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
            shutil.copy2(src, dst)
            image_paths.append(dst)
        return image_paths

    #: Default canonical→template parameter name mapping per mode.
    #: Used as a fallback when ``BackendSpec.param_map`` is empty.
    #: Size and resolution are handled specially in :meth:`generate` so
    #: their entries here are nominal; the adapter splits width/height.
    DEFAULT_PARAM_MAP: dict[str, dict[str, str]] = {
        # ── Image modes ────────────────────────────────────────────────
        "t2i": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "count": "count",
            "size": "size",
            "guidance_scale": "guidance",
            "steps": "steps",
        },
        "i2i": {
            "prompt": "prompt",
            "seed": "seed",
            "image_ref": "image_ref",
            "size": "size",
            "strength": "denoise",
            "guidance_scale": "guidance",
            "steps": "steps",
        },
        "edit": {
            "prompt": "prompt",
            "seed": "seed",
            "count": "count",
            "image_ref": "image",
            "size": "size",
            "guidance_scale": "guidance",
            "steps": "steps",
        },
        # ── Video modes ────────────────────────────────────────────────
        "t2v": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "resolution": "resolution",
            "frames": "frames",
            "fps": "fps",
        },
        "i2v": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "image_ref": "image",
            "resolution": "resolution",
            "frames": "frames",
            "fps": "fps",
        },
        "flf": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "image_ref": "start_image",
            "image_end_ref": "end_image",
            "resolution": "resolution",
            "frames": "frames",
            "fps": "fps",
        },
    }

    def prepare_catalog_workflow(
        self,
        entry: ModelEntry,
        mode: str,
        params: dict[str, Any],
        *,
        resolve_target: bool = False,
        retain_source: Any = None,
    ) -> tuple[Any, dict[str, Any] | None, Any | None]:
        """Resolve and bind a pinned graph for the existing workflow route."""
        # Lazy-import VibeComfy, and snapshot only its repository
        # template corpus.  Dynamic/user template discovery is forbidden.
        import vibecomfy  # noqa: F401

        mode_spec = entry.modes[mode]
        backend_spec: BackendSpec = mode_spec.backends["local"]
        template_id = backend_spec.template
        plan, target_schema = None, None
        if resolve_target or (backend_spec.model_roles and self._model_resolution_client() is not None):
            from astrid.core.model_catalog.registry import ModelRegistry
            from .comfy_schema import TargetSchemaProvider

            client = self._model_resolution_client()
            if client is None:
                raise ValueError("catalog model role resolution requires a selected Comfy target")
            registry = ModelRegistry.load_default()
            # Keep caller-provided typed entries usable, as on the historical
            # checkout path. Resolution is always captured afresh by the bound
            # client; no caller-provided plan or resolved filename is trusted.
            entries = [candidate for candidate in registry.list_all(include_closed=True)
                       if candidate.id != entry.id]
            registry = ModelRegistry([entry, *entries])
            plan, observed = _capture_model_resolution(client, registry, entry.id, mode)
            target_schema = TargetSchemaProvider.from_object_info(
                observed, server_url=client.http_origin, model_availability="target",
                refresh_object_info=client.object_info,
            )

        # --- build param map: canonical → template parameter name ------------
        param_map: dict[str, str] = dict(backend_spec.param_map)
        if not param_map:
            param_map = dict(self.DEFAULT_PARAM_MAP.get(mode, {}))

        # --- derive frame count deterministically ----------------------------
        # If duration is supplied without frames, and fps is known, derive frames
        computed_frames = derive_frames_from_duration(params)
        if computed_frames is not None:
            logger.debug("Computed frames=%d from duration * fps", computed_frames)

        wf = (_load_pinned_catalog_workflow(backend_spec, retain_source=retain_source)
              if retain_source is not None else _load_pinned_catalog_workflow(backend_spec))
        # Features whose param_map key is in the feature list get mapped.
        # Count is not set on the workflow — the caller loops externally.
        for canon, tmpl_param in param_map.items():
            if canon == "count":
                continue  # count is managed by the executor loop
            if canon == "size":
                w, h = _parse_size(params.get("size", ""))
                # Try set_input for width/height individually
                wf.set_input("width", w)
                wf.set_input("height", h)
                continue
            if canon == "resolution":
                res_str = str(params.get("resolution", ""))
                parsed = _parse_resolution(res_str)
                if parsed:
                    w, h = parsed
                    wf.set_input("width", w)
                    wf.set_input("height", h)
                continue
            if canon not in params:
                continue
            value = params[canon]
            if value is None:
                continue
            wf.set_input(tmpl_param, value)

        if plan is not None:
            _bind_target_model_roles(wf, backend_spec, plan["roles"])
        if target_schema is not None:
            from astrid.core.generation.asset_gaps import require_workflow_assets
            require_workflow_assets(wf, observed, entry.id)

        unbound_inputs = getattr(wf, "metadata", {}).get("unbound_inputs", {})
        if isinstance(unbound_inputs, dict):
            # Generated templates list every public input's default under
            # ``unbound_inputs``; a public input bound to a node in this
            # workflow is declared, so only names without that binding count.
            declared_inputs = getattr(wf, "inputs", None)
            workflow_nodes = getattr(wf, "nodes", None)
            bound_inputs = {
                name for name, spec in (declared_inputs.items() if isinstance(declared_inputs, dict) else ())
                if isinstance(workflow_nodes, dict) and getattr(spec, "node_id", None) in workflow_nodes
            }
            requested_unbound = sorted(
                tmpl_param
                for canon, tmpl_param in param_map.items()
                if canon not in {"count", "size", "resolution"}
                and canon in params
                and params[canon] is not None
                and tmpl_param in unbound_inputs
                and tmpl_param not in bound_inputs
            )
            if requested_unbound:
                raise ValueError(
                    f"VibeComfy template {template_id!r} does not declare inputs: "
                    + ", ".join(requested_unbound)
                )

        return wf, plan, target_schema

    def generate(
        self,
        entry: ModelEntry,
        mode: str,
        params: dict[str, Any],
        out_dir: Path,
        *,
        dry_run: bool = False,
    ) -> GenerationResult | dict[str, Any]:
        t0 = time.monotonic()
        wf, plan, target_schema = self.prepare_catalog_workflow(
            entry, mode, params, resolve_target=dry_run,
        )
        seed_used: int = params.get("seed", 0)
        applied_features, dropped_features = split_feature_support(params, entry.modes[mode].supports)
        if dry_run:
            from vibecomfy.workflow_bundle import load_bundle

            bundle = load_bundle(wf)
            bundle.require_canonical_authority("catalog target resolution dry-run")
            approved = bundle.compile(schema_provider=target_schema)
            plan["approval"] = {"status": "approved", "api_digest": approved.api_digest}
            return plan
        if target_schema is None:
            result = self._run_workflow(wf)
        else:
            result = self._run_workflow(wf, schema_provider=target_schema)
        duration_ms = int((time.monotonic() - t0) * 1000)

        # --- collect outputs -------------------------------------------------
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        image_paths = self._collect_outputs(result, out_dir)

        return GenerationResult(
            image_paths=image_paths,
            seed_used=seed_used,
            model_actual=entry.modes[mode].backends["local"].template,
            cost_usd=None,  # local backends have no cost
            duration_ms=duration_ms,
            applied_features=applied_features,
            dropped_features=dropped_features,
            error=None,
        )


class VibeComfyEngine:
    """Lifecycle wrapper around a host-owned ComfyUI runtime.

    Warmth is disposable.  Every lifecycle transition is serialized by a
    lock, while native containment fences prepare/run callers before making
    its HTTP requests.  A failed containment operation poisons the lifecycle
    until the complete interrupt + queue-clear + ``/api/free`` sequence
    succeeds.
    """

    def __init__(self, server_url: str, *, endpoint_transport: Any = None) -> None:
        self._origin = _validate_engine_url(server_url)
        self._endpoint_transport = endpoint_transport
        self._http_origin = self._origin if endpoint_transport is None else endpoint_transport.server_url
        self._lock = threading.RLock()
        self._operation: str | None = None
        self._running = False
        self._poisoned = False
        self._fence_pending = False
        self._cold_reset_verified = False
        self._warm = False
        self._fingerprint: str | None = None
        self._warmth_identity: str | None = None
        self._model_bytes_digest: str | None = None
        self._runtime_instance_id: str | None = None
        self._prepared_fingerprint: str | None = None
        self._prepared_warmth_identity: str | None = None
        self._prepared_model_bytes_digest: str | None = None
        self._prepared_runtime_instance_id: str | None = None
        self._retention_hint: tuple[str, str, str, str] | None = None
        self._lifecycle_generation = 0
        self.last_lifecycle = "cold"
        self.last_warm_reused = False

    @property
    def warm(self) -> bool:
        return self._warm

    @property
    def fingerprint(self) -> str | None:
        return self._fingerprint or self._prepared_fingerprint

    @property
    def warmth_identity(self) -> str | None:
        return self._warmth_identity or self._prepared_warmth_identity

    @property
    def model_bytes_digest(self) -> str | None:
        return self._model_bytes_digest or self._prepared_model_bytes_digest

    @property
    def prepared_fingerprint(self) -> str | None:
        return self._prepared_fingerprint

    @property
    def prepared_model_bytes_digest(self) -> str | None:
        return self._prepared_model_bytes_digest

    @property
    def prepared_runtime_instance_id(self) -> str | None:
        return self._prepared_runtime_instance_id

    @property
    def prepared_warmth_identity(self) -> str | None:
        return self._prepared_warmth_identity

    @property
    def runtime_instance_id(self) -> str | None:
        return self._runtime_instance_id or self._prepared_runtime_instance_id

    @property
    def poisoned(self) -> bool:
        return self._poisoned

    @property
    def fence_pending(self) -> bool:
        return self._fence_pending

    @staticmethod
    def _identity(value: object, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"vibecomfy engine {field} must be a non-empty string")
        return value

    @staticmethod
    def _runtime_identity(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "vibecomfy engine runtime_instance_id must come from canonical health/bootstrap"
            )
        # Reject synthetic digests and probe tags.
        if value.startswith("probe:"):
            raise ValueError(
                "vibecomfy engine runtime_instance_id must come from canonical health/bootstrap"
            )
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(
                "vibecomfy engine runtime_instance_id must be a valid UUID from canonical health/bootstrap"
            ) from exc
        return str(parsed)

    @staticmethod
    def _validate_model_bytes_digest(*values: str | None) -> str:
        candidates = [value for value in values if value is not None]
        if not candidates:
            raise ValueError(
                "vibecomfy engine requires model_bytes_digest for production warmth"
            )
        normalized: list[str] = []
        for value in candidates:
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:sha256:)?[0-9a-fA-F]{64}", value
            ):
                raise ValueError(
                    "vibecomfy engine model_bytes_digest must be sha256:<64hex> or raw 64hex"
                )
            normalized.append(
                "sha256:" + value.removeprefix("sha256:").lower()
            )
        if len(set(normalized)) != 1:
            raise ValueError("vibecomfy engine model digest aliases do not match")
        return normalized[0]

    def _clear_prepared(self) -> None:
        self._prepared_fingerprint = None
        self._prepared_warmth_identity = None
        self._prepared_model_bytes_digest = None
        self._prepared_runtime_instance_id = None

    def _clear_warm(self) -> None:
        self._warm = False
        self._fingerprint = None
        self._warmth_identity = None
        self._model_bytes_digest = None
        self._runtime_instance_id = None
        self.last_lifecycle = "cold"
        self.last_warm_reused = False

    def _clear_retention_hint(self) -> None:
        self._retention_hint = None

    def _warm_is_compatible(
        self,
        fingerprint: str,
        warmth_identity: str | None,
        model_bytes_digest: str,
        runtime_instance_id: str,
    ) -> bool:
        return (
            self._warm
            and self._fingerprint == fingerprint
            and self._model_bytes_digest == model_bytes_digest
            and (
                warmth_identity is None
                or self._warmth_identity in {None, warmth_identity}
            )
            and self._runtime_instance_id == runtime_instance_id
        )

    def adopt_warmth(
        self,
        *,
        fingerprint: str,
        warmth_identity: str,
        model_bytes_digest: str,
        runtime_instance_id: str,
    ) -> None:
        """Restore a host-issued warmth observation for a new child wrapper.

        The server process is the resident session; command children are
        intentionally short-lived.  This observation is accepted only from
        the host-owned hint path and remains subject to the normal probe and
        lifecycle fences before execution.
        """
        fingerprint = self._identity(fingerprint, "fingerprint")
        warmth_identity = self._identity(warmth_identity, "warmth_identity")
        model_bytes = self._validate_model_bytes_digest(model_bytes_digest)
        runtime_instance_id = self._runtime_identity(runtime_instance_id)
        with self._lock:
            if self._operation is not None or self._running:
                raise RuntimeError("vibecomfy engine cannot adopt warmth while busy")
            # This is permission to attempt retention, not proof that GPU
            # weights are still resident.  A successful post-hint probe and
            # subsequent run may publish warmth; adoption itself must not.
            self._retention_hint = (
                fingerprint,
                warmth_identity,
                model_bytes,
                runtime_instance_id,
            )

    def _retention_hint_matches(
        self,
        fingerprint: str,
        warmth_identity: str | None,
        model_bytes_digest: str,
        runtime_instance_id: str,
    ) -> bool:
        hint = self._retention_hint
        return hint is not None and hint == (
            fingerprint,
            warmth_identity or hint[1],
            model_bytes_digest,
            runtime_instance_id,
        )

    def _abort_preparation(self) -> None:
        """Discard both pending and previously published warmth."""
        with self._lock:
            self._clear_warm()
            self._clear_prepared()
            self._clear_retention_hint()
            self._lifecycle_generation += 1

    def _poison(self, *, cold_reset_verified: bool = False) -> None:
        self._clear_warm()
        self._clear_prepared()
        self._clear_retention_hint()
        self._poisoned = True
        self._fence_pending = True
        self._cold_reset_verified = cold_reset_verified
        self._lifecycle_generation += 1

    def _reset_after_free(self) -> None:
        self._clear_warm()
        self._clear_prepared()
        self._poisoned = False
        self._fence_pending = False
        self._cold_reset_verified = False
        self._clear_retention_hint()
        self._lifecycle_generation += 1


    def _cold_free(self) -> None:
        """Prove a cold reset with complete native containment."""
        result = self._contain(
            "reset",
            (
                ("/interrupt", {}, "interrupt"),
                ("/queue", {"clear": True}, "queue_clear"),
                ("/api/free", {"unload_models": True, "free_memory": True}, "free"),
            ),
        )
        if not result.get("ok", False):
            raise RuntimeError(
                "vibecomfy engine could not prove complete cold reset"
            )

    def _contain(
        self,
        operation: str,
        paths: tuple[tuple[str, dict[str, Any], str], ...],
        *,
        preflight: Any | None = None,
        require_completion: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            if self._operation is not None:
                raise RuntimeError(
                    f"vibecomfy engine {self._operation} is already in progress"
                )
            if (self._poisoned or self._fence_pending) and (
                not paths or paths[0][0] != "/interrupt"
            ):
                paths = (
                    ("/interrupt", {}, "interrupt"),
                    *paths,
                )
            self._operation = operation
            # Set the fence and discard old warmth before any probe/request.
            self._fence_pending = True
            self._clear_warm()
            self._clear_prepared()
            self._clear_retention_hint()
            self._lifecycle_generation += 1
        errors: list[str] = []
        results: dict[str, Any] = {}
        try:
            if preflight is not None:
                try:
                    preflight()
                except Exception as exc:
                    errors.append(str(exc))
            if not errors:
                for path, payload, key in paths:
                    try:
                        results[key] = self._post(path, payload)
                    except Exception as exc:
                        errors.append(str(exc))
            if not errors and require_completion:
                completion = results.get("free")
                completion_observed = isinstance(completion, Mapping) and (
                    completion.get("models_unloaded") is True
                    or completion.get("memory_freed") is True
                    or (
                        completion.get("completed") is True
                        and completion.get("status") in {"success", "completed"}
                    )
                    or completion.get("acknowledged") is True
                )
                if not completion_observed:
                    errors.append(
                        "vibecomfy engine release returned no explicit backend completion observation"
                    )
            with self._lock:
                if errors:
                    # Any failed step keeps the fence.  A successful free
                    # alone is not evidence that interrupt and queue state
                    # were contained.
                    self._poison(cold_reset_verified=False)
                    return {
                        "ok": False,
                        "status": "requires_fence",
                        "contained": False,
                        "cancelled": False,
                        "released": False,
                        "error": "; ".join(errors),
                        "results": results,
                    }
                self._reset_after_free()
                return {
                    "ok": True,
                    "status": "cancelled" if operation == "cancel" else "cold",
                    "contained": True,
                    "cancelled": operation == "cancel",
                    "released": operation == "release",
                    "results": results,
                }
        finally:
            with self._lock:
                self._operation = None

    def _prepare_for_warm_session(self) -> bool:
        """Fence warmth while the adapter probes the host."""
        with self._lock:
            if self._operation is not None:
                raise RuntimeError(
                    f"vibecomfy engine {self._operation} is already in progress"
                )
            if self._running:
                raise RuntimeError("vibecomfy engine run is already in progress")
            requires_cold_reset = (
                self._poisoned
                or self._fence_pending
                or self._warm
                or self._prepared_fingerprint is not None
            )
            self._fence_pending = True
            # Published warmth remains a candidate until the probe succeeds;
            # _poison() clears it if the probe fails.
            self._clear_prepared()
            self._lifecycle_generation += 1
            return requires_cold_reset

    def prepare_session(
        self,
        fingerprint: str,
        warmth_identity: str | None = None,
        *,
        runtime_instance_id: str | None = None,
        model_bytes_digest: str | None = None,
        model_digest: str | None = None,
        artifact_sha256: str | None = None,
        cold: bool = False,
    ) -> dict[str, Any]:
        """Fence incompatible warmth and report the lifecycle decision."""
        fingerprint = self._identity(fingerprint, "fingerprint")
        if warmth_identity is not None:
            warmth_identity = self._identity(warmth_identity, "warmth_identity")
        model_bytes = self._validate_model_bytes_digest(
            model_bytes_digest, model_digest, artifact_sha256
        )
        if runtime_instance_id is None:
            raise ValueError(
                "vibecomfy engine runtime_instance_id is required from canonical health/bootstrap"
            )
        runtime_instance_id = self._runtime_identity(runtime_instance_id)
        with self._lock:
            if self._operation is not None:
                raise RuntimeError(
                    f"vibecomfy engine {self._operation} is already in progress"
                )
            if self._running:
                raise RuntimeError("vibecomfy engine run is already in progress")
            if self._fence_pending or self._poisoned:
                if not cold:
                    raise RuntimeError(
                        "vibecomfy engine lifecycle is poisoned or fence-pending; "
                        "a proven cold reset is required"
                    )
                self._cold_free()
            compatible = self._warm_is_compatible(
                fingerprint,
                warmth_identity,
                model_bytes,
                runtime_instance_id,
            )
            retained = self._retention_hint_matches(
                fingerprint,
                warmth_identity,
                model_bytes,
                runtime_instance_id,
            )
            if (self._warm or self._retention_hint is not None) and not compatible and not retained:
                released = self.release(reason="incompatible fingerprint")
                if not released.get("ok", False):
                    raise ValueError("vibecomfy engine could not release incompatible warmth")
            self._prepared_fingerprint = fingerprint
            self._prepared_warmth_identity = warmth_identity
            self._prepared_model_bytes_digest = model_bytes
            self._prepared_runtime_instance_id = runtime_instance_id
            self.last_lifecycle = "warm" if compatible else "retained" if retained else "cold"
            self.last_warm_reused = compatible
            return {
                "status": self.last_lifecycle,
                "lifecycle": self.last_lifecycle,
                "warm_reused": compatible,
                "retention_permitted": retained,
                "warm_observed": compatible,
                "fingerprint": fingerprint,
                "fingerprint_stored": fingerprint,
                "warmth_identity": warmth_identity,
                "model_bytes_digest": model_bytes,
                "runtime_instance_id": runtime_instance_id,
                "poisoned": self._poisoned,
                "fence_pending": self._fence_pending,
            }

    def warm_session(
        self,
        fingerprint: str,
        warmth_identity: str | None = None,
        *,
        runtime_instance_id: str | None = None,
        model_bytes_digest: str | None = None,
        model_digest: str | None = None,
        artifact_sha256: str | None = None,
        cold: bool = False,
    ) -> dict[str, Any]:
        """Host-ABI spelling for :meth:`prepare_session`."""
        return self.prepare_session(
            fingerprint,
            warmth_identity,
            runtime_instance_id=runtime_instance_id,
            model_bytes_digest=model_bytes_digest,
            model_digest=model_digest,
            artifact_sha256=artifact_sha256,
            cold=cold,
        )

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        # Lifecycle policy stays with this managed owner; the client owns only
        # the bounded, checked HTTP exchange.
        target = ComfyTarget(
            self._origin if self._endpoint_transport is None else self._endpoint_transport.endpoint.url,
            "managed" if self._endpoint_transport is None else "pod",
        )
        return ComfyTargetClient(
            target, endpoint_transport=self._endpoint_transport, opener=_open_checkout_http
        ).request_json(path, payload)

    def run(
        self,
        workflow: Any,
        *,
        runtime_instance_id: str | None = None,
        config: Any | None = None,
        schema_provider: Any | None = None,
    ) -> Any:
        """Run one workflow without allowing poisoned lifecycle reuse."""
        if schema_provider is not None and getattr(schema_provider, "server_url", None) != self._http_origin:
            raise ValueError("vibecomfy engine schema provider differs from the selected Comfy target")
        with self._lock:
            if self._operation is not None:
                raise RuntimeError(
                    f"vibecomfy engine {self._operation} is already in progress"
                )
            if self._running:
                raise RuntimeError("vibecomfy engine run is already in progress")
            if self._poisoned or self._fence_pending:
                raise RuntimeError(
                    "vibecomfy engine lifecycle is poisoned or fence-pending; "
                    "a proven cold reset is required"
                )
            if runtime_instance_id is None:
                raise ValueError(
                    "vibecomfy engine runtime_instance_id is required from canonical health/bootstrap"
                )
            runtime_instance_id = self._runtime_identity(runtime_instance_id)
            if self._prepared_runtime_instance_id != runtime_instance_id:
                self._poison()
                raise RuntimeError("vibecomfy engine runtime instance changed")
            run_generation = self._lifecycle_generation
            self._running = True

        try:
            from vibecomfy.runtime.run import run_sync

            runtime_args: tuple[Any, ...]
            if isinstance(workflow, tuple) and len(workflow) == 2:
                runtime_args = (workflow[0], workflow[1])
            else:
                # The selected VibeComfy runtime admits only canonical
                # ApprovedProjectionRecord + WorkflowBundle pairs.  Keep the
                # lightweight raw call only for old test doubles that are not
                # VibeWorkflow instances; production objects always cross the
                # bundle compiler here.
                try:
                    from vibecomfy.workflow import VibeWorkflow
                    from vibecomfy.workflow_bundle import load_bundle
                except ImportError:
                    VibeWorkflow = None  # type: ignore[assignment,misc]
                    load_bundle = None  # type: ignore[assignment]
                if VibeWorkflow is not None and isinstance(workflow, VibeWorkflow):
                    if load_bundle is None:
                        raise RuntimeError(
                            "vibecomfy engine cannot load the canonical workflow bundle"
                        )
                    bundle = load_bundle(workflow)
                    bundle.require_canonical_authority("runtime execution")
                    runtime_args = (bundle.compile(), bundle)
                else:
                    runtime_args = (workflow,)
            runtime_kwargs: dict[str, Any] = {"server_url": self._http_origin}
            if schema_provider is not None:
                runtime_kwargs["schema_provider"] = schema_provider
            if config is not None:
                runtime_kwargs["config"] = config
            if self._endpoint_transport is not None:
                raise ValueError("endpoint transport was removed with the checkout route")
            result = run_sync(*runtime_args, **runtime_kwargs)
            with self._lock:
                # A containment transition may have superseded this run.  A
                # result from the old incarnation is never eligible for
                # output custody or settlement.
                if (
                    self._lifecycle_generation != run_generation
                    or self._fence_pending
                    or self._poisoned
                ):
                    raise RuntimeError(
                        "vibecomfy engine run completed after its lifecycle fence"
                    )
                self._warm = True
                self._fingerprint = self._prepared_fingerprint
                self._warmth_identity = self._prepared_warmth_identity
                self._model_bytes_digest = self._prepared_model_bytes_digest
                self._runtime_instance_id = self._prepared_runtime_instance_id
                self._clear_prepared()
            return result
        except BaseException as exc:
            with self._lock:
                self._running = False
                # A containment/reset transition may have superseded this
                # run.  Its completion must not alter newer fence state.
                if self._lifecycle_generation == run_generation:
                    self._poison()
            if self._endpoint_transport is not None and isinstance(exc, Exception):
                self._endpoint_transport.raise_if_submission_uncertain(exc)
            raise
        finally:
            with self._lock:
                self._running = False

    def cancel(
        self,
        frame: object | None = None,
        *,
        preflight: Any | None = None,
    ) -> dict[str, Any]:
        """Interrupt execution and clear pending ComfyUI work."""
        del frame
        return self._contain(
            "cancel",
            (
                ("/interrupt", {}, "interrupt"),
                ("/queue", {"clear": True}, "queue_clear"),
                ("/api/free", {"unload_models": True, "free_memory": True}, "free"),
            ),
            preflight=preflight,
        )

    def release(
        self,
        frame: object | None = None,
        *,
        reason: str = "requested",
        preflight: Any | None = None,
        require_completion: bool = False,
    ) -> dict[str, Any]:
        """Clear queued work and unload ComfyUI models/VAE state."""
        del frame, reason
        return self._contain(
            "release",
            (
                ("/queue", {"clear": True}, "queue_clear"),
                ("/api/free", {"unload_models": True, "free_memory": True}, "free"),
            ),
            preflight=preflight,
            require_completion=require_completion,
        )


