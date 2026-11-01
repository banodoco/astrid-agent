"""FalBackend — cloud generation via fal.ai HTTP API.

Uses :class:`~astrid.core.util.http.HttpClient` for all
HTTP transport (no fal SDK).  Image references are uploaded via
:func:`~astrid.core.util.http.fal_upload`.  Jobs are submitted and polled
via :func:`~astrid.core.util.http.fal_submit_and_poll`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from astrid.core.generation.backends.base import (
    BackendAdapter,
    GenerationResult,
    derive_frames_from_duration,
    parse_dimension_pair,
    split_feature_support,
)
from astrid.core.generation.storage_policy import (
    CLOUD_EDIT_STORAGE_POLICY,
    CLOUD_I2I_STORAGE_POLICY,
    CLOUD_T2I_STORAGE_POLICY,
    CLOUD_UNIFIED_EDIT_STORAGE_POLICY,
    ImageStoragePolicyError,
)
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.core.util.http import (
    HttpClient,
    default_client,
    fal_storage_upload,
    fal_submit_and_poll,
    fal_upload,
)

logger = logging.getLogger(__name__)


def _parse_size(size: str) -> str | None:
    """Normalize a size string for fal endpoints.

    Accepted formats: ``WxH``, ``W*H``, ``W,H``, or a fal preset like
    ``"square_hd"``, ``"landscape_4_3"``, etc.  Returns ``None`` if *size*
    is empty.
    """
    if not size or not isinstance(size, str):
        return None
    size = size.strip()
    if not size:
        return None
    parsed = parse_dimension_pair(size)
    if parsed:
        w, h = parsed
        return f"{w}x{h}"
    # Pass through as-is (could be a fal preset name like "square_hd")
    return size


# ---------------------------------------------------------------------------
# FalBackend
# ---------------------------------------------------------------------------


class FalBackend(BackendAdapter):
    """Cloud generation backend via fal.ai HTTP API.

    Uses the shared :class:`HttpClient` so tests can mock transport
    (pattern preserved).

    Parameters:
        env_file: Optional path to a ``.env`` file holding ``FAL_KEY``.
        client: Inject a mock :class:`HttpClient` for testing.
    """

    #: Default canonical→remote parameter name mapping per mode.
    #: Used as a fallback when ``BackendSpec.param_map`` is empty so that
    #: models.yaml entries can eventually drop the redundant wiring.
    DEFAULT_PARAM_MAP: dict[str, dict[str, str]] = {
        # ── Image modes ────────────────────────────────────────────────
        "t2i": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "count": "num_images",
            "size": "image_size",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
        },
        "upscale": {
            "image_ref": "image_url",
            "upscale_mode": "upscale_mode",
            "upscale_factor": "upscale_factor",
            "target_resolution": "target_resolution",
            "seed": "seed",
            "noise_scale": "noise_scale",
            "enable_safety_checker": "enable_safety_checker",
            "output_format": "output_format",
        },
        "i2i": {
            "prompt": "prompt",
            "seed": "seed",
            "image_ref": "image_url",
            "count": "num_images",
            "size": "image_size",
            "strength": "strength",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
        },
        "edit": {
            "prompt": "prompt",
            "seed": "seed",
            "image_ref": "image_url",
            "count": "num_images",
            "size": "image_size",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
        },
        "inpaint": {
            "prompt": "prompt",
            "seed": "seed",
            "image_ref": "image_url",
            "mask_ref": "mask_url",
            "count": "num_images",
            "size": "image_size",
            "strength": "strength",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
        },
        # ── Video modes ────────────────────────────────────────────────
        "t2v": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "frames": "num_frames",
            "fps": "fps",
            "duration": "duration",
            "resolution": "aspect_ratio",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
            "shift": "shift",
            "loras": "loras",
            "enable_safety_checker": "enable_safety_checker",
            "enable_prompt_expansion": "enable_prompt_expansion",
            "acceleration": "acceleration",
        },
        "i2v": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "image_ref": "image_url",
            "resolution": "aspect_ratio",
            "frames": "num_frames",
            "fps": "fps",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
            "shift": "shift",
            "loras": "loras",
            "enable_safety_checker": "enable_safety_checker",
            "enable_prompt_expansion": "enable_prompt_expansion",
            "acceleration": "acceleration",
        },
        "flf": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "image_ref": "image_url",
            "image_end_ref": "end_image_url",
            "resolution": "aspect_ratio",
            "frames": "num_frames",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
            "shift": "shift",
            "loras": "loras",
            "enable_safety_checker": "enable_safety_checker",
            "enable_prompt_expansion": "enable_prompt_expansion",
            "acceleration": "acceleration",
        },
        "v2v": {
            "prompt": "prompt",
            "seed": "seed",
            "image_ref": "image_url",
            "video_ref": "video_url",
            "mode": "mode",
            "driving_type": "driving_type",
            "subject_type": "subject_type",
            "resolution": "resolution",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
            "shift": "shift",
        },
        # ── Audio modes ────────────────────────────────────────────────
        "music": {
            "prompt": "prompt",
            "negative_prompt": "negative_prompt",
            "seed": "seed",
            "duration": "duration",
            "guidance_scale": "guidance_scale",
            "steps": "num_inference_steps",
            "lyrics_prompt": "lyrics",
            "instrumental": "is_instrumental",
            "output_format": "output_format",
        },
    }

    def __init__(
        self,
        env_file: Path | None = None,
        client: HttpClient | None = None,
    ) -> None:
        self._env_file = env_file
        self._client = client or default_client()
        self._api_key: str | None = None

    def _resolve_api_key(self) -> str:
        """Return ``FAL_KEY``, loading from environment / .env on first call."""
        if self._api_key is None:
            self._api_key = CredentialsScope.get_local("fal", env_file=self._env_file)
            self._client.register_secret(self._api_key)
        return self._api_key

    def generate(
        self,
        entry: ModelEntry,
        mode: str,
        params: dict[str, Any],
        out_dir: Path,
    ) -> GenerationResult:
        mode_spec = entry.modes[mode]
        backend_spec: BackendSpec = mode_spec.backends["cloud"]
        endpoint = backend_spec.endpoint
        param_map: dict[str, str] = dict(backend_spec.param_map)
        if not param_map:
            param_map = dict(self.DEFAULT_PARAM_MAP.get(mode, {}))

        api_key = self._resolve_api_key()

        bounded_policy = None
        if (
            mode == "t2i"
            and params.get("execution") == "cloud"
            and params.get("storage_policy_version") == CLOUD_T2I_STORAGE_POLICY.version
        ):
            bounded_policy = CLOUD_T2I_STORAGE_POLICY
        elif (
            entry.id == "z-image"
            and mode == "i2i"
            and params.get("execution") == "cloud"
            and params.get("storage_policy_version") == CLOUD_I2I_STORAGE_POLICY.version
        ):
            bounded_policy = CLOUD_I2I_STORAGE_POLICY
        elif (
            entry.id == "qwen-image-edit-2511"
            and mode == "edit"
            and params.get("execution") == "cloud"
            and params.get("storage_policy_version") == CLOUD_EDIT_STORAGE_POLICY.version
        ):
            bounded_policy = CLOUD_EDIT_STORAGE_POLICY
        elif (
            params.get("execution") == "cloud"
            and params.get("storage_policy_version") == CLOUD_UNIFIED_EDIT_STORAGE_POLICY.version
        ):
            bounded_policy = CLOUD_UNIFIED_EDIT_STORAGE_POLICY
        if bounded_policy is not None:
            try:
                bounded_policy.validate_request(
                    model=entry.id,
                    mode=mode,
                    execution="cloud",
                    params=params,
                )
            except ImageStoragePolicyError as exc:
                raise ValueError(str(exc)) from exc

        # --- compute applied / dropped feature lists -------------------------
        applied_features, dropped_features = split_feature_support(params, mode_spec.supports)

        # --- derive frame count deterministically ----------------------------
        # If duration is supplied without frames, and fps is known, derive frames.
        computed_frames = derive_frames_from_duration(params)
        if computed_frames is not None:
            logger.debug("Computed frames=%d from duration * fps", computed_frames)

        # --- LoRA resolution + routing (before main payload build) -----------
        lora_provenance: list[dict[str, Any]] = []
        loras_raw = params.get("loras")
        fal_loras: list[dict[str, Any]] = []
        if loras_raw:
            # (c) Require lora_endpoint to exist
            if not backend_spec.lora_endpoint:
                raise ValueError(
                    f"model {entry.id!r} has no LoRA endpoint; "
                    f"cannot apply LoRAs"
                )
            # Route to lora endpoint
            endpoint = backend_spec.lora_endpoint

            # (a+b) Resolve and validate each LoRA
            for item in loras_raw:
                if isinstance(item, str):
                    # Registry id lookup
                    from astrid.core.model_catalog.registry import LoraRegistry
                    lora_registry = LoraRegistry.load_default(
                        model_ids=frozenset({entry.id}),
                    )
                    try:
                        lora_entry = lora_registry.get(item)
                    except KeyError as exc:
                        raise ValueError(
                            f"Unknown LoRA id {item!r}: {exc}"
                        ) from exc
                    # (b) Validate base_model match
                    if lora_entry.base_model != entry.id:
                        raise ValueError(
                            f"LoRA {lora_entry.id!r} base_model "
                            f"{lora_entry.base_model!r} does not match "
                            f"requested model {entry.id!r}"
                        )
                    fal_loras.append({
                        "path": lora_entry.source.url,
                        "scale": lora_entry.default_scale,
                    })
                    lora_provenance.append({
                        "id": lora_entry.id,
                        "url": lora_entry.source.url,
                        "scale": lora_entry.default_scale,
                    })
                elif isinstance(item, dict):
                    # Inline {path, scale[, transformer]} spec
                    path = item.get("path")
                    scale = item.get("scale", 1.0)
                    if not path or not isinstance(path, str):
                        raise ValueError(
                            f"inline LoRA spec missing 'path': {item!r}"
                        )
                    # Inline LoRAs cannot be validated for base_model —
                    # caller assumes responsibility.
                    fal_lora: dict[str, Any] = {"path": path, "scale": scale}
                    # Some endpoints (e.g. ltx-2.3-quality lora) require a
                    # per-LoRA `transformer` target ("both"/"high"/"low");
                    # forward it verbatim when the caller supplies one.
                    transformer = item.get("transformer")
                    if transformer is not None:
                        fal_lora["transformer"] = transformer
                    fal_loras.append(fal_lora)
                    lora_provenance.append({
                        "url": path,
                        "scale": scale,
                    })
                else:
                    raise ValueError(
                        f"LoRA must be a registry id (str) or inline dict "
                        f"{{path, scale}}, got {type(item).__name__}: {item!r}"
                    )

        # --- build payload ---------------------------------------------------
        payload: dict[str, Any] = {}

        for canon, remote_param in param_map.items():
            if canon == "count":
                # The bounded Qwen edit route is explicitly one-output-per-
                # request, so keep the provider-side cardinality visible even
                # though the executor also runs each requested output as N=1.
                if (
                    remote_param == "num_images"
                    and entry.id in {
                        "qwen-image-edit-2511",
                        "qwen-image-edit-inpaint",
                        "flux2-klein-4b",
                        "flux2-klein-9b",
                    }
                ):
                    payload[remote_param] = params.get(canon, 1)
                continue  # other image profiles manage count in the executor loop
            if canon == "loras":
                continue  # loras handled separately above
            if canon not in params:
                continue
            value = params[canon]
            if value is None:
                continue

            # Special handling for size
            if canon == "size":
                normalized = _parse_size(str(value))
                if normalized:
                    if (
                        remote_param == "image_size"
                        and (
                            entry.id in {
                                "qwen-image-edit-2511",
                                "qwen-image-edit-inpaint",
                                "flux2-klein-4b",
                                "flux2-klein-9b",
                            }
                            or (entry.id == "z-image" and mode == "i2i")
                        )
                    ):
                        width, height = parse_dimension_pair(normalized) or (None, None)
                        payload[remote_param] = (
                            {"width": width, "height": height}
                            if width is not None and height is not None
                            else normalized
                        )
                    else:
                        payload[remote_param] = normalized
                continue

            # Special handling for image_ref / image_end_ref — upload if local path
            if canon in ("image_ref", "mask_ref", "image_end_ref"):
                uploaded_ref = _upload_ref_if_local(
                    str(value),
                    remote_param,
                    self._client,
                    api_key,
                    max_bytes=(
                        (
                            bounded_policy.mask_max_bytes
                            if canon == "mask_ref"
                            else bounded_policy.source_max_bytes
                        )
                        if bounded_policy is not None and canon in {"image_ref", "mask_ref"}
                        else None
                    ),
                )
                # Multi-reference edit endpoints such as Seedream expose a
                # plural ``image_urls`` input even when Astrid's basic image
                # contract supplies one canonical ``image_ref``.
                payload[remote_param] = (
                    [uploaded_ref] if remote_param == "image_urls" else uploaded_ref
                )
                continue

            # Special handling for video_ref — upload to fal CDN (videos are too
            # large for base64 data URIs and the API prefers CDN URLs).
            if canon == "video_ref":
                ref_path = Path(str(value))
                if ref_path.is_file():
                    logger.info("Uploading %s to fal CDN: %s", remote_param, ref_path)
                    payload[remote_param] = fal_storage_upload(
                        self._client, ref_path, api_key
                    )
                else:
                    payload[remote_param] = str(value)
                continue

            # Special handling for resolution — parse WxH, split into
            # width/height per param_map or write as single video_size string
            if canon == "resolution":
                res_str = str(value)
                parsed = parse_dimension_pair(res_str)
                if parsed:
                    w, h = parsed
                    # If param_map maps resolution to separate width/height keys
                    # we write them individually; otherwise write as a single string
                    if remote_param == "video_size" or "width" not in param_map.values():
                        payload[remote_param] = f"{w}x{h}"
                    else:
                        payload["width"] = w
                        payload["height"] = h
                else:
                    payload[remote_param] = res_str
                continue

            payload[remote_param] = value

        # Apply backend hints (model × mode × backend configuration).
        # Hints are the declarative replacement for adapter hardcodes.
        # guidance_scale_override: force-set a key (model contract, e.g.
        #   flux-schnell guidance_scale ≡ 1.0).
        # All other hint keys: setdefault so an explicit caller-supplied
        #   value (if ever wired) still wins.
        if backend_spec.hints:
            for hint_key, hint_value in backend_spec.hints.items():
                if hint_key == "guidance_scale_override":
                    payload["guidance_scale"] = hint_value
                else:
                    payload.setdefault(hint_key, hint_value)

        # --- attach LoRA payload if present ----------------------------------
        if loras_raw and fal_loras:
            payload["loras"] = fal_loras

        # --- submit + poll ---------------------------------------------------
        t0 = time.monotonic()
        submit_kwargs = (
            {"max_response_bytes": bounded_policy.control_max_bytes}
            if bounded_policy is not None
            else {}
        )
        result = fal_submit_and_poll(
            self._client,
            endpoint,
            payload,
            api_key,
            **submit_kwargs,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        # --- extract metadata ------------------------------------------------
        seed_used: int = params.get("seed", 0)
        cost_usd: float | None = None
        request_id: str | None = result.get("request_id")
        source_urls: list[str] | None = None

        # Try to extract cost from result (fal API-reported cost is preferred)
        if isinstance(result.get("cost"), (int, float)):
            cost_usd = float(result["cost"])

        # --- download images -------------------------------------------------
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        image_paths: list[Path] = []

        # fal returns results in various shapes: {"images": [{"url": ...}, ...]}
        # or {"image": {"url": ...}} or {"video": {"url": ...}} etc.
        asset_urls = _extract_asset_urls(result)
        source_urls = list(asset_urls)
        expected_provider_outputs = (
            bounded_policy.provider_outputs_per_call
            if bounded_policy is not None
            and hasattr(bounded_policy, "provider_outputs_per_call")
            else bounded_policy.max_count
            if bounded_policy is not None
            else None
        )
        if bounded_policy is not None and len(asset_urls) != expected_provider_outputs:
            raise ValueError(
                f"bounded cloud image call expected exactly "
                f"{expected_provider_outputs} provider output, "
                f"got {len(asset_urls)}"
            )

        # --- cost fallback to typed registry price ----------------------------
        # If the API did not report a cost (missing or non-numeric), fall back
        # to the registry price when available.  Unpriced backends keep None.
        if cost_usd is None and backend_spec.price is not None:
            if backend_spec.price.unit == "second":
                duration_s = params.get("duration") or 0
                cost_usd = duration_s * backend_spec.price.usd
            else:
                cost_usd = len(asset_urls) * backend_spec.price.usd

        for idx, url in enumerate(asset_urls):
            try:
                download_kwargs = (
                    {"max_bytes": bounded_policy.output_max_bytes}
                    if bounded_policy is not None
                    else {}
                )
                data = self._client.get_bytes(
                    url,
                    timeout=120,
                    **download_kwargs,
                )
                if bounded_policy is not None:
                    bounded_policy.validate_download(data, index=idx)
            except ImageStoragePolicyError as exc:
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                raise ValueError(
                    f"Failed to download fal result output {idx}: {exc}"
                ) from exc
            suffix = _guess_suffix(url)
            dst = out_dir / f"output_{idx:03d}{suffix}"
            if dst.exists():
                counter = 1
                while dst.exists():
                    dst = out_dir / f"output_{idx:03d}_{counter}{suffix}"
                    counter += 1
            dst.write_bytes(data)
            image_paths.append(dst)

        return GenerationResult(
            image_paths=image_paths,
            seed_used=seed_used,
            model_actual=endpoint,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            applied_features=applied_features,
            dropped_features=dropped_features,
            request_id=request_id,
            source_urls=source_urls,
            error=None,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _upload_ref_if_local(
    ref_str: str,
    feature_name: str,
    client: HttpClient,
    api_key: str,
    max_bytes: int | None = None,
) -> str:
    """Upload one host-materialized local input to fal.

    Small images are inlined as base64 data URIs. Files larger than 500 KB,
    and any non-image media, are uploaded to fal CDN because fal docs
    discourage base64 for large payloads. Provider inputs never accept an
    arbitrary URL or unresolved caller path at this boundary.
    """
    ref_path = Path(ref_str)
    if not ref_path.is_file():
        raise ValueError(
            f"{feature_name} must be a host-materialized local file, got {ref_str!r}"
        )
    size = ref_path.stat().st_size
    if max_bytes is not None and size > max_bytes:
        raise ValueError(
            f"{feature_name} exceeds bounded input limit of {max_bytes} bytes"
        )
    if size > 512_000:
        logger.info("Uploading %s to fal CDN: %s (%d bytes)", feature_name, ref_path, size)
        return fal_storage_upload(client, ref_path, api_key)
    logger.info("Uploading %s to fal as data URI: %s", feature_name, ref_path)
    return fal_upload(client, ref_path, api_key)


def _extract_asset_urls(result: dict[str, Any]) -> list[str]:
    """Extract image, video, or audio URLs from a fal result dict.

    Handles common fal response shapes:

    *Image shapes:*
    - ``{"images": [{"url": "..."}, ...]}``
    - ``{"image": {"url": "..."}}``
    - ``{"output": {"image": {"url": "..."}}}``
    - ``{"output": "..."}`` (direct URL string)

    *Video shapes:*
    - ``{"video": {"url": "..."}}`` — single video dict
    - ``{"videos": [{"url": "..."}, ...]}`` — video list

    *Audio shapes:*
    - ``{"audio": {"url": "..."}}`` — single audio dict
    - ``{"audio_file": {"url": "..."}}`` — single audio dict (alt key)
    - ``{"audios": [{"url": "..."}, ...]}`` — audio list
    - ``{"output": {"audio": {"url": "..."}}}`` — nested output.audio
    """
    urls: list[str] = []

    # Direct images array
    images = result.get("images")
    if isinstance(images, list):
        for item in images:
            if isinstance(item, dict) and "url" in item:
                urls.append(item["url"])
            elif isinstance(item, str):
                urls.append(item)
        if urls:
            return urls

    # Single image object
    image = result.get("image")
    if isinstance(image, dict) and "url" in image:
        urls.append(image["url"])
        return urls

    # Single video object
    video = result.get("video")
    if isinstance(video, dict) and "url" in video:
        urls.append(video["url"])
        return urls

    # Video list
    videos = result.get("videos")
    if isinstance(videos, list):
        for item in videos:
            if isinstance(item, dict) and "url" in item:
                urls.append(item["url"])
            elif isinstance(item, str):
                urls.append(item)
        if urls:
            return urls

    # Single audio object
    audio = result.get("audio")
    if isinstance(audio, dict) and "url" in audio:
        urls.append(audio["url"])
        return urls

    # Alternative single-audio key used by some endpoints
    audio_file = result.get("audio_file")
    if isinstance(audio_file, dict) and "url" in audio_file:
        urls.append(audio_file["url"])
        return urls

    # Audio list
    audios = result.get("audios")
    if isinstance(audios, list):
        for item in audios:
            if isinstance(item, dict) and "url" in item:
                urls.append(item["url"])
            elif isinstance(item, str):
                urls.append(item)
        if urls:
            return urls

    # Nested output.image / output.video / output.audio
    output = result.get("output")
    if isinstance(output, dict):
        nested_image = output.get("image")
        if isinstance(nested_image, dict) and "url" in nested_image:
            urls.append(nested_image["url"])
            return urls
        nested_video = output.get("video")
        if isinstance(nested_video, dict) and "url" in nested_video:
            urls.append(nested_video["url"])
            return urls
        nested_audio = output.get("audio")
        if isinstance(nested_audio, dict) and "url" in nested_audio:
            urls.append(nested_audio["url"])
            return urls
        # output is a direct URL string
    elif isinstance(output, str):
        urls.append(output)
        return urls

    # Last resort: scan for any key ending with "url" or "urls"
    for key, value in result.items():
        if key.endswith("url") and isinstance(value, str):
            urls.append(value)
        elif key.endswith("urls") and isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    urls.append(item)

    return urls


def _guess_suffix(url: str) -> str:
    """Guess a file extension from a URL path."""
    path = url.split("?")[0]
    suffix = Path(path).suffix.lower()
    if suffix in {
        ".png", ".jpg", ".jpeg", ".webp", ".gif",
        ".mp4", ".webm", ".mov",
        ".wav", ".mp3", ".flac", ".m4a",
    }:
        return suffix
    return ".png"
