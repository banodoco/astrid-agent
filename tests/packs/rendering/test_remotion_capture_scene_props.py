"""Exercise current capture preparation without a browser, server or Node child."""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.core.theme import builtin_theme
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion


@pytest.fixture
def capture_host(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    theme = builtin_theme()
    theme["id"] = "pinned-runtime-theme"
    theme["visual"]["canvas"] = {"width": 800, "height": 600, "fps": 24}
    theme["visual"]["color"]["bg"] = "#102030"
    theme_path = tmp_path / "theme.json"
    theme_path.write_text(json.dumps(theme))
    raw = {
        "theme": "historical-slug",
        "theme_overrides": {"visual": {"canvas": {"width": 640, "fps": 29.97}}},
        "tracks": [{"id": "v", "kind": "visual", "label": "Visual"}],
        "clips": [
            {
                "id": "original-clip",
                "track": "v",
                "clipType": "media",
                "asset": "image",
                "at": 1.25,
                "hold": 2.5,
                "params": {"opaque": {"explicit_null": None, "values": [0, False, ""]}},
                "app": {"future": {"occurrence": "keep-exact"}},
            }
        ],
        "metadata": {"future": {"revision": "opaque-value"}},
        "app": {"future": {"enabled": False}},
    }
    timeline_path = tmp_path / "timeline.json"
    timeline_path.write_text(json.dumps(raw))
    payload = b"runtime-managed-image"
    registry = {
        "assets": {
            "image": {
                "object_id": "managed-image",
                "digest": hashlib.sha256(payload).hexdigest(),
                "mime_type": "image/png",
                "opaque": {"keep": [None, False]},
            }
        },
        "opaque": {"registry": "preserved"},
    }
    assets_path = tmp_path / "assets.json"
    assets_path.write_text(json.dumps(registry))
    state = SimpleNamespace(
        project=project,
        timeline_path=timeline_path,
        assets_path=assets_path,
        theme_path=theme_path,
        raw=raw,
        registry=registry,
        events=[],
        commands=[],
        requests=[],
        frames=None,
        serialized=None,
        active=False,
        staged_paths=[],
        public_root=None,
        fail_capture=False,
        omit_frame=None,
    )

    class AssetServer:
        def __init__(self, root, *, allowed_origin):
            self.root = root
            assert allowed_origin == "http://localhost:32101"

        def __enter__(self):
            state.active = True
            state.events.append("server-enter")
            return self

        def local_url(self, path):
            assert state.active and path.is_relative_to(self.root)
            assert path.read_bytes() == payload
            state.staged_paths.append(path)
            return "http://localhost:32102/" + path.name

        def __exit__(self, *args):
            state.events.append("server-exit")
            state.active = False

    def stage(timeline_data, *, render_hash, **kwargs):
        state.public_root = project / "public" / "astrid-effects" / render_hash
        state.public_root.mkdir(parents=True)
        (state.public_root / "sprite.png").write_bytes(b"staged-effect-asset")
        timeline_data["clips"][0]["params"]["staged_sprite"] = "astrid-effects/sprite.png"
        state.events.append("stage")
        return {"root": str(state.public_root), "effects": []}

    def consume(props_path, output_dir, frames):
        assert state.active and all(path.is_file() for path in state.staged_paths)
        assert state.public_root.is_dir()
        state.events.append("capture")
        state.frames = frames
        state.serialized = props_path.read_bytes()
        if state.fail_capture:
            raise RuntimeError("capture failed")
        for frame in frames:
            if frame != state.omit_frame:
                Image.new("RGB", (8, 6)).save(output_dir / f"frame-{frame}.png")

    def command(argv, **kwargs):
        state.commands.append(argv)
        frames = tuple(
            int(n)
            for n in next(a for a in argv if a.startswith("--frames=")).split("=")[1].split(",")
        )
        consume(
            Path(argv[argv.index("--props") + 1]),
            Path(argv[argv.index("--output") + 1]),
            frames,
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    class FrameSession:
        def render(self, **kwargs):
            state.requests.append(kwargs)
            consume(kwargs["props_path"], kwargs["output_dir"], kwargs["frames"])

        def retain_staged_public_root(self, path):
            assert path == state.public_root and path.is_dir()

    state.session = FrameSession()
    state.kwargs = {
        "project_dir": project,
        "theme_path": theme_path,
        "materialized_objects": {"managed-image": payload},
        "staging_parent": tmp_path / "staging",
    }
    monkeypatch.setattr(remotion, "InvocationAssetServer", AssetServer)
    monkeypatch.setattr(remotion, "_available_remotion_port", lambda: 32101)
    monkeypatch.setattr(remotion.remotion_lock, "remotion_render_lock", nullcontext)
    monkeypatch.setattr(
        remotion,
        "_validate_project_dir",
        lambda _: SimpleNamespace(
            node_executable=Path("/owned/node"),
            remotion_cli=Path("/owned/cli"),
            node_version="v20.19.4",
        ),
    )
    monkeypatch.setattr(remotion, "_regenerate_element_registries", lambda *a: None)
    monkeypatch.setattr(remotion, "_validate_renderer_effect_registry", lambda *a: None)
    monkeypatch.setattr(remotion, "_effective_registry_state", lambda *a: {})
    monkeypatch.setattr(remotion, "_stage_effect_assets_for_timeline", stage)
    monkeypatch.setattr(remotion.subprocess, "run", command)
    monkeypatch.delenv("ASTRID_PROGRESS_PATH", raising=False)
    return state


def capture(host, tmp_path, **kwargs):
    return remotion.capture_remotion_frames(
        host.timeline_path,
        host.assets_path,
        tmp_path / "frames",
        [7, 0, 3, 7],
        **host.kwargs,
        **kwargs,
    )


def assert_cleaned(host, tmp_path):
    assert not host.active and host.events[-1] == "server-exit"
    assert all(not path.exists() for path in host.staged_paths)
    assert not host.public_root.exists()
    assert not (tmp_path / "frames" / ".remotion-props.json").exists()
    assert not (tmp_path / "frames" / ".capture-placeholder.mp4").exists()
    assert list(host.kwargs["staging_parent"].iterdir()) == []


@pytest.mark.parametrize("alpha", [False, True])
@pytest.mark.parametrize("persistent", [False, True])
def test_callback_observes_final_props_and_same_capture_frames(
    capture_host, tmp_path, alpha, persistent
):
    host = capture_host
    if alpha:
        host.raw["metadata"]["astrid_layer"] = {"alpha": True, "z": 1}
        host.timeline_path.write_text(json.dumps(host.raw))
    originals = [
        path.read_bytes() for path in (host.timeline_path, host.assets_path, host.theme_path)
    ]
    seen = []

    def observe(props, frames):
        assert host.active and all(path.is_file() for path in host.staged_paths)
        assert host.public_root.is_dir()
        serialized = (tmp_path / "frames" / ".remotion-props.json").read_bytes()
        assert json.dumps(props).encode() == serialized
        assert frames == (0, 3, 7)
        assert props["assets"] == {
            **host.registry,
            "assets": {
                "image": {
                    **host.registry["assets"]["image"],
                    "file": "http://localhost:32102/" + host.staged_paths[0].name,
                }
            },
        }
        expected = remotion._serialize_timeline(
            host.timeline_path, default_theme="pinned-runtime-theme"
        )
        expected["clips"][0]["params"]["staged_sprite"] = "astrid-effects/sprite.png"
        assert props["timeline"] == expected
        assert props["timeline"]["clips"][0]["app"] == host.raw["clips"][0]["app"]
        assert (
            props["timeline"]["clips"][0]["params"]["opaque"]
            == host.raw["clips"][0]["params"]["opaque"]
        )
        expected_theme = remotion._resolved_theme_for_render(host.timeline_path, host.theme_path)
        if alpha:
            expected_theme["visual"]["color"]["bg"] = "transparent"
        assert props["theme"] == expected_theme and props["review"] is None
        # Whole partial canvas selection uses Root defaults, not the merged
        # theme's height=600 or the requested thumbnail dimensions.
        profile = remotion._canonical_profile(host.timeline_path, host.registry, host.theme_path)
        assert (profile.width, profile.height, profile.fps_rational) == (
            640,
            1080,
            (2997, 100),
        )
        seen.append((frames, serialized))
        host.events.append("callback")

    result = capture(
        host,
        tmp_path,
        on_scene_props=observe,
        frame_session=host.session if persistent else None,
        frame_resolution=(4, 3),
    )
    assert list(sorted(result)) == [0, 3, 7] and len(seen) == 1
    assert host.events == [
        "server-enter",
        "stage",
        "callback",
        "capture",
        "server-exit",
    ]
    assert host.serialized == seen[0][1]
    if persistent:
        assert host.frames is seen[0][0]
        assert host.requests[0]["resolution"] == (4, 3)
    else:
        assert "--frames=0,3,7" in host.commands[0]
    for path in result.values():
        with Image.open(path) as image:
            assert image.size == (4, 3)
    assert originals == [
        path.read_bytes() for path in (host.timeline_path, host.assets_path, host.theme_path)
    ]
    assert_cleaned(host, tmp_path)


@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_callback_keeps_existing_call_shape(tmp_path, monkeypatch, explicit_none):
    monkeypatch.setattr(remotion.remotion_lock, "remotion_render_lock", nullcontext)
    calls = []

    def execute(*args, **kwargs):
        assert "on_scene_props" not in kwargs and "window" not in kwargs
        calls.append(kwargs["frame_numbers"])
        for frame in kwargs["frame_numbers"]:
            (kwargs["frame_output_dir"] / f"frame-{frame}.png").write_bytes(b"boundary-only")

    monkeypatch.setattr(remotion, "_execute_remotion_locked", execute)
    result = remotion.capture_remotion_frames(
        tmp_path / "timeline",
        tmp_path / "assets",
        tmp_path / "frames",
        [7, 0, 3, 7],
        project_dir=tmp_path,
        **({"on_scene_props": None} if explicit_none else {}),
    )
    assert calls == [(0, 3, 7)] and sorted(result) == [0, 3, 7]


def test_callback_leaves_serialized_node_props_unchanged(capture_host, tmp_path):
    host = capture_host
    serialized = []
    observations = []
    for options in (
        {},
        {"on_scene_props": None},
        {
            "on_scene_props": lambda props, frames: observations.append(json.dumps(props).encode()),
        },
    ):
        host.staged_paths.clear()
        capture(host, tmp_path, frame_session=host.session, **options)
        serialized.append(host.serialized)
        assert_cleaned(host, tmp_path)
    assert serialized[0] == serialized[1] == serialized[2] == observations[0]


def test_locked_video_render_does_not_call_capture_observer(capture_host, tmp_path, monkeypatch):
    host = capture_host
    output = tmp_path / "video.mp4"
    review = {"opaque": {"keep": [None, False, ""]}}

    def command(argv, **kwargs):
        assert host.active and not any(arg.startswith("--frames=") for arg in argv)
        props = json.loads(Path(argv[argv.index("--props") + 1]).read_text())
        assert props["review"] == review
        Path(argv[argv.index("--output") + 1]).write_bytes(b"boundary-only")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(remotion.subprocess, "run", command)
    remotion._execute_remotion_locked(
        host.timeline_path,
        host.assets_path,
        output,
        provenance_out_path=output,
        composition_id="TimelineComposition",
        min_free_gb=None,
        review=review,
        on_scene_props=lambda *a: pytest.fail("capture observer called for video"),
        **host.kwargs,
    )
    assert output.read_bytes() == b"boundary-only"
    assert not (tmp_path / ".remotion-props.json").exists()
    assert_cleaned(host, tmp_path)


@pytest.mark.parametrize("failure", ["callback", "capture", "missing-frame"])
def test_callback_and_capture_failures_preserve_cleanup(capture_host, tmp_path, failure):
    host = capture_host
    host.fail_capture = failure == "capture"
    host.omit_frame = 3 if failure == "missing-frame" else None
    seen = []
    error = RuntimeError("callback failed")

    def observe(props, frames):
        assert host.active and frames == (0, 3, 7)
        seen.append(props)
        if failure == "callback":
            raise error

    message = {
        "callback": "callback failed",
        "capture": "capture failed",
        "missing-frame": "omitted requested frames: \\[3\\]",
    }[failure]
    with pytest.raises(RuntimeError, match=message) as raised:
        capture(host, tmp_path, on_scene_props=observe, frame_session=host.session)
    assert len(seen) == 1
    if failure == "callback":
        assert raised.value is error and not host.requests and not host.commands
    else:
        assert len(host.requests) == 1
    assert_cleaned(host, tmp_path)


@pytest.mark.parametrize("failure", [None, RuntimeError("query failed"), KeyboardInterrupt()])
def test_read_only_root_context_matches_capture_without_launch(capture_host, tmp_path, failure):
    host = capture_host
    capture(host, tmp_path, frame_session=host.session)
    expected = host.serialized
    host.events.clear()
    host.staged_paths.clear()
    host.requests.clear()
    for path in (tmp_path / "frames").glob("frame-*.png"):
        path.unlink()
    marker = tmp_path / "frames" / ".capture-placeholder.mp4"

    def query():
        with remotion._prepared_remotion_root(
            host.timeline_path, host.assets_path, marker,
            provenance_out_path=marker, composition_id="TimelineComposition",
            min_free_gb=None, frame_numbers=(0, 3, 7),
            frame_output_dir=tmp_path / "frames", **host.kwargs,
        ) as prepared:
            props, props_path = prepared[:2]
            assert host.active and host.public_root.is_dir()
            assert all(path.is_file() for path in host.staged_paths)
            assert json.dumps(props).encode() == props_path.read_bytes() == expected
            assert host.events == ["server-enter", "stage"]
            assert not host.commands and not host.requests
            assert not list((tmp_path / "frames").glob("frame-*.png"))
            if failure is not None:
                raise failure

    if failure is None:
        query()
    else:
        with pytest.raises(type(failure)) as raised:
            query()
        assert raised.value is failure
    assert host.events == ["server-enter", "stage", "server-exit"]
    assert_cleaned(host, tmp_path)


def _occurrence_timeline(host, *occurrences):
    host.raw["app"]["astrid_shot_composition"] = {"occurrences": list(occurrences)}
    host.timeline_path.write_text(json.dumps(host.raw))


@pytest.mark.parametrize("persistent", [False, True])
def test_frame_capture_composition_reaches_occurrence_only_extent(
    capture_host, tmp_path, persistent
):
    """A frame inside an occurrence window with no clips must be in range."""
    host = capture_host
    # The only clip ends at 3.75 s; the last placed occurrence ends at 140 s.
    _occurrence_timeline(
        host,
        {"occurrence_id": "occ-00", "at": 0, "hold": 2},
        {"occurrence_id": "occ-19", "at": 138, "hold": 2},
    )
    seen = []

    def observe(props, frames):
        seen.append(props.get("astridCompositionDurationInFrames"))

    capture(
        host,
        tmp_path,
        on_scene_props=observe,
        frame_session=host.session if persistent else None,
    )
    fps = 2997 / 100
    assert seen == [int(140 * fps + 0.5)]
    assert seen[0] > int(3.75 * fps + 0.5)


def test_frame_capture_keeps_clip_composition_when_clips_cover_occurrences(
    capture_host, tmp_path
):
    host = capture_host
    _occurrence_timeline(host, {"occurrence_id": "occ-00", "at": 1.25, "hold": 1})
    seen = []
    capture(
        host,
        tmp_path,
        on_scene_props=lambda props, frames: seen.append("astridCompositionDurationInFrames" in props),
    )
    assert seen == [False]
