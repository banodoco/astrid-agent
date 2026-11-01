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
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from runtime_protocol import platform

from astrid.core.path_utils import has_url_scheme, is_native_absolute_path
from astrid.core.generation.backends.base import (
    BackendAdapter,
    GenerationResult,
    derive_frames_from_duration,
    parse_dimension_pair,
    split_feature_support,
)
from astrid.core.generation.model_root import (
    MODEL_ROOT_ENV,
    ModelRootBindingError,
    model_root_binding_from_profile,
    use_attested_vibecomfy_models_root,
    validate_model_root_binding,
)
from astrid.core.generation.vibecomfy_dependency import (
    VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV,
    VIBECOMFY_ATTESTED_REVISION_ENV,
    VIBECOMFY_ENGINE_REVISION,
)
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry

logger = logging.getLogger(__name__)

# Explicit runtime adapter precedence.  The pip-installed embedded runtime is
# preferred; the checked-out managed server runtime remains the second choice.
ADAPTER_ORDER: tuple[str, ...] = ("pip_embedded", "checkout_server")

COMFYUI_VERSION = "0.26.0"
VIBECOMFY_SESSION_OWNERSHIP_ATTESTED_ENV = (
    "ASTRID_VIBECOMFY_SESSION_OWNERSHIP_ATTESTED"
)
VIBECOMFY_WARMTH_HINT_ENV = "ASTRID_VIBECOMFY_WARMTH_HINT"


