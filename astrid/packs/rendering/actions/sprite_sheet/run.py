#!/usr/bin/env python3
"""Generate a GPT Image sprite sheet, slice frames, and assemble previews."""

# The canonical-entrypoint guard intentionally runs before imports.
# ruff: noqa: E402

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint

guard_canonical_entrypoint("rendering.sprite_sheet")

# Re-exports from focused modules
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.cli_choices import add_choice_arg
from astrid.core.util.credentials_scope import CredentialsScope
from astrid.packs.generation.actions.generate_image_openai.run import DEFAULT_MODEL

from .png_io import (  # noqa: F401
    _alpha_bbox,
    _png_chunk,
    _png_dimensions,
    _read_rgba_png,
    _write_rgb_png,
    _write_rgba_png,
    analyze_frames,
    scrub_fully_transparent_rgb,
)
from .sheet import (  # noqa: F401
    _draw_line,
    _hex_color_no_hash,
    _key_color_name,
    _layout_is_valid,
    _parse_hex_color,
    _set_pixel,
    choose_layout,
    validate_sheet_dimensions,
    write_layout_guide,
)
from .upscale import (  # noqa: F401
    DEFAULT_KEY_COLOR,
    EDIT_API_URL,
    _call_image_edit_api,
    _image_content_type,
    _multipart_field,
    _multipart_file,
    _request_payload_for_image_model,
    _sprite_prompt,
    _write_first_image,
    upscale_frames,
)
from .web_outputs import (  # noqa: F401
    _run,
    _scale_filter_for_web,
    assemble_animated_webp,
    assemble_prores_video,
    assemble_review_video,
    assemble_sprite_sheet_from_frames,
    assemble_web_mp4,
    build_web_outputs,
    convert_frames_to_webp,
    convert_image_to_webp,
    normalize_frame_frame,
    normalize_frames,
    remove_chroma_key,
    slice_frames,
)


