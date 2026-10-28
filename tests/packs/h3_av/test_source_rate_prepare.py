from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from astrid.packs.h3_av.actions.prepare import prepare as prepare_module
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.errors import PreparationError
from astrid.packs.h3_av.shared.request import normalize_request

CONFORM_COMMAND = "conform with: ffmpeg -i IN -vf fps=24 -c:v libx264 -crf 12 -c:a aac OUT"


def _v1(operation: str) -> Any:
    raw: dict[str, Any] = {
        "version": 1,
        "operation": operation,
        "output": {"duration": 1},
        "content": {"prompt": "Keep the source second intact."},
        "changes": {"video": [], "audio": []},
        "references": [],
        "overrides": {"seed": 42, "steps": 8},
    }
    if operation == "generate":
        raw["references"] = [{"asset": "image-0", "purpose": "appearance"}]
    else:
        raw["source"] = {"asset": "source", "range": [0, 1]}
    return normalize_request(raw)


def _v2(*, continuation: bool) -> Any:
    raw: dict[str, Any] = {
        "version": 2,
        "prompt": "Keep the source second intact.",
        "duration": 1,
        "media": [
            {
                "id": "source",
                "asset": "source.mp4",
                "role": "timeline",
                "modality": "video",
                "at": {"frame": 0},
                "range": [0, 1],
            }
        ],
        "settings": {},
    }
    if continuation:
        raw["continuation"] = True
    return normalize_request(raw)


def _v2_source_free() -> Any:
    return normalize_request(
        {
            "version": 2,
            "prompt": "A quiet room.",
            "duration": 1,
            "media": [{"id": "look", "asset": "look.png", "role": "reference", "modality": "image"}],
            "settings": {},
        }
    )


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True, capture_output=True, text=True)


def _make_23976_source(path: Path) -> None:
    """A real 1 s clip at 24000/1001 fps (23.976) with a 1 s AAC audio stream."""
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=24000/1001:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
    )


def _conform(source: Path, out: Path) -> None:
    """Run the exact conform command the rejection message prints, with IN and OUT filled in."""
    _ffmpeg(
        "-i", str(source), "-vf", "fps=24", "-c:v", "libx264", "-crf", "12", "-c:a", "aac", str(out),
    )


ROUTES = [
    pytest.param(lambda: _v1("continue"), "source", id="v1-continue"),
    pytest.param(lambda: _v1("edit"), "source", id="v1-edit"),
    pytest.param(lambda: _v2(continuation=False), "source.mp4", id="v2-source-backed"),
    pytest.param(lambda: _v2(continuation=True), "source.mp4", id="v2-extension-context"),
]


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("make_request", "asset_id"), ROUTES)
def test_23976_source_is_rejected_on_every_source_backed_route(
    tmp_path: Path, make_request: Any, asset_id: str
) -> None:
    source = tmp_path / "source-23976.mp4"
    _make_23976_source(source)
    with pytest.raises(PreparationError) as raised:
        prepare_request(make_request(), asset_map={asset_id: str(source)})
    message = str(raised.value)
    assert "H3_SOURCE_AV_RATE" in message
    assert "24000/1001" in message
    assert "23.976 fps" in message
    assert "video duration" in message
    assert "audio duration" in message
    assert CONFORM_COMMAND in message


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("make_request", "asset_id"), ROUTES)
def test_conformed_24fps_source_passes_on_every_source_backed_route(
    tmp_path: Path, make_request: Any, asset_id: str
) -> None:
    source = tmp_path / "source-23976.mp4"
    conformed = tmp_path / "source-24.mp4"
    _make_23976_source(source)
    _conform(source, conformed)
    preparation = prepare_request(make_request(), asset_map={asset_id: str(conformed)})
    assert preparation["status"] == "prepared"
    assert preparation["runtime_submission"] == "eligible"


def test_source_free_generate_never_probes_a_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    probed: list[list[str]] = []
    real_run = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        probed.append([str(part) for part in argv])
        return real_run(argv, *args, **kwargs)

    reference = tmp_path / "reference.png"
    reference.write_bytes(b"reference-image")
    monkeypatch.setattr(prepare_module.subprocess, "run", spy)
    v1 = prepare_request(_v1("generate"), asset_map={"image-0": str(reference)})
    v2 = prepare_request(_v2_source_free(), asset_map={"look.png": str(reference)})
    assert v1["status"] == "prepared"
    assert v2["status"] == "prepared"
    assert not [argv for argv in probed if "ffprobe" in argv[0]]
