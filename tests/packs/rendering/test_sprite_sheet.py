from __future__ import annotations

import json

import pytest

from astrid.packs.rendering.actions.sprite_sheet.run import choose_layout, main, validate_sheet_dimensions, write_layout_guide


def test_layout_guide_writes_png_and_manifest_shape(tmp_path):
    guide = tmp_path / "layout.png"
    layout = write_layout_guide(guide, cols=2, rows=2, frame_width=128, frame_height=128)

    assert guide.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert layout["sheet_width"] == 256
    assert layout["sheet_height"] == 256
    assert len(layout["frames"]) == 4
    assert layout["frames"][3] == {"index": 4, "x": 128, "y": 128, "width": 128, "height": 128}
    validate_sheet_dimensions(guide, expected_width=256, expected_height=256)


def test_sprite_sheet_dry_run(capsys, tmp_path):
    code = main(
        [
            "--animation",
            "four-frame blink cycle",
            "--subject",
            "simple black dot",
            "--cols",
            "2",
            "--rows",
            "2",
            "--frame-width",
            "512",
            "--frame-height",
            "512",
            "--out-dir",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model"] == "gpt-image-2"
    assert "background" not in payload
    assert "output_format" not in payload
    assert payload["size"] == "1024x1024"
    assert payload["layout_guide"] == str(tmp_path / "layout_guide.png")
    assert payload["sprite_sheet"] == str(tmp_path / "sprite_sheet.png")
    assert payload["alpha_sprite_sheet"] == str(tmp_path / "sprite_sheet_alpha.png")
    assert payload["review_video"] == str(tmp_path / "sprite_preview.mp4")
    assert payload["master_video"] == str(tmp_path / "sprite_preview_prores.mov")
    assert payload["web_dir"] == str(tmp_path / "web")
    assert "Grid: 2 columns by 2 rows" in payload["prompt"]
    assert "chroma-key background" in payload["prompt"]
    assert "pixel-registered in the same place" in payload["prompt"]
    assert "only the parts named by the animation should move" in payload["prompt"]
    assert "no accidental camera movement" in payload["prompt"]
    assert payload["reference_image"] is None
    assert "ai_upscale_provider" not in payload


def test_fal_upscale_is_exposed_by_the_fal_pack_not_sprite_sheet():
    with pytest.raises(SystemExit):
        main(
            [
                "--animation", "four-frame blink cycle",
                "--subject", "simple black dot",
                "--ai-upscale-provider", "fal",
                "--dry-run",
            ]
        )


def test_sprite_sheet_reference_image_dry_run(capsys, tmp_path):
    reference = tmp_path / "reference.png"
    write_layout_guide(reference, cols=1, rows=1, frame_width=128, frame_height=128)

    code = main(
        [
            "--animation",
            "pincer snap",
            "--subject",
            "blue crab mascot",
            "--reference-image",
            str(reference),
            "--cols",
            "2",
            "--rows",
            "2",
            "--frame-width",
            "512",
            "--frame-height",
            "512",
            "--out-dir",
            str(tmp_path / "out"),
            "--dry-run",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["endpoint"].endswith("/images/edits")
    assert payload["model"] == "gpt-image-2"
    assert "background" not in payload
    assert "output_format" not in payload
    assert payload["reference_image"] == str(reference.resolve())
    assert "Use the provided reference image as the source of truth" in payload["prompt"]
    assert "the character reference controls identity" in payload["prompt"]
    assert "The torso/core/base must not bob" in payload["prompt"]


def test_choose_layout_for_25_frames():
    layout = choose_layout(25, frame_width=256, frame_height=256)

    assert layout["cols"] == 5
    assert layout["rows"] == 5
    assert layout["capacity"] == 25


def test_choose_layout_for_30_frames():
    layout = choose_layout(30, frame_width=256, frame_height=256)

    assert layout["cols"] * layout["rows"] >= 30
    assert layout["cols"] * 256 <= 3840
    assert layout["rows"] * 256 <= 3840


def test_choose_layout_for_rectangular_frames():
    layout = choose_layout(12, frame_width=384, frame_height=224)

    assert layout["cols"] * layout["rows"] >= 12
    assert layout["cols"] * 384 <= 3840
    assert layout["rows"] * 224 <= 3840


def test_input_sheet_writes_result_manifest(tmp_path):
    """Prove that non-dry-run --input-sheet flow writes a universal result manifest."""
    # Create a minimal valid sprite sheet as input (512x512 frames, 2x2 grid
    # yields 1024x1024 = 1,048,576 px which exceeds GPT_IMAGE_2_MIN_PIXELS).
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    guide = source_dir / "input_sheet.png"
    write_layout_guide(guide, cols=2, rows=2, frame_width=512, frame_height=512)

    out_dir = tmp_path / "out"
    code = main(
        [
            "--animation", "test blink cycle",
            "--subject", "test dot",
            "--input-sheet", str(guide),
            "--cols", "2",
            "--rows", "2",
            "--frame-width", "512",
            "--frame-height", "512",
            "--out-dir", str(out_dir),
            "--key-color", "#ffffff",
            "--no-prores",
            "--no-web",
            "--force",
        ]
    )

    assert code == 0
    manifest_path = out_dir / "manifest.json"
    assert manifest_path.is_file(), f"universal manifest not found at {manifest_path}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["kind"] == "sprite_sheet"
    assert manifest["schema_version"] == 1
    assert isinstance(manifest["inputs"], dict)
    assert manifest["inputs"]["animation"] == "test blink cycle"
    assert manifest["inputs"]["subject"] == "test dot"
    assert isinstance(manifest["outputs"], list)
    output_paths = {o["path"] for o in manifest["outputs"]}
    assert "layout_guide.png" in output_paths
    assert "sprite_sheet_alpha.png" in output_paths  # transparent mode on
    assert "sprite_manifest.json" in output_paths
    assert "frames" in output_paths
    assert "sprite_preview.mp4" in output_paths
    assert isinstance(manifest["warnings"], list)


def test_rectangular_sprite_sheet_dry_run(capsys, tmp_path):
    code = main(
        [
            "--animation",
            "wide banner motion test",
            "--subject",
            "wide spaceship sprite",
            "--frames",
            "12",
            "--frame-width",
            "384",
            "--frame-height",
            "224",
            "--out-dir",
            str(tmp_path),
            "--dry-run",
        ]
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["size"].endswith("x896")
    assert "Each frame cell is exactly 384x224 pixels." in payload["prompt"]
