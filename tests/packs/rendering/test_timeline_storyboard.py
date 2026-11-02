from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.contracts.errors import AstridError
from astrid.packs.rendering.actions.timeline_storyboard import run as storyboard


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _image(path: Path, content: bytes = b"image") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _color_image(
    path: Path,
    *,
    size: tuple[int, int],
    color: tuple[int, int, int],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def test_explicit_references_are_ordered_preferred_and_render_clickable_html(
    tmp_path: Path,
) -> None:
    first = _image(tmp_path / "media" / "first.png")
    second = _image(tmp_path / "media" / "second.jpg")
    timeline_path = _write_json(
        tmp_path / "timeline.json",
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {
                    "id": "c1",
                    "at": 3,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "fallback-1",
                    "hold": 2,
                    "generation": {
                        "references": [
                            {"asset": "asset:ref-2"},
                            {"assetKey": "ref-1"},
                            {"id": "broken-ref"},
                        ],
                        "input_media": "fallback-1",
                    },
                },
                {
                    "id": "c2",
                    "at": 5,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "fallback-2",
                    "hold": 3,
                },
            ],
            "pinnedShotGroups": [
                {
                    "shotId": "shot-explicit",
                    "trackId": "v1",
                    "clipIds": ["c1", "c2"],
                    "mode": "images",
                }
            ],
        },
    )
    registry_path = _write_json(
        tmp_path / "assets.json",
        {
            "assets": {
                "ref-1": {"file": str(first), "type": "image/png"},
                "ref-2": {"file": str(second), "type": "image/jpeg"},
                "fallback-1": {"file": str(first), "type": "image/png"},
                "fallback-2": {"file": str(second), "type": "image/jpeg"},
            }
        },
    )
    timeline_before = timeline_path.read_bytes()
    registry_before = registry_path.read_bytes()

    result = storyboard.build_storyboard(
        timeline_path=timeline_path,
        assets_registry_path=registry_path,
        out_dir=tmp_path / "out",
    )

    shot = result["view_model"]["shots"][0]
    assert shot == {
        "shot_id": "shot-explicit",
        "track_id": "v1",
        "clip_ids": ["c1", "c2"],
        "start": 3.0,
        "end": 8.0,
        "placeholder": False,
        "prompt": None,
        "metadata": {},
        "inputs": [
            {
                "asset_id": "ref-2",
                "src": str(second.resolve()),
                "type": "image/jpeg",
                "missing": False,
            },
            {
                "asset_id": "ref-1",
                "src": str(first.resolve()),
                "type": "image/png",
                "missing": False,
            },
            {
                "asset_id": "broken-ref",
                "src": None,
                "type": None,
                "missing": True,
            },
        ],
    }
    page = result["preview_html"].read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(2" in page
    assert 'target="_blank"' in page
    assert "Input 1" in page
    assert "3.00s–8.00s" in page
    assert "Missing input image" in page
    assert timeline_path.read_bytes() == timeline_before
    assert registry_path.read_bytes() == registry_before


def test_discord_inputs_sort_bare_then_numeric_through_ten(tmp_path: Path) -> None:
    image_paths = {
        name: _image(tmp_path / f"{name}.png")
        for name in ("one", "two", "three", "ten")
    }
    timeline_path = _write_json(
        tmp_path / "timeline.json",
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {
                    "id": "video",
                    "at": 0,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "output",
                    "from": 2,
                    "to": 10,
                    "speed": 2,
                    "generation": {
                        "input_media_10": "asset:ten",
                        "input_media_3": "three",
                        "input_media": "asset:one",
                        "input_media_2": "two",
                    },
                }
            ],
            "pinnedShotGroups": [
                {
                    "shotId": "shot-discord",
                    "trackId": "v1",
                    "clipIds": ["video"],
                    "mode": "video",
                    "imageClipSnapshot": [{"assetKey": "snapshot"}],
                }
            ],
        },
    )
    registry = {
        "assets": {
            name: {"file": str(path), "type": "image/png"}
            for name, path in image_paths.items()
        }
    }
    registry["assets"]["output"] = {
        "file": str(_image(tmp_path / "output.mp4")),
        "type": "video/mp4",
    }
    registry["assets"]["snapshot"] = {
        "file": str(_image(tmp_path / "snapshot.png")),
        "type": "image/png",
    }
    registry_path = _write_json(tmp_path / "assets.json", registry)

    view_model = storyboard.build_view_model(
        storyboard.load_timeline(timeline_path),
        storyboard.load_registry(registry_path),
        registry_dir=registry_path.parent,
    )

    shot = view_model["shots"][0]
    assert [item["asset_id"] for item in shot["inputs"]] == [
        "one",
        "two",
        "three",
        "ten",
    ]
    assert shot["start"] == 0.0
    assert shot["end"] == 4.0


