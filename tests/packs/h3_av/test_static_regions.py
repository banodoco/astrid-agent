from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.static_regions import StaticRegionError, resolve_static_region
from astrid.packs.h3_av.shared.compose import iter_mask_frames, probe_mask
from astrid.packs.h3_av.shared.spec import Region

POLARITY = "black_preserve_white_edit"


def _pixels(png: bytes):
    with Image.open(BytesIO(png)) as image:
        return image.mode, image.size, list(image.getdata())


def _save(tmp_path, mode, values, *, size=(4, 3), name="mask.png"):
    image = Image.new(mode, size)
    image.putdata(values)
    path = tmp_path / name
    image.save(path)
    return path


def _resolve_image(path, **kwargs):
    return resolve_static_region(
        Region("region", mask_asset="mask"), width=4, height=3,
        assets={"mask": path}, **kwargs,
    )


def _assert_error(code, callback):
    with pytest.raises(StaticRegionError) as caught:
        callback()
    envelope = caught.value.to_envelope()
    assert envelope["code"] == f"h3.region.{code}"
    assert all(envelope[key] for key in ("message", "field", "fix"))


def test_box_geometry_preview_and_repeatability(tmp_path):
    region = Region("window", box=(1, 1, 2, 2))
    result = resolve_static_region(region, width=4, height=3)
    assert result == resolve_static_region(region, width=4, height=3)
    mask = [0, 0, 0, 0, 0, 255, 255, 0, 0, 255, 255, 0]
    assert _pixels(result.mask_png) == ("L", (4, 3), mask)
    assert _pixels(result.preview_png) == (
        "RGB", (4, 3), [(0, 220, 255) if x else (32, 32, 32) for x in mask],
    )
    path = tmp_path / "resolved.png"
    path.write_bytes(result.mask_png)
    assert probe_mask(path)["kind"] == "image"
    assert next(iter_mask_frames(path, start_frame=0, count=1, width=4, height=3)) == bytes(
        channel for value in mask for channel in (value, value, value)
    )


def test_fractional_box_uses_half_open_pixel_centers():
    result = resolve_static_region(Region("r", box=(0.5, 0.5, 2, 1)), width=4, height=3)
    assert _pixels(result.mask_png)[2] == [255, 255, 0, 0] + [0] * 8


@pytest.mark.parametrize("box", [(-1, 0, 1, 1), (0, 0, 0, 1), (3, 0, 2, 1), (0, 0, float("nan"), 1), (0, 0, True, 1)])
def test_bad_boxes_are_coded(box):
    _assert_error("box", lambda: resolve_static_region(Region("r", box=box), width=4, height=3))


def test_image_mask_binary_and_preview_repeatability(tmp_path):
    values = [0, 255, 255, 0] * 3
    path = _save(tmp_path, "L", values)
    result = _resolve_image(path, polarity=POLARITY)
    assert result == _resolve_image(path, polarity=POLARITY)
    assert _pixels(result.mask_png) == ("L", (4, 3), values)
    assert _pixels(result.preview_png)[2] == [(0, 220, 255) if x else (32, 32, 32) for x in values]


def test_threshold_has_explicit_inclusive_boundary(tmp_path):
    values = [0, 127, 128, 255] * 3
    path = _save(tmp_path, "L", values)
    _assert_error("non_binary", lambda: _resolve_image(path, polarity=POLARITY))
    result = _resolve_image(path, polarity=POLARITY, threshold=128)
    assert _pixels(result.mask_png)[2] == [0, 0, 255, 255] * 3


@pytest.mark.parametrize("threshold", [0, 255, -1, 1.5, True, "128"])
def test_bad_threshold_is_coded(tmp_path, threshold):
    path = _save(tmp_path, "L", [0] * 12)
    _assert_error("threshold", lambda: _resolve_image(path, polarity=POLARITY, threshold=threshold))


@pytest.mark.parametrize("polarity", [None, "white_preserve_black_edit", "", "unknown"])
def test_missing_or_bad_polarity_is_coded(tmp_path, polarity):
    path = _save(tmp_path, "L", [0] * 12)
    _assert_error("polarity", lambda: _resolve_image(path, polarity=polarity))


