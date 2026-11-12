"""The developer gate and catalog test share one public-CLI admission probe.

This lives beside the gate because synthetic inputs and captured CLI output are
verification machinery, not a second generation API. It never runs generation:
every invocation carries --dry-run.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class DryRun:
    argv: list[str]
    exit_status: int
    stdout: str
    stderr: str

    @property
    def failure(self) -> str:
        try:
            payload = json.loads(self.stdout)
        except ValueError:
            return "CLI did not return JSON: " + (self.stdout + self.stderr).strip()
        if self.exit_status or not payload.get("ok", True):
            return (self.stdout + self.stderr).strip()
        return ""


def required_value(name: str, directory: Path) -> str:
    if name in {"prompt", "negative_prompt"}:
        return "A clear test scene"
    if name in {"image_ref", "video_ref"}:
        path = directory / f"{name}.png"
        path.write_bytes(b"test image")
        return str(path)
    return "test-value"


def catalog_dry_run(entry, mode: str, directory: Path, *, invoke: Callable | None = None) -> DryRun:
    """Admit one catalog entry/mode through console.main, with required inputs."""
    from astrid.core.model_catalog.requests import PaidControls, build_generate_argv

    if invoke is None:
        from astrid import console
        invoke = console.main
    spec = entry.modes[mode]
    # Synthetic controls satisfy request validation, never live authority.
    # The request builder owns their CLI representation; dry-run is mandatory.
    controls_path = directory / "dry-run-controls.json"
    controls_path.write_text(json.dumps({"max_usd": 2, "deadline": "2099-01-01T00:00:00Z"}), encoding="utf-8")
    controls = PaidControls.from_json(controls_path)
    inputs = {name: required_value(name, directory) for name in spec.requires}
    argv = build_generate_argv(entry, mode, inputs, "runpod", controls, dry_run=True)
    if argv[:1] != ["generate"] or "--dry-run" not in argv:
        raise ValueError("catalog preflight only permits generate --dry-run")
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            rc = invoke(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return DryRun(argv, int(rc or 0), stdout.getvalue(), stderr.getvalue())
