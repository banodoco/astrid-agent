"""Original composition bounds survive native layer isolation, without rendering."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    FrameWindow,
    RenderProfile,
    RenderRequest,
)
from astrid.core.rendering.service import RenderService
from astrid.core.timeline.duration import timeline_render_duration_frames
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion

EXTENT_KEY = "astrid_composition_duration_frames"


def _profile(pair=(30, 1)):
    return RenderProfile(320, 180, pair, (1, 90000), "h264", "yuv420p")


def _adapt(tmp_path, *, shape="empty", hold=10, pair=(30, 1), end=180,
           native=True, layer=True, metadata=None):
    raw = {
        "theme": "banodoco-default",
        "tracks": [{"id": key, "kind": "visual", "label": key} for key in ("bottom", "top")],
        "clips": [{"id": "plate", "track": "bottom", "asset": "plate", "at": 0, "hold": hold, "from": 0, "to": hold}],
        "metadata": {"duration_seconds": 999, "opaque": {"keep": [None, False]}},
    }
    if shape == "sparse":
        raw["clips"].append({"id": "early", "track": "top", "at": 0.016682, "hold": 1})
    if shape == "source":
        raw["tracks"].append({"id": "reference", "kind": "visual", "label": "Reference", "role": "source"})
        raw["clips"].append({"id": "reference", "track": "reference", "asset": "reference", "at": 0, "hold": 40})
    path = tmp_path / "timeline.json"
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    window = FrameWindow(150, end, pair)
    request = RenderRequest(SCHEMA_VERSION, str(path), "out.mp4", window=window,
                            metadata=metadata or {})
    adapted, hashes = RenderService._segment_request(
        object.__new__(RenderService), request,
        candidate=SimpleNamespace(id="fixture", manifest=SimpleNamespace(capabilities={"supports_windows": native})),
        segment=SimpleNamespace(window=window, layer=SimpleNamespace(tracks=("top",), z=1) if layer else None),
        index=0, workspace=tmp_path,
    )
    assert path.read_bytes() == before
    return request, adapted, remotion._serialize_timeline(Path(adapted.timeline_path)), hashes


@pytest.mark.parametrize("pair", [(30, 1), (30000, 1001)])
@pytest.mark.parametrize("shape", ["empty", "sparse", "source"])
@pytest.mark.parametrize("total", [300, 600])
def test_native_layer_keeps_original_extent_and_absolute_window(tmp_path, pair, shape, total):
    hold = total * pair[1] / pair[0]
    original, adapted, selected, hashes = _adapt(tmp_path, pair=pair, shape=shape, hold=hold,
                                                metadata={EXTENT_KEY: "999999", "review": "{}"})
    assert adapted.metadata == {EXTENT_KEY: str(total), "review": "{}"}
    assert adapted.window is original.window
    assert "materialized_timeline" in hashes
    assert selected["metadata"] == {
        "duration_seconds": 999, "opaque": {"keep": [None, False]}, "astrid_layer": {"z": 1, "alpha": True},
    }
    raw = json.loads(Path(original.timeline_path).read_text())
    materialized = json.loads(Path(adapted.timeline_path).read_text())
    assert materialized["clips"] == [clip for clip in raw["clips"] if clip["track"] == "top"]
    if shape == "sparse":
        # CURRENT's serializer still rounds placement; precision is a separate held unit.
        assert selected["clips"] == [{**raw["clips"][1], "at": 0.017}]
    else:
        assert selected["clips"] == []
    assert timeline_render_duration_frames(selected, pair[0] / pair[1]) < 150
    total = remotion._composition_duration_from_request(adapted)
    assert remotion._native_window_argument(adapted.window, selected, _profile(pair),
                                            composition_duration_frames=total) == "--frames=150-179"


def test_identical_empty_layers_retain_distinct_original_extents(tmp_path):
    _, first, selected_first, _ = _adapt(tmp_path, hold=10)
    _, second, selected_second, _ = _adapt(tmp_path, hold=20)
    assert selected_first == selected_second
    assert [first.metadata[EXTENT_KEY], second.metadata[EXTENT_KEY]] == ["300", "600"]


@pytest.mark.parametrize("z", [0, 1])
def test_threejs_layer_retains_same_local_slice_and_pinned_scene(tmp_path, z):
    from astrid.packs.rendering.rendering.renderers.threejs import run as threejs
    from tests.packs.rendering.test_live_scene_boundary import _package
    from tests.packs.rendering.test_live_scene_composition import _composition, _request

    raw = _composition(_package())
    raw["clips"][0]["hold"] = 20
    path = tmp_path / "scene.json"
    path.write_text(json.dumps(raw))
    before = path.read_bytes()
    request = _request(path)
    adapted, _ = RenderService._segment_request(
        object.__new__(RenderService), request,
        candidate=SimpleNamespace(manifest=SimpleNamespace(capabilities={"supports_windows": True})),
        segment=SimpleNamespace(window=request.window, layer=SimpleNamespace(tracks=("scene",), z=z)),
        index=0, workspace=tmp_path,
    )
    selected = json.loads(Path(adapted.timeline_path).read_text())
    assert selected["clips"] == [raw["clips"][0]]
    assert adapted.window is request.window
    expected = RenderService._window_timeline(raw, request.window, tracks=("scene",))
    expected["metadata"]["astrid_layer"] = {"z": z, "alpha": z > 0}
    actual = threejs._window_timeline(selected, adapted)
    assert actual == expected
    assert threejs._support_reasons(actual) == []
    clip = actual["clips"][0]
    assert (clip["at"], clip["from"], clip["to"], clip["speed"], clip["hold"]) == (0, 63, 65, 2, 2)
    assert clip["app"]["liveScene"] == raw["clips"][0]["app"]["liveScene"]
    assert path.read_bytes() == before


def test_authored_extent_metadata_does_not_enlarge_direct_bounds(tmp_path):
    request, _, raw, _ = _adapt(tmp_path, layer=False)
    raw["metadata"][EXTENT_KEY] = "999999"
    with pytest.raises(ValueError, match="beyond the full composition"):
        remotion._native_window_argument(FrameWindow(150, 301, (30, 1)), raw, _profile())
    assert EXTENT_KEY not in request.metadata


@pytest.mark.parametrize("shape", ["empty", "source"])
def test_caller_cannot_enlarge_original_layer_bounds(tmp_path, shape):
    with pytest.raises(ValueError, match="beyond the full composition"):
        _adapt(tmp_path, shape=shape, end=301, metadata={EXTENT_KEY: "999999"})


@pytest.mark.parametrize("native,layer", [(True, False), (False, False), (False, True)])
def test_reserved_caller_extent_stripped_outside_native_layers(tmp_path, native, layer):
    _, adapted, _, _ = _adapt(tmp_path, native=native, layer=layer,
                              metadata={EXTENT_KEY: "999999", "review": "{}"})
    assert adapted.metadata == {"review": "{}"}
    assert (adapted.window is not None) == native


@pytest.mark.parametrize("raw", [None, True, 300, 300.0, "", "0", "-1", "1.5", "NaN", "Infinity", "true", " 300", "+300", "0300", "3e2", "9007199254740992", "9" * 5000])
def test_reserved_extent_rejects_malformed_request_values(tmp_path, raw):
    request = SimpleNamespace(metadata={EXTENT_KEY: raw})
    with pytest.raises(ValueError, match="positive safe integer"):
        remotion._composition_duration_from_request(request)


def _support_setup(monkeypatch, profile):
    monkeypatch.setattr(remotion, "_canonical_profile", lambda *a: profile)
    monkeypatch.setattr(remotion, "_settings_from_request", lambda *a: SimpleNamespace(
        project_dir=Path("/unused"), composition_id="TimelineComposition", theme_path=None, min_free_gb=None))
    monkeypatch.setattr(remotion, "_validate_project_dir", lambda *a: object())
    monkeypatch.setattr(remotion, "_resolve_timeline_element_references", lambda *a, **k: None)


def test_support_and_protocol_forward_service_extent(tmp_path, monkeypatch):
    _, adapted, _, _ = _adapt(tmp_path)
    _support_setup(monkeypatch, _profile())
    report = remotion.support(adapted, workspace=tmp_path)
    assert report.supported, report.reasons

    class ExecutionObserved(Exception):
        pass

    def execute(*args, **kwargs):
        assert kwargs["composition_duration_frames"] == 300
        assert kwargs["window"] is adapted.window
        raise ExecutionObserved()

    monkeypatch.setattr(remotion, "_execute_remotion", execute)
    with pytest.raises(ExecutionObserved):
        remotion._protocol_render(adapted, workspace=tmp_path)


@pytest.mark.parametrize("fault", ["overlong", "short", "fps", "speed", "source_range", "full", "unlayered", "legacy", "tail", "malformed"])
def test_support_and_locked_execution_reject_same_extent_conflicts(tmp_path, monkeypatch, fault):
    _, request, selected, _ = _adapt(tmp_path, shape="sparse")
    total = 300
    if fault == "overlong":
        request = replace(request, window=FrameWindow(150, 301, (30, 1)))
    elif fault == "short":
        total = 1
    elif fault == "fps":
        request = replace(request, window=FrameWindow(150, 180, (30000, 1001)))
    elif fault == "speed":
        request = replace(request, window=replace(request.window, speed=2))
    elif fault == "source_range":
        request = replace(request, window=replace(request.window, source_range=(150, 180)))
    elif fault == "full":
        request = replace(request, window=None)
    elif fault == "unlayered":
        selected["metadata"].pop("astrid_layer")
    elif fault == "legacy":
        selected["metadata"].update(source_window_start_seconds=5, duration_seconds=1)
    elif fault == "tail":
        # A real tail clock remains a separate policy; the scalar must not replace it.
        authored = timeline_render_duration_frames(selected, 30)
        selected["app"] = {"astrid_render_clock": {
            "authored_duration_frames": authored, "render_duration_frames": 300,
            "tail": {"policy": "unmapped_excess_rendered_region", "source_asset": "plate",
                     "start_frame": authored, "end_frame": 300,
                     "source_start_frame": authored, "source_end_frame": 300},
        }}
        assert timeline_render_duration_frames(selected, 30) == 300
        assert remotion._native_window_argument(request.window, selected, _profile()) == "--frames=150-179"
    elif fault == "malformed":
        total = True
    Path(request.timeline_path).write_text(json.dumps(selected))
    request = replace(request, metadata={EXTENT_KEY: str(total)})
    (tmp_path / "assets").write_text('{"assets": {}}')
    _support_setup(monkeypatch, _profile())
    report = remotion.support(request, workspace=tmp_path)
    assert not report.supported
    monkeypatch.setattr(remotion, "_validate_project_dir", lambda *a: pytest.fail("allocated before extent refusal"))
    with pytest.raises(ValueError) as error:
        remotion._execute_remotion_locked(
            Path(request.timeline_path), tmp_path / "assets", tmp_path / "out.mp4",
            provenance_out_path=tmp_path / "out.mp4", project_dir=tmp_path,
            composition_id="TimelineComposition", theme_path=None, min_free_gb=None,
            window=request.window, composition_duration_frames=total,
        )
    assert str(error.value) in report.reasons


def test_direct_unlayered_metadata_cannot_extend_bounds(tmp_path, monkeypatch):
    _, adapted, _, _ = _adapt(tmp_path, layer=False)
    _support_setup(monkeypatch, _profile())
    forged = replace(adapted, window=FrameWindow(150, 301, (30, 1)), metadata={EXTENT_KEY: "999999"})
    assert not remotion.support(forged, workspace=tmp_path).supported


@pytest.mark.parametrize("extent", [True, 1.5, float("inf"), 0, -1, 9007199254740992])
def test_direct_executor_extent_requires_positive_safe_integer(tmp_path, extent):
    selected = {"clips": [], "metadata": {"astrid_layer": {"z": 1, "alpha": True}}}
    with pytest.raises(ValueError, match="positive safe integer"):
        remotion._native_window_argument(FrameWindow(150, 180, (30, 1)), selected, _profile(),
                                          composition_duration_frames=extent)


@pytest.mark.parametrize("layer", [None, {}, {"z": True, "alpha": True},
                                  {"z": -1, "alpha": False}, {"z": 1, "alpha": False},
                                  {"z": 0, "alpha": True}, {"z": 1, "alpha": 1}])
def test_extent_requires_consistent_native_layer_stamp(layer):
    selected = {"clips": [], "metadata": {"astrid_layer": layer}}
    with pytest.raises(ValueError, match="native isolated layer window"):
        remotion._native_window_argument(FrameWindow(150, 180, (30, 1)), selected, _profile(),
                                         composition_duration_frames=300)


@pytest.mark.parametrize("direct_output", [False, True])
def test_both_execution_staging_paths_forward_extent(tmp_path, monkeypatch, direct_output):
    from contextlib import nullcontext

    monkeypatch.setattr(remotion.remotion_lock, "remotion_render_lock", nullcontext)
    final = tmp_path / "out.mp4"

    def execute(*args, **kwargs):
        assert kwargs["composition_duration_frames"] == 300
        args[2].write_bytes(b"test consumption only")
        return "observed"

    monkeypatch.setattr(remotion, "_execute_remotion_locked", execute)
    assert remotion._execute_remotion(
        tmp_path / "timeline", tmp_path / "assets", final if direct_output else tmp_path / "stage.mp4",
        provenance_out_path=final, project_dir=tmp_path, composition_id="TimelineComposition",
        theme_path=None, min_free_gb=None, composition_duration_frames=300,
    ) == "observed"


@pytest.mark.parametrize("pair", [(30, 1), (30000, 1001)])
@pytest.mark.parametrize("shape", ["empty", "sparse"])
@pytest.mark.parametrize("z", [0, 1])
def test_locked_normal_props_keep_full_extent_and_original_layer(tmp_path, monkeypatch, pair, shape, z):
    _, request, selected, _ = _adapt(tmp_path, pair=pair, shape=shape)
    selected["metadata"]["astrid_layer"] = {"z": z, "alpha": z > 0}
    Path(request.timeline_path).write_text(json.dumps(selected))
    assets = tmp_path / "assets.json"
    assets.write_text('{"assets": {}}')
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / ("out.mov" if z else "out.mp4")
    runtime = SimpleNamespace(node_executable=Path("/unused/node"), remotion_cli=Path("/unused/cli"), node_version="v20.19.4")
    monkeypatch.setattr(remotion, "_validate_project_dir", lambda *a: runtime)
    monkeypatch.setattr(remotion, "_regenerate_element_registries", lambda *a: None)
    monkeypatch.setattr(remotion, "_validate_renderer_effect_registry", lambda *a: None)
    monkeypatch.setattr(remotion, "_effective_registry_state", lambda *a: {"hash": "fixture"})
    monkeypatch.setattr(remotion, "_canonical_profile", lambda *a: _profile(pair))
    monkeypatch.setattr(remotion, "_resolved_theme_for_render", lambda *a: {
        "id": "banodoco-default", "visual": {"canvas": {"width": 320, "height": 180, "fps": pair[0] / pair[1]}},
    })
    monkeypatch.setattr(remotion, "_stage_effect_assets_for_timeline", lambda *a, **k: {"effects": []})
    monkeypatch.setattr(remotion, "_available_remotion_port", lambda: 32101)
    monkeypatch.setattr(remotion, "InvocationAssetServer", lambda *a, **k: pytest.fail("unexpected socket"))
    monkeypatch.setenv("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES", "0")
    monkeypatch.delenv("ASTRID_PROGRESS_PATH", raising=False)

    class CommandObserved(Exception):
        pass

    def command(argv, **kwargs):
        props = json.loads(Path(argv[argv.index("--props") + 1]).read_text())
        assert props["astridCompositionDurationInFrames"] == 300
        assert props["timeline"] == selected
        assert props["theme"]["visual"]["canvas"]["fps"] == pair[0] / pair[1]
        if z:
            assert props["theme"]["visual"]["color"]["bg"] == "transparent"
        else:
            assert "color" not in props["theme"]["visual"]
        assert [arg for arg in argv if arg.startswith("--frames=")] == ["--frames=150-179"]
        assert ("--codec=prores" in argv) == (z > 0)
        assert ("--audio-codec=pcm-16" in argv) == (z == 0)
        raise CommandObserved()

    monkeypatch.setattr(remotion.subprocess, "run", command)
    with pytest.raises(CommandObserved):
        remotion._execute_remotion_locked(
            Path(request.timeline_path), assets, output, provenance_out_path=output, project_dir=project,
            composition_id="TimelineComposition", theme_path=None, min_free_gb=None, window=request.window,
            composition_duration_frames=remotion._composition_duration_from_request(request), encode_aac_at_stitch=True,
        )
    assert not (tmp_path / ".remotion-props.json").exists()
    assert not list(tmp_path.glob(".remotion-runtime-*"))
    assert not list(tmp_path.glob("astrid-render-assets-*"))
