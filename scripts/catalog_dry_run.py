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
    if name in {"image_ref", "video_ref", "audio_ref"}:
        path = directory / f"{name}.png"
        path.write_bytes(b"test media")
        return str(path)
    return "test-value"


def catalog_dry_run(entry, mode: str, directory: Path, *, invoke: Callable | None = None) -> DryRun:
    """Admit one catalog entry/mode through console.main, with required inputs."""
    if invoke is None:
        from astrid import console
        invoke = console.main
    spec = entry.modes[mode]
    argv = ["generate", entry.modality, "A clear test scene", "--entry", entry.id,
            "--mode", mode, "--placement", "runpod", "--dry-run", "--json"]
    for name in spec.requires:
        if name == "prompt":
            continue
        value = required_value(name, directory)
        if name == "image_ref":
            argv.extend(["--edit", value])
        elif name == "video_ref":
            argv.extend(["--video-ref", value])
        else:
            argv.extend(["--input", f"{name}={value}"])
    assert "--dry-run" in argv
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            rc = invoke(argv)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
    return DryRun(argv, int(rc or 0), stdout.getvalue(), stderr.getvalue())
