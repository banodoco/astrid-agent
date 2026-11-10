"""T6.4 — shared raw/SDK conformance harness (one harness, both backends).

The fixture pack at ``tests/fixtures/renderer_packs/sdk/`` ships ONE canonical
implementation (``_shared.py``) behind TWO thin backends:

* ``render.py`` — raw-command backend (pure stdlib, no Astrid SDK import);
* ``sdk_render.py`` — SDK backend that delegates the whole protocol to
  ``astrid.sdk.rendering.renderer_main`` (T6.2 shared contract).

This file is the single conformance harness: every case below is driven
through BOTH backends with :class:`CommandTransport` (exactly like
production) and asserts the emitted result/support JSON is semantically
identical — same keys, same normalized values; only workspace-absolute paths
may differ, while hashes and profile values must match exactly.

Covered cases (each a committed fixture request):

1. minimal render (media-only timeline);
2. request-sensitive support (supported only for the specific
   window=[0, 48) @ 24fps + supported-audio combination);
3. passthrough audio (visual-only media, audio_ownership='passthrough');
4. no-audio (visual-only, audio_ownership='none');
5. attachment (named byte payload alongside the primary video);
6. intentional failure (invalid output name -> structured RendererError).

If ``astrid.sdk.rendering`` is not present yet (T6.2 still in flight), the SDK
half of every case is skipped with a clear reason while the raw half is still
fully asserted; the SDK half lights up automatically once the module lands.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


from astrid.core.rendering import RenderResult, SupportReport
from astrid.core.rendering.errors import RendererException
from astrid.core.rendering.transport import CommandTransport

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "fixtures" / "renderer_packs" / "sdk"
PACK_ROOT = FIXTURE_ROOT
REQUESTS_DIR = FIXTURE_ROOT / "requests"
BACKEND_ID = "sdk.renderer"
BACKEND_VERSION = "1.0.0"

RAW_ENTRYPOINT = "render.py"
SDK_ENTRYPOINT = "sdk_render.py"

SUPPORTED_WINDOW_FRAMES = 48
ATTACHMENT_NAME = "sdk_manifest.json"



# ---------------------------------------------------------------------------
# Harness plumbing
# ---------------------------------------------------------------------------


def _write_request(workspace: Path, request_name: str) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    request = json.loads((REQUESTS_DIR / request_name).read_text(encoding="utf-8"))
    request_path = workspace / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    timeline = REQUESTS_DIR / "timeline.json"
    if timeline.is_file():
        shutil.copyfile(timeline, workspace / "timeline.json")
    return request_path


def _run_backend(
    workspace: Path,
    entrypoint: str,
    verb: str,
    request_name: str,
) -> tuple[CommandTransport, object, dict]:
    """Run one backend through CommandTransport and return the written result.

    Returns ``(transport, value, payload)`` where *value* is the validated DTO
    on success or the raised :class:`RendererException` for a structured
    backend error, and *payload* is the authoritative result-file JSON.
    """
    request_path = _write_request(workspace, request_name)
    result_path = workspace / "result.json"
    transport = CommandTransport(BACKEND_ID, termination_grace=0.15)
    try:
        value = transport.run(
            verb,
            [sys.executable, entrypoint],
            request_path=request_path,
            result_path=result_path,
            cwd=PACK_ROOT,
            timeout=30,
        )
    except RendererException as exc:
        value = exc
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    return transport, value, payload


def _normalize(value: object, workspace: Path) -> object:
    """Recursively strip workspace-absolute prefixes so the two workspaces
    compare semantically (relative paths/hashes/profile values must match).

    Empty ``stdout``/``stderr`` captures are also dropped from error
    ``details``: the service-dispatch (SDK) path enriches structured errors
    with the child's log captures (production transport behavior), while the
    raw path writes only the backend-emitted payload. Both backends are
    silent, so the captures are empty strings — not wire fields emitted by
    either backend.
    """
    workspace_str = str(workspace.resolve())
    if isinstance(value, str):
        if value.startswith(workspace_str):
            value = value[len(workspace_str):].lstrip("/\\")
        return value.replace("\\", "/")
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if key in ("stdout", "stderr") and item == "":
                continue
            normalized[key] = _normalize(item, workspace)
        return normalized
    if isinstance(value, list):
        return [_normalize(item, workspace) for item in value]
    return value


def _run_both(
    tmp_path: Path,
    verb: str,
    request_name: str,
) -> tuple[tuple[CommandTransport, object, dict], tuple[CommandTransport, object, dict]]:
    raw = _run_backend(tmp_path / "raw", RAW_ENTRYPOINT, verb, request_name)
    sdk = _run_backend(tmp_path / "sdk", SDK_ENTRYPOINT, verb, request_name)
    return raw, sdk


def _assert_parity(raw_payload: dict, sdk_payload: dict, raw_ws: Path, sdk_ws: Path, *, label: str) -> None:
    raw_normalized = _normalize(raw_payload, raw_ws)
    sdk_normalized = _normalize(sdk_payload, sdk_ws)
    assert raw_normalized == sdk_normalized, (
        f"{label}: raw and SDK wire JSON differ\n"
        f"--- raw ---\n{json.dumps(raw_normalized, indent=2, sort_keys=True)}\n"
        f"--- sdk ---\n{json.dumps(sdk_normalized, indent=2, sort_keys=True)}"
    )


def _assert_clean_render_payload(payload: dict, *, audio: str, frames: int) -> None:
    assert payload["schema_version"] == 1
    assert payload["audio_ownership"] == audio
    video = payload["video"]
    assert video["audio"] == audio
    assert video["duration_frames"] == frames
    assert video["path"].startswith("outputs/")
    assert len(video["sha256"]) == 64
    profile = video["profile"]
    assert profile["width"] == 1920
    assert profile["height"] == 1080
    assert profile["fps_rational"] == [24, 1]
    assert profile["time_base"] == [1, 12288]
    assert profile["container"] == "mp4"
    assert profile["video_codec"] == "h264"
    assert profile["pixel_format"] == "yuv420p"
    if audio == "rendered":
        assert profile["audio_codec"] == "pcm_s16le"
        assert profile["audio_sample_rate"] == 48000
        assert profile["audio_channel_layout"] == "stereo"
    else:
        assert profile["audio_codec"] is None
        assert profile["audio_sample_rate"] is None
        assert profile["audio_channel_layout"] is None
    assert payload["backend_fragments"][BACKEND_ID]["renderer"] == "sdk"
    assert payload["normalization"] == []
    assert payload["logs"] == []
    assert payload["metadata"] == {}


def _assert_transport_value(value: object, expected_type: type) -> None:
    assert isinstance(value, expected_type), (
        f"transport returned {type(value).__name__}, expected {expected_type.__name__}"
    )


# ---------------------------------------------------------------------------
# Case 1 — minimal render (media-only timeline)
# ---------------------------------------------------------------------------


def test_minimal_render_wire_parity(tmp_path: Path) -> None:
    (raw_transport, raw_value, raw_payload), (sdk_transport, sdk_value, sdk_payload) = (
        _run_both(tmp_path, "render", "minimal-render.json")
    )

    _assert_transport_value(raw_value, RenderResult)
    _assert_transport_value(sdk_value, RenderResult)
    _assert_clean_render_payload(raw_payload, audio="rendered", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_clean_render_payload(sdk_payload, audio="rendered", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_parity(raw_payload, sdk_payload, tmp_path / "raw", tmp_path / "sdk", label="minimal render")

    # Determinism: hashes must match between backends AND across invocations.
    assert raw_payload["video"]["sha256"] == sdk_payload["video"]["sha256"]
    _, second_value, second_payload = _run_backend(
        tmp_path / "raw-2", RAW_ENTRYPOINT, "render", "minimal-render.json"
    )
    _assert_transport_value(second_value, RenderResult)
    assert second_payload["video"]["sha256"] == raw_payload["video"]["sha256"]

    # The raw backend never touches the Astrid ledger.
    assert list((tmp_path / "raw").rglob("run.json")) == []
    assert raw_transport.last_logs == {"stdout": "", "stderr": ""}
    assert sdk_transport.last_logs == {"stdout": "", "stderr": ""}


# ---------------------------------------------------------------------------
# Case 2 — request-sensitive support (specific window/audio combination)
# ---------------------------------------------------------------------------


def test_request_sensitive_support_wire_parity(tmp_path: Path) -> None:
    # Supported combination: window [0, 48) @ 24fps + supported audio.
    (_, raw_supported, raw_supported_payload), (_, sdk_supported, sdk_supported_payload) = (
        _run_both(tmp_path / "supported", "support", "support-supported.json")
    )
    _assert_transport_value(raw_supported, SupportReport)
    _assert_transport_value(sdk_supported, SupportReport)
    assert raw_supported_payload["supported"] is True
    assert raw_supported_payload["features"] == {"media": True, "audio_mode": "rendered"}
    assert raw_supported_payload["reasons"] == []
    assert raw_supported_payload["backend"] == BACKEND_ID
    assert raw_supported_payload["backend_version"] == BACKEND_VERSION
    _assert_parity(
        raw_supported_payload,
        sdk_supported_payload,
        tmp_path / "supported" / "raw",
        tmp_path / "supported" / "sdk",
        label="support (supported combination)",
    )

    # Unsupported combination: same request, different window [0, 96).
    (_, raw_unsupported, raw_unsupported_payload), (_, sdk_unsupported, sdk_unsupported_payload) = (
        _run_both(tmp_path / "unsupported", "support", "support-window.json")
    )
    _assert_transport_value(raw_unsupported, SupportReport)
    _assert_transport_value(sdk_unsupported, SupportReport)
    assert raw_unsupported_payload["supported"] is False
    assert raw_unsupported_payload["features"] == {"media": False, "audio_mode": "none"}
    assert len(raw_unsupported_payload["reasons"]) == 1
    assert "[0, 96)" in raw_unsupported_payload["reasons"][0]
    assert raw_unsupported_payload["alternatives"] == []
    assert raw_unsupported_payload["backend"] == BACKEND_ID
    _assert_parity(
        raw_unsupported_payload,
        sdk_unsupported_payload,
        tmp_path / "unsupported" / "raw",
        tmp_path / "unsupported" / "sdk",
        label="support (unsupported window combination)",
    )

    # The decision is genuinely request-sensitive: same backend, two requests.
    assert raw_supported_payload["supported"] is not raw_unsupported_payload["supported"]


# ---------------------------------------------------------------------------
# Case 3 — passthrough audio
# ---------------------------------------------------------------------------


def test_passthrough_audio_wire_parity(tmp_path: Path) -> None:
    (_, raw_value, raw_payload), (_, sdk_value, sdk_payload) = (
        _run_both(tmp_path, "render", "passthrough-render.json")
    )
    _assert_transport_value(raw_value, RenderResult)
    _assert_transport_value(sdk_value, RenderResult)
    _assert_clean_render_payload(raw_payload, audio="passthrough", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_clean_render_payload(sdk_payload, audio="passthrough", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_parity(raw_payload, sdk_payload, tmp_path / "raw", tmp_path / "sdk", label="passthrough audio")
    assert raw_payload["video"]["sha256"] == sdk_payload["video"]["sha256"]
    assert raw_payload["backend_fragments"][BACKEND_ID]["audio_mode"] == "passthrough"


# ---------------------------------------------------------------------------
# Case 4 — no-audio (visual-only)
# ---------------------------------------------------------------------------


def test_no_audio_wire_parity(tmp_path: Path) -> None:
    (_, raw_value, raw_payload), (_, sdk_value, sdk_payload) = (
        _run_both(tmp_path, "render", "no-audio-render.json")
    )
    _assert_transport_value(raw_value, RenderResult)
    _assert_transport_value(sdk_value, RenderResult)
    _assert_clean_render_payload(raw_payload, audio="none", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_clean_render_payload(sdk_payload, audio="none", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_parity(raw_payload, sdk_payload, tmp_path / "raw", tmp_path / "sdk", label="no-audio render")
    assert raw_payload["video"]["sha256"] == sdk_payload["video"]["sha256"]
    assert raw_payload["backend_fragments"][BACKEND_ID]["audio_mode"] == "none"


# ---------------------------------------------------------------------------
# Case 5 — attachment (named byte payload)
# ---------------------------------------------------------------------------


def test_attachment_wire_parity(tmp_path: Path) -> None:
    (_, raw_value, raw_payload), (_, sdk_value, sdk_payload) = (
        _run_both(tmp_path, "render", "attachment-render.json")
    )
    _assert_transport_value(raw_value, RenderResult)
    _assert_transport_value(sdk_value, RenderResult)
    _assert_clean_render_payload(raw_payload, audio="rendered", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_clean_render_payload(sdk_payload, audio="rendered", frames=SUPPORTED_WINDOW_FRAMES)
    _assert_parity(raw_payload, sdk_payload, tmp_path / "raw", tmp_path / "sdk", label="attachment render")

    for workspace, payload in (
        (tmp_path / "raw", raw_payload),
        (tmp_path / "sdk", sdk_payload),
    ):
        attachments = payload["video"]["attachments"]
        assert set(attachments) == {ATTACHMENT_NAME}
        attachment = attachments[ATTACHMENT_NAME]
        assert attachment["name"] == ATTACHMENT_NAME
        assert attachment["kind"] == "json"
        assert len(attachment["sha256"]) == 64
        attachment_file = workspace / attachment["path"]
        assert attachment_file.is_file()
        # The declared digest is the REAL digest of the written byte payload.
        from astrid.core.foundation.hash import sha256_file

        assert sha256_file(attachment_file) == attachment["sha256"]

    assert raw_payload["video"]["sha256"] == sdk_payload["video"]["sha256"]
    assert (
        raw_payload["video"]["attachments"][ATTACHMENT_NAME]["sha256"]
        == sdk_payload["video"]["attachments"][ATTACHMENT_NAME]["sha256"]
    )


# ---------------------------------------------------------------------------
# Case 6 — intentional failure (invalid output -> structured error)
# ---------------------------------------------------------------------------


def test_intentional_failure_wire_parity(tmp_path: Path) -> None:
    (_, raw_value, raw_payload), (_, sdk_value, sdk_payload) = (
        _run_both(tmp_path, "render", "failure-render.json")
    )

    # Both transports surface the structured RendererError (kind=protocol).
    assert isinstance(raw_value, RendererException)
    assert isinstance(sdk_value, RendererException)
    assert raw_value.error.kind == "protocol"
    assert raw_value.error.backend == BACKEND_ID
    assert sdk_value.error.kind == "protocol"
    assert sdk_value.error.backend == BACKEND_ID
    assert "invalid output name" in raw_value.error.message

    # And both wrote the SAME structured error payload to the result file.
    _assert_parity(raw_payload, sdk_payload, tmp_path / "raw", tmp_path / "sdk", label="intentional failure")
    assert raw_payload["kind"] == "protocol"
    assert raw_payload["backend"] == BACKEND_ID
    assert raw_payload["message"] == sdk_payload["message"]
    assert "invalid output name" in raw_payload["message"]
    assert raw_payload["details"] == {"error_type": "ValueError"}
    assert raw_payload["recovery_command"] is None
    assert sdk_payload["recovery_command"] is None


# ---------------------------------------------------------------------------
# Static fixture surface (no backend execution)
# ---------------------------------------------------------------------------


def test_fixture_pack_static_surface(tmp_path: Path) -> None:
    from unittest import mock

    from astrid.core.pack import load_pack_manifest
    from astrid.core.pack.validate import validate_pack
    from astrid.core.rendering import registry as rendering_registry_module
    from astrid.core.rendering.contracts import RendererManifest
    from astrid.core.rendering.registry import load_default_registries

    errors, _warnings = validate_pack(str(PACK_ROOT))
    assert not errors, errors

    pack = load_pack_manifest(PACK_ROOT / "pack.yaml")
    assert pack.id == "sdk"
    permission_ids = {permission.id for permission in pack.permissions}
    assert permission_ids == {"subprocess", "project_files"}
    assert pack.schema_version == "3"
    assert pack.rendering == {"renderer": {"type": "renderer", "path": "rendering/renderer.yaml"}}

    def scanner(source_root: Path):
        def scan(root: str | Path | None = None):
            from astrid.core.pack import discover_packs

            return discover_packs(source_root if root is None else root)

        return scan

    empty_source = tmp_path / "empty-source"
    empty_source.mkdir()
    with (
        mock.patch.object(
            rendering_registry_module,
            "discover_packs",
            side_effect=scanner(empty_source),
        ),
        mock.patch.dict(os.environ, {"ASTRID_PACKS_PATH": ""}, clear=False),
    ):
        renderers, _, _ = load_default_registries(
            tmp_path / "project",
            extra_pack_roots=(str(FIXTURE_ROOT.parent),),
        )
    candidate = renderers.get(BACKEND_ID)
    assert candidate.source_kind == "extra"
    assert isinstance(candidate.manifest, RendererManifest)
    assert candidate.manifest.command == ("python3", "render.py")
    assert candidate.manifest.operations == ("render", "support")
    assert candidate.manifest.protocol_version == 1
    assert candidate.execution_eligible is True

    caps = candidate.manifest.capabilities
    assert "media" in caps["clip_types"]
    assert {"visual", "audio"} <= set(caps["track_types"])
    assert caps["features"] == {
        "media": True,
        "audio_mode": "dynamic",
        "deterministic": True,
    }
    assert caps["supports_windows"] is True
    assert caps["output_profiles"] == ["video/mp4"]
    assert set(caps["audio_ownership"]) == {"rendered", "passthrough", "none"}