def test_preview_png_uses_two_by_two_layout_for_four_inputs(tmp_path: Path) -> None:
    colors = [
        (220, 42, 42),
        (42, 190, 74),
        (48, 92, 220),
        (224, 185, 42),
    ]
    sizes = [(1200, 400), (300, 900), (640, 640), (900, 500)]
    image_paths = {
        f"asset-{index}": _color_image(
            tmp_path / f"asset-{index}.png",
            size=size,
            color=color,
        )
        for index, (size, color) in enumerate(zip(sizes, colors), start=1)
    }
    timeline_path = _write_json(
        tmp_path / "timeline.json",
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {
                    "id": "c1",
                    "at": 0,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "output",
                    "hold": 1,
                    "generation": {
                        "references": [
                            {"asset": f"asset-{index}"} for index in range(1, 5)
                        ]
                    },
                }
            ],
            "pinnedShotGroups": [
                {
                    "shotId": "shot-four",
                    "trackId": "v1",
                    "clipIds": ["c1"],
                    "mode": "video",
                }
            ],
        },
    )
    registry_path = _write_json(
        tmp_path / "assets.json",
        {
            "assets": {
                asset_id: {"file": str(path), "type": "image/png"}
                for asset_id, path in image_paths.items()
            }
        },
    )

    result = storyboard.build_storyboard(
        timeline_path=timeline_path,
        assets_registry_path=registry_path,
        out_dir=tmp_path / "out",
    )

    assert result["preview_png"].is_file()
    expected_width = (
        2 * storyboard.CONTACT_SHEET_CELL_WIDTH
        + 3 * storyboard.CONTACT_SHEET_GUTTER
    )
    expected_height = (
        2 * storyboard.CONTACT_SHEET_CELL_HEIGHT
        + 3 * storyboard.CONTACT_SHEET_GUTTER
    )
    with Image.open(result["preview_png"]) as preview:
        assert preview.size == (expected_width, expected_height)
        for index, expected_color in enumerate(colors):
            row, col = divmod(index, 2)
            center_x = (
                storyboard.CONTACT_SHEET_GUTTER
                + col
                * (
                    storyboard.CONTACT_SHEET_CELL_WIDTH
                    + storyboard.CONTACT_SHEET_GUTTER
                )
                + storyboard.CONTACT_SHEET_CELL_WIDTH // 2
            )
            center_y = (
                storyboard.CONTACT_SHEET_GUTTER
                + row
                * (
                    storyboard.CONTACT_SHEET_CELL_HEIGHT
                    + storyboard.CONTACT_SHEET_GUTTER
                )
                + storyboard.CONTACT_SHEET_CELL_HEIGHT // 2
            )
            assert preview.getpixel((center_x, center_y)) == expected_color
        assert preview.getpixel((0, 0)) == storyboard._SHEET_BACKGROUND
        vertical_gutter_x = (
            storyboard.CONTACT_SHEET_GUTTER
            + storyboard.CONTACT_SHEET_CELL_WIDTH
            + storyboard.CONTACT_SHEET_GUTTER // 2
        )
        assert preview.getpixel((vertical_gutter_x, 100)) == storyboard._SHEET_BACKGROUND

    second_preview = tmp_path / "second-preview.png"
    storyboard.render_contact_sheet(result["view_model"], second_preview)
    assert second_preview.read_bytes() == result["preview_png"].read_bytes()


