"""CPU-side validation for one concrete VibeComfy invocation.

This module deliberately validates the invocation rather than only the bundle.
The bundle can be structurally valid while a task-bound filename, prompt, or
source audiovisual prefix is not executable on the target.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any


class InvocationPreflightError(ValueError):
    """The concrete task-bound workflow or media input is not safe to run."""


@dataclass(frozen=True, slots=True)
class H3SourceAVMeasurement:
    """Decoded source measurements used by the H3 AV timing contract."""

    path: str
    video_frames: int
    fps: int
    audio_samples: int
    audio_sample_rate: int
    expected_audio_samples: int
    fractional_mismatch: float
    has_audio: bool
    source_frame_rate: str = "24/1"
    source_avg_frame_rate: str = "24/1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_frames": self.video_frames,
            "fps": self.fps,
            "audio_samples": self.audio_samples,
            "audio_sample_rate": self.audio_sample_rate,
            "expected_audio_samples": self.expected_audio_samples,
            "fractional_mismatch": self.fractional_mismatch,
            "has_audio": self.has_audio,
            "source_frame_rate": self.source_frame_rate,
            "source_avg_frame_rate": self.source_avg_frame_rate,
        }


_MEDIA_FIELDS = frozenset({
    "audio", "image", "mask", "video", "videopreview", "filename",
})
_MEDIA_INPUT_NAMES = frozenset({"source_video"})
_VIDEO_CLASS_MARKERS = ("VHS_LoadVideo", "LoadVideo")
_UNRESOLVED_PLACEHOLDER = re.compile(
    r"(?:\{\{[^{}]+\}\}|\$\{[^{}]+\}|<(?:(?:PROMPT|SOURCE|TODO)[^>]*)>|__[A-Z][A-Z0-9_]+__)",
    re.IGNORECASE,
)


def _run_checked(argv: list[str], *, label: str) -> bytes:
    executable = shutil.which(argv[0])
    if executable is None:
        raise InvocationPreflightError(
            f"{label}: required executable {argv[0]!r} is not installed"
        )
    argv = [executable, *argv[1:]]
    completed = subprocess.run(argv, capture_output=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise InvocationPreflightError(
            f"{label}: {detail or 'decoder exited unsuccessfully'}"
        )
    return completed.stdout


def measure_source_av(
    path: str | Path,
    *,
    fps: int = 24,
    audio_sample_rate: int = 32000,
    max_fractional_mismatch: float = 0.005,
) -> H3SourceAVMeasurement:
    """Validate and decode the exact submitted bytes with H3 timing assumptions."""
    source = Path(path).expanduser().resolve()
    if source.is_symlink() or not source.is_file():
        raise InvocationPreflightError(
            f"H3_SOURCE_AV_MISSING: source is not a regular file: {source}"
        )
    if fps <= 0 or audio_sample_rate <= 0:
        raise InvocationPreflightError("H3_SOURCE_AV_INVALID: timing parameters must be positive")

    probe = _run_checked(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate,avg_frame_rate",
            "-of", "json", str(source),
        ],
        label="H3_SOURCE_AV_PROBE",
    )
    try:
        streams = json.loads(probe.decode("utf-8")).get("streams", [])
        stream = streams[0] if isinstance(streams, list) and streams else None
        source_rate = str(stream.get("r_frame_rate")) if isinstance(stream, Mapping) else ""
        average_rate = str(stream.get("avg_frame_rate")) if isinstance(stream, Mapping) else ""
        parsed_source_rate = Fraction(source_rate)
        parsed_average_rate = Fraction(average_rate)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InvocationPreflightError(
            "H3_SOURCE_AV_RATE: ffprobe did not return usable video frame-rate metadata"
        ) from exc
    if parsed_source_rate != parsed_average_rate or parsed_source_rate != Fraction(int(fps), 1):
        raise InvocationPreflightError(
            "H3_SOURCE_AV_RATE: H3 requires constant "
            f"{fps} fps input (source_fps={source_rate}, average_fps={average_rate}). "
            "Normalize the submitted source to CFR before task admission."
        )

    # One 2x2 grayscale frame is enough to count decoded frames without
    # materializing the source video in memory. passthrough is important:
    # applying fps=24 here would validate a hypothetical filtered stream.
    frame_bytes = _run_checked(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-fps_mode", "passthrough", "-an", "-f", "rawvideo",
            "-pix_fmt", "gray", "-s", "2x2", "-",
        ],
        label="H3_SOURCE_AV_VIDEO_DECODE",
    )
    frame_size = 4
    if len(frame_bytes) % frame_size:
        raise InvocationPreflightError(
            "H3_SOURCE_AV_VIDEO_DECODE: decoder returned a partial frame"
        )
    video_frames = len(frame_bytes) // frame_size
    if video_frames <= 0:
        raise InvocationPreflightError("H3_SOURCE_AV_EMPTY: source contains no decoded video frames")

    expected_audio_samples = int(
        round(video_frames / float(fps) * int(audio_sample_rate))
    )
    duration = video_frames / float(fps)
    try:
        audio_bytes = _run_checked(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
                "-t", f"{duration:.12g}", "-map", "0:a:0", "-vn", "-ac", "2",
                "-ar", str(int(audio_sample_rate)), "-f", "f32le", "-",
            ],
            label="H3_SOURCE_AV_AUDIO_DECODE",
        )
    except InvocationPreflightError as exc:
        if "matches no streams" in str(exc).lower() or "stream map" in str(exc).lower():
            raise InvocationPreflightError(
                "H3_SOURCE_AV_MISSING_AUDIO: speaking-prefix tasks require an audio stream"
            ) from exc
        raise
    bytes_per_sample = 4 * 2  # f32le stereo
    if len(audio_bytes) % bytes_per_sample:
        raise InvocationPreflightError(
            "H3_SOURCE_AV_AUDIO_DECODE: decoder returned a partial stereo sample"
        )
    audio_samples = len(audio_bytes) // bytes_per_sample
    fractional_mismatch = abs(expected_audio_samples - audio_samples) / float(
        expected_audio_samples
    )
    if fractional_mismatch > float(max_fractional_mismatch):
        raise InvocationPreflightError(
            "H3_SOURCE_AV_MISMATCH: "
            f"{video_frames} frames at {fps} fps require {expected_audio_samples} "
            f"samples at {audio_sample_rate} Hz; decoded {audio_samples}; "
            f"mismatch {fractional_mismatch * 100:.3f}%, limit "
            f"{max_fractional_mismatch * 100:.3f}%. Re-extract an aligned source clip."
        )
    if audio_samples <= 0:
        raise InvocationPreflightError(
            "H3_SOURCE_AV_MISSING_AUDIO: speaking-prefix tasks require an audio stream"
        )
    return H3SourceAVMeasurement(
        path=str(source),
        video_frames=video_frames,
        fps=int(fps),
        audio_samples=audio_samples,
        audio_sample_rate=int(audio_sample_rate),
        expected_audio_samples=expected_audio_samples,
        fractional_mismatch=fractional_mismatch,
        has_audio=True,
        source_frame_rate=source_rate,
        source_avg_frame_rate=average_rate,
    )


def _reachable_node_ids(workflow: Any) -> set[str]:
    outputs = getattr(workflow, "outputs", ()) or ()
    sinks = {str(output.node_id) for output in outputs if getattr(output, "node_id", None)}
    incoming: dict[str, set[str]] = {}
    for edge in getattr(workflow, "edges", ()) or ():
        incoming.setdefault(str(edge.to_node), set()).add(str(edge.from_node))
    reachable: set[str] = set()
    pending = list(sinks)
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(incoming.get(node_id, ()))
    return reachable


def _reachable_inputs(workflow: Any, api: Mapping[str, Any], field: str) -> list[Any]:
    reachable = _reachable_node_ids(workflow)
    values: list[Any] = []
    for node_id in sorted(reachable):
        node = api.get(node_id)
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), Mapping):
            continue
        if field in node["inputs"]:
            values.append(node["inputs"][field])
    return values


_PROMPT_NODE_MARKERS = ("CLIPTextEncode", "TextEncode", "Prompt")


def _prompt_values(workflow: Any, api: Mapping[str, Any], *, reachable_only: bool) -> list[Any]:
    """Read prompt-bearing fields across API and UI naming conventions."""

    node_ids = _reachable_node_ids(workflow) if reachable_only else {
        str(node_id) for node_id in api
    }
    values: list[Any] = []
    for node_id in sorted(node_ids):
        node = api.get(node_id)
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), Mapping):
            continue
        inputs = node["inputs"]
        class_type = str(node.get("class_type") or "")
        if "prompt" in inputs:
            values.append(inputs["prompt"])
        if "text" in inputs and any(marker.lower() in class_type.lower() for marker in _PROMPT_NODE_MARKERS):
            values.append(inputs["text"])
    return values


def _declares_prompt_input(workflow: Any) -> bool:
    """True when the entry's public contract declares a prompt the caller must fill.

    ``node:<id>.<field>`` names are not declarations: the bundle registers them
    automatically for every schema literal (Whisper's optional ``prompt`` among
    them), so only an author-named input counts.
    """
    public_inputs = getattr(workflow, "inputs", None) or {}
    return any(
        str(name) == "prompt"
        or (not str(name).startswith("node:") and str(getattr(spec, "field", "") or "") == "prompt")
        for name, spec in public_inputs.items()
    )


def _required_prompt_values(workflow: Any, api: Mapping[str, Any], *, declared: bool) -> list[Any]:
    """Prompt values a prompt-driven workflow must fill.

    A ``prompt`` field only counts when its node is prompt-bearing (a text
    encoder or prompt node) or the entry declares a prompt input. Optional
    hints on other nodes, such as Whisper's ``initial_prompt``, are not
    requirements: an empty hint is the node's default, not a missing prompt.
    """
    values: list[Any] = []
    for node_id in sorted(_reachable_node_ids(workflow)):
        node = api.get(node_id)
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), Mapping):
            continue
        inputs = node["inputs"]
        class_type = str(node.get("class_type") or "")
        prompt_node = any(marker.lower() in class_type.lower() for marker in _PROMPT_NODE_MARKERS)
        if "prompt" in inputs and (prompt_node or declared):
            values.append(inputs["prompt"])
        if "text" in inputs and prompt_node:
            values.append(inputs["text"])
    return values


def _all_inputs(api: Mapping[str, Any], field: str) -> list[Any]:
    values: list[Any] = []
    for node in api.values():
        if not isinstance(node, Mapping) or not isinstance(node.get("inputs"), Mapping):
            continue
        if field in node["inputs"]:
            values.append(node["inputs"][field])
    return values


def _assert_no_unresolved_placeholders(api: Mapping[str, Any]) -> None:
    """Reject unresolved template tokens anywhere in the compiled graph."""

    def visit(value: Any, path: str) -> None:
        if isinstance(value, str):
            if _UNRESOLVED_PLACEHOLDER.search(value):
                code = (
                    "VIBECOMFY_PROMPT_UNRESOLVED"
                    if path.endswith(".prompt")
                    else "VIBECOMFY_PLACEHOLDER_UNRESOLVED"
                )
                raise InvocationPreflightError(
                    f"{code}: "
                    f"compiled workflow contains an unresolved placeholder at {path}"
                )
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                visit(child, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(api, "workflow")


def _assert_no_reachable_blank_media(workflow: Any, api: Mapping[str, Any]) -> None:
    reachable = _reachable_node_ids(workflow)
    for node_id in sorted(reachable):
        node = api.get(node_id)
        if not isinstance(node, Mapping):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        for field, value in inputs.items():
            if value not in (None, "") or str(field) not in _MEDIA_FIELDS:
                continue
            raise InvocationPreflightError(
                "VIBECOMFY_REACHABLE_BLANK_MEDIA: "
                f"node {node_id} ({class_type}) has blank media input {field!r}"
            )


def _assert_reachable_prompt(
    workflow: Any,
    api: Mapping[str, Any],
    *,
    expected_prompt: str | None,
) -> None:
    values = _prompt_values(workflow, api, reachable_only=True)
    all_values = _prompt_values(workflow, api, reachable_only=False)
    if all_values and not values:
        raise InvocationPreflightError(
            "VIBECOMFY_PROMPT_DISCONNECTED: prompt inputs do not reach a workflow output"
        )
    if not values:
        if expected_prompt is not None:
            raise InvocationPreflightError(
                "VIBECOMFY_PROMPT_BINDING: expected prompt has no reachable prompt input"
            )
        return
    for value in values:
        if isinstance(value, str) and _UNRESOLVED_PLACEHOLDER.search(value):
            raise InvocationPreflightError(
                "VIBECOMFY_PROMPT_UNRESOLVED: reachable prompt contains an unresolved placeholder"
            )
    if expected_prompt is not None:
        if not isinstance(expected_prompt, str) or not expected_prompt.strip():
            raise InvocationPreflightError("VIBECOMFY_PROMPT_EMPTY: expected prompt is empty")
        if expected_prompt not in values:
            raise InvocationPreflightError(
                "VIBECOMFY_PROMPT_BINDING: expected prompt is not present in a reachable prompt node"
            )
    else:
        required = _required_prompt_values(
            workflow, api, declared=_declares_prompt_input(workflow)
        )
        if required and not any(isinstance(value, str) and value.strip() for value in required):
            raise InvocationPreflightError(
                "VIBECOMFY_PROMPT_EMPTY: every reachable prompt input is empty"
            )


def _is_h3_workflow(api: Mapping[str, Any]) -> bool:
    classes = {
        str(node.get("class_type") or "")
        for node in api.values()
        if isinstance(node, Mapping)
    }
    return any("H3" in value for value in classes) and any(
        any(marker in value for marker in _VIDEO_CLASS_MARKERS) for value in classes
    )


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preflight_invocation(
    workflow_path: str | Path,
    *,
    run_inputs: Mapping[str, Any] | None = None,
    comfy_url: str = "",
    expected_prompt: str | None = None,
    source_video_path: str | Path | None = None,
    expected_source_digest: str | None = None,
    expected_source_node: str | None = None,
    expected_source_field: str | None = None,
    phase: str = "submission",
) -> dict[str, Any]:
    """Validate one exact workflow invocation and return a bound receipt."""
    source = Path(workflow_path).expanduser().resolve(strict=True)
    bindings = dict(run_inputs or {})
    if not all(isinstance(key, str) and key.strip() for key in bindings):
        raise InvocationPreflightError("workflow run input names must be non-empty strings")

    from astrid.packs.vibecomfy.production_engine import load_workflow_path

    with tempfile.TemporaryDirectory(prefix="astrid-invocation-preflight-") as raw_scratch:
        loaded = load_workflow_path(source, Path(raw_scratch), comfy_url=comfy_url)
    bundle = loaded.resolved
    workflow = bundle.workflow
    public_inputs = getattr(workflow, "inputs", {})
    for name in bindings:
        if name not in public_inputs:
            raise InvocationPreflightError(
                f"VIBECOMFY_UNKNOWN_RUN_INPUT: workflow has no public input {name!r}"
            )
        spec = public_inputs[name]
        media_semantics = str(getattr(spec, "media_semantics", "") or "").lower()
        is_media = media_semantics in {"audio", "image", "mask", "video"} or name in _MEDIA_INPUT_NAMES
        value = bindings[name]
        if is_media:
            if not isinstance(value, str) or not value.strip() or Path(value).name != value:
                raise InvocationPreflightError(
                    "VIBECOMFY_MEDIA_BINDING: media workflow inputs must be safe non-empty basenames"
                )
        elif not isinstance(value, (str, int, float, bool, list, dict)) or (
            isinstance(value, float) and not math.isfinite(value)
        ):
            raise InvocationPreflightError(
                f"VIBECOMFY_INPUT_BINDING: workflow input {name!r} is not JSON-safe"
            )
        else:
            try:
                json.dumps(value, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise InvocationPreflightError(
                    f"VIBECOMFY_INPUT_BINDING: workflow input {name!r} is not JSON-safe"
                ) from exc
    source_input = public_inputs.get("source_video")
    if "source_video" in bindings:
        if source_input is None:
            raise InvocationPreflightError(
                "VIBECOMFY_SOURCE_BINDING: source_video is not declared by the workflow"
            )
        if expected_source_node is not None and str(source_input.node_id) != str(expected_source_node):
            raise InvocationPreflightError(
                "VIBECOMFY_SOURCE_BINDING: declared source node does not match the workflow contract"
            )
        if expected_source_field is not None and str(source_input.field) != str(expected_source_field):
            raise InvocationPreflightError(
                "VIBECOMFY_SOURCE_BINDING: declared source field does not match the workflow contract"
            )

    try:
        api = workflow.compile("api", run_inputs=bindings)
    except Exception as exc:  # noqa: BLE001 - normalize authoring boundary
        raise InvocationPreflightError(
            f"VIBECOMFY_API_COMPILE: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(api, Mapping):
        raise InvocationPreflightError("VIBECOMFY_API_COMPILE: projection is not an object")
    _assert_no_unresolved_placeholders(api)
    if "source_video" in bindings:
        node = api.get(str(source_input.node_id))
        actual = node.get("inputs", {}).get(str(source_input.field)) if isinstance(node, Mapping) else None
        if actual != bindings["source_video"]:
            raise InvocationPreflightError(
                "VIBECOMFY_SOURCE_BINDING: compiled source_video value does not match staged basename"
            )
    _assert_reachable_prompt(workflow, api, expected_prompt=expected_prompt)
    _assert_no_reachable_blank_media(workflow, api)

    if source_video_path is not None and expected_source_digest is not None:
        source_bytes = Path(source_video_path).read_bytes()
        actual_digest = "sha256:" + hashlib.sha256(source_bytes).hexdigest()
        if actual_digest != expected_source_digest:
            raise InvocationPreflightError(
                "VIBECOMFY_SOURCE_DIGEST: staged source bytes do not match the declared digest"
            )

    receipt: dict[str, Any] = {
        "schema_version": 1,
        "phase": phase,
        "workflow_identity": str(bundle.workflow_identity),
        "revision_id": str(bundle.revision_id),
        "semantic_digest": str(bundle.semantic_digest),
        "workflow_content_digest": str(loaded.workflow_content_digest),
        "api_digest": _canonical_digest(api),
        "run_inputs": bindings,
        "reachable_nodes": sorted(_reachable_node_ids(workflow)),
    }
    if expected_prompt is not None:
        receipt["expected_prompt"] = expected_prompt
    if source_video_path is not None and _is_h3_workflow(api):
        receipt["source_av"] = measure_source_av(source_video_path).to_dict()
    return receipt


class InvocationReceiptMismatch(InvocationPreflightError):
    """A receipt is missing, malformed, or does not describe the admitted bytes."""

    code = "VIBECOMFY_INVOCATION_RECEIPT_MISMATCH"

    def __init__(self, field: str, *, admitted: str, receipt: str) -> None:
        self.field = field
        super().__init__(f"{self.code}: {field} admitted={admitted} receipt={receipt}")


def _short_receipt_digest(value: Any) -> str:
    if isinstance(value, str):
        bare = value.removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-f]{64}", bare):
            return bare[:12]
    return "<missing>" if value is None else "<invalid>"


def _compare_receipt_digest(receipt: Mapping[str, Any], field: str, actual: str) -> None:
    recorded = receipt.get(field)
    if recorded != actual:
        raise InvocationReceiptMismatch(
            field, admitted=_short_receipt_digest(actual), receipt=_short_receipt_digest(recorded)
        )


def invocation_run_inputs(value: Any) -> dict[str, Any]:
    """Decode the public invocation bindings before merging managed assets."""
    if value is None or value == "":
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise InvocationPreflightError("workflow_inputs must be valid JSON") from exc
    if not isinstance(value, Mapping) or any(
        not isinstance(name, str) or not name.strip() for name in value
    ):
        raise InvocationPreflightError("workflow_inputs must be a JSON object")
    return dict(value)


def verify_admitted_invocation_receipt(
    receipt: Any, inputs: Mapping[str, Any], scratch: Path,
) -> None:
    """Bind the admission receipt to host-materialized CAS bytes, offline.

    The receipt is evidence, not a source of execution inputs. In particular,
    media basenames come from the actual archive or staged file. Compare content
    first (before executing any Python), revision second, and API digest last,
    before placement, live approval, or attempt-private graph rewrites.
    """
    from vibecomfy.workflow import WorkflowCompileError

    from astrid.packs.vibecomfy.asset_manifest import (
        AssetManifestError,
        read_archive,
        resolve_inputs,
    )
    from astrid.packs.vibecomfy.production_engine import (
        ProductionEngineError,
        _workflow_content_digest,
        load_workflow_path,
    )
    from astrid.packs.vibecomfy.shared.bundle_inputs import (
        CanonicalBundleInputError,
        staged_workflow_path,
    )

    if not isinstance(receipt, Mapping):
        raise InvocationReceiptMismatch(
            "invocation_preflight", admitted="<required>",
            receipt="<missing>" if receipt is None else "<invalid>",
        )
    # Require the existing v1 receipt shape. The three digest comparisons
    # below use exact equality; don't trust receipt-supplied binding values.
    for field in ("revision_id", "workflow_content_digest", "api_digest"):
        if not isinstance(receipt.get(field), str) or not receipt[field]:
            raise InvocationReceiptMismatch(
                field, admitted="<required>", receipt=_short_receipt_digest(receipt.get(field)),
            )
    if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or any(not isinstance(receipt.get(field), str) or not receipt[field]
                   for field in ("phase", "workflow_identity", "semantic_digest"))
            or not isinstance(receipt.get("run_inputs"), Mapping)
            or not isinstance(receipt.get("reachable_nodes"), list)):
        raise InvocationReceiptMismatch(
            "invocation_preflight", admitted="<v1>", receipt="<invalid>",
        )
    # A receipt cannot describe a legacy UI conversion: that conversion may
    # consult a live target and has no admitted Python/companion authority.
    if inputs.get("workflow") or not all(inputs.get(name) for name in ("python", "companion", "source")):
        raise InvocationReceiptMismatch(
            "invocation_preflight", admitted="<canonical bundle required>", receipt="<invalid>",
        )

    # Match the host's existing canonical-loader isolation. Never let a
    # schema/engine import bootstrap the live ComfyUI process during this gate.
    import os

    previous_headless = os.environ.get("VIBECOMFY_HEADLESS")
    os.environ["VIBECOMFY_HEADLESS"] = "1"
    try:
        # Same canonical staging/loader as generic_host._prepare_vibecomfy_execution_identity.
        with staged_workflow_path(
            workflow=None, python=inputs["python"], companion=inputs["companion"],
            source=inputs["source"], scratch=scratch,
        ) as (workflow_path, _authority):
            # Detect edited companion bytes even when loading that companion
            # would fail its own authority checks. This is the existing content
            # digest, including source.json, not a new generation_id derivation.
            _compare_receipt_digest(receipt, "workflow_content_digest", _workflow_content_digest(workflow_path))
            loaded = load_workflow_path(workflow_path, scratch / "canonical-loader")
            _compare_receipt_digest(receipt, "revision_id", loaded.workflow_revision)
            _compare_receipt_digest(receipt, "workflow_content_digest", loaded.workflow_content_digest)

            try:
                bindings = invocation_run_inputs(inputs.get("workflow_inputs"))
            except InvocationPreflightError as exc:
                raise InvocationReceiptMismatch(
                    "bytes_unrecomputable", admitted="<unrecomputable>", receipt="<provided>",
                ) from exc
            if inputs.get("managed_assets"):
                # Same archive validation/merge as run.py's _stage_managed_asset_manifest,
                # without writing engine files or contacting a target.
                archive = read_archive(inputs["managed_assets"])
                bindings = resolve_inputs(archive.manifest, bindings)
            if inputs.get("source_video"):
                # Mirror run.py's _stage_source_video binding, before target custody.
                if "source_video" in bindings:
                    raise InvocationReceiptMismatch(
                        "bytes_unrecomputable", admitted="<duplicate source_video>", receipt="<provided>",
                    )
                bindings["source_video"] = Path(inputs["source_video"]).name
            api = loaded.resolved.workflow.compile("api", run_inputs=bindings)
            if not isinstance(api, Mapping):
                raise InvocationReceiptMismatch(
                    "bytes_unrecomputable", admitted="<invalid API>", receipt="<provided>",
                )
            _compare_receipt_digest(receipt, "api_digest", _canonical_digest(api))
    except (CanonicalBundleInputError, ProductionEngineError, AssetManifestError, WorkflowCompileError) as exc:
        raise InvocationReceiptMismatch(
            "bytes_unrecomputable", admitted="<unrecomputable>", receipt="<provided>",
        ) from exc
    finally:
        if previous_headless is None:
            os.environ.pop("VIBECOMFY_HEADLESS", None)
        else:
            os.environ["VIBECOMFY_HEADLESS"] = previous_headless