def test_image_size_is_exact(tmp_path):
    path = _save(tmp_path, "L", [0] * 6, size=(3, 2))
    _assert_error("image_size", lambda: _resolve_image(path, polarity=POLARITY))


@pytest.mark.parametrize("mode,values", [
    ("RGBA", [(0, 0, 0, 0)] * 12),
    ("RGB", [(255, 0, 0)] * 12),
    ("P", [0] * 12),
])
def test_ambiguous_image_modes_are_coded(tmp_path, mode, values):
    path = _save(tmp_path, mode, values)
    _assert_error("unsupported_image", lambda: _resolve_image(path, polarity=POLARITY))


def test_unsupported_format_and_invalid_file_are_coded(tmp_path):
    path = tmp_path / "mask.gif"
    Image.new("L", (4, 3)).save(path)
    _assert_error("unsupported_image", lambda: _resolve_image(path, polarity=POLARITY))
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    _assert_error("unsupported_image", lambda: _resolve_image(path, polarity=POLARITY))


def test_grayscale_rgb_is_supported(tmp_path):
    values = [(0, 0, 0), (255, 255, 255)] * 6
    path = _save(tmp_path, "RGB", values)
    assert _pixels(_resolve_image(path, polarity=POLARITY).mask_png)[2] == [0, 255] * 6


def test_missing_asset_and_invalid_dimensions_are_coded():
    _assert_error("asset", lambda: resolve_static_region(Region("r", mask_asset="missing"), width=4, height=3, polarity=POLARITY))
    _assert_error("dimensions", lambda: resolve_static_region(Region("r", box=(0, 0, 1, 1)), width=True, height=3))


def test_authored_canvas_box_scales_edges_with_ties_down():
    result = resolve_static_region(Region("r", box=(1, 0, 2, 2), canvas=(8, 4)), width=4, height=2, authored_canvas=True)
    assert _pixels(result.mask_png)[2] == [255, 0, 0, 0, 0, 0, 0, 0]


def test_authored_canvas_image_resamples_after_validation(tmp_path):
    path = _save(tmp_path, "L", [0, 255, 255, 0], size=(2, 2))
    result = resolve_static_region(Region("r", mask_asset="mask"), width=4, height=2,
                                   assets={"mask": path}, polarity=POLARITY, authored_canvas=True)
    assert _pixels(result.mask_png)[2] == [0, 0, 255, 255, 255, 255, 0, 0]


def test_authored_box_scales_to_non_16_9_canvas():
    result = resolve_static_region(Region("landmark", box=(256, 144, 256, 144)), width=768, height=448, authored_canvas=True)
    _, size, pixels = _pixels(result.mask_png)
    assert size == (768, 448)
    assert sum(pixel == 255 for pixel in pixels) == 192 * 112
    assert pixels[112 * 768 + 192] == 255
    assert pixels[111 * 768 + 192] == 0
    assert pixels[112 * 768 + 384] == 0


def test_sampler_strength_mask_uses_requested_dimensions(tmp_path):
    from astrid.packs.h3_av.shared.compile import _combined_strength_mask
    from astrid.packs.h3_av.shared.compose import iter_mask_frames

    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    image = Image.new("L", (32, 32), 0)
    for y in (7, 8):
        for x in (7, 8):
            image.putpixel((x, y), 255)
    image.save(a)
    image.save(b)
    path = _combined_strength_mask([(a, "image", (0, 2), 0.25, "a"),
                                    (b, "image", (1, 2), 0.75, "b")],
                                   frames=2, destination=tmp_path, size=(32, 32))
    frames = list(iter_mask_frames(path, start_frame=0, count=2, width=32, height=32))
    assert [frame[(7 * 32 + 7) * 3] for frame in frames] == [64, 191]
    assert all(frame[0] == 0 for frame in frames)