def test_preview_png_draws_a_visible_missing_image_placeholder(tmp_path: Path) -> None:
    out_path = tmp_path / "preview.png"
    storyboard.render_contact_sheet(
        {
            "shots": [
                {
                    "inputs": [
                        {
                            "asset_id": "missing-asset",
                            "src": None,
                            "type": None,
                            "missing": True,
                        }
                    ]
                }
            ]
        },
        out_path,
    )

    with Image.open(out_path) as preview:
        card = preview.crop(
            (
                storyboard.CONTACT_SHEET_GUTTER,
                storyboard.CONTACT_SHEET_GUTTER,
                storyboard.CONTACT_SHEET_GUTTER
                + storyboard.CONTACT_SHEET_CELL_WIDTH,
                storyboard.CONTACT_SHEET_GUTTER
                + storyboard.CONTACT_SHEET_CELL_HEIGHT,
            )
        )
        color_counts = card.getcolors(
            maxcolors=storyboard.CONTACT_SHEET_CELL_WIDTH
            * storyboard.CONTACT_SHEET_CELL_HEIGHT
        )
        assert color_counts is not None
        colors = {color for _count, color in color_counts}
        assert storyboard._MISSING_BACKGROUND in colors
        assert (119, 58, 69) in colors


def test_image_and_video_groups_use_their_declared_fallbacks_and_shot_filter(
    tmp_path: Path,
) -> None:
    images = {
        name: _image(tmp_path / f"{name}.png")
        for name in ("member-a", "member-b", "snapshot-a", "snapshot-b")
    }
    timeline_path = _write_json(
        tmp_path / "timeline.json",
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {
                    "id": "a",
                    "at": 0,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "member-a",
                    "hold": 1,
                },
                {
                    "id": "b",
                    "at": 1,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "member-b",
                    "hold": 1,
                },
                {
                    "id": "video",
                    "at": 4,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "output-video",
                    "hold": 5,
                },
            ],
            "pinnedShotGroups": [
                {
                    "shotId": "images-shot",
                    "trackId": "v1",
                    "clipIds": ["a", "b"],
                    "mode": "images",
                },
                {
                    "shotId": "video-shot",
                    "trackId": "v1",
                    "clipIds": ["video"],
                    "mode": "video",
                    "imageClipSnapshot": [
                        {"assetKey": "snapshot-a"},
                        {"assetKey": "asset:snapshot-b"},
                    ],
                },
            ],
        },
    )
    registry = {
        "assets": {
            name: {"file": str(path), "type": "image/png"}
            for name, path in images.items()
        }
    }
    registry["assets"]["output-video"] = {
        "file": str(_image(tmp_path / "output.mp4")),
        "type": "video/mp4",
    }
    registry_path = _write_json(tmp_path / "assets.json", registry)

    all_shots = storyboard.build_storyboard(
        timeline_path=timeline_path,
        assets_registry_path=registry_path,
        out_dir=tmp_path / "all",
    )["view_model"]["shots"]
    assert [item["asset_id"] for item in all_shots[0]["inputs"]] == [
        "member-a",
        "member-b",
    ]
    assert [item["asset_id"] for item in all_shots[1]["inputs"]] == [
        "snapshot-a",
        "snapshot-b",
    ]

    selected = storyboard.build_storyboard(
        timeline_path=timeline_path,
        assets_registry_path=registry_path,
        out_dir=tmp_path / "selected",
        shot_id="video-shot",
    )["view_model"]["shots"]
    assert [shot["shot_id"] for shot in selected] == ["video-shot"]

    with pytest.raises(AstridError, match="pinned shot not found"):
        storyboard.build_storyboard(
            timeline_path=timeline_path,
            assets_registry_path=registry_path,
            out_dir=tmp_path / "missing",
            shot_id="does-not-exist",
        )


