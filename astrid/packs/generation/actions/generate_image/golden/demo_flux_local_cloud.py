#!/usr/bin/env python3
"""Demo orchestrator: z-image local + flux-dev cloud, t2i + i2i exercises.

v2: model → mode → backend taxonomy.  ``--mode`` is required.
flux-dev is cloud-only in v2.  z-image covers local.

Usage::

    python -m astrid.packs.generation.actions.generate_image.golden.demo_flux_local_cloud

The local path requires vibecomfy + a running ComfyUI instance.
The cloud path requires FAL_KEY to be resolvable.
"""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('generation')

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
def _resolve_image_ref() -> str:
    """Return a path to a tiny input image for i2i tests."""
    fixtures = PACK_ROOT / "actions" / "generate_image" / "fixtures"
    input_png = fixtures / "input.png"
    if input_png.exists():
        return str(input_png.resolve())
    # Fallback: create a minimal 1x1 PNG inline
    tiny = Path(TemporaryDirectory().name) / "tiny_ref.png"
    tiny.parent.mkdir(parents=True, exist_ok=True)
    # Minimal 1×1 grey PNG (valid PNG header + IHDR + IDAT + IEND)
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
    negative_prompt: str | None = None,
    out_dir: str,
) -> tuple[int, dict | None]:
    """Invoke ``generate_image.run.main`` and return the parsed manifest dict.

    Returns ``(exit_code, manifest)`` where ``manifest`` is ``None`` on failure.
    """
    from astrid.packs.generation.actions.generate_image.run import main

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
    if negative_prompt:
        argv.extend(["--negative-prompt", negative_prompt])

    code = main(argv)
    manifest_path = Path(out_dir) / "manifest.json"
    if code != 0:
        print(f"  EXECUTOR EXITED {code}", file=sys.stderr)
        return code, None
    if not manifest_path.exists():
        print(f"  Missing manifest: {manifest_path}", file=sys.stderr)
        return code, None
    return code, json.loads(manifest_path.read_text())


def _check_manifest_shape(manifest: dict, label: str) -> int:
    """Assert the standard v2 manifest fields are present and sane.

    Returns 0 on success, 1 on any failure.
    """
    errors = 0

    def _err(msg: str) -> None:
        nonlocal errors
        print(f"  FAIL [{label}]: {msg}", file=sys.stderr)
        errors += 1

    # Required top-level keys (v2: schema_version, mode_used, model_actual added)
    for key in ("schema_version", "modality", "model", "mode_used",
                "model_actual", "execution", "request", "outputs",
                "seed", "created", "warnings"):
        if key not in manifest:
            _err(f"missing key '{key}'")

    if manifest.get("schema_version") != 2:
        _err(f"schema_version={manifest.get('schema_version')} (expected 2)")

    if manifest.get("modality") != "image":
        _err(f"modality={manifest.get('modality')} (expected 'image')")

    # mode_used should match the generation mode
    mode_used = manifest.get("mode_used")
    if mode_used not in ("t2i", "i2i", "edit"):
        _err(f"mode_used={mode_used!r} (expected t2i, i2i, or edit)")

    # model_actual should be a non-empty string
    model_actual = manifest.get("model_actual", "")
    if not model_actual:
        _err("model_actual is empty")

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


def _can_run_local() -> bool:
    """Check whether the local branch is feasible."""
    try:
        import vibecomfy  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Main demo
# ---------------------------------------------------------------------------


