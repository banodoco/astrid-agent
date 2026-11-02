"""M2-11a skeleton: in-process ``wan2gp.generate_video`` over a session client.

Pure pack logic only. The action compiles settings, submits them once through
the session client, then materializes and verifies the result exactly as the
host path does. It never owns the native child, the admission token or the
managed tool session; session authority is reached only through
``session.submit`` and ``session.cancel``.

The host path this mirrors is ``GenericPackHost._run_wan_session``. Each
deliberate difference is listed in the module docstring of the test file
``tests/packs/wan2gp/test_session_action_skeleton.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.packs.wan2gp.src.driver import (
    compile_host_settings,
    materialize_host_result,
    verify_host_result,
)


def generate_video_session(
    *,
    prompt: str,
    model: str = "wan-2.2",
    negative_prompt: str | None = None,
    resolution: str | None = None,
    video_length: int | None = None,
    fps: str | None = None,
    seed: int | None = None,
    guidance_scale: float | None = None,
    steps: int | None = None,
    loras: str | None = None,
    output_root: Path,
    timeout_seconds: float,
    session: Any,
) -> dict[str, Any]:
    """Run one Wan2GP generation through ``session``. Submit is never retried."""
    inputs: dict[str, Any] = {
        "prompt": prompt,
        "model": model,
        "negative_prompt": negative_prompt,
        "resolution": resolution,
        "video_length": video_length,
        "fps": fps,
        "seed": seed,
        "guidance_scale": guidance_scale,
        "steps": steps,
        "loras": loras,
    }
    clean: dict[str, Any] = {k: v for k, v in inputs.items() if v is not None}

    # Invalid inputs raise ValueError here, before anything is submitted.
    settings = compile_host_settings(clean)

    evidence = session.submit(settings, timeout_seconds=timeout_seconds)

    try:
        result = evidence.get("result") if isinstance(evidence, Mapping) else None
        if not isinstance(result, Mapping) or result.get("success") is not True:
            errors = result.get("errors", []) if isinstance(result, Mapping) else []
            if not errors and isinstance(evidence, Mapping) and isinstance(evidence.get("native_error"), str):
                errors = [evidence["native_error"]]
            raise RuntimeError(
                "native Wan2GP job failed: " + "; ".join(str(item) for item in errors)
            )
        mapped = materialize_host_result(evidence, attempt_root=output_root, inputs=clean)
        verify_host_result(mapped, attempt_root=output_root)
    except Exception as exc:
        # The run was submitted, so ask the host to stop it. A cancel failure
        # must not hide the original error.
        try:
            session.cancel(f"wan2gp.generate_video_session failed after submit: {type(exc).__name__}")
        except Exception:
            pass
        raise

    return {
        "payload": {"wan_native": dict(evidence), "wan_mapping": mapped},
        "process_id": evidence.get("process_id"),
        "returncode": evidence.get("returncode"),
        "output_root": output_root,
    }