@pytest.mark.parametrize(
    "entry",
    [
        {"file": "relative/frame.png", "url": "https://example.invalid/frame.png"},
        {"file": "file:///tmp/frame.png", "thumbnailUrl": "https://example.invalid/thumb.png"},
        {"file": "data:image/png;base64,AAAA", "thumbnailUrl": "https://example.invalid/thumb.png"},
        {"file": "https://example.invalid/frame.png", "thumbnailUrl": "https://example.invalid/thumb.png"},
        {"file": "/does/not/exist/frame.png", "thumbnailUrl": "https://example.invalid/thumb.png"},
    ],
)
def test_storyboard_never_resolves_locator_or_thumbnail_fallbacks(
    tmp_path: Path, entry: dict[str, str]
) -> None:
    view_model = storyboard.build_view_model(
        {
            "clips": [
                {
                    "id": "c1",
                    "at": 0,
                    "clipType": "media",
                    "asset": "frame",
                    "hold": 1,
                }
            ],
            "pinnedShotGroups": [
                {"shotId": "shot-1", "clipIds": ["c1"], "mode": "images"}
            ],
        },
        {"assets": {"frame": entry}},
        registry_dir=tmp_path,
    )

    item = view_model["shots"][0]["inputs"][0]
    assert item["src"] is None
    assert item["missing"] is True
    assert "example.invalid" not in storyboard.render_html(view_model)


def test_storyboard_accepts_only_existing_absolute_materialized_file(
    tmp_path: Path,
) -> None:
    image_path = _image(tmp_path / "frame.png")
    view_model = storyboard.build_view_model(
        {
            "clips": [
                {
                    "id": "c1",
                    "at": 0,
                    "clipType": "media",
                    "asset": "frame",
                    "hold": 1,
                }
            ],
            "pinnedShotGroups": [
                {"shotId": "shot-1", "clipIds": ["c1"], "mode": "images"}
            ],
        },
        {"assets": {"frame": {"file": str(image_path), "type": "image/png"}}},
        registry_dir=tmp_path,
    )

    item = view_model["shots"][0]["inputs"][0]
    assert item["src"] == str(image_path.resolve())
    assert item["missing"] is False


def test_placeholder_shot_carries_prompt_metadata_and_authored_bounds(
    tmp_path: Path,
) -> None:
    image_path = _image(tmp_path / "media" / "frame.png")
    timeline_path = _write_json(
        tmp_path / "timeline.json",
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {
                    "id": "frame-1",
                    "at": 0,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "frame",
                    "hold": 4,
                },
                {
                    "id": "prompt-slot-1",
                    "at": 4,
                    "track": "v1",
                    "clipType": "effect-layer",
                    "hold": 3,
                    "generation": {
                        "role": "placeholder",
                        "prompt": "Empty slot: title card prompt.",
                        "metadata": {"label": "Intro prompt slot", "note": "No media."},
                    },
                },
            ],
            "pinnedShotGroups": [
                {
                    "shotId": "storyboard-shot",
                    "trackId": "v1",
                    "clipIds": ["frame-1"],
                    "mode": "images",
                },
                {
                    "shotId": "prompt-slot-shot",
                    "trackId": "v1",
                    "clipIds": ["prompt-slot-1"],
                    "mode": "images",
                },
            ],
        },
    )
    registry_path = _write_json(
        tmp_path / "assets.json",
        {"assets": {"frame": {"file": str(image_path), "type": "image/png"}}},
    )

    view_model = storyboard.build_view_model(
        storyboard.load_timeline(timeline_path),
        storyboard.load_registry(registry_path),
        registry_dir=registry_path.parent,
    )

    slot = view_model["shots"][1]
    assert slot["placeholder"] is True
    assert slot["prompt"] == "Empty slot: title card prompt."
    assert slot["metadata"] == {"label": "Intro prompt slot", "note": "No media."}
    assert slot["start"] == 4.0
    assert slot["end"] == 7.0
    assert slot["inputs"] == []

    page = storyboard.render_html(view_model)
    assert "placeholder" in page
    assert "Empty slot: title card prompt." in page
    assert "Intro prompt slot" in page

    # Authored bounds on a media-less shot are honored when the config carries
    # them (the shared PinnedShotGroup schema must be opened by the epic before
    # validated timelines can express this; build_view_model is schema-free).
    authored_view = storyboard.build_view_model(
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [],
            "pinnedShotGroups": [
                {
                    "shotId": "authored-bounds-shot",
                    "trackId": "v1",
                    "mode": "images",
                    "start": 10.0,
                    "end": 14.0,
                }
            ],
        },
        {"assets": {}},
        registry_dir=registry_path.parent,
    )
    authored = authored_view["shots"][0]
    assert authored["placeholder"] is True
    assert authored["start"] == 10.0
    assert authored["end"] == 14.0
    assert authored["prompt"] is None
    assert authored["metadata"] == {}
    assert "10.00s–14.00s" in storyboard.render_html(authored_view)


