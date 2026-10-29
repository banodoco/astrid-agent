from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from astrid.core.contracts.errors import AstridValueError, coerce_astrid_error
from astrid.packs.h3_av.actions.prepare import prepare as prepare_module
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.errors import PreparationError
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.h3_av.shared.spec import normalize_spec

RATE_COMMAND = "ffmpeg -i IN -vf fps=24 -c:v libx264 -crf 12 -c:a aac OUT"
CONFORM_COMMAND = "conform with: " + RATE_COMMAND
DIMENSIONS_COMMAND = "ffmpeg -i IN -vf scale=1024:576,fps=24 -c:v libx264 -crf 12 -c:a aac OUT"


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


def _make_23976_source(path: Path, *, size: str = "1024x576") -> None:
    """A real 1 s clip at 24000/1001 fps (23.976) with a 1 s AAC audio stream."""
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24000/1001:duration=1",
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
    _assert_envelope(raised.value, "H3_SOURCE_AV_RATE", RATE_COMMAND)


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


def _assert_envelope(error: PreparationError, code: str, command: str) -> None:
    assert isinstance(error, AstridValueError)
    envelope = coerce_astrid_error(error).to_envelope()
    assert envelope["code"] == code
    assert envelope["field"] == "source"
    assert envelope["fix"] == command
    assert envelope["message"] == str(error)
    assert command in envelope["message"]


def _make_cfr_source(path: Path, *, size: str) -> None:
    _ffmpeg(
        "-f", "lavfi", "-i", f"color=c=blue:s={size}:r=24:d=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    )


def _make_vfr_source(path: Path) -> None:
    first = path.with_name("vfr-first.mp4")
    second = path.with_name("vfr-second.mp4")
    for clip, color, rate in ((first, "blue", "24"), (second, "red", "12")):
        _ffmpeg(
            "-f", "lavfi", "-i", f"color=c={color}:s=1024x576:r={rate}:d=0.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
        )
    concat = path.with_name("vfr-input.txt")
    concat.write_text(f"file '{first}'\nfile '{second}'\n", encoding="utf-8")
    _ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(path))


@pytest.fixture(scope="module")
def preflight_sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    directory = tmp_path_factory.mktemp("source-preflight")
    sources = {name: directory / f"{name}.mp4" for name in ("vfr", "portrait", "undersized", "good")}
    _make_vfr_source(sources["vfr"])
    _make_cfr_source(sources["portrait"], size="576x1024")
    _make_cfr_source(sources["undersized"], size="64x48")
    _make_cfr_source(sources["good"], size="1024x576")
    return sources


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("make_request", "asset_id"), ROUTES)
@pytest.mark.parametrize(
    ("source_name", "code", "command", "measurement"),
    [
        ("vfr", "H3_SOURCE_AV_RATE", RATE_COMMAND, "avg_frame_rate"),
        ("portrait", "H3_SOURCE_DIMENSIONS", DIMENSIONS_COMMAND, "576x1024"),
        ("undersized", "H3_SOURCE_DIMENSIONS", DIMENSIONS_COMMAND, "64x48"),
    ],
)
def test_source_preflight_rejections_are_coded(
    preflight_sources: dict[str, Path], make_request: Any, asset_id: str,
    source_name: str, code: str, command: str, measurement: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_run = subprocess.run
    calls: list[list[str]] = []

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append([str(part) for part in argv])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(prepare_module.subprocess, "run", spy)
    with pytest.raises(PreparationError) as raised:
        prepare_request(make_request(), asset_map={asset_id: str(preflight_sources[source_name])})
    _assert_envelope(raised.value, code, command)
    assert measurement in str(raised.value)
    assert all(Path(argv[0]).name != "ffmpeg" for argv in calls)


@pytest.mark.ffmpeg
@pytest.mark.parametrize(("make_request", "asset_id"), ROUTES)
def test_native_dimensions_24fps_source_passes(
    preflight_sources: dict[str, Path], make_request: Any, asset_id: str
) -> None:
    preparation = prepare_request(make_request(), asset_map={asset_id: str(preflight_sources["good"])})
    assert preparation["status"] == "prepared"
    assert preparation["runtime_submission"] == "eligible"


@pytest.mark.ffmpeg
def test_printed_dimensions_conform_command_produces_accepted_source(
    preflight_sources: dict[str, Path], tmp_path: Path
) -> None:
    source = preflight_sources["portrait"]
    conformed = tmp_path / "portrait-conformed.mp4"
    with pytest.raises(PreparationError) as raised:
        prepare_request(_v1("continue"), asset_map={"source": str(source)})
    _assert_envelope(raised.value, "H3_SOURCE_DIMENSIONS", DIMENSIONS_COMMAND)
    _ffmpeg(
        "-i", str(source), "-vf", "scale=1024:576,fps=24",
        "-c:v", "libx264", "-crf", "12", "-c:a", "aac", str(conformed),
    )
    preparation = prepare_request(_v1("continue"), asset_map={"source": str(conformed)})
    assert preparation["status"] == "prepared"
    assert preparation["runtime_submission"] == "eligible"


@pytest.mark.ffmpeg
def test_rate_and_dimensions_mismatch_suggests_one_complete_conform(tmp_path: Path) -> None:
    source = tmp_path / "portrait-23976.mp4"
    _make_23976_source(source, size="576x1024")
    with pytest.raises(PreparationError) as raised:
        prepare_request(_v1("continue"), asset_map={"source": str(source)})
    _assert_envelope(raised.value, "H3_SOURCE_AV_RATE", DIMENSIONS_COMMAND)
    assert "576x1024" in str(raised.value)
    assert "24000/1001" in str(raised.value)


@pytest.mark.ffmpeg
def test_canonical_spec_source_uses_same_preflight(preflight_sources: dict[str, Path]) -> None:
    request = normalize_spec(
        {
            "spec_version": 1,
            "source": {"asset": "source.mp4", "range": [0, 1]},
            "prompt": "Continue the scene.",
            "duration": 2,
            "operations": [{"op": "extend"}],
        }
    ).requests[0]
    with pytest.raises(PreparationError) as raised:
        prepare_request(request, asset_map={"source.mp4": str(preflight_sources["portrait"])})
    _assert_envelope(raised.value, "H3_SOURCE_DIMENSIONS", DIMENSIONS_COMMAND)
    preparation = prepare_request(request, asset_map={"source.mp4": str(preflight_sources["good"])})
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