def build(args: argparse.Namespace) -> int:
    input_sheet = args.input_sheet.expanduser().resolve() if args.input_sheet is not None else None
    reference_image = (
        Path(args.reference_image).expanduser().resolve() if args.reference_image else None
    )
    if reference_image is not None and not reference_image.is_file():
        from astrid.core.contracts.die import pack_die as _die

        _die(f"--reference-image not found: {reference_image}")
    if input_sheet is not None and reference_image is not None:
        from astrid.core.contracts.die import pack_die as _die

        _die(
            "--reference-image generates a new sheet and cannot be combined with --input-sheet post-processing"
        )
    inferred_cols = args.cols
    inferred_rows = args.rows
    if input_sheet is not None:
        if not input_sheet.is_file():
            from astrid.core.contracts.die import pack_die as _die

            _die(f"--input-sheet not found: {input_sheet}")
        input_width, input_height = _png_dimensions(input_sheet)
        if inferred_cols is None:
            if input_width % args.frame_width:
                from astrid.core.contracts.die import pack_die as _die

                _die(
                    "--input-sheet width is not divisible by --frame-width; pass --cols explicitly or adjust frame size"
                )
            inferred_cols = input_width // args.frame_width
        if inferred_rows is None:
            if input_height % args.frame_height:
                from astrid.core.contracts.die import pack_die as _die

                _die(
                    "--input-sheet height is not divisible by --frame-height; pass --rows explicitly or adjust frame size"
                )
            inferred_rows = input_height // args.frame_height

    requested_frames = (
        args.frames
        if args.frames is not None
        else ((inferred_cols or 4) * (inferred_rows or 4) if inferred_cols or inferred_rows else 16)
    )
    planned = choose_layout(
        requested_frames,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        fixed_cols=inferred_cols,
        fixed_rows=inferred_rows,
    )
    cols = planned["cols"]
    rows = planned["rows"]
    frame_count = planned["frame_count"]
    sheet_width = cols * args.frame_width
    sheet_height = rows * args.frame_height
    size = f"{sheet_width}x{sheet_height}"
    validation_payload = dict(_request_payload_for_image_model(args, "validation", size))
    validation_payload.setdefault("n", 1)

    from astrid.packs.generation.actions.generate_image_openai.run import (
        API_URL,
        DEFAULT_MODEL,
        _call_image_api,
        _validate_payload,
    )

    _validate_payload(validation_payload)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    guide_path = args.out_dir / "layout_guide.png"
    sheet_path = args.out_dir / "sprite_sheet.png"
    alpha_sheet_path = args.out_dir / "sprite_sheet_alpha.png"
    frames_dir = args.out_dir / "frames"
    normalized_frames_dir = args.out_dir / "frames_normalized"
    upscaled_frames_dir = args.out_dir / "frames_upscaled"
    web_dir = args.out_dir / "web"
    processed_sheet_path = args.out_dir / "sprite_sheet_processed.png"
    review_video_path = args.out_dir / "sprite_preview.mp4"
    master_video_path = args.out_dir / "sprite_preview_prores.mov"
    manifest_path = args.out_dir / "sprite_manifest.json"

    guide_background = args.key_color if args.transparent else "#ffffff"
    layout = write_layout_guide(
        guide_path,
        cols=cols,
        rows=rows,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        frame_count=frame_count,
        safe_margin=args.safe_margin,
        background_color=guide_background,
    )
    prompt = _sprite_prompt(args, layout, has_reference_image=reference_image is not None)
    request_payload = _request_payload_for_image_model(args, prompt, size)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "endpoint": "postprocess"
                    if input_sheet is not None
                    else (
                        EDIT_API_URL
                        if reference_image is not None
                        or (args.use_layout_guide and args.model != DEFAULT_MODEL)
                        else API_URL
                    ),
                    "input_sheet": str(input_sheet) if input_sheet is not None else None,
                    "reference_image": str(reference_image)
                    if reference_image is not None
                    else None,
                    "layout_guide": str(guide_path),
                    "sprite_sheet": str(input_sheet or sheet_path),
                    "alpha_sprite_sheet": str(alpha_sheet_path) if args.transparent else None,
                    "frames_dir": str(frames_dir),
                    "review_video": str(review_video_path),
                    "master_video": str(master_video_path),
                    "web_dir": str(web_dir) if args.web else None,
                    "manifest": str(manifest_path),
                    **request_payload,
                },
                indent=2,
            )
        )
        return 0

    response: dict[str, Any] = {}
    source_sheet_path = input_sheet or sheet_path
    if input_sheet is None:
        api_key = CredentialsScope.get_local("openai", env_file=args.env_file)
        print(f"Calling {args.model} for {size} sprite sheet", file=sys.stderr)
        started = time.time()
        if reference_image is not None:
            response = _call_image_edit_api(request_payload, reference_image, api_key, args.timeout)
        elif args.use_layout_guide and args.model != DEFAULT_MODEL:
            response = _call_image_edit_api(request_payload, guide_path, api_key, args.timeout)
        else:
            response = _call_image_api(request_payload, api_key, args.timeout)
        print(f"Sprite sheet completed in {time.time() - started:.1f}s", file=sys.stderr)
        _write_first_image(response, sheet_path, args.force)
    else:
        print(f"Post-processing existing sprite sheet {input_sheet}", file=sys.stderr)

    validate_sheet_dimensions(
        source_sheet_path, expected_width=sheet_width, expected_height=sheet_height
    )
    slice_source_path = source_sheet_path
    if args.transparent:
        remove_chroma_key(
            source_sheet_path,
            alpha_sheet_path,
            key_color=args.key_color,
            similarity=args.key_similarity,
            blend=args.key_blend,
            force=args.force,
        )
        validate_sheet_dimensions(
            alpha_sheet_path, expected_width=sheet_width, expected_height=sheet_height
        )
        slice_source_path = alpha_sheet_path

    frames = slice_frames(
        slice_source_path,
        frames_dir,
        cols=cols,
        rows=rows,
        frame_width=args.frame_width,
        frame_height=args.frame_height,
        frame_count=frame_count,
        trim=args.slice_trim,
        force=args.force,
    )
    frame_analysis = analyze_frames(frames, edge_margin=args.edge_margin)
    edge_warnings = [item for item in frame_analysis if item.get("touches_edge")]
    if edge_warnings:
        print(
            f"Warning: {len(edge_warnings)} frame(s) touch the safety edge after slicing.",
            file=sys.stderr,
        )

    output_frames = frames
    normalized_frame_paths: list[str] | None = None
    normalize_report: list[dict[str, Any]] | None = None
    preview_frames_dir = frames_dir
    if args.normalize_frames:
        output_frames, normalize_report = normalize_frames(
            frames, normalized_frames_dir, margin=args.normalize_margin, force=args.force
        )
        normalized_frame_paths = output_frames
        preview_frames_dir = normalized_frames_dir
    upscaled_frames: list[str] | None = None
    if abs(args.upscale_factor - 1.0) >= 0.0001:
        upscaled_frames = upscale_frames(
            output_frames,
            upscaled_frames_dir,
            factor=args.upscale_factor,
            filter_name=args.upscale_filter,
            force=args.force,
        )
        output_frames = upscaled_frames
        preview_frames_dir = upscaled_frames_dir
    final_sheet_path: Path | None = None
    if preview_frames_dir != frames_dir:
        assemble_sprite_sheet_from_frames(
            preview_frames_dir, processed_sheet_path, cols=cols, rows=rows, force=args.force
        )
        final_sheet_path = processed_sheet_path

    assemble_review_video(
        preview_frames_dir,
        review_video_path,
        fps=args.fps,
        background=args.review_background,
        force=args.force,
    )
    if args.prores:
        assemble_prores_video(preview_frames_dir, master_video_path, fps=args.fps, force=args.force)
    web_outputs: dict[str, Any] | None = None
    if args.web:
        web_outputs = build_web_outputs(
            source_sheet=final_sheet_path or slice_source_path,
            frames=output_frames,
            frames_dir=preview_frames_dir,
            out_dir=web_dir,
            fps=args.fps,
            background=args.review_background,
            quality=args.web_quality,
            lossless_frames=args.web_lossless,
            max_dim=args.web_max_dim,
            mp4_crf=args.web_mp4_crf,
            animated=args.web_animated,
            force=args.force,
        )

    manifest = {
        "animation": args.animation,
        "subject": args.subject,
        "style": args.style,
        "model": args.model,
        "reference_image": str(reference_image) if reference_image is not None else None,
        "layout": layout,
        "prompt": prompt,
        "layout_guide": str(guide_path),
        "sprite_sheet": str(source_sheet_path),
        "alpha_sprite_sheet": str(alpha_sheet_path) if args.transparent else None,
        "processed_sprite_sheet": str(final_sheet_path) if final_sheet_path else None,
        "frames": frames,
        "normalized_frames": normalized_frame_paths,
        "upscaled_frames": upscaled_frames,
        "review_video": str(review_video_path),
        "master_video": str(master_video_path) if args.prores else None,
        "web_outputs": web_outputs,
        "fps": args.fps,
        "slice_trim": args.slice_trim,
        "transparent": args.transparent,
        "key_color": args.key_color if args.transparent else None,
        "key_similarity": args.key_similarity if args.transparent else None,
        "key_blend": args.key_blend if args.transparent else None,
        "review_background": args.review_background,
        "frame_analysis": frame_analysis,
        "edge_warning_count": len(edge_warnings),
        "normalized_frame_report": normalize_report,
        "upscale_factor": args.upscale_factor,
        "upscale_filter": args.upscale_filter if abs(args.upscale_factor - 1.0) >= 0.0001 else None,
        "request": {key: value for key, value in request_payload.items() if key != "prompt"},
        "usage": response.get("usage"),
        "created": response.get("created"),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {manifest_path}")

    # --- universal result manifest -----------------------
    universal_outputs: list[dict[str, Any]] = [
        {"path": "layout_guide.png", "type": "file"},
        {"path": "sprite_manifest.json", "type": "file"},
        {"path": "frames", "type": "directory"},
        {"path": "sprite_preview.mp4", "type": "file"},
    ]
    if input_sheet is None:
        universal_outputs.append({"path": "sprite_sheet.png", "type": "file"})
    if args.transparent:
        universal_outputs.append({"path": "sprite_sheet_alpha.png", "type": "file"})
    if args.prores:
        universal_outputs.append(
            {"path": "sprite_preview_prores.mov", "type": "file", "optional": True}
        )
    if final_sheet_path is not None:
        universal_outputs.append({"path": "sprite_sheet_processed.png", "type": "file"})
    if args.normalize_frames and normalized_frame_paths:
        universal_outputs.append({"path": "frames_normalized", "type": "directory"})
    if upscaled_frames:
        universal_outputs.append({"path": "frames_upscaled", "type": "directory"})
    if args.web:
        universal_outputs.append({"path": "web", "type": "directory"})

    universal_manifest = build_manifest(
        kind="sprite_sheet",
        inputs={
            "animation": args.animation,
            "subject": args.subject,
            "model": args.model,
            "reference_image": str(reference_image) if reference_image is not None else None,
            "input_sheet": str(input_sheet) if input_sheet is not None else None,
        },
        outputs=universal_outputs,
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(args.out_dir / "manifest.json", universal_manifest)
    # -------------------------------------------------------------------------

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate, slice, and preview a GPT Image sprite sheet."
    )
    add = parser.add_argument
    add("--animation", required=True, help="Specific animation to depict across the frames.")
    add("--subject", required=True, help="Character/object being animated.")
    add(
        "--style",
        default="clean high-quality 2D game animation, crisp silhouette, consistent character design",
    )
    add("--background", default="plain white background")
    add(
        "--transparent",
        dest="transparent",
        action="store_true",
        default=True,
        help="Generate on chroma key and remove it into alpha PNG frames.",
    )
    add(
        "--no-transparent",
        dest="transparent",
        action="store_false",
        help="Keep the prompted background instead of removing it.",
    )
    add(
        "--key-color",
        default=DEFAULT_KEY_COLOR,
        help="Chroma-key color used when --transparent is enabled.",
    )
    add("--key-similarity", type=float, default=0.08, help="ffmpeg colorkey similarity threshold.")
    add("--key-blend", type=float, default=0.03, help="ffmpeg colorkey edge blend.")
    add(
        "--review-background",
        default="white",
        help="Background color for the review MP4 after alpha extraction.",
    )
    add(
        "--edge-margin",
        type=int,
        default=4,
        help="Warn when sliced alpha content is within this many pixels of a frame edge.",
    )
    add(
        "--normalize-frames",
        action="store_true",
        help="Crop each sliced frame to alpha content, scale down if needed, and recenter it in the frame.",
    )
    add(
        "--normalize-margin",
        type=int,
        default=16,
        help="Minimum transparent margin for --normalize-frames.",
    )
    add(
        "--upscale-factor",
        type=float,
        default=1.0,
        help="Scale transparent frames after slicing/normalization and before video/web exports.",
    )
    add_choice_arg(
        parser,
        "--upscale-filter",
        values=("lanczos", "bicubic", "spline", "neighbor"),
        default="lanczos",
        help="ffmpeg scale filter for --upscale-factor.",
    )
    add(
        "--frames",
        type=int,
        help="Number of animation frames to generate. Defaults to grid capacity, or 16 when no grid is supplied.",
    )
    add("--cols", type=int, help="Grid columns. If omitted, chosen automatically.")
    add("--rows", type=int, help="Grid rows. If omitted, chosen automatically.")
    add("--frame-width", type=int, default=256)
    add("--frame-height", type=int, default=256)
    add(
        "--safe-margin",
        type=int,
        help="Minimum pixel margin inside each cell requested in the layout guide and prompt. Defaults to max(24, frame_size/8).",
    )
    add("--fps", type=int, default=8)
    add(
        "--slice-trim",
        type=int,
        default=0,
        help="Pixels to trim from each cell edge while slicing frames.",
    )
    add("--model", default=DEFAULT_MODEL)
    add("--quality", default="medium")
    add("--out-dir", type=Path, default=Path("runs/sprite-sheet"))
    add(
        "--input-sheet",
        type=Path,
        help="Existing PNG sprite sheet to post-process instead of generating a new sheet.",
    )
    add(
        "--reference-image",
        "--input-image",
        default="",
        help="Reference image for the character/object to preserve while generating a new sprite sheet.",
    )
    add("--env-file", type=Path)
    add("--timeout", type=int, default=240)
    add("--force", action="store_true")
    add("--dry-run", action="store_true")
    add(
        "--prores",
        dest="prores",
        action="store_true",
        default=True,
        help="Also write a high-quality ProRes MOV preview.",
    )
    add("--no-prores", dest="prores", action="store_false")
    add(
        "--web",
        dest="web",
        action="store_true",
        default=True,
        help="Write web-optimized WebP frames/sheet and a lighter MP4 preview.",
    )
    add("--no-web", dest="web", action="store_false")
    add("--web-quality", type=int, default=82, help="WebP quality for web exports.")
    add(
        "--web-lossless", action="store_true", help="Use lossless WebP for sheet and frame exports."
    )
    add(
        "--web-max-dim",
        type=int,
        default=512,
        help="Maximum width/height for web frame animation exports. Use 0 for original size.",
    )
    add(
        "--web-mp4-crf",
        type=int,
        default=24,
        help="CRF for web MP4 preview; lower is higher quality.",
    )
    add(
        "--web-animated",
        action="store_true",
        help="Also write animated WebP. Off by default because atlas/frame assets are faster for web runtimes.",
    )
    add("--use-layout-guide", dest="use_layout_guide", action="store_true", default=True)
    add("--no-layout-guide", dest="use_layout_guide", action="store_false")
    return parser


def main(argv: list[str] | None = None) -> int:
    from astrid.core.contracts.die import pack_die as _die

    args = build_parser().parse_args(argv)
    if args.cols is not None and args.cols < 1:
        _die("--cols must be >= 1")
    if args.rows is not None and args.rows < 1:
        _die("--rows must be >= 1")
    if args.frame_width < 16 or args.frame_height < 16:
        _die("--frame-width and --frame-height must be >= 16")
    if args.frame_width % 16 or args.frame_height % 16:
        _die("--frame-width and --frame-height must be multiples of 16")
    if args.fps < 1:
        _die("--fps must be >= 1")
    if args.slice_trim < 0:
        _die("--slice-trim must be >= 0")
    if args.safe_margin is not None and args.safe_margin < 0:
        _die("--safe-margin must be >= 0")
    _parse_hex_color(args.key_color)
    if args.key_similarity < 0 or args.key_similarity > 1:
        _die("--key-similarity must be between 0 and 1")
    if args.key_blend < 0 or args.key_blend > 1:
        _die("--key-blend must be between 0 and 1")
    if args.edge_margin < 0:
        _die("--edge-margin must be >= 0")
    if args.normalize_margin < 0:
        _die("--normalize-margin must be >= 0")
    if args.upscale_factor <= 0:
        _die("--upscale-factor must be > 0")
    if args.web_quality < 0 or args.web_quality > 100:
        _die("--web-quality must be between 0 and 100")
    if args.web_max_dim < 0:
        _die("--web-max-dim must be >= 0")
    if args.web_mp4_crf < 0 or args.web_mp4_crf > 51:
        _die("--web-mp4-crf must be between 0 and 51")
    if args.web_max_dim == 0:
        args.web_max_dim = None
    return build(args)


if __name__ == "__main__":
    raise SystemExit(main())