def test_placeholder_flag_distinguishes_media_shots_from_slots(tmp_path: Path) -> None:
    image_path = _image(tmp_path / "media" / "frame.png")
    video_path = _image(tmp_path / "media" / "clip.mp4")
    timeline_path = _write_json(
        tmp_path / "timeline.json",
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {
                    "id": "video",
                    "at": 0,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "clip",
                    "hold": 5,
                }
            ],
            "pinnedShotGroups": [
                {
                    "shotId": "video-shot",
                    "trackId": "v1",
                    "clipIds": ["video"],
                    "mode": "video",
                }
            ],
        },
    )
    registry_path = _write_json(
        tmp_path / "assets.json",
        {
            "assets": {
                "clip": {"file": str(video_path), "type": "video/mp4"},
                "frame": {"file": str(image_path), "type": "image/png"},
            }
        },
    )

    view_model = storyboard.build_view_model(
        storyboard.load_timeline(timeline_path),
        storyboard.load_registry(registry_path),
        registry_dir=registry_path.parent,
    )
    shot = view_model["shots"][0]
    assert shot["placeholder"] is False, (
        "a media clip with no image snapshot is still a media shot, not a slot"
    )
    assert shot["inputs"] == []

    # Dangling clipIds must not unlock authored bounds, and reversed bounds
    # are ignored (build_view_model is schema-free; the shared schema rejects
    # these keys until the epic opens it).
    v = storyboard.build_view_model(
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [],
            "pinnedShotGroups": [
                {
                    "shotId": "dangling",
                    "trackId": "v1",
                    "clipIds": ["ghost"],
                    "mode": "images",
                    "start": 3.0,
                    "end": 9.0,
                },
                {
                    "shotId": "reversed",
                    "trackId": "v1",
                    "mode": "images",
                    "start": 9.0,
                    "end": 3.0,
                },
            ],
        },
        {"assets": {}},
        registry_dir=registry_path.parent,
    )
    by_id = {s["shot_id"]: s for s in v["shots"]}
    assert by_id["dangling"]["start"] == 0.0 and by_id["dangling"]["end"] == 0.0
    assert by_id["reversed"]["start"] == 0.0 and by_id["reversed"]["end"] == 0.0


def test_main_writes_declared_outputs_and_universal_manifest(tmp_path: Path) -> None:
    image_path = _image(tmp_path / "input.png")
    timeline_path = _write_json(
        tmp_path / "timeline.json",
        {
            "tracks": [{"id": "v1", "kind": "visual", "label": "Visual"}],
            "clips": [
                {
                    "id": "c1",
                    "at": 0,
                    "track": "v1",
                    "clipType": "media",
                    "asset": "input",
                    "hold": 2,
                }
            ],
            "pinnedShotGroups": [
                {
                    "shotId": "shot-1",
                    "trackId": "v1",
                    "clipIds": ["c1"],
                    "mode": "images",
                }
            ],
        },
    )
    registry_path = _write_json(
        tmp_path / "assets.json",
        {"assets": {"input": {"file": str(image_path), "type": "image/png"}}},
    )
    out_dir = tmp_path / "out"

    assert (
        storyboard.main(
            [
                "--timeline",
                str(timeline_path),
                "--assets-registry",
                str(registry_path),
                "--out",
                str(out_dir),
            ]
        )
        == 0
    )

    assert (out_dir / "preview.json").is_file()
    assert (out_dir / "preview.png").is_file()
    assert (out_dir / "preview.html").is_file()
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "timeline_storyboard"
    assert [item["path"] for item in manifest["outputs"]] == [
        "preview.json",
        "preview.png",
        "preview.html",
    ]
    assert all(item["bytes"] > 0 for item in manifest["outputs"])
