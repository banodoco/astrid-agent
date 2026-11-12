"""Build catalog generation requests from stage-owned paid controls."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .schema import ModelEntry


@dataclass(frozen=True)
class PaidControls:
    max_usd: float
    deadline: str

    @classmethod
    def from_json(cls, path: str | Path) -> "PaidControls":
        """Read the cap and absolute deadline supplied by the stage owner."""
        with open(path, encoding="utf-8") as source:
            values = json.load(source)
        return cls(max_usd=values["max_usd"], deadline=values["deadline"])


def build_generate_argv(
    entry: ModelEntry,
    mode: str,
    inputs: Mapping[str, str],
    placement: str,
    paid_controls: PaidControls,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Build arguments after ``astrid``, using only declared required inputs."""
    required = entry.modes[mode].requires
    args = ["generate", entry.modality]
    if "prompt" in required:
        args.append(inputs["prompt"])
    args.extend(["--entry", entry.id, "--mode", mode, "--placement", placement])
    for name in required:
        if name == "prompt":
            continue
        value = inputs[name]
        if name == "image_ref":
            args.extend(["--edit", value])
        elif name == "video_ref":
            args.extend(["--video-ref", value])
        else:
            args.extend(["--input", f"{name}={value}"])
    args.extend(["--consent", "paid", "--max-usd", str(paid_controls.max_usd),
                 "--deadline", paid_controls.deadline, "--json"])
    if dry_run:
        args.append("--dry-run")
    return args