def test_compiler_hands_prepared_static_permission_to_composition(tmp_path):
    import copy
    from astrid.packs.h3_av.shared.compile import compile_preparation
    from astrid.packs.h3_av.shared.prepare import _v2_mask_schedule
    from astrid.packs.h3_av.shared.request import normalize_request
    from tests.packs.h3_av.test_native_masked_edit import WATERMARK, _prepare

    raw = copy.deepcopy(WATERMARK)
    raw["media"][0]["edit"][0]["mask"] = {"rectangle": [256, 144, 256, 144]}
    raw["output"] = {"size": [512, 288]}
    preparation = _prepare(raw, tmp_path)
    preparation["mask_schedule"] = _v2_mask_schedule(normalize_request(raw))
    result = compile_preparation(preparation, out_dir=tmp_path / "compile")
    change = preparation["mask_schedule"]["video"]["changes"][0]
    binding = change["prepared_mask_asset"]
    record = next(item for item in preparation["assets"] if item["asset"] == binding)
    assert any(item["binding"] == binding for item in result["managed_assets"]["manifest"]["assets"])
    with Image.open(record["path"]) as mask:
        assert mask.size == (512, 288)
        assert mask.getpixel((128, 72)) == 255
        assert mask.getpixel((127, 72)) == 0


def test_compiled_permission_is_used_by_composition_and_verification(tmp_path):
    from astrid.packs.h3_av.shared.compile import compile_preparation
    from astrid.packs.h3_av.shared.compose import compose_candidate
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.verify import verify_candidate
    from tests.packs.h3_av.test_moving_masks import _fixtures

    source, generated = _fixtures(tmp_path)
    request = normalize_request({
        "version": 2, "prompt": "Change the rectangle", "duration": 1,
        "media": [{"id": "source", "asset": "source.mkv", "role": "timeline", "modality": "video",
                   "at": {"frame": 0}, "range": [0, 1],
                   "edit": [{"stream": "video", "during": [0, 1],
                             "mask": {"rectangle": [160, 96, 128, 96]}}]}],
    })
    preparation = prepare_request(request, asset_map={"source.mkv": str(source)})
    compile_preparation(preparation, out_dir=tmp_path / "compiled")
    composition = compose_candidate(preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition")
    result = verify_candidate(preparation=preparation, composition=composition, source=source)
    assert result["preservation"]["spatial_complement"][0]["protected_pixel_count_per_frame"] == 1024 * 576 - 128 * 96


def test_resized_still_uses_prepared_bytes_after_original_moves(tmp_path):
    from astrid.packs.h3_av.shared.compile import compile_preparation
    from astrid.packs.h3_av.shared.compose import compose_candidate
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.verify import verify_candidate
    from tests.packs.h3_av.test_moving_masks import _fixtures

    source, generated = _fixtures(tmp_path)
    original = tmp_path / "small.png"
    image = Image.new("L", (2, 2), 0)
    image.putpixel((0, 0), 200)
    image.save(original)
    request = normalize_request({
        "version": 2, "prompt": "Change one quadrant", "duration": 1,
        "media": [{"asset": "source.mkv", "role": "timeline", "modality": "video",
                   "at": {"frame": 0}, "range": [0, 1],
                   "edit": [{"stream": "video", "during": [0, 1],
                             "mask": {"asset": "small.png", "threshold": 128}}]}],
    })
    preparation = prepare_request(request, asset_map={"source.mkv": str(source), "small.png": str(original)})
    compile_preparation(preparation, out_dir=tmp_path / "compiled")
    original.rename(tmp_path / "moved-small.png")
    composition = compose_candidate(preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition")
    result = verify_candidate(preparation=preparation, composition=composition, source=source)
    assert result["preservation"]["spatial_complement"][0]["protected_pixel_count_per_frame"] == 1024 * 576 * 3 // 4


def test_managed_permission_survives_compile_scratch_move_through_hooks(tmp_path):
    import importlib.util
    import json
    from pathlib import Path
    from astrid.core.generation.engine_facts import comfyui_facts
    from tests.packs.h3_av.test_moving_masks import _fixtures

    source, generated = _fixtures(tmp_path)
    original = tmp_path / "small.png"
    image = Image.new("L", (2, 2), 0)
    image.putpixel((0, 0), 255)
    image.save(original)
    document = {"version": 2, "prompt": "Change one quadrant", "duration": 1,
                "media": [{"asset": "source", "role": "timeline", "modality": "video",
                           "at": {"frame": 0}, "range": [0, 1],
                           "edit": [{"stream": "video", "during": [0, 1], "mask": {"asset": "mask"}}]}]}
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps(document))
    hook_path = Path(__file__).parents[3] / "astrid/packs/h3_av/extensions/vibecomfy.workflows/hooks.py"
    spec = importlib.util.spec_from_file_location("h3_ds3_test_hooks", hook_path)
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    job = tmp_path / "job"
    hooks.build({"request": str(request_file), "asset_map": {"source": str(source), "mask": str(original)}}, {}, job, engine=comfyui_facts())
    (job / "h3" / "plan" / "compile").rename(tmp_path / "old-compile")
    original.rename(tmp_path / "old-mask.png")
    outputs = hooks.after_outputs(job, {}, {}, [str(generated)], engine=comfyui_facts())
    assert Path(outputs[0]).is_file()
    result = json.loads((job / "h3" / "verify.json").read_text())
    assert result["status"] == "verified"
    assert result["preservation"]["spatial_complement"][0]["protected_pixel_count_per_frame"] == 1024 * 576 * 3 // 4


def test_zero_strength_does_not_shift_prepared_grant_binding(tmp_path):
    import copy
    from astrid.packs.h3_av.shared.compile import compile_preparation
    from astrid.packs.h3_av.shared.prepare import _v2_mask_schedule
    from astrid.packs.h3_av.shared.request import normalize_request
    from tests.packs.h3_av.test_native_masked_edit import WATERMARK, _prepare

    raw = copy.deepcopy(WATERMARK)
    raw["media"][0]["edit"] = [
        {"stream": "video", "during": [0, 12], "strength": 0, "mask": {"rectangle": [0, 0, 64, 64]}},
        {"stream": "video", "during": [0, 12], "strength": 1, "mask": {"rectangle": [512, 288, 128, 128]}},
    ]
    preparation = _prepare(raw, tmp_path)
    preparation["mask_schedule"] = _v2_mask_schedule(normalize_request(raw))
    compile_preparation(preparation, out_dir=tmp_path / "compile")
    changes = preparation["mask_schedule"]["video"]["changes"]
    assert len(changes) == 1 and changes[0]["index"] == 1
    record = next(row for row in preparation["assets"] if row["asset"] == changes[0]["prepared_mask_asset"])
    with Image.open(record["path"]) as mask:
        assert mask.getpixel((512, 288)) == 255 and mask.getpixel((0, 0)) == 0


@pytest.mark.parametrize("offset", [0, 1])
def test_prepared_permission_verifies_each_source_offset(tmp_path, offset):
    import subprocess
    from astrid.packs.h3_av.shared.compile import compile_preparation
    from astrid.packs.h3_av.shared.compose import compose_candidate
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.verify import verify_candidate
    from tests.packs.h3_av.test_moving_masks import _fixtures

    _, generated = _fixtures(tmp_path)
    source = tmp_path / f"source-{offset}.mkv"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=1024x576:rate=24:duration=2",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
                    "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source)], check=True, capture_output=True)
    request = normalize_request({"version": 2, "prompt": "Edit the box", "duration": 1,
        "media": [{"asset": "source", "role": "timeline", "modality": "video", "at": {"frame": 0},
                   "range": [offset, offset + 1], "edit": [{"stream": "video", "during": [0, 1],
                   "mask": {"rectangle": [128, 96, 128, 96]}}]}]})
    preparation = prepare_request(request, asset_map={"source": str(source)})
    compile_preparation(preparation, out_dir=tmp_path / "compiled")
    composition = compose_candidate(preparation=preparation, generated=generated, source=source, out_dir=tmp_path / "composition")
    evidence = verify_candidate(preparation=preparation, composition=composition, source=source)["preservation"]["spatial_complement"]
    assert evidence[0]["protected_pixel_count_per_frame"] == 1024 * 576 - 128 * 96
    assert evidence[0]["max_abs_channel_difference"] == 0


def test_nonempty_grant_vanishing_on_target_grid_names_grant(tmp_path):
    from astrid.packs.h3_av.shared.compile import _combined_strength_mask
    from astrid.packs.h3_av.shared.errors import CompilationError

    mask = Image.new("L", (32, 32), 0)
    mask.putpixel((0, 0), 255)
    path = tmp_path / "tiny.png"
    mask.save(path)
    with pytest.raises(CompilationError, match="mouth.*vanishes"):
        _combined_strength_mask([(path, "image", (0, 1), 0.75, "mouth")], frames=1, destination=tmp_path, size=(32, 32))