def _session_registry_binding(session_dir: Path) -> dict[str, Any]:
    """Read the owner-written process binding, failing closed on ambiguity."""
    required = ("pid", "comfy_pid", "url", "launch", "config", "comfy_process_start_identity")
    paths = {name: session_dir / ("launch.json" if name == "launch" else f"{name}.json" if name == "config" else name) for name in required}
    if any(path.is_symlink() or not path.is_file() for path in paths.values()):
        raise ValueError("checkout_server session registry is incomplete")
    try:
        marker = json.loads(paths["launch"].read_text(encoding="utf-8"))
        if not isinstance(marker, Mapping):
            raise ValueError("checkout_server launch marker is malformed")
        return {
            "pid": int(paths["pid"].read_text(encoding="utf-8").strip()),
            "comfy_pid": int(paths["comfy_pid"].read_text(encoding="utf-8").strip()),
            "server_url": _validate_checkout_server_url(paths["url"].read_text(encoding="utf-8").strip()),
            "launch_token": str(marker.get("launch_token") or ""),
            "process_birth_id": str(marker.get("process_start_identity") or ""),
            "comfy_process_birth_id": str(paths["comfy_process_start_identity"].read_text(encoding="utf-8").strip()),
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("checkout_server session registry is unreadable") from exc


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
                raise ValueError("checkout_server owner config is unreadable") from exc
            if not isinstance(config, Mapping):
                raise ValueError("checkout_server owner config is malformed")
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
        raise RuntimeError("checkout_server cannot verify old process termination") from exc
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return
        raise RuntimeError("checkout_server process-absence probe failed") from exc
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
        raise RuntimeError("checkout_server cannot identify a still-live old process")
    if current_identity == expected_identity:
        raise RuntimeError("checkout_server old process incarnation did not terminate")
    raise RuntimeError("checkout_server old process pid was reused before termination was proven")


def _normalise_comfyui_constraint(value: object | None) -> str:
    """Return one validated PEP 440 ComfyUI version constraint.

    The historical checkout profile defaults to 0.26.0, but a canonical
    workflow may carry a stricter runtime declaration (for example
    ``==0.36.0``). Keeping the constraint on the adapter makes the server
    probe and the session fingerprint agree without weakening the gate.
    """
    constraint = COMFYUI_VERSION if value is None else str(value).strip()
    # The legacy constant was a bare version, while workflow declarations use
    # normal PEP 440 specifiers. Treat a bare version as an exact pin so old
    # callers retain their fail-closed behaviour.
    if re.fullmatch(r"\d+(?:\.\d+)+(?:[._-][0-9A-Za-z]+)?", constraint):
        constraint = f"=={constraint}"
    if not constraint:
        raise ValueError("checkout_server ComfyUI version constraint is empty")
    try:
        from packaging.specifiers import SpecifierSet

        SpecifierSet(constraint)
    except Exception as exc:  # noqa: BLE001 - normalize dependency-boundary errors.
        raise ValueError(
            f"checkout_server has an invalid ComfyUI version constraint {constraint!r}"
        ) from exc
    return constraint


def _comfyui_version_satisfies(actual: object, constraint: str) -> bool:
    try:
        from packaging.specifiers import SpecifierSet
        from packaging.version import Version

        return Version(str(actual)) in SpecifierSet(constraint)
    except Exception:  # noqa: BLE001 - a malformed observation is a mismatch.
        return False


def _observed_source_identity(
    expected_revision: str, expected_content_digest: str
) -> tuple[str | None, str | None]:
    """Read host-attested source identity without spawning native children.

    GenericPackHost has already verified the live checkout against the
    digest-bound readiness profile.  Under a child network hook, repeating
    that check through ``git`` would be an unnecessary native descendant and
    is correctly rejected.  Direct/non-host callers retain the historical
    VibeComfy probe as a fallback.
    """
    attested_revision = os.environ.get(VIBECOMFY_ATTESTED_REVISION_ENV, "").strip()
    attested_digest = os.environ.get(
        VIBECOMFY_ATTESTED_CONTENT_DIGEST_ENV, ""
    ).strip()
    if (
        re.fullmatch(r"[0-9a-fA-F]{40}", attested_revision)
        and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", attested_digest)
        and attested_revision == expected_revision
        and attested_digest == expected_content_digest
    ):
        return attested_revision, attested_digest
    try:
        from vibecomfy.runtime.session import (
            current_source_content_digest,
            current_source_revision,
        )

        return current_source_revision(), current_source_content_digest()
    except (ImportError, AttributeError):
        return None, None


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        raise ValueError("checkout_server remote request redirected unexpectedly")


def _open_checkout_http(request: urllib_request.Request, *, timeout: float) -> Any:
    """Open one checkout-server request without following redirects."""
    return urllib_request.build_opener(_NoRedirectHandler).open(
        request, timeout=timeout
    )


def _validate_checkout_server_url(server_url: str) -> str:
    """Return a canonical, origin-only HTTP(S) endpoint."""
    if not isinstance(server_url, str) or not server_url:
        raise ValueError("checkout_server requires an explicit server_url")
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in server_url):
        raise ValueError("checkout_server server_url must not contain whitespace or control characters")
    if "\\" in server_url:
        raise ValueError("checkout_server server_url must not contain backslashes")
    if "?" in server_url or "#" in server_url:
        raise ValueError("checkout_server server_url must not contain query or fragment")
    parsed = urllib_parse.urlsplit(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("checkout_server server_url must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("checkout_server server_url must not contain credentials")
    if parsed.netloc.endswith(":"):
        raise ValueError("checkout_server server_url has an invalid port")
    if parsed.query or parsed.fragment:
        raise ValueError("checkout_server server_url must not contain query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("checkout_server server_url must contain only an origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("checkout_server server_url has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("checkout_server server_url has an invalid port")

    host = parsed.hostname
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in host):
        raise ValueError("checkout_server server_url has an invalid host")
    if ":" in host:
        # IPv6 literals must use the bracketed URL spelling.
        try:
            import ipaddress

            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError("checkout_server server_url has an invalid host") from exc
        host_part = f"[{host.lower()}]"
    else:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("checkout_server server_url has an invalid host")
        if ".." in host or any(
            not label or label.startswith("-") or label.endswith("-")
            for label in host.split(".")
        ):
            raise ValueError("checkout_server server_url has an invalid host")
        host_part = host.lower()
    return f"{parsed.scheme}://{host_part}{f':{port}' if port is not None else ''}"


def _validate_output_field(
    value: object,
    *,
    field: str,
    allow_empty: bool = False,
    reject_slash: bool = False,
) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError(f"checkout_server output descriptor has invalid {field}")
    if any(
        char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\"
        for char in value
    ):
        raise ValueError(f"checkout_server output descriptor has unsafe {field}")
    if is_native_absolute_path(value) or value.startswith("~") or has_url_scheme(value):
        raise ValueError(f"checkout_server output descriptor has unsafe {field}")
    if ".." in value or (reject_slash and "/" in value):
        raise ValueError(f"checkout_server output descriptor has unsafe {field}")
    if not allow_empty and not value:
        raise ValueError(f"checkout_server output descriptor has invalid {field}")
    return value


def _validate_output_descriptor(descriptor: object) -> tuple[str, str, str]:
    if not isinstance(descriptor, dict):
        raise ValueError("checkout_server output descriptor must be an object")
    if not {"filename", "subfolder", "type"}.issubset(descriptor):
        raise ValueError("checkout_server output descriptor has an invalid schema")
    # Comfy/VHS may attach routing metadata to a descriptor, but accepting an
    # arbitrary key set would make the custody contract ambiguous.  Keep the
    # required routing fields plus the documented metadata emitted by Comfy;
    # reject unknown fields so malformed or attacker-controlled descriptors do
    # not silently pass through settlement.
    allowed_metadata = {
        "filename",
        "subfolder",
        "type",
        "format",
        "frame_rate",
        "workflow",
        "fullpath",
        "prompt_id",
        "node_id",
    }
    if set(descriptor).difference(allowed_metadata):
        raise ValueError("checkout_server output descriptor has an invalid schema")
    filename = _validate_output_field(
        descriptor.get("filename"), field="filename", reject_slash=True
    )
    subfolder = _validate_output_field(
        descriptor.get("subfolder"), field="subfolder", allow_empty=True
    )
    output_type = _validate_output_field(descriptor.get("type"), field="type")
    if output_type not in {"output", "temp"}:
        raise ValueError("checkout_server output descriptor has invalid type")
    return filename, subfolder, output_type


def _extract_output_descriptors(value: object) -> list[object]:
    "Find Comfy output descriptors nested in node/output containers."
    if isinstance(value, list):
        descriptors: list[object] = []
        for item in value:
            descriptors.extend(_extract_output_descriptors(item))
        return descriptors
    if isinstance(value, dict):
        # Comfy's output descriptors include the required routing fields plus
        # optional metadata (format, frame rate, workflow, fullpath, ...).
        # Treat the required subset as a descriptor instead of requiring an
        # exact three-key shape, otherwise valid VHS video outputs disappear
        # at settlement time.
        if {"filename", "subfolder", "type"}.issubset(value):
            return [value]
        descriptors = []
        for item in value.values():
            descriptors.extend(_extract_output_descriptors(item))
        return descriptors
    return []


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def vibecomfy_warmth_hint(
    *,
    session_id: str,
    process_birth_id: str,
    comfy_process_birth_id: str,
    runtime_instance_id: str,
    model_id: str,
    model_bytes_digest: str,
    facts_digest: str,
    source_revision: str,
    source_content_digest: str,
    config_digest: str,
) -> str:
    """Bind a host-owned warm hint to one verified server incarnation."""
    return _canonical_sha256(
        {
            "schema": "astrid.vibecomfy.warmth-hint.v1",
            "session_id": _require_identity(session_id, "session_id"),
            "process_birth_id": _require_identity(process_birth_id, "process_birth_id"),
            "comfy_process_birth_id": _require_identity(
                comfy_process_birth_id, "comfy_process_birth_id"
            ),
            "runtime_instance_id": VibeComfyEngine._runtime_identity(runtime_instance_id),
            "model_id": _require_identity(model_id, "model_id"),
            "model_bytes_digest": VibeComfyEngine._validate_model_bytes_digest(
                model_bytes_digest
            ),
            "facts_digest": _require_identity(facts_digest, "facts_digest"),
            "source_revision": _require_identity(source_revision, "source_revision"),
            "source_content_digest": _require_identity(
                source_content_digest, "source_content_digest"
            ),
            "config_digest": _require_identity(config_digest, "config_digest"),
        }
    )


def _require_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"checkout_server {field} must be a non-empty string")
    return value.strip()


def _strict_absolute_directory(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"checkout_server {field} must be an absolute directory")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError(f"checkout_server {field} must be an existing non-symlink directory")
    return path


def _verify_owned_vibe_session(session_dir: Path, pid: int) -> None:
    """Verify the daemon, Comfy child, and listener as one owned composite."""
    try:
        import vibecomfy.runtime.session as session_module
        _session_composite_ownership_verified = session_module._session_composite_ownership_verified
    except (ImportError, AttributeError) as exc:
        raise ValueError("checkout_server cannot load VibeComfy ownership verifier") from exc
    from astrid.core.execution.vibecomfy_engine_adapter import EngineAdapterError
    try:
        if sys.platform == "darwin":
            from astrid.core.execution.vibecomfy_engine_adapter import SelectedCompositeObserver
            owned = SelectedCompositeObserver(session_module).verify(session_dir, pid)
        else:
            owned = _session_composite_ownership_verified(session_dir, pid)
    except (OSError, TypeError, ValueError, EngineAdapterError) as exc:
        raise ValueError("checkout_server session ownership verification failed") from exc
    if owned is not True:
        raise ValueError("checkout_server session is not manager-owned")

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


class VibeComfyBackend(BackendAdapter):
    """Local generation backend via pinned VibeComfy ready templates.

    The base adapter has no transport endpoint.  ``CheckoutServerAdapter`` is
    the explicit remote-only subclass and owns all remote HTTP behavior.
    """

    def _run_workflow(self, workflow: Any) -> Any:
        """Run a workflow through the local embedded VibeComfy runtime."""
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
            return run_sync(bundle.compile(), bundle)
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

    def generate(
        self,
        entry: ModelEntry,
        mode: str,
        params: dict[str, Any],
        out_dir: Path,
    ) -> GenerationResult:
        # Lazy-import VibeComfy, and snapshot only its repository
        # template corpus.  Dynamic/user template discovery is forbidden.
        import vibecomfy  # noqa: F401
        from vibecomfy.registry.ready import (
            repo_ready_template_discovery,
            resolve_ready_template,
            workflow_from_ready,
        )

        mode_spec = entry.modes[mode]
        backend_spec: BackendSpec = mode_spec.backends["local"]
        template_id = backend_spec.template

        # --- resolve seed (or generate one) ----------------------------------
        seed_used: int = params.get("seed", 0)

        # --- build param map: canonical → template parameter name ------------
        param_map: dict[str, str] = dict(backend_spec.param_map)
        if not param_map:
            param_map = dict(self.DEFAULT_PARAM_MAP.get(mode, {}))

        # --- derive frame count deterministically ----------------------------
        # If duration is supplied without frames, and fps is known, derive frames
        computed_frames = derive_frames_from_duration(params)
        if computed_frames is not None:
            logger.debug("Computed frames=%d from duration * fps", computed_frames)

        # --- compute applied / dropped feature lists -------------------------
        applied_features, dropped_features = split_feature_support(
            params, mode_spec.supports
        )
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
        expected_hash = str(backend_spec.template_hash)
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", expected_hash):
            raise ValueError(
                f"VibeComfy template {canonical_id!r} has no valid sha256 pin"
            )
        actual_hash = hashlib.sha256(template_path_resolved.read_bytes()).hexdigest()
        if actual_hash.lower() != expected_hash[7:].lower():
            raise ValueError(
                f"VibeComfy template {canonical_id!r} failed its sha256 pin"
            )
        t0 = time.monotonic()
        wf = workflow_from_ready(canonical_id, _discovery=discovery)
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

        unbound_inputs = getattr(wf, "metadata", {}).get("unbound_inputs", {})
        if isinstance(unbound_inputs, dict):
            requested_unbound = sorted(
                tmpl_param
                for canon, tmpl_param in param_map.items()
                if canon not in {"count", "size", "resolution"}
                and canon in params
                and params[canon] is not None
                and tmpl_param in unbound_inputs
            )
            if requested_unbound:
                raise ValueError(
                    f"VibeComfy template {template_id!r} does not declare inputs: "
                    + ", ".join(requested_unbound)
                )

        result = self._run_workflow(wf)
        duration_ms = int((time.monotonic() - t0) * 1000)

        # --- collect outputs -------------------------------------------------
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        image_paths = self._collect_outputs(result, out_dir)

        return GenerationResult(
            image_paths=image_paths,
            seed_used=seed_used,
            model_actual=template_id,
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

    def __init__(self, server_url: str) -> None:
        self._origin = _validate_checkout_server_url(server_url)
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
            raise ValueError(f"checkout_server {field} must be a non-empty string")
        return value

    @staticmethod
    def _runtime_identity(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "checkout_server runtime_instance_id must come from canonical health/bootstrap"
            )
        # Reject synthetic digests and probe tags.
        if value.startswith("probe:"):
            raise ValueError(
                "checkout_server runtime_instance_id must come from canonical health/bootstrap"
            )
        try:
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(
                "checkout_server runtime_instance_id must be a valid UUID from canonical health/bootstrap"
            ) from exc
        return str(parsed)

    @staticmethod
    def _validate_model_bytes_digest(*values: str | None) -> str:
        candidates = [value for value in values if value is not None]
        if not candidates:
            raise ValueError(
                "checkout_server requires model_bytes_digest for production warmth"
            )
        normalized: list[str] = []
        for value in candidates:
            if not isinstance(value, str) or not re.fullmatch(
                r"(?:sha256:)?[0-9a-fA-F]{64}", value
            ):
                raise ValueError(
                    "checkout_server model_bytes_digest must be sha256:<64hex> or raw 64hex"
                )
            normalized.append(
                "sha256:" + value.removeprefix("sha256:").lower()
            )
        if len(set(normalized)) != 1:
            raise ValueError("checkout_server model digest aliases do not match")
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
                raise RuntimeError("checkout_server cannot adopt warmth while busy")
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
                "checkout_server could not prove complete cold reset"
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
                    f"checkout_server {self._operation} is already in progress"
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
                        "checkout_server release returned no explicit backend completion observation"
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
                    f"checkout_server {self._operation} is already in progress"
                )
            if self._running:
                raise RuntimeError("checkout_server run is already in progress")
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
                "checkout_server runtime_instance_id is required from canonical health/bootstrap"
            )
        runtime_instance_id = self._runtime_identity(runtime_instance_id)
        with self._lock:
            if self._operation is not None:
                raise RuntimeError(
                    f"checkout_server {self._operation} is already in progress"
                )
            if self._running:
                raise RuntimeError("checkout_server run is already in progress")
            if self._fence_pending or self._poisoned:
                if not cold:
                    raise RuntimeError(
                        "checkout_server lifecycle is poisoned or fence-pending; "
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
                    raise ValueError("checkout_server could not release incompatible warmth")
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
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = urllib_request.Request(
            f"{self._origin}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _open_checkout_http(request, timeout=10.0) as response:
                status = getattr(response, "status", 200)
                if not 200 <= status < 300:
                    raise ValueError(f"checkout_server {path} returned a non-2xx status")
                raw = response.read(64 * 1024 + 1)
                if len(raw) > 64 * 1024:
                    raise ValueError(f"checkout_server {path} response is too large")
                if not raw:
                    # ComfyUI's control endpoints commonly acknowledge a
                    # successful POST with an empty 2xx body. Preserve that
                    # transport-level completion evidence so managed release
                    # can distinguish it from an unknown payload.
                    return {"acknowledged": True}
                try:
                    return json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return {"acknowledged": True}
        except (OSError, urllib_error.URLError) as exc:
            raise ValueError(f"checkout_server {path} request failed") from exc

    def run(
        self,
        workflow: Any,
        *,
        runtime_instance_id: str | None = None,
        config: Any | None = None,
    ) -> Any:
        """Run one workflow without allowing poisoned lifecycle reuse."""
        with self._lock:
            if self._operation is not None:
                raise RuntimeError(
                    f"checkout_server {self._operation} is already in progress"
                )
            if self._running:
                raise RuntimeError("checkout_server run is already in progress")
            if self._poisoned or self._fence_pending:
                raise RuntimeError(
                    "checkout_server lifecycle is poisoned or fence-pending; "
                    "a proven cold reset is required"
                )
            if runtime_instance_id is None:
                raise ValueError(
                    "checkout_server runtime_instance_id is required from canonical health/bootstrap"
                )
            runtime_instance_id = self._runtime_identity(runtime_instance_id)
            if self._prepared_runtime_instance_id != runtime_instance_id:
                self._poison()
                raise RuntimeError("checkout_server runtime instance changed")
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
                            "checkout_server cannot load the canonical workflow bundle"
                        )
                    bundle = load_bundle(workflow)
                    bundle.require_canonical_authority("runtime execution")
                    runtime_args = (bundle.compile(), bundle)
                else:
                    runtime_args = (workflow,)
            runtime_kwargs: dict[str, Any] = {"server_url": self._origin}
            if config is not None:
                runtime_kwargs["config"] = config
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
                        "checkout_server run completed after its lifecycle fence"
                    )
                self._warm = True
                self._fingerprint = self._prepared_fingerprint
                self._warmth_identity = self._prepared_warmth_identity
                self._model_bytes_digest = self._prepared_model_bytes_digest
                self._runtime_instance_id = self._prepared_runtime_instance_id
                self._clear_prepared()
            return result
        except BaseException:
            with self._lock:
                self._running = False
                # A containment/reset transition may have superseded this
                # run.  Its completion must not alter newer fence state.
                if self._lifecycle_generation == run_generation:
                    self._poison()
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



class CheckoutServerAdapter(VibeComfyBackend):
    """Submit pinned workflows to an already-running host-owned server."""

    def __init__(
        self,
        server_url: str,
        *,
        environment_fingerprint: str = "checkout_server",
        expected_comfyui_version: str | None = None,
    ) -> None:
        # Keep the validated origin private; the base class has no remote path.
        self._origin = _validate_checkout_server_url(server_url)
        self._environment_fingerprint = VibeComfyEngine._identity(
            environment_fingerprint, "environment_fingerprint"
        )
        self._expected_comfyui_version = _normalise_comfyui_constraint(
            expected_comfyui_version
        )
        self._engine = VibeComfyEngine(self._origin)
        self._system_stats_verified = False
        self._runtime_instance_id: str | None = None
        self._startup_probe_digest: str | None = None
        self._host_session: dict[str, Any] | None = None
        self._host_profile: Mapping[str, Any] | None = None
        self._bound_model_id: str | None = None
        self._bound_template_id: str | None = None
        self._invocation_identity: str | None = None
        self._output_root: Path | None = None
        self._bound_fingerprint: str | None = None
        self._bound_warmth_identity: str | None = None
        self._last_run_result: Any | None = None

    @classmethod
    def from_host_session(
        cls,
        *,
        hc03_profile: Mapping[str, Any],
        model_id: str,
        template_id: str,
        invocation_identity: str,
        expected_comfyui_version: str | None = None,
    ) -> "CheckoutServerAdapter":
        """Bind one manager-owned Vibe session to the verified facts.

        ``hc03_profile.vibecomfy_session`` is a deliberately narrow extension
        to the engine-neutral readiness document.  It is produced by the
        manager after reading the Vibe registry and contains the launch marker
        pairing, process incarnation, source revision, configuration digest,
        and listener URL.  A reachable listener without those facts is not an
        admissible checkout server.
        """
        if not isinstance(hc03_profile, Mapping):
            raise ValueError("checkout_server requires a verified VibeComfy readiness profile")
        if hc03_profile.get("schema_version") != "hc03-worker-readiness.v1":
            raise ValueError("checkout_server readiness profile has an unsupported schema")
        if hc03_profile.get("status") != "ready":
            raise ValueError("checkout_server requires a ready VibeComfy readiness profile")
        facts = hc03_profile.get("verified_facts")
        if not isinstance(facts, Mapping):
            raise ValueError("checkout_server readiness profile lacks verified facts")
        exact = facts.get("exact")
        minimum = facts.get("minimum")
        if not isinstance(exact, Mapping) or not isinstance(minimum, Mapping):
            raise ValueError("checkout_server verified facts are malformed")
        expected_exact = {
            "interpreter",
            "runtime_lock",
            "engine_lock",
            "model_digest",
            "custom_node_digest",
            "driver",
            "root",
            "port",
        }
        expected_minimum = {"vram_bytes", "scratch_bytes"}
        if set(exact) != expected_exact or set(minimum) != expected_minimum:
            raise ValueError("checkout_server verified facts are incomplete")
        facts_digest = hc03_profile.get("verified_facts_digest")
        if not isinstance(facts_digest, str) or _canonical_sha256(
            {"exact": dict(sorted(exact.items())), "minimum": dict(sorted(minimum.items()))}
        ) != facts_digest:
            raise ValueError("checkout_server verified facts digest does not match")
        runtime = hc03_profile.get("runtime")
        launch = hc03_profile.get("launch")
        session = hc03_profile.get("vibecomfy_session")
        if not isinstance(runtime, Mapping) or not isinstance(launch, Mapping):
            raise ValueError("checkout_server runtime/launch facts are missing")
        try:
            model_root_binding = model_root_binding_from_profile(
                hc03_profile, verify_files=False
            )
        except ModelRootBindingError as exc:
            raise ValueError(f"checkout_server model-root binding is invalid: {exc}") from exc
        if not isinstance(session, Mapping):
            raise ValueError(
                "checkout_server requires a manager-owned vibecomfy_session binding"
            )
        # The worker profile is itself a digest-bound release contract. Use
        # its version when a legacy caller has not threaded the workflow's
        # typed requirement through the production-engine seam; never infer a
        # version from reachability or silently accept either server version.
        if expected_comfyui_version is None:
            profile_runtime = hc03_profile.get("runtime")
            if isinstance(profile_runtime, Mapping):
                profile_version = profile_runtime.get("comfyui_version")
                if profile_version is not None:
                    expected_comfyui_version = profile_version

        model_name = _require_identity(model_id, "model_id")
        template_name = _require_identity(template_id, "template_id")
        invocation = _require_identity(invocation_identity, "invocation_identity")
        runtime_instance_id = VibeComfyEngine._runtime_identity(
            runtime.get("runtime_instance_id")
        )
        model_bytes_digest = VibeComfyEngine._validate_model_bytes_digest(
            exact.get("model_digest")
        )
        output_root = _strict_absolute_directory(launch.get("output_root"), "output_root")

        session_dir = _strict_absolute_directory(
            session.get("session_dir"), "vibecomfy_session.session_dir"
        )
        server_url = _validate_checkout_server_url(session.get("server_url"))
        pid = session.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError("checkout_server session pid is invalid")
        launch_token = _require_identity(
            session.get("launch_token"), "vibecomfy_session.launch_token"
        )
        process_birth_id = _require_identity(
            session.get("process_birth_id"), "vibecomfy_session.process_birth_id"
        )
        comfy_pid = session.get("comfy_pid")
        if isinstance(comfy_pid, bool) or not isinstance(comfy_pid, int) or comfy_pid <= 0:
            raise ValueError("checkout_server session comfy_pid is invalid")
        comfy_process_birth_id = _require_identity(
            session.get("comfy_process_birth_id"),
            "vibecomfy_session.comfy_process_birth_id",
        )
        source_revision = _require_identity(
            session.get("source_revision"), "vibecomfy_session.source_revision"
        )
        source_content_digest = _require_identity(
            session.get("source_content_digest"),
            "vibecomfy_session.source_content_digest",
        )
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", source_content_digest):
            raise ValueError("checkout_server source_content_digest is not sha256")
        config_digest = _require_identity(
            session.get("config_digest"), "vibecomfy_session.config_digest"
        )
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", config_digest):
            raise ValueError("checkout_server session config_digest is not sha256")

        registry = {
            "pid": session_dir / "pid",
            "comfy_pid": session_dir / "comfy_pid",
            "comfy_process_start_identity": session_dir / "comfy_process_start_identity",
            "url": session_dir / "url",
            "config": session_dir / "config.json",
            "source_revision": session_dir / "source_revision",
            "source_content_digest": session_dir / "source_content_digest",
            "launch": session_dir / "launch.json",
            "daemon_log": session_dir / "daemon.log",
        }
        if any(not path.is_file() or path.is_symlink() for path in registry.values()):
            raise ValueError("checkout_server session registry is incomplete")
        try:
            if registry["pid"].read_text(encoding="utf-8").strip() != str(pid):
                raise ValueError("checkout_server session pid registry does not match")
            if _validate_checkout_server_url(
                registry["url"].read_text(encoding="utf-8").strip()
            ) != server_url:
                raise ValueError("checkout_server session URL registry does not match")
            if registry["source_revision"].read_text(encoding="utf-8").strip() != source_revision:
                raise ValueError("checkout_server session source revision does not match")
            if registry["source_content_digest"].read_text(encoding="utf-8").strip() != source_content_digest:
                raise ValueError("checkout_server session source content digest does not match")
            config_bytes = registry["config"].read_bytes()
            config_digest_observed = "sha256:" + hashlib.sha256(
                config_bytes
            ).hexdigest()
            if config_digest_observed != config_digest:
                raise ValueError("checkout_server session configuration digest does not match")
            session_config = json.loads(config_bytes.decode("utf-8"))
            marker = json.loads(registry["launch"].read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("checkout_server session registry is unreadable") from exc
        if not isinstance(session_config, Mapping):
            raise ValueError("checkout_server session configuration is malformed")
        session_output_directory: Path | None = None
        configured_output_directory = session_config.get("output_directory")
        if configured_output_directory is None:
            raise ValueError(
                "checkout_server session configuration lacks its host-owned output_directory"
            )
        session_output_directory = _strict_absolute_directory(
            configured_output_directory,
            "vibecomfy_session.output_directory",
        )
        if not isinstance(marker, Mapping):
            raise ValueError("checkout_server launch marker is malformed")
        if (
            marker.get("pid") != pid
            or marker.get("url") != server_url
            or marker.get("launch_token") != launch_token
            or marker.get("process_start_identity") != process_birth_id
            or marker.get("comfy_pid") != comfy_pid
            or marker.get("comfy_process_start_identity") != comfy_process_birth_id
        ):
            raise ValueError("checkout_server launch marker does not match the VibeComfy binding")
        observed_source_revision, observed_source_content_digest = (
            _observed_source_identity(source_revision, source_content_digest)
        )
        if (
            observed_source_revision is None
            or observed_source_revision != source_revision
            or observed_source_content_digest is None
            or observed_source_content_digest != source_content_digest
        ):
            raise ValueError("checkout_server source revision does not match the live VibeComfy checkout")
        try:
            if platform.process_identity(pid) is None:
                raise ProcessLookupError(pid)
        except (OSError, ProcessLookupError) as exc:
            raise ValueError("checkout_server session process is not alive") from exc
        _verify_owned_vibe_session(session_dir, pid)

        adapter = cls(
            server_url,
            environment_fingerprint=str(facts_digest),
            expected_comfyui_version=expected_comfyui_version,
        )
        adapter._host_session = {
            "session_dir": str(session_dir),
            "pid": pid,
            "comfy_pid": comfy_pid,
            "server_url": server_url,
            "launch_token": launch_token,
            "process_birth_id": process_birth_id,
            "comfy_process_birth_id": comfy_process_birth_id,
            "source_revision": source_revision,
            "source_content_digest": source_content_digest,
            "config_digest": config_digest,
            "model_bytes_digest": model_bytes_digest,
            "model_root": str(model_root_binding.path),
            "model_root_binding": model_root_binding.as_dict(),
            "output_directory": (
                str(session_output_directory)
                if session_output_directory is not None
                else None
            ),
        }
        adapter._host_profile = hc03_profile
        adapter._bound_model_id = model_name
        adapter._bound_template_id = template_name
        adapter._invocation_identity = invocation
        adapter._runtime_instance_id = runtime_instance_id
        adapter._output_root = output_root
        adapter._bound_fingerprint = adapter.session_fingerprint(
            model_fingerprint=model_name,
            model_bytes_digest=model_bytes_digest,
            environment_fingerprint=str(facts_digest),
            server_url=server_url,
            runtime_instance_id=runtime_instance_id,
            comfyui_version=adapter._expected_comfyui_version,
        )
        adapter._bound_warmth_identity = (
            f"{model_name}:{model_bytes_digest}:{facts_digest}"
        )
        # GenericPackHost passes this only after a prior successful settlement
        # for the exact verified session/process/model binding.  It lets a new
        # command child reuse the server's resident weights without treating a
        # fresh Python wrapper as proof that the server is cold.
        expected_warmth_hint = vibecomfy_warmth_hint(
            session_id=str(session_dir),
            process_birth_id=process_birth_id,
            comfy_process_birth_id=comfy_process_birth_id,
            runtime_instance_id=runtime_instance_id,
            model_id=model_name,
            model_bytes_digest=model_bytes_digest,
            facts_digest=str(facts_digest),
            source_revision=source_revision,
            source_content_digest=source_content_digest,
            config_digest=config_digest,
        )
        if os.environ.get(VIBECOMFY_WARMTH_HINT_ENV) == expected_warmth_hint:
            adapter._engine.adopt_warmth(
                fingerprint=adapter._bound_fingerprint,
                warmth_identity=adapter._bound_warmth_identity,
                model_bytes_digest=model_bytes_digest,
                runtime_instance_id=runtime_instance_id,
            )
        adapter._probe_system_stats()
        return adapter

    def _restart_owned_session(self) -> dict[str, Any]:
        """Restart exactly the owner daemon, then require a fresh binding."""
        binding = self._host_session
        if not isinstance(binding, Mapping):
            raise ValueError("checkout_server restart requires a managed session")
        session_dir = _strict_absolute_directory(binding.get("session_dir"), "session_dir")
        old_pid = int(binding["pid"])
        old_child = int(binding["comfy_pid"])
        old_daemon_identity = str(binding["process_birth_id"])
        old_child_identity = str(binding["comfy_process_birth_id"])
        try:
            owner_config = json.loads((session_dir / "config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("checkout_server owner config is unreadable") from exc
        if not isinstance(owner_config, Mapping):
            raise ValueError("checkout_server owner config is malformed")
        _verify_owned_vibe_session(session_dir, old_pid)
        try:
            stop = subprocess.run(
                _owner_session_command(session_dir, "stop"),
                capture_output=True,
                text=True,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "checkout_server owner stop timed out; session retained for reconciliation"
            ) from exc
        if stop.returncode != 0:
            raise RuntimeError(
                "checkout_server owner stop failed: " + (stop.stderr.strip() or "unknown error")
            )
        for pid, expected in ((old_pid, old_daemon_identity), (old_child, old_child_identity)):
            _assert_process_incarnation_gone(pid, expected)
        try:
            ready_timeout = float(
                owner_config.get("ready_timeout_sec")
                or os.environ.get("VIBECOMFY_SESSION_READY_TIMEOUT_SEC")
                or 300
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("checkout_server owner readiness timeout is invalid") from exc
        if ready_timeout < 0:
            raise RuntimeError("checkout_server owner readiness timeout is invalid")
        try:
            start = subprocess.run(
                _owner_session_command(session_dir, "start", config=owner_config),
                capture_output=True,
                text=True,
                check=False,
                timeout=max(60.0, ready_timeout + 30.0),
            )
        except subprocess.TimeoutExpired as exc:
            # The owner may have detached before the CLI timeout.  Only issue
            # a cleanup stop after a fresh registry and composite ownership
            # proof; otherwise leave markers intact for reconciliation.
            try:
                fresh = _session_registry_binding(session_dir)
                _verify_owned_vibe_session(session_dir, fresh["pid"])
                cleanup = subprocess.run(
                    _owner_session_command(session_dir, "stop"),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=120,
                )
                if cleanup.returncode != 0:
                    raise RuntimeError("owner cleanup stop failed")
            except Exception as cleanup_exc:
                raise RuntimeError(
                    "checkout_server owner start timed out; session retained for reconciliation"
                ) from cleanup_exc
            raise RuntimeError(
                "checkout_server owner start timed out; verified started session stopped"
            ) from exc
        if start.returncode != 0:
            raise RuntimeError(
                "checkout_server owner start/readiness failed: "
                + (start.stderr.strip() or "unknown error")
            )
        fresh = _session_registry_binding(session_dir)
        _verify_owned_vibe_session(session_dir, fresh["pid"])
        if fresh["pid"] == old_pid or fresh["comfy_pid"] == old_child:
            raise RuntimeError("checkout_server restart did not produce a fresh process incarnation")
        source_revision = (session_dir / "source_revision").read_text(encoding="utf-8").strip()
        source_content_digest = (session_dir / "source_content_digest").read_text(encoding="utf-8").strip()
        config_digest = "sha256:" + hashlib.sha256((session_dir / "config.json").read_bytes()).hexdigest()
        if source_revision != binding["source_revision"] or source_content_digest != binding["source_content_digest"] or config_digest != binding["config_digest"]:
            raise RuntimeError("checkout_server restart changed pinned source/config identity")
        updated = {
            **dict(binding),
            "pid": fresh["pid"],
            "comfy_pid": fresh["comfy_pid"],
            "server_url": fresh["server_url"],
            "launch_token": fresh["launch_token"],
            "process_birth_id": fresh["process_birth_id"],
            "comfy_process_birth_id": fresh["comfy_process_birth_id"],
        }
        self._host_session = updated
        if isinstance(self._host_profile, Mapping):
            profile_session = self._host_profile.get("vibecomfy_session")
            if isinstance(profile_session, dict):
                profile_session.update({key: updated[key] for key in ("pid", "comfy_pid", "server_url", "launch_token", "process_birth_id", "comfy_process_birth_id")})
            profile_path = os.environ.get("ASTRID_HOST_READINESS_PROFILE_PATH")
            if profile_path:
                path = Path(profile_path)
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError("checkout_server readiness profile is not an owned regular file")
                profile_bytes = json.dumps(
                    self._host_profile, sort_keys=True, indent=2
                ).encode("utf-8")
                path.write_bytes(profile_bytes)
                os.environ["ASTRID_HOST_READINESS_PROFILE_HASH"] = (
                    "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
                )
        self._origin = fresh["server_url"]
        self._engine = VibeComfyEngine(self._origin)
        self._probe_system_stats()
        return {"ok": True, "released": True, "restarted": True, "old_pid": old_pid, "old_comfy_pid": old_child, "pid": fresh["pid"], "comfy_pid": fresh["comfy_pid"]}

    @property
    def runtime_instance_id(self) -> str | None:
        """Canonical instance identity supplied by the health/bootstrap handshake."""
        return (
            self._engine.runtime_instance_id
            or self._engine.prepared_runtime_instance_id
            or self._runtime_instance_id
        )

    @property
    def poisoned(self) -> bool:
        return self._engine.poisoned

    @property
    def fence_pending(self) -> bool:
        return self._engine.fence_pending

    def _probe_system_stats(self) -> None:
        """Verify the pinned ComfyUI version only.

        Runtime instance identity is supplied by the health/bootstrap
        handshake; a system-stats digest is never an identity fence.
        """
        self._system_stats_verified = False
        request = urllib_request.Request(f"{self._origin}/system_stats", method="GET")
        try:
            with _open_checkout_http(request, timeout=5.0) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise ValueError("checkout_server /system_stats returned a non-200 status")
                body = response.read(64 * 1024 + 1)
                if len(body) > 64 * 1024:
                    raise ValueError("checkout_server /system_stats response is too large")
                payload = json.loads(body.decode("utf-8"))
        except (OSError, urllib_error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("checkout_server /system_stats probe failed") from exc
        if not isinstance(payload, dict):
            raise ValueError("checkout_server /system_stats response is not an object")
        system = payload.get("system")
        observed_version = system.get("comfyui_version") if isinstance(system, dict) else None
        if not isinstance(system, dict) or not _comfyui_version_satisfies(
            observed_version, self._expected_comfyui_version
        ):
            raise ValueError(
                "checkout_server requires ComfyUI "
                f"{self._expected_comfyui_version} according to /system_stats; "
                f"observed {observed_version!r}"
            )
        self._startup_probe_digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._system_stats_verified = True


    @staticmethod
    def session_fingerprint(
        *,
        model_fingerprint: str,
        model_bytes_digest: str,
        environment_fingerprint: str,
        server_url: str,
        runtime_instance_id: str,
        comfyui_version: str = COMFYUI_VERSION,
    ) -> str:
        """Derive a fingerprint bound to model bytes and runtime instance."""
        payload = {
            "schema": "astrid.vibecomfy.session.v2",
            "engine_revision": VIBECOMFY_ENGINE_REVISION,
            "comfyui_version": _normalise_comfyui_constraint(comfyui_version),
            "model": VibeComfyEngine._identity(model_fingerprint, "model_fingerprint"),
            "model_bytes": VibeComfyEngine._validate_model_bytes_digest(model_bytes_digest),
            "environment": VibeComfyEngine._identity(
                environment_fingerprint, "environment_fingerprint"
            ),
            "server": _validate_checkout_server_url(server_url),
            "runtime_instance": VibeComfyEngine._runtime_identity(runtime_instance_id),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

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
        """Fence warmth before probing caller-provided health and identity."""
        fingerprint = VibeComfyEngine._identity(fingerprint, "fingerprint")
        if warmth_identity is not None:
            warmth_identity = VibeComfyEngine._identity(
                warmth_identity, "warmth_identity"
            )
        model_bytes = VibeComfyEngine._validate_model_bytes_digest(
            model_bytes_digest, model_digest, artifact_sha256
        )
        if runtime_instance_id is None:
            raise ValueError(
                "checkout_server runtime_instance_id is required from canonical health/bootstrap"
            )
        instance_id = VibeComfyEngine._runtime_identity(runtime_instance_id)
        with self._engine._lock:
            was_blocked = self._engine._poisoned or self._engine._fence_pending
            requires_cold_reset = self._engine._prepare_for_warm_session()
            try:
                self._control_preflight()
            except BaseException:
                self._engine._poison(cold_reset_verified=False)
                raise
            compatible = self._engine._warm_is_compatible(
                fingerprint,
                warmth_identity,
                model_bytes,
                instance_id,
            )
            retained = self._engine._retention_hint_matches(
                fingerprint,
                warmth_identity,
                model_bytes,
                instance_id,
            )
            if (compatible or retained) and not cold and not was_blocked:
                # Only the probe fence is transient; retain the published
                # snapshot/hint so prepare_session can report reuse or
                # retention permission without claiming GPU-hot residency.
                requires_cold_reset = False
            if not requires_cold_reset:
                self._engine._fence_pending = False
            return self._engine.prepare_session(
                fingerprint,
                warmth_identity,
                runtime_instance_id=instance_id,
                model_bytes_digest=model_bytes,
                cold=cold or requires_cold_reset,
            )


    @staticmethod
    def _model_bytes_digest(
        backend_spec: BackendSpec,
        *supplied: str | None,
    ) -> str:
        """Resolve an exact model artifact digest; fail closed when undeclared."""
        caller_values = [value for value in supplied if value is not None]
        if caller_values:
            return VibeComfyEngine._validate_model_bytes_digest(*caller_values)
        hints = backend_spec.hints
        if isinstance(hints, dict):
            hint_values = [
                hints[key]
                for key in ("model_bytes_digest", "model_digest", "artifact_sha256")
                if key in hints
            ]
            if hint_values:
                return VibeComfyEngine._validate_model_bytes_digest(*hint_values)
        return VibeComfyEngine._validate_model_bytes_digest(None)

    def generate(
        self,
        entry: ModelEntry,
        mode: str,
        params: dict[str, Any],
        out_dir: Path,
        *,
        fingerprint: str | None = None,
        warmth_identity: str | None = None,
        environment_fingerprint: str | None = None,
        model_bytes_digest: str | None = None,
        model_digest: str | None = None,
        artifact_sha256: str | None = None,
        runtime_instance_id: str | None = None,
    ) -> GenerationResult:
        """Generate with disposable warm reuse and host-compatible identities."""
        backend_spec = entry.modes[mode].backends["local"]
        # Ready-template/workflow identity is per invocation.  Keep the
        # resident-session identity at the model level so compatible workflow
        # changes do not unload the same weights.
        model_fingerprint = entry.id
        model_bytes = self._model_bytes_digest(
            backend_spec, model_bytes_digest, model_digest, artifact_sha256
        )
        if runtime_instance_id is None:
            raise ValueError(
                "checkout_server runtime_instance_id is required from canonical health/bootstrap"
            )
        instance_id = VibeComfyEngine._runtime_identity(runtime_instance_id)
        environment = (
            self._environment_fingerprint
            if environment_fingerprint is None
            else VibeComfyEngine._identity(
                environment_fingerprint, "environment_fingerprint"
            )
        )
        canonical_fingerprint = self.session_fingerprint(
            model_fingerprint=model_fingerprint,
            model_bytes_digest=model_bytes,
            environment_fingerprint=environment,
            server_url=self._origin,
            runtime_instance_id=instance_id,
            comfyui_version=self._expected_comfyui_version,
        )
        if fingerprint is not None and fingerprint != canonical_fingerprint:
            raise ValueError(
                "checkout_server fingerprint must equal canonical session fingerprint"
            )
        effective_fingerprint = canonical_fingerprint
        if warmth_identity is None:
            warmth_identity = f"{model_fingerprint}:{model_bytes}:{environment}"
        try:
            self.warm_session(
                effective_fingerprint,
                warmth_identity,
                runtime_instance_id=instance_id,
                model_bytes_digest=model_bytes,
            )
            return super().generate(entry, mode, params, out_dir)
        except BaseException:
            self._engine._abort_preparation()
            raise

    def _revalidate_host_session(self) -> None:
        """Recheck the manager binding immediately before native admission."""
        binding = self._host_session
        if binding is None:
            raise ValueError("checkout_server has no verified host-session binding")
        session_dir = Path(str(binding["session_dir"]))
        pid = binding["pid"]
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("checkout_server host-session pid is invalid")
        comfy_pid = binding.get("comfy_pid")
        if not isinstance(comfy_pid, int) or comfy_pid <= 0:
            raise ValueError("checkout_server host-session comfy pid is invalid")
        try:
            if platform.process_identity(pid) is None:
                raise ProcessLookupError(pid)
            if platform.process_identity(comfy_pid) is None:
                raise ProcessLookupError(comfy_pid)
            marker = json.loads(
                (session_dir / "launch.json").read_text(encoding="utf-8")
            )
            observed_comfy_birth_id = (
                session_dir / "comfy_process_start_identity"
            ).read_text(encoding="utf-8").strip()
            url = (session_dir / "url").read_text(encoding="utf-8").strip()
            source_revision = (
                session_dir / "source_revision"
            ).read_text(encoding="utf-8").strip()
            source_content_digest = (
                session_dir / "source_content_digest"
            ).read_text(encoding="utf-8").strip()
            config_digest = "sha256:" + hashlib.sha256(
                (session_dir / "config.json").read_bytes()
            ).hexdigest()
        except (OSError, ProcessLookupError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("checkout_server host session is no longer live") from exc
        if not isinstance(marker, Mapping) or any(
            marker.get(key) != value
            for key, value in (
                ("pid", pid),
                ("url", binding["server_url"]),
                ("launch_token", binding["launch_token"]),
                ("process_start_identity", binding["process_birth_id"]),
                ("comfy_pid", comfy_pid),
                ("comfy_process_start_identity", binding["comfy_process_birth_id"]),
            )
        ):
            raise ValueError("checkout_server host-session ownership changed")
        if (
            _validate_checkout_server_url(url) != binding["server_url"]
            or source_revision != binding["source_revision"]
            or source_content_digest != binding["source_content_digest"]
            or config_digest != binding["config_digest"]
            or observed_comfy_birth_id != binding["comfy_process_birth_id"]
        ):
            raise ValueError("checkout_server host-session registry identity changed")
        observed_source_revision, observed_source_content_digest = (
            _observed_source_identity(source_revision, source_content_digest)
        )
        if (
            observed_source_revision is None
            or observed_source_revision != source_revision
            or observed_source_content_digest is None
            or observed_source_content_digest != source_content_digest
        ):
            raise ValueError("checkout_server live source revision changed")
        _verify_owned_vibe_session(session_dir, pid)

    def run_compiled_workflow(
        self,
        workflow: Any,
        out_dir: Path,
        *,
        task_identity: str | None = None,
        attempt_identity: str | None = None,
        run_inputs: Mapping[str, Any] | None = None,
    ) -> list[Path]:
        """Compile one canonical workflow, run it, and custody private outputs."""
        if self._bound_fingerprint is None or self._bound_warmth_identity is None:
            raise ValueError("checkout_server has no verified workflow binding")
        if self._runtime_instance_id is None:
            raise ValueError("checkout_server has no verified runtime identity")
        if self._output_root is None:
            raise ValueError("checkout_server has no verified output root")
        if not isinstance(out_dir, Path):
            out_dir = Path(out_dir)
        if out_dir.is_symlink():
            raise ValueError("checkout_server output directory must not be a symlink")
        destination = out_dir.resolve()
        try:
            destination.relative_to(self._output_root)
        except ValueError as exc:
            raise ValueError("checkout_server output directory escaped the VibeComfy output root") from exc
        destination.mkdir(parents=True, exist_ok=True)
        self._last_run_result = None

        try:
            self._revalidate_host_session()
            host_session = self._host_session
            if host_session is None:
                raise ValueError("checkout_server has no verified host-session binding")
            from vibecomfy.workflow import VibeWorkflow
            from vibecomfy.workflow_bundle import WorkflowBundle, load_bundle

            if isinstance(workflow, WorkflowBundle):
                bundle = workflow
            elif isinstance(workflow, VibeWorkflow):
                bundle = load_bundle(workflow)
            else:
                raise ValueError(
                    "checkout_server requires a canonical VibeComfy loader result"
                )
            bundle.require_canonical_authority("checkout_server execution")
            metadata = getattr(bundle.workflow, "metadata", {})
            # Template/workflow identity is per invocation.  The stable
            # session binding is the model/runtime identity; a compatible
            # workflow may therefore change without unloading resident
            # weights.  If canonical metadata declares a model, still fail
            # closed when it disagrees with the session's model set.
            metadata_model = (
                metadata.get("model_id") if isinstance(metadata, Mapping) else None
            )
            if metadata_model is not None and str(metadata_model) != self._bound_model_id:
                raise ValueError("checkout_server workflow model binding changed")
            # The selected checkout server is the schema authority for this
            # execution.  Do not let an offline object-info cache reject a
            # node that is demonstrably installed on the live Comfy target;
            # the target provider still records the fresh object-info digest
            # and the bundle's lock/provenance gates remain in force.
            from vibecomfy.schema import get_target_schema_provider

            target_schema = get_target_schema_provider(server_url=self._origin)
            refresh = getattr(target_schema, "refresh", None)
            if callable(refresh):
                refresh()
            # The VibeComfy release owns the model volume.  The live Comfy session
            # reads it through extra_model_paths.yaml, but bundle approval
            # performs its own local asset preflight and must be given the
            # same verified root explicitly.  Without this, a worker with an
            # otherwise healthy session falls back to VibeComfy's empty
            # default models directory and falsely reports a missing model.
            try:
                bound_model_root = validate_model_root_binding(
                    host_session.get("model_root_binding"),
                    verify_files=False,
                )
            except ModelRootBindingError as exc:
                raise ValueError(f"checkout_server model-root binding is invalid: {exc}") from exc
            environment_model_root = os.environ.get(MODEL_ROOT_ENV)
            if environment_model_root is not None and environment_model_root != str(bound_model_root.path):
                raise ValueError(
                    "checkout_server environment model root disagrees with its VibeComfy binding"
                )
            compile_kwargs: dict[str, Any] = {
                "schema_provider": target_schema,
                "models_root": str(bound_model_root.path),
            }
            if run_inputs is not None:
                if not isinstance(run_inputs, Mapping):
                    raise ValueError("checkout_server workflow run inputs must be an object")
                compile_kwargs["run_inputs"] = dict(run_inputs)
            with use_attested_vibecomfy_models_root(bundle, bound_model_root.path):
                approved = bundle.compile(**compile_kwargs)
            model_bytes_digest = self._engine._validate_model_bytes_digest(
                self._bound_host_model_digest()
            )
            from vibecomfy.runtime.session import SessionConfig

            config_values: dict[str, Any] = {}
            workflow_config = (
                metadata.get("comfy_configuration")
                if isinstance(metadata, Mapping)
                else None
            )
            if isinstance(workflow_config, Mapping):
                config_values.update(dict(workflow_config))
            # The running Comfy process owns its output root. Carry that
            # manager-verified fact into every external runtime invocation so
            # artifact verification can recognize a shared RunPod filesystem
            # and probe the actual file before delivery. A workflow may not
            # redirect a host-owned checkout session to an arbitrary directory.
            host_output_directory = host_session.get("output_directory")
            if not isinstance(host_output_directory, str) or not host_output_directory:
                raise ValueError(
                    "checkout_server has no verified host-owned output directory"
                )
            configured_workflow_output = config_values.get("output_directory")
            if (
                configured_workflow_output is not None
                and Path(str(configured_workflow_output)).expanduser().resolve()
                != Path(host_output_directory).resolve()
            ):
                raise ValueError(
                    "checkout_server workflow output_directory does not match "
                    "the host-owned Comfy session output directory"
                )
            config_values["output_directory"] = host_output_directory
            if task_identity is not None:
                config_values["task_id"] = task_identity
            if attempt_identity is not None:
                config_values["attempt_id"] = attempt_identity
            runtime_config = SessionConfig.from_dict(config_values)

            # VibeComfy's dynamic environment layer normally has precedence
            # over SessionConfig.extra. Reject a conflicting environment root
            # and freeze the verified host root into the effective environment
            # for this invocation so the two artifact-resolution paths cannot
            # disagree.
            environment_key = "VIBECOMFY_COMFY_CONFIGURATION"
            previous_environment_configuration = os.environ.get(environment_key)
            environment_values: dict[str, Any] = {}
            if previous_environment_configuration:
                try:
                    parsed_environment = json.loads(previous_environment_configuration)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "checkout_server environment configuration is not valid JSON"
                    ) from exc
                if not isinstance(parsed_environment, dict):
                    raise ValueError(
                        "checkout_server environment configuration must be an object"
                    )
                environment_values.update(parsed_environment)
            configured_environment_output = environment_values.get("output_directory")
            if (
                configured_environment_output is not None
                and Path(str(configured_environment_output)).expanduser().resolve()
                != Path(host_output_directory).resolve()
            ):
                raise ValueError(
                    "checkout_server environment output_directory does not match "
                    "the host-owned Comfy session output directory"
                )
            environment_values["output_directory"] = host_output_directory

            self.warm_session(
                self._bound_fingerprint,
                self._bound_warmth_identity,
                runtime_instance_id=self._runtime_instance_id,
                model_bytes_digest=model_bytes_digest,
            )
            try:
                os.environ[environment_key] = json.dumps(
                    environment_values, sort_keys=True, separators=(",", ":")
                )
                result = self._run_workflow((approved, bundle), config=runtime_config)
            finally:
                if previous_environment_configuration is None:
                    os.environ.pop(environment_key, None)
                else:
                    os.environ[environment_key] = previous_environment_configuration
            outputs = self._collect_outputs(result, destination)
            self._last_run_result = result
            return outputs
        except BaseException:
            self._engine._abort_preparation()
            raise

    def _bound_host_model_digest(self) -> str:
        binding = self._host_session
        if binding is None:
            raise ValueError("checkout_server has no host-session model binding")
        digest = binding.get("model_bytes_digest")
        if not isinstance(digest, str):
            raise ValueError("checkout_server host-session model digest is missing")
        return digest

    def cancel(self, frame: object | None = None) -> dict[str, Any]:
        """Fence native work before probing or settling host cancellation."""
        return self._engine.cancel(frame, preflight=self._control_preflight)

    def release(
        self, frame: object | None = None, *, reason: str = "requested"
    ) -> dict[str, Any]:
        """Fence native state before probing and mark the adapter cold."""
        if (
            self._host_session is not None
            and reason == "capacity_replacement"
            and isinstance(self._host_session.get("session_dir"), str)
        ):
            del frame
            self._engine._clear_warm()
            self._engine._clear_prepared()
            self._engine._clear_retention_hint()
            self._control_preflight()
            return self._restart_owned_session()
        return self._engine.release(
            frame,
            reason=reason,
            preflight=self._control_preflight,
            require_completion=self._host_session is not None,
        )

    def _control_preflight(self) -> None:
        """Verify ownership immediately before destructive control calls."""
        if self._host_session is not None:
            self._revalidate_host_session()
        self._probe_system_stats()

    def _run_workflow(
        self, workflow: Any, *, config: Any | None = None
    ) -> Any:
        """Probe version and submit with the canonical runtime identity."""
        self._origin = _validate_checkout_server_url(self._origin)
        self._probe_system_stats()
        instance_id = self._engine.prepared_runtime_instance_id
        if instance_id is None:
            instance_id = self._engine.runtime_instance_id
        if instance_id is None:
            raise ValueError(
                "checkout_server runtime_instance_id is required from canonical health/bootstrap"
            )
        return self._engine.run(
            workflow, runtime_instance_id=instance_id, config=config
        )

    def _collect_outputs(self, result: Any, out_dir: Path) -> list[Path]:
        """Custody remote outputs through validated Comfy ``/view`` downloads."""
        metadata_path = getattr(result, "metadata_path", None)
        if not isinstance(metadata_path, (str, Path)) or not str(metadata_path):
            raise ValueError("checkout_server result has no metadata_path")
        try:
            metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("checkout_server result metadata is unreadable") from exc
        outputs = metadata.get("comfy_outputs") if isinstance(metadata, dict) else None
        descriptors = _extract_output_descriptors(outputs)
        if not descriptors:
            raise ValueError("checkout_server result metadata has no comfy_outputs")

        image_paths: list[Path] = []
        for descriptor in descriptors:
            filename, subfolder, output_type = _validate_output_descriptor(descriptor)
            query = urllib_parse.urlencode(
                {"filename": filename, "subfolder": subfolder, "type": output_type}
            )
            request = urllib_request.Request(
                f"{self._origin}/view?{query}", method="GET"
            )
            destination = out_dir / filename
            if destination.exists():
                stem = destination.stem
                suffix = destination.suffix
                counter = 1
                while destination.exists():
                    destination = out_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
            expected_shared_digest: str | None = None
            if output_type == "output" and self._host_session is not None:
                host_output_directory = self._host_session.get("output_directory")
                if isinstance(host_output_directory, str):
                    shared_root = Path(host_output_directory).resolve(strict=True)
                    shared_path = (shared_root / subfolder / filename).resolve(strict=True)
                    try:
                        shared_path.relative_to(shared_root)
                    except ValueError as exc:
                        raise ValueError(
                            "checkout_server output descriptor escaped the host-owned output directory"
                        ) from exc
                    expected_shared_digest = _file_sha256(shared_path)
            temporary_path: Path | None = None
            try:
                with _open_checkout_http(request, timeout=30.0) as response:
                    status = getattr(response, "status", 200)
                    if status != 200:
                        raise ValueError("checkout_server /view returned a non-200 status")
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=out_dir,
                        prefix=".checkout-download-",
                        delete=False,
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                        shutil.copyfileobj(response, temporary)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                os.replace(temporary_path, destination)
                temporary_path = None
                if (
                    expected_shared_digest is not None
                    and _file_sha256(destination) != expected_shared_digest
                ):
                    destination.unlink(missing_ok=True)
                    raise ValueError(
                        f"checkout_server downloaded output {filename!r} changed during custody"
                    )
            except (OSError, urllib_error.URLError) as exc:
                raise ValueError(
                    f"checkout_server could not download output {filename!r}"
                ) from exc
            finally:
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
            image_paths.append(destination)
        return image_paths
