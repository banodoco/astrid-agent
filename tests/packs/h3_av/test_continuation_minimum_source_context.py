"""Continuation admission checks the selected source context before execution."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation import entries
from astrid.core.generation.engine_facts import comfyui_facts
from astrid.packs.h3_av.shared import engine_facts


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
                   check=True, capture_output=True, text=True, timeout=60)


def _source(path: Path, *, frames: int) -> None:
    _ffmpeg("-f", "lavfi", "-i", "color=size=1024x576:rate=24:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path))


def _request(path: Path, *, source_frames: int) -> Path:
    end = source_frames / 24
    output = (source_frames + 1) / 24
    document = {
        "version": 1,
        "operation": "continue",
        "source": {"asset": "source", "range": [0, end]},
        "output": {"duration": output},
        "content": {"prompt": "Continue the selected audiovisual context."},
        "changes": {
            "video": [{"during": [end, output], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [{"during": [end, output], "action": "generate"}],
        },
        "references": [],
        "overrides": {"seed": 7, "steps": 8},
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.mark.ffmpeg
@pytest.mark.parametrize("source_frames", [38, 39])
def test_h3_continuation_requires_39_selected_source_context_frames(
    tmp_path: Path, source_frames: int,
) -> None:
    import astrid.packs.vibecomfy.extensions  # noqa: F401  (register H3)

    engine_facts.install(comfyui_facts())
    source = tmp_path / f"source-{source_frames}.mp4"
    request = _request(tmp_path / "request.json", source_frames=source_frames)
    _source(source, frames=source_frames)
    entry = entries.find_entry("h3")
    assert entry is not None

    if source_frames == 38:
        with pytest.raises(ValueError, match="continuation requires at least 39 source frames; received 38"):
            entries.build_entry(
                entry,
                {"request": str(request), "asset_map": {"source": str(source)}},
                tmp_path / "worker-38",
            )
    else:
        built = entries.build_entry(
            entry,
            {"request": str(request), "asset_map": {"source": str(source)}},
            tmp_path / "worker-39",
        )
        assert built is not None
        assert built["bindings"]["source_frames"] == 39
    # `entries.build_entry` only runs the CPU build hook; generation submission
    # and machine/pod dispatch are outside this call path.
