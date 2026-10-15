"""Canonical runtime entrypoint for ``vibecomfy.inspect``."""

from __future__ import annotations

import argparse
from pathlib import Path

from astrid.core.contracts.errors import AstridError, render_astrid_error
from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("vibecomfy.inspect")

from astrid.packs.vibecomfy.shared.python_execution_consent import (  # noqa: E402
    PythonExecutionConsentError,
)
from astrid.packs.vibecomfy.shared.workflow_ir import (  # noqa: E402
    WorkflowIrBridgeError,
    inspect_canonical_bundle,
    inspect_workflow,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Project a ComfyUI UI graph through VibeComfy's readable IR."
    )
    parser.add_argument("--workflow", default="")
    parser.add_argument("--python", default="")
    parser.add_argument("--companion", default="")
    parser.add_argument("--source", default="")
    parser.add_argument("--python-execution-consent", default="")
    parser.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.python or args.companion or args.source:
            inspect_canonical_bundle(
                Path(args.python),
                Path(args.companion),
                Path(args.source),
                args.out,
                python_execution_consent=args.python_execution_consent,
            )
        elif args.workflow:
            inspect_workflow(Path(args.workflow), args.out)
        else:
            raise WorkflowIrBridgeError(
                "provide workflow JSON or python, companion, and source bundle members"
            )
    except (WorkflowIrBridgeError, PythonExecutionConsentError) as exc:
        render_astrid_error(
            AstridError(
                f"vibecomfy.inspect: {exc}",
                recovery_command="invoke vibecomfy.inspect through astrid.sdk.invoke with its declared inputs",
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
