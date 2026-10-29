"""Thin public CLI for the existing generation SDK facade."""

from __future__ import annotations

import argparse
import json
from typing import Any


def _open_client() -> Any:
    """Acquire the Runtime connection at the explicit public CLI boundary."""
    from astrid.sdk.client import AstridClient

    return AstridClient.open_from_launcher()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="astrid generate",
        description="Generate media through the selected Comfy target and the Astrid runtime.",
    )
    sub = parser.add_subparsers(dest="modality", required=True)
    for modality in ("image", "video"):
        command = sub.add_parser(modality, help=f"Generate {'an image' if modality == 'image' else 'a video'}.")
        command.add_argument("prompt", help="Describe the media to generate.")
        command.add_argument("--model", "--entry", dest="model", help="Generation catalog entry ID.")
        command.add_argument("--mode", help="Generation mode; inferred from the supplied inputs by default.")
        command.add_argument("--execution", help="Catalog execution backend, when explicitly selected.")
        command.add_argument(
            "--target", default="auto", metavar="TARGET",
            help="auto (default), managed, a loopback ComfyUI URL, or runpod.",
        )
        command.add_argument("--project", help="Project ID or slug; otherwise use the selected project.")
        command.add_argument("--out", help="Output destination passed to the runtime.")
        command.add_argument("--dry-run", action="store_true", help="Show the generation plan without running it.")
        command.add_argument("--edit", "--image-ref", dest="image_ref", help="Reference image for an edit or image-to-video request.")
        command.add_argument("--width", type=int)
        command.add_argument("--height", type=int)
        command.add_argument("--steps", type=int)
        command.add_argument("--seed", type=int)
        command.add_argument("--cfg", "--guidance-scale", dest="guidance_scale", type=float)
        command.add_argument("--negative-prompt")
        command.add_argument("--json", action="store_true", help="Print the SDK result as JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse one request and call its existing public SDK method exactly once."""
    parser = _parser()
    parsed = parser.parse_args(argv)
    if not parsed.prompt.strip():
        parser.error("prompt must not be empty")
    if (parsed.width is None) != (parsed.height is None):
        parser.error("--width and --height must be supplied together")
    if parsed.width is not None and (parsed.width <= 0 or parsed.height <= 0):
        parser.error("--width and --height must be positive")
    import astrid
    from astrid.core.contracts.errors import (
        AstridError,
        coerce_astrid_error,
        error_from_result,
    )
    from astrid.sdk.exceptions import AstridSDKError

    kwargs: dict[str, Any] = {
        "model": parsed.model,
        "target": parsed.target,
        "dry_run": parsed.dry_run,
        "prompt": parsed.prompt,
    }
    for name in (
        "mode", "execution", "project", "out", "image_ref",
        "steps", "seed", "guidance_scale", "negative_prompt",
    ):
        value = getattr(parsed, name)
        if value is not None:
            kwargs[name] = value
    if parsed.width is not None:
        kwargs["size" if parsed.modality == "image" else "resolution"] = f"{parsed.width}x{parsed.height}"
    try:
        generate = getattr(astrid.generate, parsed.modality)
        if parsed.dry_run:
            result = generate(**kwargs)
        else:
            with _open_client() as client:
                result = generate(client=client, **kwargs)
    except AstridSDKError as exc:
        # Preserve target/model problem codes and recovery guidance at the
        # gateway boundary instead of treating a typed SDK failure as a bug.
        raise coerce_astrid_error(exc) from exc
    if getattr(result, "ok", True) is False:
        raise error_from_result(result) or AstridError(
            "Generation did not succeed.",
            code="generation_failed",
            recovery_command="astrid tasks list",
        )
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    if parsed.json or parsed.dry_run or not isinstance(payload, dict):
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{parsed.modality} generation complete")
        for path in payload.get("image_paths", []):
            print(path)
        if payload.get("run_dir"):
            print(f"run: {payload['run_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
