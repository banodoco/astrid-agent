#!/usr/bin/env python3
"""Generate an image by injecting a prompt into a ComfyUI workflow and running it via vibecomfy.

Loads a ComfyUI workflow JSON file at run-time, injects the user's ``--prompt``
into the positive CLIPTextEncode node, executes through vibecomfy (the existing
Astrid surface that wraps ComfyUI), and copies the result to ``--out``.
"""

from __future__ import annotations

# ruff: noqa: E402
from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("comfy_wrap.run")
import argparse
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from astrid.core.contracts.errors import AstridError

logger = logging.getLogger(__name__)

# Path to the workflow JSON that will be loaded at run-time.
_WORKFLOW_SOURCE = Path("/tmp/example_comfy.json")

# Node ID and field in the workflow JSON where the prompt is injected.
_PROMPT_NODE_ID = "6"
_PROMPT_FIELD = "text"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="comfy_wrap.run",
        description="Generate an image from a ComfyUI workflow with a parameterized prompt.",
    )
    parser.add_argument(
        "--prompt",
        required=True,
        help="Text prompt describing the desired image.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output file path for the generated image.",
    )
    return parser


def _load_workflow_json(path: Path) -> dict[str, Any]:
    """Load and return the ComfyUI workflow JSON as a dict.

    Reads the file at run-time — the workflow is never baked into Python.
    Raises AstridError with a recovery hint if the file is missing or unparseable.
    """
    if not path.is_file():
        raise AstridError(
            f"Workflow JSON not found: {path}",
            recovery_command=(
                f"ensure the workflow file exists at {path} "
                f"(e.g. copy a ComfyUI export there) and retry"
            ),
            state_snapshot={"workflow_source": str(path)},
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AstridError(
            f"Failed to parse workflow JSON from {path}: {exc}",
            recovery_command="verify the file contains valid JSON and retry",
            state_snapshot={"workflow_source": str(path), "parse_error": str(exc)},
        ) from exc


def _inject_prompt(workflow_dict: dict[str, Any], prompt: str) -> None:
    """Inject *prompt* into the positive CLIPTextEncode node's text field.

    The ComfyUI workflow JSON stores nodes at the top level (keyed by node id
    strings like ``"6"``), not under a ``"nodes"`` key.  We operate directly
    on *workflow_dict*.
    """
    if _PROMPT_NODE_ID not in workflow_dict:
        raise AstridError(
            f"Prompt node '{_PROMPT_NODE_ID}' not found in workflow JSON. "
            f"Available nodes: {sorted(workflow_dict.keys())}",
            recovery_command=(
                "check the workflow JSON has the expected node id "
                f"'{_PROMPT_NODE_ID}' (a CLIPTextEncode for the positive prompt)"
            ),
            state_snapshot={"expected_node": _PROMPT_NODE_ID, "available_nodes": sorted(workflow_dict.keys())},
        )
    node = workflow_dict[_PROMPT_NODE_ID]
    inputs = node.get("inputs", {})
    if _PROMPT_FIELD not in inputs:
        raise AstridError(
            f"Field '{_PROMPT_FIELD}' not found in node '{_PROMPT_NODE_ID}' inputs. "
            f"Available fields: {sorted(inputs.keys())}",
            recovery_command=(
                f"check the workflow node '{_PROMPT_NODE_ID}' has a "
                f"'{_PROMPT_FIELD}' input field"
            ),
            state_snapshot={
                "node_id": _PROMPT_NODE_ID,
                "available_fields": sorted(inputs.keys()),
            },
        )
    inputs[_PROMPT_FIELD] = prompt
    logger.info("Injected prompt into node %s.%s: %r", _PROMPT_NODE_ID, _PROMPT_FIELD, prompt)


def _run_workflow(workflow_dict: dict[str, Any]) -> list[Path]:
    """Execute the workflow through vibecomfy and return output paths.

    Routes through vibecomfy (the existing Astrid surface that wraps ComfyUI)
    rather than a fresh HTTP client.
    """
    # Lazy-import vibecomfy so it is only loaded when this backend is used.
    # Build a VibeWorkflow from the modified JSON dict.
    # vibecomfy.workflow_from_file expects a path, so we write the modified
    # JSON to a temp file for loading.  This also serves as a run record.
    import tempfile

    import vibecomfy
    from vibecomfy.runtime.run import run_sync

    tmp_path = Path(tempfile.mktemp(suffix=".json", prefix="comfy_wrap_"))
    tmp_path.write_text(json.dumps(workflow_dict, indent=2), encoding="utf-8")
    logger.info("Wrote modified workflow to %s", tmp_path)

    try:
        wf = vibecomfy.workflow_from_file(str(tmp_path))
        result = run_sync(wf)

        output_paths: list[Path] = []
        for output_str in result.outputs:
            src = Path(output_str)
            if src.is_file():
                output_paths.append(src)
            else:
                logger.warning("VibeComfy output not found on disk: %s", src)

        if not output_paths:
            raise AstridError(
                "Workflow completed but produced no output files",
                recovery_command="check the workflow JSON for a SaveImage node and verify ComfyUI output directory configuration",
                state_snapshot={
                    "run_id": result.run_id,
                    "prompt_id": result.prompt_id,
                    "metadata_path": result.metadata_path,
                    "log_path": result.log_path,
                },
            )
        return output_paths
    finally:
        # Clean up the temp workflow file
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)

        out = args.out.expanduser().resolve()
        prompt = args.prompt.strip()

        if not prompt:
            raise AstridError(
                "--prompt must not be empty",
                recovery_command="provide a non-empty text prompt and retry",
            )

        # 1. Load the workflow JSON at run-time (never baked into Python).
        logger.info("Loading workflow from %s", _WORKFLOW_SOURCE)
        workflow_dict = _load_workflow_json(_WORKFLOW_SOURCE)

        # 2. Inject the prompt into the positive CLIPTextEncode node.
        _inject_prompt(workflow_dict, prompt)

        # 3. Execute through vibecomfy (the existing Astrid ComfyUI surface).
        logger.info("Running workflow via vibecomfy...")
        output_paths = _run_workflow(workflow_dict)

        # 4. Copy the first output image to --out.
        out.parent.mkdir(parents=True, exist_ok=True)
        src = output_paths[0]
        shutil.copy2(src, out)
        logger.info("Generated image written to %s", out)

        if len(output_paths) > 1:
            logger.info(
                "Workflow produced %d outputs; first image copied to %s, "
                "remaining outputs at: %s",
                len(output_paths),
                out,
                [str(p) for p in output_paths[1:]],
            )

        return 0

    return run_pack_main(
        "comfy_wrap.run",
        _run,
        argv=argv,
        recovery_command="invoke comfy_wrap.run through astrid.sdk.invoke with its declared prompt and output inputs",
    )


if __name__ == "__main__":
    raise SystemExit(main())
