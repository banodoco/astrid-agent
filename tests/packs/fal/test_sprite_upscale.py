from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.contracts.errors import AstridError
from astrid.packs.fal.actions.sprite_upscale import upscale
from astrid.packs.fal.actions.sprite_upscale import run as sprite_upscale_run


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        fal_env_file=None,
        env_file=None,
        fal_timeout=30,
        ai_upscale_prompt=None,
        subject="blue bird",
        animation="flap",
        ai_upscale_model="fal-ai/clarity-upscaler",
        ai_upscale_factor=2.0,
        ai_upscale_negative_prompt="text",
        ai_upscale_creativity=0.2,
        ai_upscale_resemblance=0.9,
        ai_upscale_guidance_scale=4.0,
        ai_upscale_steps=28,
        fal_logs=False,
        transparent=False,
    )


def test_sprite_upscale_uses_fal_action_and_reports_output(tmp_path, monkeypatch):
    frame = tmp_path / "frame.png"
    frame.write_bytes(b"source")
    seen: dict[str, object] = {}

    class FakeClient:
        def __init__(self, *, key: str, default_timeout: float):
            seen["client"] = (key, default_timeout)

        def upload_file(self, path: Path) -> str:
            seen["upload"] = path
            return "https://upload.invalid/frame.png"

        def subscribe(self, model: str, **kwargs):
            seen["subscribe"] = (model, kwargs)
            return {"image": {"url": "https://result.invalid/frame.png"}, "seed": 7}

    monkeypatch.setitem(sys.modules, "fal_client", SimpleNamespace(SyncClient=FakeClient))
    monkeypatch.setattr(upscale, "load_fal_key", lambda _path: "test-key")

    def download(_url, path, **_kwargs):
        path.write_bytes(b"raw")

    def scale(_source, _raw, output, **_kwargs):
        output.write_bytes(b"scaled")

    monkeypatch.setattr(upscale, "_download_url", download)
    monkeypatch.setattr(upscale, "_scale_upscaled_image", scale)
    outputs, report = upscale.ai_upscale_frames_with_fal(
        [str(frame)], tmp_path / "upscaled", args=_args(), force=False
    )

    assert outputs == [str(tmp_path / "upscaled/frame_001.png")]
    assert report[0]["seed"] == 7
    assert seen["client"] == ("test-key", 30.0)
    assert seen["upload"] == frame
    assert seen["subscribe"][0] == "fal-ai/clarity-upscaler"


def test_sprite_upscale_rejects_result_without_image_url():
    with pytest.raises(AstridError, match="did not include an image URL"):
        upscale._fal_image_url({"seed": 7})


@pytest.mark.parametrize(
    ("flags", "expected_transparent"),
    [([], True), (["--transparent"], True), (["--no-transparent"], False)],
)
def test_sprite_upscale_cli_parses_factor_and_transparency(
    tmp_path, monkeypatch, flags, expected_transparent
):
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "frame.png").write_bytes(b"frame")
    seen = {}

    def upscale_frames(frames, out_dir, *, args, force):
        seen.update(
            frames=frames,
            out_dir=out_dir,
            factor=args.ai_upscale_factor,
            transparent=args.transparent,
            force=force,
        )
        return [], []

    monkeypatch.setattr(sprite_upscale_run, "ai_upscale_frames_with_fal", upscale_frames)

    assert sprite_upscale_run.main(
        [
            "--frames-dir", str(frames_dir),
            "--out-dir", str(tmp_path / "out"),
            "--subject", "blue bird",
            "--animation", "flap",
            "--ai-upscale-factor", "3",
            *flags,
        ]
    ) == 0
    assert seen["factor"] == 3
    assert seen["transparent"] is expected_transparent
