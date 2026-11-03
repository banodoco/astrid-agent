#!/usr/bin/env python3
"""Golden demo: all wired cells for wan-2.2 and ltx-2.3.

Covers 8 wired cells across two models:

  wan-2.2:
    t2v/cloud, i2v/local, i2v/cloud, flf/cloud

  ltx-2.3:
    t2v/local, t2v/cloud, i2v/local, i2v/cloud, flf/local

Uses mocked ``HttpClient`` transport and mocked ``vibecomfy`` runtime so
no external services are required.  Every cell exercises the full
executor pipeline (``generate_video.run.main``) and asserts manifest
shape correctness.

.. attention::

   wan-2.2 flf/cloud is wired (Q1 confirmed: the fal image-to-video/turbo
   endpoint accepts ``end_image_url``), so it is included here even though
   the original task list omitted it for brevity.  See models.yaml for
   the registry entry.

Usage::

    python -m astrid.packs.generation.actions.generate_video.golden.demo_wan_ltx_local_cloud
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Minimal valid MP4 (ISO base media file) — just enough for ffprobe to parse
# and for the executor to compute a non-zero content hash.
# ---------------------------------------------------------------------------
# ftpy box + moov box with a minimal track header.  Not playable, but valid.
_MINIMAL_MP4 = (
    b"\x00\x00\x00\x1c"  # box size (28)
    b"ftyp"               # box type
    b"isom"               # major_brand
    b"\x00\x00\x02\x00"   # minor_version
    b"isom"               # compatible_brand
    b"iso2"               # compatible_brand
    b"mp41"               # compatible_brand
    b"\x00\x00\x00\x08"  # box size (8)
    b"moov"               # box type — empty moov makes ffprobe parse video
)


def _minimal_mp4_bytes() -> bytes:
    """Return a minimal MP4 file that ffprobe will accept as valid."""
    return _MINIMAL_MP4


# ---------------------------------------------------------------------------
# Transport mock for HttpClient (cloud cells)
# ---------------------------------------------------------------------------

def _build_cloud_transport():
    """Return a ``(Request) -> (status, bytes)`` transport that returns
    synthetic video data for fal.ai queue / response endpoints."""
    fake_video = _minimal_mp4_bytes()

    def transport(request: Any) -> tuple[int, bytes]:
        url = request.full_url if hasattr(request, "full_url") else str(request)
        method = getattr(request, "method", "GET")

        # POST / submit returns a queued job
        if method == "POST" and "queue.fal.run" in url:
            return (200, json.dumps({
                "status": "IN_QUEUE",
                "request_id": "mock-req-0001",
                "status_url": "https://queue.fal.run/mock/status/1",
                "response_url": "https://queue.fal.run/mock/response/1",
            }).encode())

        # GET status — return COMPLETED on second poll
        if "/status/" in url:
            return (200, json.dumps({"status": "COMPLETED"}).encode())

        # GET response — return video dict
        if "/response/" in url:
            return (200, json.dumps({
                "video": {"url": "https://mock.fal.run/output.mp4"},
                "request_id": "mock-req-0001",
                "cost": 0.05,
            }).encode())

        # GET binary download
        if "output.mp4" in url or "mock.fal.run" in url:
            return (200, fake_video)

        # Fallback
        return (404, b'{"error":"not found"}')

    return transport


# ---------------------------------------------------------------------------
# Vibecomfy mock (local cells)
# ---------------------------------------------------------------------------

class _FakeRunResult:
    """Minimal fake for vibecomfy.runtime.run result."""
    def __init__(self, outputs: list[str]):
        self.outputs = outputs


class _FakeInput:
    """Minimal fake for a ComfyUI node input."""
    def __init__(self, name: str, value: Any = None):
        self.name = name


class _FakeNode:
    """Minimal fake for a ComfyUI workflow node."""
    def __init__(self, class_type: str, inputs: dict[str, Any] | None = None):
        self.class_type = class_type
        self.inputs = inputs or {}
        self.widgets: dict[str, Any] = {}


class _FakeWorkflow:
    """Minimal fake for a vibecomfy workflow with set_input support."""
    def __init__(self):
        self._inputs: dict[str, Any] = {}
        self.metadata: dict[str, Any] = {"unbound_inputs": {}}
        self.nodes: dict[str, _FakeNode] = {}

    def set_input(self, name: str, value: Any) -> None:
        self._inputs[name] = value


def _build_fake_workflow_from_ready(template_id: str) -> _FakeWorkflow:
    """Return a fake workflow with nodes appropriate for the template."""
    wf = _FakeWorkflow()
    # Add a few standard nodes so node-target matching doesn't crash
    wf.nodes["1"] = _FakeNode("CLIPTextEncode", {"text": ""})
    wf.nodes["2"] = _FakeNode("KSampler", {"seed": 0, "steps": 20, "cfg": 7.0, "denoise": 1.0})
    wf.nodes["3"] = _FakeNode("LoadImage", {"image": ""})
    wf.nodes["4"] = _FakeNode("LoadImage", {"image": ""})  # second LoadImage for end ref
    wf.nodes["5"] = _FakeNode("KSamplerAdvanced", {"noise_seed": 0, "steps": 20, "cfg": 7.0})
    wf.nodes["6"] = _FakeNode("KSamplerAdvanced", {"noise_seed": 0, "steps": 20, "cfg": 7.0})
    wf.nodes["7"] = _FakeNode("EmptyHunyuanLatentVideo", {"width": 1024, "height": 576, "length": 81})
    wf.nodes["8"] = _FakeNode("EmptyLTXVLatentVideo", {"width": 1024, "height": 576, "length": 81})
    wf.nodes["9"] = _FakeNode("WanImageToVideo", {"positive": "", "width": 1024, "height": 576})
    wf.nodes["10"] = _FakeNode("VHS_VideoCombine", {"frame_rate": 24})
    wf.nodes["11"] = _FakeNode("SaveAnimatedWEBP", {"fps": 24})
    return wf


def _build_fake_run_sync(output_dir: Path):
    """Return a ``run_sync`` mock that creates a fake MP4 output file."""

    def fake_run_sync(wf: _FakeWorkflow) -> _FakeRunResult:
        out_file = output_dir / "fake_output.mp4"
        out_file.write_bytes(_minimal_mp4_bytes())
        return _FakeRunResult([str(out_file)])

    return fake_run_sync


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
def _resolve_image_ref() -> str:
    """Return a path to a tiny input image for i2v/flf tests."""
    tiny = Path(tempfile.gettempdir()) / "astrid_golden_tiny_ref.png"
    if tiny.exists():
        return str(tiny.resolve())
    # Minimal 1x1 grey PNG
    tiny.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
        b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return str(tiny.resolve())


def _call_executor(
    model: str,
    mode: str,
    execution: str,
    prompt: str,
    *,
    seed: int | None = None,
    image_ref: str | None = None,
    image_end_ref: str | None = None,
    negative_prompt: str | None = None,
    resolution: str | None = None,
    frames: int | None = None,
    fps: int | None = None,
    duration: float | None = None,
    out_dir: str,
) -> tuple[int, dict | None]:
    """Invoke ``generate_video.run.main`` and return the parsed manifest dict.

    Returns ``(exit_code, manifest)`` where ``manifest`` is ``None`` on failure.
    """
    from astrid.packs.generation.actions.generate_video.run import main

    argv = [
        "--model", model,
        "--mode", mode,
        "--execution", execution,
        "--prompt", prompt,
        "--out", out_dir,
    ]
    if seed is not None:
        argv.extend(["--seed", str(seed)])
    if image_ref:
        argv.extend(["--image-ref", image_ref])
    if image_end_ref:
        argv.extend(["--image-end-ref", image_end_ref])
    if negative_prompt:
        argv.extend(["--negative-prompt", negative_prompt])
    if resolution:
        argv.extend(["--resolution", resolution])
    if frames is not None:
        argv.extend(["--frames", str(frames)])
    if fps is not None:
        argv.extend(["--fps", str(fps)])
    if duration is not None:
        argv.extend(["--duration", str(duration)])

    code = main(argv)
    manifest_path = Path(out_dir) / "manifest.json"
    if code != 0:
        print(f"  EXECUTOR EXITED {code}", file=sys.stderr)
        return code, None
    if not manifest_path.exists():
        print(f"  Missing manifest: {manifest_path}", file=sys.stderr)
        return code, None
    return code, json.loads(manifest_path.read_text())


def _check_manifest_shape(manifest: dict, label: str, expected_mode: str) -> int:
    """Assert the standard v2 video manifest fields are present and sane.

    Returns 0 on success, 1 on any failure.
    """
    errors = 0

    def _err(msg: str) -> None:
        nonlocal errors
        print(f"  FAIL [{label}]: {msg}", file=sys.stderr)
        errors += 1

    # Required top-level keys
    for key in (
        "schema_version", "modality", "model", "mode_used",
        "model_actual", "execution", "request", "outputs",
        "seed", "created", "warnings",
    ):
        if key not in manifest:
            _err(f"missing key '{key}'")

    if manifest.get("schema_version") != 2:
        _err(f"schema_version={manifest.get('schema_version')} (expected 2)")

    if manifest.get("modality") != "video":
        _err(f"modality={manifest.get('modality')} (expected 'video')")

    mode_used = manifest.get("mode_used")
    if mode_used != expected_mode:
        _err(f"mode_used={mode_used!r} (expected {expected_mode!r})")

    model_actual = manifest.get("model_actual", "")
    if not model_actual:
        _err("model_actual is empty")

    # Video-specific request fields
    request = manifest.get("request", {})
    video_fields = ("frames", "fps", "duration", "resolution",
                    "image_ref_resolved", "image_end_ref_resolved")
    for fld in video_fields:
        if fld not in request:
            _err(f"request missing video field '{fld}'")

    # Outputs
    outputs = manifest.get("outputs") or []
    if len(outputs) == 0:
        _err("outputs is empty")

    for i, output in enumerate(outputs):
        for key in ("path", "content_hash", "bytes"):
            if key not in output:
                _err(f"outputs[{i}] missing '{key}'")
        ch = output.get("content_hash", "")
        if not ch.startswith("sha256:"):
            _err(f"outputs[{i}].content_hash does not start with 'sha256:'")
        if output.get("bytes", 0) <= 0:
            _err(f"outputs[{i}].bytes={output.get('bytes')} (expected > 0)")

    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


def run_demo() -> int:
    """Run the full golden demo against all wired cells.

    Returns 0 on success, non-zero on failure.
    """
    failures = 0
    prompt = "a single red triangle on a white background, simple, minimal"
    seed = 42
    neg = "blurry, complex, text, watermark"
    image_ref = _resolve_image_ref()
    end_ref = image_ref  # reuse for flf

    # ------------------------------------------------------------------
    # Cloud transport mock — injected into FalBackend's HttpClient
    # ------------------------------------------------------------------
    cloud_transport = _build_cloud_transport()

    # ------------------------------------------------------------------
    # 1. wan-2.2 t2v cloud  (cloud-only per FLAG-001)
    # ------------------------------------------------------------------
    print("\n=== wan-2.2 t2v cloud ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_wan_t2v_cloud_")))
    Path(out).mkdir(parents=True, exist_ok=True)

    with patch(
        "astrid.core.generation.backends.fal.default_client",
        return_value=__import__("astrid.core.util.http", fromlist=["HttpClient"]).HttpClient(
            transport=cloud_transport
        ),
    ):
        code, manifest = _call_executor(
            "wan-2.2", "t2v", "cloud", prompt,
            seed=seed, negative_prompt=neg,
            resolution="1280x720", frames=81, fps=24,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: wan-2.2 t2v cloud returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "wan-2.2/t2v/cloud", "t2v")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 2. wan-2.2 i2v local
    # ------------------------------------------------------------------
    print("\n=== wan-2.2 i2v local ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_wan_i2v_local_")))

    fake_run_sync = _build_fake_run_sync(Path(out) / "videos")

    with patch(
        "vibecomfy.registry.ready.workflow_from_ready",
        side_effect=_build_fake_workflow_from_ready,
    ), patch(
        "vibecomfy.runtime.run.run_sync",
        side_effect=fake_run_sync,
    ):
        code, manifest = _call_executor(
            "wan-2.2", "i2v", "local", prompt,
            seed=seed, image_ref=image_ref,
            resolution="1280x720", frames=81,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: wan-2.2 i2v local returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "wan-2.2/i2v/local", "i2v")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 3. wan-2.2 i2v cloud
    # ------------------------------------------------------------------
    print("\n=== wan-2.2 i2v cloud ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_wan_i2v_cloud_")))

    with patch(
        "astrid.core.generation.backends.fal.default_client",
        return_value=__import__("astrid.core.util.http", fromlist=["HttpClient"]).HttpClient(
            transport=cloud_transport
        ),
    ):
        code, manifest = _call_executor(
            "wan-2.2", "i2v", "cloud", prompt,
            seed=seed, image_ref=image_ref,
            resolution="1280x720", frames=81,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: wan-2.2 i2v cloud returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "wan-2.2/i2v/cloud", "i2v")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 4. wan-2.2 flf cloud  (Q1 confirmed: end_image_url accepted)
    # ------------------------------------------------------------------
    print("\n=== wan-2.2 flf cloud ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_wan_flf_cloud_")))

    with patch(
        "astrid.core.generation.backends.fal.default_client",
        return_value=__import__("astrid.core.util.http", fromlist=["HttpClient"]).HttpClient(
            transport=cloud_transport
        ),
    ):
        code, manifest = _call_executor(
            "wan-2.2", "flf", "cloud", prompt,
            seed=seed, image_ref=image_ref, image_end_ref=end_ref,
            resolution="1280x720", frames=81,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: wan-2.2 flf cloud returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "wan-2.2/flf/cloud", "flf")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 5. ltx-2.3 t2v local
    # ------------------------------------------------------------------
    print("\n=== ltx-2.3 t2v local ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_ltx_t2v_local_")))

    fake_run_sync = _build_fake_run_sync(Path(out) / "videos")

    with patch(
        "vibecomfy.registry.ready.workflow_from_ready",
        side_effect=_build_fake_workflow_from_ready,
    ), patch(
        "vibecomfy.runtime.run.run_sync",
        side_effect=fake_run_sync,
    ):
        code, manifest = _call_executor(
            "ltx-2.3", "t2v", "local", prompt,
            seed=seed, resolution="1280x720", frames=81, fps=24,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: ltx-2.3 t2v local returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "ltx-2.3/t2v/local", "t2v")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 6. ltx-2.3 t2v cloud
    # ------------------------------------------------------------------
    print("\n=== ltx-2.3 t2v cloud ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_ltx_t2v_cloud_")))

    with patch(
        "astrid.core.generation.backends.fal.default_client",
        return_value=__import__("astrid.core.util.http", fromlist=["HttpClient"]).HttpClient(
            transport=cloud_transport
        ),
    ):
        code, manifest = _call_executor(
            "ltx-2.3", "t2v", "cloud", prompt,
            seed=seed, resolution="1280x720", frames=81, fps=24,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: ltx-2.3 t2v cloud returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "ltx-2.3/t2v/cloud", "t2v")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 7. ltx-2.3 i2v local
    # ------------------------------------------------------------------
    print("\n=== ltx-2.3 i2v local ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_ltx_i2v_local_")))

    fake_run_sync = _build_fake_run_sync(Path(out) / "videos")

    with patch(
        "vibecomfy.registry.ready.workflow_from_ready",
        side_effect=_build_fake_workflow_from_ready,
    ), patch(
        "vibecomfy.runtime.run.run_sync",
        side_effect=fake_run_sync,
    ):
        code, manifest = _call_executor(
            "ltx-2.3", "i2v", "local", prompt,
            seed=seed, image_ref=image_ref,
            resolution="1280x720", frames=81, fps=24,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: ltx-2.3 i2v local returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "ltx-2.3/i2v/local", "i2v")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 8. ltx-2.3 i2v cloud
    # ------------------------------------------------------------------
    print("\n=== ltx-2.3 i2v cloud ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_ltx_i2v_cloud_")))

    with patch(
        "astrid.core.generation.backends.fal.default_client",
        return_value=__import__("astrid.core.util.http", fromlist=["HttpClient"]).HttpClient(
            transport=cloud_transport
        ),
    ):
        code, manifest = _call_executor(
            "ltx-2.3", "i2v", "cloud", prompt,
            seed=seed, image_ref=image_ref,
            resolution="1280x720", frames=81, fps=24,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: ltx-2.3 i2v cloud returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "ltx-2.3/i2v/cloud", "i2v")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # 9. ltx-2.3 flf local  (local-only per A5)
    # ------------------------------------------------------------------
    print("\n=== ltx-2.3 flf local ===")
    out = str(Path(tempfile.mkdtemp(prefix="golden_ltx_flf_local_")))

    fake_run_sync = _build_fake_run_sync(Path(out) / "videos")

    with patch(
        "vibecomfy.registry.ready.workflow_from_ready",
        side_effect=_build_fake_workflow_from_ready,
    ), patch(
        "vibecomfy.runtime.run.run_sync",
        side_effect=fake_run_sync,
    ):
        code, manifest = _call_executor(
            "ltx-2.3", "flf", "local", prompt,
            seed=seed, image_ref=image_ref, image_end_ref=end_ref,
            resolution="1280x720", frames=81, fps=24,
            out_dir=out,
        )

    if code != 0 or manifest is None:
        print("  FAIL: ltx-2.3 flf local returned code", code, file=sys.stderr)
        failures += 1
    else:
        failures += _check_manifest_shape(manifest, "ltx-2.3/flf/local", "flf")
        print(f"  model={manifest['model']} mode={manifest['mode_used']} "
              f"execution={manifest['execution']}")
        print(f"  model_actual={manifest['model_actual']}")
        print(f"  outputs={len(manifest['outputs'])} "
              f"content_hash={manifest['outputs'][0]['content_hash']}")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\n{'='*60}")
    if failures:
        print(f"DEMO FAILED: {failures} assertion(s) failed", file=sys.stderr)
    else:
        print("DEMO PASSED: all checks green")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_demo())
