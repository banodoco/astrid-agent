from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageChops

from astrid.core.cli.domain_product import run_product_family
from astrid.core.media_mask import RECIPE_KEY, _fitted_frame, build_mask
from astrid.sdk.contracts import DomainResult


class ProjectMedia:
    def __init__(self):
        self.items = {}

    def add(self, path, media_type):
        payload = path.read_bytes()
        ref = "sha256:" + hashlib.sha256(payload).hexdigest()
        self.items[ref] = ({"object_id": ref, "filename": path.name, "media_type": media_type}, payload)
        return ref

    def import_file(self, *, project, path, realm):
        assert project == "P" and realm == "managed_local"
        return self.show(project, self.add(path, "image/png"))

    def show(self, project, ref):
        assert project == "P"
        return DomainResult.success(self.items[ref][0])

    def read_bytes(self, ref):
        return self.items[ref][1]


@pytest.fixture
def media_cli(capsys):
    media = ProjectMedia()

    def run(*args, as_json=True):
        flags = ["--json"] if as_json else []
        code = run_product_family(
            "media", ["mask", "--project", "P", *args, *flags], client=SimpleNamespace(media=media)
        )
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if as_json else captured.out

    return media, run


def test_mask_storage_and_recipe(media_cli):
    media, run = media_cli
    args = ("--name", "02c lenses", "--ellipse", "282,121,119,99",
            "--ellipse", "622,114,132,96", "--grow", "8")
    code, result = run(*args)
    assert code == 0
    ref = result["data"]["object_id"]
    payload = media.read_bytes(ref)
    assert ref == "sha256:" + hashlib.sha256(payload).hexdigest()
    with Image.open(io.BytesIO(payload)) as image:
        assert (image.format, image.mode, image.size) == ("PNG", "L", (1024, 576))
        assert set(image.tobytes()) == {0, 255}
        assert json.loads(image.info[RECIPE_KEY]) == result["data"]["recipe"]
    assert run(*args)[1]["data"]["object_id"] == ref
    assert run("--mask", ref)[1]["data"] == result["data"]
    assert len(media.items) == 1


def test_grow_rotated_ellipses_and_boxes():
    no_grow, _ = build_mask("test", ["20,20,10,3,90"], ["40,40,4,6"], 0, (64, 64), False)
    grown, _ = build_mask("test", ["20,20,10,3,90"], ["40,40,4,6"], 2, (64, 64), False)
    inverted, _ = build_mask("test", ["20,20,10,3,90"], ["40,40,4,6"], 2, (64, 64), True)
    assert no_grow.getpixel((20, 29)) == 255
    assert no_grow.getpixel((24, 20)) == 0
    assert grown.getpixel((24, 20)) == 255
    assert no_grow.getpixel((44, 45)) == 0
    assert grown.getpixel((45, 47)) == 255
    assert grown.getpixel((46, 47)) == 0
    assert ImageChops.invert(grown).tobytes() == inverted.tobytes()


@pytest.mark.parametrize(("flag", "shape", "reason"), [
    ("--box", "1023,0,2,2", "outside the working frame"),
    ("--ellipse", "1,1,3,4", "outside the working frame"),
    ("--box", "0,0,0,2", "width and height must be positive"),
    ("--ellipse", "20,20,2,0", "radii must be positive"),
])
def test_bad_shape_refused(media_cli, flag, shape, reason):
    media, run = media_cli
    code, result = run("--name", "bad", flag, shape)
    assert code != 0 and reason in result["error"]["message"]
    assert not media.items


def test_preview_uses_h3_fill_and_writes_sheet(media_cli, tmp_path):
    media, run = media_cli
    path = tmp_path / "wide.png"
    image = Image.new("RGB", (160, 60), (24, 80, 128))
    image.paste((255, 0, 0), (0, 0, 24, 60))
    image.paste((0, 255, 0), (136, 0, 160, 60))
    image.paste((188, 162, 0), (47, 13, 89, 43))
    image.save(path)
    ref = media.add(path, "image/png")
    # The original H3 conform filter is an independent framing reference.
    original_filter = ("setsar=1,crop=w='if(gt(dar,16/9),ih*16/(9*sar),iw)':"
                       "h='if(gt(dar,16/9),ih,iw*sar*9/16)',scale=1024:576,setsar=1")
    expected = subprocess.check_output(
        ["ffmpeg", "-v", "error", "-filter_threads", "1", "-i", str(path), "-vf", original_filter,
         "-frames:v", "1", "-threads", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"]
    )
    frame = _fitted_frame(path, 0, (1024, 576), square_default=True)
    with Image.open(io.BytesIO(expected)) as fitted:
        assert frame.tobytes() == fitted.convert("RGB").tobytes()

    code, result = run("--name", "overlay", "--box", "30,30,100,100", "--preview-on", ref)
    assert code == 0
    data = result["data"]
    sheet = Path(data["preview_path"])
    try:
        with Image.open(sheet) as preview:
            assert preview.size == (1024, 576)
            assert preview.getpixel((0, 60)) == frame.getpixel((0, 60))
            assert preview.getpixel((30, 60)) == (0, 255, 255)
            assert preview.getpixel((60, 60)) != frame.getpixel((60, 60))
    finally:
        sheet.unlink()
        sheet.parent.rmdir()

    code, stdout = run("--mask", data["object_id"], "--preview-on", ref, as_json=False)
    assert code == 0
    sheet = Path(stdout.splitlines()[-1])
    assert sheet.is_file()
    sheet.unlink()
    sheet.parent.rmdir()


def test_preview_video_samples_and_mask_area_change(media_cli, tmp_path):
    media, run = media_cli
    path = tmp_path / "change.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=red:s=128x96:r=4:d=1",
         "-f", "lavfi", "-i", "color=blue:s=128x96:r=4:d=1",
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0", "-c:v", "ffv1", str(path)], check=True,
    )
    ref = media.add(path, "video/x-matroska")
    code, result = run("--name", "change", "--box", "8,8,16,16", "--size", "64x36",
                       "--preview-on", ref, "--range", "0,2", "--samples", "2")
    assert code == 0
    assert result["data"]["samples"] == [
        {"seconds": 0.5, "changed_fraction": None},
        {"seconds": 1.5, "changed_fraction": 1.0},
    ]
    sheet = Path(result["data"]["preview_path"])
    try:
        with Image.open(sheet) as preview:
            assert preview.size == (128, 36)
    finally:
        sheet.unlink()
        sheet.parent.rmdir()