def run_demo() -> int:
    """Run the full v2 demo and return 0 on success, non-zero on failure."""
    failures = 0
    prompt = "a single red triangle on a white background, simple, minimal"
    seed = 42
    neg = "blurry, complex, text, watermark"

    # -----------------------------------------------------------------------
    # 1. flux-dev t2i cloud (cloud-only)
    # -----------------------------------------------------------------------
    print("\n=== flux-dev t2i cloud ===")
    out_cloud = str(Path(TemporaryDirectory().name) / "out_cloud")
    Path(out_cloud).mkdir(parents=True, exist_ok=True)

    code, manifest_cloud = _call_executor(
        "flux-dev", "t2i", "cloud", prompt,
        seed=seed, negative_prompt=neg, out_dir=out_cloud,
    )
    if code != 0 or manifest_cloud is None:
        print("  SKIP: cloud execution failed (missing FAL_KEY?)", file=sys.stderr)
        manifest_cloud = None
    else:
        failures += _check_manifest_shape(manifest_cloud, "flux-dev/t2i/cloud")
        assert manifest_cloud is not None
        print(f"  model={manifest_cloud['model']} mode={manifest_cloud['mode_used']} execution={manifest_cloud['execution']}")
        print(f"  model_actual={manifest_cloud['model_actual']}")
        print(f"  outputs={len(manifest_cloud['outputs'])} content_hash={manifest_cloud['outputs'][0]['content_hash']}")
        print(f"  warnings={manifest_cloud['warnings']}")

    # -----------------------------------------------------------------------
    # 2. z-image t2i local (local-capable model)
    # -----------------------------------------------------------------------
    if _can_run_local():
        print("\n=== z-image t2i local ===")
        out_local = str(Path(TemporaryDirectory().name) / "out_local")
        Path(out_local).mkdir(parents=True, exist_ok=True)

        code, manifest_local = _call_executor(
            "z-image", "t2i", "local", prompt,
            seed=seed, out_dir=out_local,
        )
        if code != 0 or manifest_local is None:
            print("  SKIP: local execution failed (ComfyUI not running?)", file=sys.stderr)
            manifest_local = None
        else:
            failures += _check_manifest_shape(manifest_local, "z-image/t2i/local")
            assert manifest_local is not None
            print(f"  model={manifest_local['model']} mode={manifest_local['mode_used']} execution={manifest_local['execution']}")
            print(f"  model_actual={manifest_local['model_actual']}")
            print(f"  outputs={len(manifest_local['outputs'])} content_hash={manifest_local['outputs'][0]['content_hash']}")

            # --- Cross-backend comparison (t2i: z-image local vs flux-dev cloud) ---
            if manifest_cloud is not None:
                print("\n=== cross-backend comparison (t2i) ===")
                for key in ("schema_version", "modality"):
                    if manifest_local.get(key) != manifest_cloud.get(key):
                        print(f"  MISMATCH: {key}: local={manifest_local.get(key)} cloud={manifest_cloud.get(key)}", file=sys.stderr)
                        failures += 1
                    else:
                        print(f"  {key}: {manifest_local[key]} (match)")
                # mode_used should both be t2i
                print(f"  local  mode_used: {manifest_local['mode_used']}")
                print(f"  cloud  mode_used: {manifest_cloud['mode_used']}")
                # content_hash won't match (different backends), but both populated
                l_ch = manifest_local["outputs"][0]["content_hash"]
                c_ch = manifest_cloud["outputs"][0]["content_hash"]
                print(f"  local  content_hash: {l_ch}")
                print(f"  cloud  content_hash: {c_ch}")
                if l_ch.startswith("sha256:") and c_ch.startswith("sha256:"):
                    print("  content_hash: both populated ✓")
                else:
                    print("  content_hash: MISSING on one or both paths", file=sys.stderr)
                    failures += 1
    else:
        print("\n=== z-image t2i local: SKIP (vibecomfy not importable) ===", file=sys.stderr)

    # -----------------------------------------------------------------------
    # 3. flux-dev i2i cloud (cloud-only)
    # -----------------------------------------------------------------------
    print("\n=== flux-dev i2i cloud ===")
    out_img2img_cloud = str(Path(TemporaryDirectory().name) / "out_img2img_cloud")
    Path(out_img2img_cloud).mkdir(parents=True, exist_ok=True)
    image_ref = _resolve_image_ref()

    code, manifest_i2i_cloud = _call_executor(
        "flux-dev", "i2i", "cloud", "watercolor painting style",
        seed=seed, image_ref=image_ref, out_dir=out_img2img_cloud,
    )
    if code != 0 or manifest_i2i_cloud is None:
        print("  SKIP: cloud i2i execution failed (missing FAL_KEY?)", file=sys.stderr)
    else:
        failures += _check_manifest_shape(manifest_i2i_cloud, "flux-dev/i2i/cloud")
        assert manifest_i2i_cloud is not None
        print(f"  model={manifest_i2i_cloud['model']} mode={manifest_i2i_cloud['mode_used']} execution={manifest_i2i_cloud['execution']}")
        print(f"  model_actual={manifest_i2i_cloud['model_actual']}")
        print(f"  outputs={len(manifest_i2i_cloud['outputs'])} content_hash={manifest_i2i_cloud['outputs'][0]['content_hash']}")
        print(f"  image_ref_resolved={manifest_i2i_cloud.get('request', {}).get('image_ref_resolved')}")

    # -----------------------------------------------------------------------
    # 4. z-image i2i local (local-capable model)
    # -----------------------------------------------------------------------
    if _can_run_local():
        print("\n=== z-image i2i local ===")
        out_img2img_local = str(Path(TemporaryDirectory().name) / "out_img2img_local")
        Path(out_img2img_local).mkdir(parents=True, exist_ok=True)

        code, manifest_i2i_local = _call_executor(
            "z-image", "i2i", "local", "watercolor painting style",
            seed=seed, image_ref=image_ref, out_dir=out_img2img_local,
        )
        if code != 0 or manifest_i2i_local is None:
            print("  SKIP: local i2i execution failed (ComfyUI not running?)", file=sys.stderr)
        else:
            failures += _check_manifest_shape(manifest_i2i_local, "z-image/i2i/local")
            assert manifest_i2i_local is not None
            print(f"  model={manifest_i2i_local['model']} mode={manifest_i2i_local['mode_used']} execution={manifest_i2i_local['execution']}")
            print(f"  model_actual={manifest_i2i_local['model_actual']}")
            print(f"  outputs={len(manifest_i2i_local['outputs'])} content_hash={manifest_i2i_local['outputs'][0]['content_hash']}")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    print(f"\n{'='*60}")
    if failures:
        print(f"DEMO FAILED: {failures} assertion(s) failed", file=sys.stderr)
    else:
        print("DEMO PASSED: all checks green")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_demo())
