"""WavespeedBackend — cloud generation via the WaveSpeedAI HTTP API.

Mirrors the fal adapter: all HTTP transport goes through
:class:`~astrid.core.util.http.HttpClient` and jobs are submitted/polled via
:func:`~astrid.core.util.http.wavespeed_submit_and_poll`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from astrid.core.generation.backends.base import (
    BackendAdapter,
    GenerationResult,
    split_feature_support,
)
from astrid.core.model_catalog.schema import BackendSpec, ModelEntry
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.core.util.http import (
    HttpClient,
    default_client,
    wavespeed_submit_and_poll,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WavespeedBackend
# ---------------------------------------------------------------------------


class WavespeedBackend(BackendAdapter):
    """Cloud generation backend via the WaveSpeedAI HTTP API.

    Uses the shared :class:`HttpClient` so tests can mock transport
    (same pattern as :class:`~astrid.core.generation.backends.fal.FalBackend`).

    Parameters:
        env_file: Optional path to a ``.env`` file holding ``WAVESPEED_API_KEY``.
        client: Inject a mock :class:`HttpClient` for testing.
    """

    #: Default canonical→remote parameter name mapping per mode.
    DEFAULT_PARAM_MAP: dict[str, dict[str, str]] = {
        # ── Audio modes ────────────────────────────────────────────────
        "music": {
            "prompt": "prompt",
            "lyrics_prompt": "lyrics",
            "instrumental": "is_instrumental",
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
        """Return ``WAVESPEED_API_KEY`` from Astrid's shared credential resolver."""
        if self._api_key is None:
            self._api_key = CredentialsScope.get_local("wavespeed", env_file=self._env_file)
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
        backend_spec: BackendSpec = mode_spec.backends["wavespeed"]
        endpoint = backend_spec.endpoint
        param_map: dict[str, str] = dict(backend_spec.param_map)
        if not param_map:
            param_map = dict(self.DEFAULT_PARAM_MAP.get(mode, {}))

        api_key = self._resolve_api_key()

        # --- compute applied / dropped feature lists -------------------------
        applied_features, dropped_features = split_feature_support(params, mode_spec.supports)

        # --- build payload ---------------------------------------------------
        payload: dict[str, Any] = {}
        for canon, remote_param in param_map.items():
            if canon == "count":
                continue  # count is managed by the executor loop
            if canon not in params:
                continue
            value = params[canon]
            if value is None:
                continue
            payload[remote_param] = value

        # Apply backend hints (declarative model/endpoint overrides).
        if backend_spec.hints:
            for hint_key, hint_value in backend_spec.hints.items():
                if hint_key == "guidance_scale_override":
                    payload["guidance_scale"] = hint_value
                else:
                    payload.setdefault(hint_key, hint_value)

        # --- submit + poll ---------------------------------------------------
        t0 = time.monotonic()
        result = wavespeed_submit_and_poll(
            self._client,
            endpoint,
            payload,
            api_key,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)

        # --- extract metadata ------------------------------------------------
        seed_used: int = params.get("seed", 0)
        cost_usd: float | None = None
        request_id: str | None = result.get("request_id")

        # WaveSpeed predictions may carry a recorded cost; prefer it.
        if isinstance(result.get("cost"), (int, float)):
            cost_usd = float(result["cost"])
        elif backend_spec.price is not None:
            if backend_spec.price.unit == "second":
                duration_s = params.get("duration") or 0
                cost_usd = duration_s * backend_spec.price.usd
            else:
                cost_usd = len(_extract_asset_urls(result)) * backend_spec.price.usd

        # --- download outputs ------------------------------------------------
        out_dir = out_dir.resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        asset_urls = _extract_asset_urls(result)
        image_paths: list[Path] = []
        for idx, url in enumerate(asset_urls):
            try:
                data = self._client.get_bytes(url, timeout=300)
            except Exception as exc:
                raise ValueError(
                    f"Failed to download wavespeed result output {idx}: {exc}"
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
            source_urls=list(asset_urls),
            error=None,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_asset_urls(result: dict[str, Any]) -> list[str]:
    """Extract audio/video/image URLs from a WaveSpeed prediction result.

    Handles the completed-result shapes WaveSpeed returns: ``outputs`` as a
    list of ``{"audio": {"url": ...}}`` items (or dicts carrying a ``url``),
    a single ``audio`` object, and the generic URL-key fallback scan.
    """
    urls: list[str] = []

    outputs = result.get("outputs")
    if isinstance(outputs, list):
        for item in outputs:
            if isinstance(item, str):
                urls.append(item)
                continue
            if isinstance(item, dict):
                audio = item.get("audio")
                if isinstance(audio, dict) and isinstance(audio.get("url"), str):
                    urls.append(audio["url"])
                    continue
                direct = item.get("url")
                if isinstance(direct, str):
                    urls.append(direct)
                    continue
                nested = item.get("output")
                if isinstance(nested, dict) and isinstance(nested.get("url"), str):
                    urls.append(nested["url"])
        if urls:
            return urls
    elif isinstance(outputs, dict):
        audio = outputs.get("audio")
        if isinstance(audio, dict) and isinstance(audio.get("url"), str):
            urls.append(audio["url"])
            return urls

    # Single audio object
    audio = result.get("audio")
    if isinstance(audio, dict) and isinstance(audio.get("url"), str):
        urls.append(audio["url"])
        return urls

    # output object / direct URL string
    output = result.get("output")
    if isinstance(output, dict) and isinstance(output.get("url"), str):
        urls.append(output["url"])
        return urls
    if isinstance(output, str):
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
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".mp4",
        ".webm",
        ".mov",
        ".wav",
        ".mp3",
        ".flac",
        ".m4a",
    }:
        return suffix
    return ".mp3"
