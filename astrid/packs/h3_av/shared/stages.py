"""The CPU stages the ``vibecomfy.workflows`` build runs: prepare and compile from a file map.

``build`` runs these once; ``after_outputs`` reads what ``build`` saved and never calls them
(``save_build_state`` / ``load_build_state``). The module imports nothing from Astrid.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping

from .errors import PreparationError
from .request import H3Request, _asset_ids, normalize_request
from .request_v2 import branch_for, json_times, read_prepared_request

STATE_NAME = "build-state.json"
_OUTPUT_CONTRACTS = {"muxed_av_full_timeline", "separate_av_full_timeline"}


class UnsupportedVHSFrameTopology(ValueError):
    """Pinned VHS may count raw frame bytes ambiguously for multiple video streams."""


def stable_digest(value: Any) -> str:
    payload = json.dumps(json_times(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _authoritative_source_asset_id(request: Any) -> str | None:
    """Return the source baseline asset without projecting v1 keys onto v2."""

    value = request.value
    if value.get("version") != 2:
        source = value["source"]
        return None if source is None else str(source["asset"])
    branch = branch_for(request)
    if branch in {"source_free", "audio_only"}:
        return None
    videos = [
        item
        for item in value["media"]
        if item["role"] == "timeline" and item.get("modality") == "video"
    ]
    return str(videos[0]["asset"]) if len(videos) == 1 else None


def _generation_intent(compilation: Mapping[str, Any]) -> dict[str, Any]:
    """Derive the child publication declaration from the sealed compilation."""

    capabilities = compilation.get("capabilities")
    public = capabilities.get("public_generation") if isinstance(capabilities, Mapping) else None
    if not isinstance(public, Mapping):
        raise ValueError("sealed H3 compilation is missing public_generation")
    selectors = public.get("selectors")
    modality = public.get("modality", "video")
    if not isinstance(selectors, list) or not selectors:
        raise ValueError("sealed H3 compilation declares no public generation selectors")
    if not all(isinstance(selector, Mapping) for selector in selectors):
        raise ValueError("sealed H3 generation selectors must be objects")
    return {
        "version": 1,
        "modality": modality,
        "partial_success_policy": "reject",
        "groups": [{"group_key": "main", "selectors": [dict(selector) for selector in selectors]}],
        "metadata": {"compiled_generation_contract": True},
    }


def _sealed_plan(compilation: Mapping[str, Any], *, preparation: Mapping[str, Any] | None = None) -> dict[str, Any]:
    capabilities = compilation.get("capabilities", {})
    contract = capabilities.get("output_contract")
    if contract not in _OUTPUT_CONTRACTS:
        raise ValueError("unsupported H3 parent output mapping")
    intent = _generation_intent(compilation)
    if (intent["modality"] != "video" or len(intent["groups"]) != 1
            or len(intent["groups"][0]["selectors"]) != 1):
        raise ValueError("tracked H3 requires the sealed single video selector")
    digest = compilation.get("compilation_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("H3 compilation has no sealed identity")
    plan = {"compilation_digest": digest, "output_contract": contract, "intent": intent,
            "workflow_members": {name: row["sha256"] for name, row in compilation["workflow"].items()},
            "workflow_inputs_sha256": stable_digest(compilation["workflow_inputs"])}
    if isinstance(preparation, Mapping):
        plan["warnings"] = [dict(row) for row in preparation.get("warnings", []) if isinstance(row, Mapping)]
        if isinstance(preparation.get("resource_estimate"), Mapping):
            plan["resource_estimate"] = dict(preparation["resource_estimate"])
    # The compiler's conditioning list is the single source of reference order,
    # tags and graph bindings. Keep this projection path-free for dry-run and
    # settled-result consumers; never copy it into compilation.json.
    conditioning = capabilities.get("conditioning") if isinstance(capabilities, Mapping) else None
    if isinstance(conditioning, list):
        timing = compilation.get("generation_timing")
        if not isinstance(timing, Mapping) and isinstance(capabilities, Mapping):
            timing = capabilities.get("generation_timing")
        measurements = {}
        if isinstance(preparation, Mapping):
            measurements = {row.get("occurrence"): row for row in preparation.get("conditioning_measurements", [])
                            if isinstance(row, Mapping) and isinstance(row.get("occurrence"), str)}
        refs = []
        for row in conditioning:
            if not isinstance(row, Mapping):
                raise ValueError("H3 conditioning rows must be objects")
            reference = {key: row[key] for key in ("occurrence", "modality", "trim", "input_binding", "tag") if key in row}
            measurement = measurements.get(row.get("occurrence"), {})
            if row.get("modality") == "video":
                estimate = measurement.get("retained_picture_frames_estimate")
                if isinstance(estimate, Mapping):
                    reference["retained_picture_frames_estimate"] = {
                        "value": estimate.get("value"), "kind": "metadata_upper_bound", "observed": False,
                    }
            if isinstance(measurement.get("effective_size"), Mapping):
                reference["effective_size"] = dict(measurement["effective_size"])
            if "audio_duration_seconds" in measurement:
                reference["audio_duration_seconds"] = measurement["audio_duration_seconds"]
            if isinstance(measurement.get("resource_estimate"), Mapping):
                reference["resource_estimate"] = dict(measurement["resource_estimate"])
            refs.append(reference)
        view = {"references": refs}
        if isinstance(timing, Mapping) and type(timing.get("raw_frames")) is int:
            view["target_frame_cap"] = timing["raw_frames"]
        plan["reference_view"] = view
    audio_view = {}
    requested_audio_grants = capabilities.get("requested_audio_grants") if isinstance(capabilities, Mapping) else None
    audio_time_mask = capabilities.get("audio_time_mask") if isinstance(capabilities, Mapping) else None
    expected_audio_spans = capabilities.get("expected_delivered_audio_spans") if isinstance(capabilities, Mapping) else None
    if isinstance(requested_audio_grants, list):
        audio_view["requested_grants"] = [dict(row) for row in requested_audio_grants if isinstance(row, Mapping)]
    if isinstance(audio_time_mask, Mapping):
        audio_view["effective_40hz_mask"] = {
            "values": list(audio_time_mask.get("resampled_40hz", [])),
            "basis": "compiler resampling of the decoded per-frame mask",
            "precision": audio_time_mask.get("precision"),
        }
        audio_view["decoded_frame_mask"] = list(audio_time_mask.get("decoded_frame_values", []))
    if isinstance(expected_audio_spans, list):
        audio_view["expected_delivered_sample_spans"] = [dict(row) for row in expected_audio_spans if isinstance(row, Mapping)]
    if audio_view:
        view = plan.get("reference_view")
        if not isinstance(view, dict):
            view = {"references": []}
            plan["reference_view"] = view
        view["audio"] = audio_view
    return plan


def materialize_file_map(request: H3Request, files: Mapping[str, Any], destination: Path,
                         ) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Copy exactly the request's declared assets from ``{id: path}`` into ``destination``."""
    destination = destination.resolve()
    paths: dict[str, str] = {}
    identities: list[dict[str, Any]] = []
    for asset_id in _asset_ids(request):
        value = files.get(asset_id)
        if not isinstance(value, str) or not value:
            raise PreparationError(f"asset {asset_id!r} needs a file in asset_map before task submission")
        source = Path(value).expanduser().resolve(strict=True)
        if not source.is_file():
            raise PreparationError(f"asset {asset_id!r} is not a regular file: {source}")
        member = f"assets/{hashlib.sha256(asset_id.encode()).hexdigest()[:16]}{source.suffix}"
        target = (destination / member).resolve()
        if not target.is_relative_to(destination):
            raise PreparationError("asset member escapes its attempt directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        paths[asset_id] = str(target)
        identities.append({"asset": asset_id, "member": member, "sha256": digest, "size": target.stat().st_size,
                           "kind": "file_map_member", "status": "resolved", "path": str(target)})
    return paths, identities


def compile_stages(request: H3Request, files: Mapping[str, Any], destination: Path):
    """Prepare and compile one request from its file map: CPU only. Returns (paths, preparation, compilation)."""
    from .compile import compile_preparation
    from .prepare import prepare_request

    paths, identities = materialize_file_map(request, files, destination / "assets")
    preparation = prepare_request(request, asset_map=paths)
    preparation["assets"] = identities
    compilation = compile_preparation(preparation, out_dir=destination / "compile")
    return paths, preparation, compilation


# The name the planner and the bundle-based transform used for the same stage.
_compile_stages = compile_stages


def save_build_state(directory: Path, *, request: H3Request, document: Mapping[str, Any], preparation: Mapping[str, Any],
                     source: str | None, plan: Mapping[str, Any],
                     reference_assets: Mapping[str, Mapping[str, Any]] | None = None) -> Path:
    """What ``after_outputs`` needs, written once by ``build`` into the shared job directory."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_NAME
    path.write_text(json.dumps(json_times({"request": document, "request_digest": request.digest,
                                "preparation": preparation, "source": source, "plan": plan,
                                "reference_assets": reference_assets or {}}),
                               sort_keys=True), encoding="utf-8")
    return path


def load_build_state(directory: Path) -> dict[str, Any]:
    path = Path(directory) / STATE_NAME
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"h3 build state is missing or unreadable ({path}); after_outputs runs after build "
                         "in the same job directory") from exc
    request = normalize_request(state["request"])
    if request.digest != state["request_digest"]:
        raise ValueError("h3 build state does not match its request digest; rebuild this request")
    preparation = state["preparation"]
    if preparation["request"].get("version") == 2:
        preparation["request"] = read_prepared_request(
            preparation["request"], preparation["request_digest"], require_normalized_v2=True,
        ).value
    return {**state, "request_object": request}


def _vhs_frame_count(path: Path, *, start_time: float, frame_load_cap: int,
                     ffmpeg: str | None = None) -> int:
    """Count frames through the pinned VHS FFmpeg seek, fps and cap path.

    Mirrors ComfyUI-VideoHelperSuite ``ffmpeg_frame_generator`` at commit
    4ee72c065db22c9d96c2427954dc69e7b908444b: input seek plus four-second
    output seek for starts over 4s, otherwise output seek; ``fps=24`` and the
    optional ``frame_load_cap``. The framehash muxer counts decoded output
    frames without retaining temporary media bytes.
    """
    executable = ffmpeg or shutil.which("ffmpeg")
    if not executable:
        raise FileNotFoundError("ffmpeg is unavailable")
    args_input = ["-i", str(path)]
    # VHS probes first and forces libvpx-vp9 for VP9 streams, then probes again
    # with that decoder before building its timestamp/filter command.
    dummy = [executable, *args_input, "-c", "copy", "-frames:v", "1", "-f", "null", "-"]
    probe = subprocess.run(dummy, check=True, capture_output=True, text=False, timeout=30)
    lines = probe.stderr.decode("utf-8", errors="replace")
    input_report = lines.split("Output #", 1)[0]
    video_streams = re.findall(r"^\s*Stream #[^\n]*Video:", input_report, flags=re.MULTILINE)
    if len(video_streams) > 1:
        raise UnsupportedVHSFrameTopology("multiple_video_streams")
    if "Video: vp9 " in lines:
        args_input = ["-c:v", "libvpx-vp9", *args_input]
        dummy = [executable, *args_input, "-c", "copy", "-frames:v", "1", "-f", "null", "-"]
        probe = subprocess.run(dummy, check=True, capture_output=True, text=False, timeout=30)
        lines = probe.stderr.decode("utf-8", errors="replace")
        input_report = lines.split("Output #", 1)[0]
        video_streams = re.findall(r"^\s*Stream #[^\n]*Video:", input_report, flags=re.MULTILINE)
        if len(video_streams) > 1:
            raise UnsupportedVHSFrameTopology("multiple_video_streams")
    post_seek: list[str] = []
    if start_time > 0:
        if start_time > 4:
            args_input = ["-ss", str(start_time - 4), *args_input]
            post_seek = ["-ss", "4"]
        else:
            post_seek = ["-ss", str(start_time)]
    command = [executable, "-v", "error", "-an", *args_input, "-pix_fmt", "rgba64le", *post_seek,
               "-vf", "fps=fps=24"]
    if frame_load_cap > 0:
        command.extend(["-frames:v", str(frame_load_cap)])
    command.extend(["-f", "framehash", "-"])
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    return sum(bool(line.strip()) and not line.lstrip().startswith("#") for line in result.stdout.splitlines())


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _settled_reference_view(reference_view: Mapping[str, Any], *, reference_assets: Mapping[str, Mapping[str, Any]],
                            workdir: Path, ffmpeg: str | None = None,
                            composition: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the sealed view plus counts derived by re-decoding its bound bytes.

    Failed path, custody, hash, or decode checks stay explicitly unqualified;
    a dry-run metadata estimate is never promoted into a settled count.
    """
    from copy import deepcopy
    from .prepare import _retained_video_frames

    settled = deepcopy(dict(reference_view))
    audio_view = settled.get("audio")
    composition_audio = composition.get("exact_audio") if isinstance(composition, Mapping) else None
    actual_spans = composition_audio.get("delivered_spans") if isinstance(composition_audio, Mapping) else None
    if isinstance(audio_view, dict) and isinstance(actual_spans, list):
        audio_view["actual_delivered_sample_spans"] = deepcopy(actual_spans)
    target = settled.get("target_frame_cap")
    root = Path(workdir).resolve()
    managed_candidate = root / "managed"
    managed_root = None
    managed_root_reason = None
    if managed_candidate.is_symlink():
        managed_root_reason = "managed_reference_asset_root_symlink"
    else:
        try:
            resolved_root = managed_candidate.resolve(strict=True)
            if not resolved_root.is_dir() or not resolved_root.is_relative_to(root):
                managed_root_reason = "managed_reference_asset_root_outside_custody"
            else:
                managed_root = resolved_root
        except FileNotFoundError:
            managed_root_reason = "managed_reference_asset_root_missing"
    executable = ffmpeg or shutil.which("ffmpeg")
    for reference in settled.get("references", []):
        if not isinstance(reference, dict) or reference.get("modality") != "video":
            continue
        input_binding = reference.get("input_binding")
        input_binding = input_binding if isinstance(input_binding, Mapping) else {}
        binding = input_binding.get("binding")
        record = reference_assets.get(binding) if isinstance(binding, str) else None
        reason = managed_root_reason
        frame_count = None
        if reason is None and ("start_time" not in input_binding or "frame_load_cap" not in input_binding):
            reason = "vhs_binding_parameters_unavailable"
        if reason is None and not isinstance(record, Mapping):
            reason = "managed_reference_asset_unavailable"
        elif reason is None and (not isinstance(record.get("member"), str) or not isinstance(record.get("sha256"), str)):
            reason = "managed_reference_asset_unavailable"
        elif reason is None and (type(target) is not int or target < 1):
            reason = "h3_target_frame_cap_unavailable"
        elif reason is None and not executable:
            reason = "ffmpeg_unavailable"
        elif reason is None:
            candidate = managed_root / record["member"]
            try:
                member_path = Path(record["member"])
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise ValueError("managed reference member is not a relative custody path")
                cursor = managed_root
                for part in member_path.parts:
                    cursor = cursor / part
                    if cursor.is_symlink():
                        raise ValueError("managed reference member traverses a symlink")
                try:
                    path = candidate.resolve(strict=True)
                except FileNotFoundError:
                    reason = "managed_reference_asset_missing"
                    raise
                if not path.is_relative_to(managed_root) or candidate.is_symlink() or not path.is_file():
                    raise ValueError("managed reference asset is outside job custody")
                digest = _file_sha256(path)
                if digest != record.get("sha256"):
                    raise ValueError("managed reference asset hash differs from compilation manifest")
                start_time = input_binding["start_time"]
                frame_load_cap = input_binding["frame_load_cap"]
                if isinstance(start_time, bool) or not isinstance(start_time, (int, float)) or not math.isfinite(start_time) or start_time < 0:
                    raise ValueError("invalid VHS start_time")
                if type(frame_load_cap) is not int or frame_load_cap < 0:
                    raise ValueError("invalid VHS frame_load_cap")
                frame_count = _vhs_frame_count(path, start_time=float(start_time),
                                               frame_load_cap=frame_load_cap,
                                               ffmpeg=ffmpeg)
            except FileNotFoundError:
                if reason is None:
                    reason = "ffmpeg_unavailable"
            except subprocess.TimeoutExpired:
                reason = "ffmpeg_redecode_timeout"
            except subprocess.CalledProcessError:
                reason = "ffmpeg_redecode_failed"
            except UnsupportedVHSFrameTopology:
                reason = "multiple_video_streams_unqualified"
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                # Do not persist command arguments or scratch locators in the
                # settled result; they can include the local bound-file path.
                reason = "bound_reference_redecode_unavailable"
        retained = _retained_video_frames(frame_count, target) if frame_count is not None else None
        if retained is None:
            reference["retained_picture_frames_derived"] = {
                "status": "unqualified", "reason": reason or "decoded_frames_below_h3_minimum",
                "kind": "derived_pinned_node_rule_unavailable",
            }
            continue
        reference["retained_picture_frames_derived"] = {
            "value": retained,
            "status": "derived",
            "kind": "derived_pinned_node_rule",
            "input_frame_count": frame_count,
            "input_sha256": record["sha256"],
            "basis": "CPU re-decode of the SHA-256 verified managed reference using pinned VHS seek, force_rate=24, and frame_load_cap semantics",
            "conformance": "not_conformed",
            "target_frame_cap": target,
            "alignment": "5 + 17k",
            "rule_source": "ComfyUI@6bfaacc67c2103481e5f0c84d75257cd0581d86a:comfy_extras/nodes_minimax_h3.py:324-332",
            "loader_source": "ComfyUI-VideoHelperSuite@4ee72c065db22c9d96c2427954dc69e7b908444b:videohelpersuite/load_video_nodes.py:216-246",
            "decoder_source": "ComfyUI-VideoHelperSuite@4ee72c065db22c9d96c2427954dc69e7b908444b:videohelpersuite/load_video_nodes.py:170-193",
        }
    return settled
