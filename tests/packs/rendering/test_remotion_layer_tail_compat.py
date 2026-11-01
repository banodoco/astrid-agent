"""Bounded native layer windows retain the legacy render-clock positive.

The fixture is a sparse layer
whose selected window is completely before the declared render tail.  Empty
layers and windows that merely have a gap are not represented here: those are
separate, pre-existing failures and are not evidence for this positive.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
from contextlib import nullcontext
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
from astrid.core.rendering.remotion_runtime import resolve_remotion_project_dir
from astrid.core.rendering.service import RenderService
from astrid.core.timeline.duration import (
    timeline_duration_frames,
    timeline_render_duration_frames,
)
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion

_EXTENT_KEY = "astrid_composition_duration_frames"
_ROOT = Path(__file__).resolve().parents[3]
_REMOTION_ROOT = _ROOT / "remotion"
_ROOT_TSX = _REMOTION_ROOT / "src" / "Root.tsx"
_DURATION_CHUNK = "chunk-A6LVBEHN.js"
_COMPOSITION_PACKAGE = "@banodoco/timeline-composition"

_ROOT_PROBE = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const ts = require(process.argv[4]);

const [rootPath, durationPath, propsJSON] = process.argv.slice(1, 4);

function compile(filename, modules) {
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
    fileName: filename,
  }).outputText;
  const module = {exports: {}};
  new Function('require', 'module', 'exports', code)(name => {
    assert.ok(name in modules, `Unexpected import: ${name}`);
    return modules[name];
  }, module, module.exports);
  return module.exports;
}

const duration = compile(durationPath, {});
const nil = () => null;
const Composition = () => null;
const Sequence = props => props.children;
const Video = () => null;
const modules = {
  'react/jsx-runtime': {
    Fragment: 'Fragment',
    jsx: (type, props) => ({type, props}),
    jsxs: (type, props) => ({type, props}),
  },
  remotion: {Composition, Sequence, useVideoConfig: () => ({fps: 30})},
  '@remotion/media': {Video},
  '@banodoco/timeline-composition': {
    ...duration,
    TimelineComposition: nil,
    DEFAULT_THEME: {},
  },
  './fonts': {FontProvider: nil},
  './ReviewOverlay': {ReviewOverlay: nil},
  './FrameDiagnostics': {FrameDiagnostics: nil},
  './BoundsProbe': {BoundsProbe: nil},
  './asset-source': {resolveInvocationAsset: file => file},
  './host-registries': {resolveHostRegistries: () => ({})},
};

function walk(node, visit) {
  if (Array.isArray(node)) return node.forEach(child => walk(child, visit));
  if (!node || typeof node !== 'object') return;
  visit(node);
  if (typeof node.type === 'function') walk(node.type(node.props), visit);
  else walk(node.props?.children, visit);
}

(async () => {
  const props = JSON.parse(propsJSON);
  const root = compile(rootPath, modules).Root();
  const compositions = root.props.children.filter(element => element.type === Composition);
  assert.equal(compositions.length, 2);
  const metadata = await Promise.all(
    compositions.map(element => element.props.calculateMetadata({props})),
  );
  assert.deepEqual(metadata[0], metadata[1]);
  const sequences = [];
  const videos = [];
  walk(compositions[0].props.component(props), element => {
    if (element.type === Sequence) sequences.push(element.props);
    if (element.type === Video) videos.push(element.props);
  });
  process.stdout.write(JSON.stringify({
    metadata: metadata[0],
    sequence: sequences[0] ?? null,
    video: videos[0] ?? null,
  }));
})().catch(error => {
  process.stdout.write(JSON.stringify({error: String(error && error.message || error)}));
  process.exitCode = 0;
});
"""


def _profile() -> RenderProfile:
    return RenderProfile(320, 180, (30, 1), (1, 90000), "h264", "yuv420p")


def _clock() -> dict[str, object]:
    return {
        "authored_duration_frames": 300,
        "render_duration_frames": 330,
        "tail": {
            "policy": "unmapped_excess_rendered_region",
            "source_asset": "tail",
            "start_frame": 300,
            "end_frame": 330,
            "source_start_frame": 300,
            "source_end_frame": 330,
        },
    }


def _timeline() -> dict[str, object]:
    return {
        "theme": "banodoco-default",
        "theme_overrides": {"visual": {"canvas": {"width": 320, "height": 180, "fps": 30}}},
        "tracks": [
            {"id": "bottom", "kind": "visual", "label": "Bottom"},
            {"id": "top", "kind": "visual", "label": "Top"},
        ],
        "clips": [
            {"id": "base", "track": "bottom", "asset": "base", "at": 0, "from": 0, "to": 10},
            # This clip covers the whole selected [0, 1s) window.  It is not
            # an empty layer or a gap: the bounded positive is explicit.
            {"id": "top", "track": "top", "asset": "base", "at": 0, "from": 0, "to": 1},
        ],
        "app": {"astrid_render_clock": _clock()},
    }


def _props(timeline: dict[str, object], *, extent: int | None = None) -> dict[str, object]:
    props: dict[str, object] = {
        "timeline": timeline,
        "assets": {"assets": {"base": {"file": "base.mp4"}, "tail": {"file": "tail.mp4"}}},
        "theme": {},
    }
    if extent is not None:
        props["astridCompositionDurationInFrames"] = extent
    return props


def _composition_archive() -> Path:
    package = json.loads((_REMOTION_ROOT / "package.json").read_text(encoding="utf-8"))
    dependency = package["dependencies"][_COMPOSITION_PACKAGE]
    assert isinstance(dependency, str) and dependency.startswith("file:"), dependency
    archive = (_REMOTION_ROOT / dependency.removeprefix("file:")).resolve()
    try:
        archive.relative_to(_REMOTION_ROOT.resolve())
    except ValueError as error:
        raise AssertionError(f"composition archive escapes Remotion root: {archive}") from error
    assert archive.is_file(), archive
    return archive


def _root_probe(tmp_path: Path, props: dict[str, object]) -> dict[str, object]:
    archive = _composition_archive()
    duration_path = tmp_path / _DURATION_CHUNK
    extract = subprocess.run(
        ["tar", "-xOf", str(archive), f"package/dist/{_DURATION_CHUNK}"],
        check=True,
        capture_output=True,
    )
    duration_path.write_bytes(extract.stdout)
    node = shutil.which("node")
    assert node is not None, "Root callback probe requires Node"
    typescript = (
        resolve_remotion_project_dir()
        / "node_modules"
        / "typescript"
        / "lib"
        / "typescript.js"
    )
    assert typescript.is_file(), (
        "Root callback probe requires TypeScript from the selected Remotion runtime: "
        f"{typescript}"
    )
    completed = subprocess.run(
        [
            node,
            "-e",
            _ROOT_PROBE,
            str(_ROOT_TSX),
            str(duration_path),
            json.dumps(props),
            str(typescript),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == "", completed.stderr
    return json.loads(completed.stdout)


def _segment(
    tmp_path: Path, *, start: int = 0, end: int = 30, native: bool = True,
    raw: dict[str, object] | None = None,
) -> tuple[RenderRequest, RenderRequest, dict[str, object]]:
    timeline_path = tmp_path / "timeline.json"
    if raw is None:
        raw = _timeline()
        raw["clips"][1].update(at=start / 30, to=(end - start) / 30)
    timeline_path.write_text(json.dumps(raw), encoding="utf-8")
    before = timeline_path.read_bytes()
    window = FrameWindow(start, end, (30, 1))
    request = RenderRequest(SCHEMA_VERSION, str(timeline_path), "out.mp4", window=window)
    adapted, _ = RenderService._segment_request(
        object.__new__(RenderService),
        request,
        candidate=SimpleNamespace(
            id="remotion",
            manifest=SimpleNamespace(capabilities={"supports_windows": native}),
        ),
        segment=SimpleNamespace(window=window, layer=SimpleNamespace(tracks=("top",), z=1)),
        index=0,
        workspace=tmp_path,
    )
    assert timeline_path.read_bytes() == before
    selected = remotion._serialize_timeline(Path(adapted.timeline_path))
    return request, adapted, selected


def test_fully_covered_pre_tail_layer_window_keeps_legacy_native_positive(tmp_path: Path) -> None:
    """The current filtered-native route must retain the old bounded positive.

    The legacy route is the frozen 2019d7e69 behavior: it slices this exact
    layer/window, clears the native window, and Root accepts the source-window
    metadata with a 30-frame composition.  The current route retains the
    original clock and uses native bounds, so it must accept the same request
    without treating the filtered clip extent as authored clock authority.
    """

    request, adapted, selected = _segment(tmp_path)
    raw = _timeline()
    assert timeline_duration_frames(raw, 30) == 300
    assert timeline_render_duration_frames(raw, 30) == 330
    assert timeline_duration_frames(selected, 30) == 30
    assert selected["app"]["astrid_render_clock"] == _clock()
    assert adapted.metadata[_EXTENT_KEY] == "330"
    assert adapted.window is request.window

    legacy = RenderService._window_timeline(raw, request.window, tracks=("top",))
    legacy.setdefault("metadata", {})["astrid_layer"] = {"z": 1, "alpha": True}
    assert legacy["clips"] == [
        {"id": "top_0_30", "track": "top", "asset": "base", "at": 0.0, "from": 0.0, "to": 1.0}
    ]
    assert timeline_duration_frames(legacy, 30) == 30
    assert remotion._native_window_argument(None, legacy, _profile()) is None
    legacy_tree = _root_probe(tmp_path, _props(legacy))
    assert legacy_tree["metadata"] == {
        "width": 320,
        "height": 180,
        "fps": 30,
        "durationInFrames": 30,
    }
    assert legacy_tree["sequence"]["from"] == 30
    assert legacy_tree["video"]["trimBefore"] == 300
    assert legacy_tree["video"]["trimAfter"] == 330

    native = remotion._native_window_argument(
        adapted.window,
        selected,
        _profile(),
        composition_duration_frames=remotion._composition_duration_from_request(adapted),
    )
    assert native == "--frames=0-29"


def test_fully_covered_pre_tail_root_callback_accepts_native_extent(tmp_path: Path) -> None:
    """Root's real callbacks must accept the service's original extent grant."""

    _, adapted, selected = _segment(tmp_path)
    tree = _root_probe(
        tmp_path,
        _props(selected, extent=remotion._composition_duration_from_request(adapted)),
    )
    # The selected clip covers the native request; the tail starts at the
    # exclusive end and therefore contributes no frames to [0, 30).
    assert tree.get("error") is None, tree
    assert tree["metadata"]["durationInFrames"] == 330
    assert tree["sequence"]["from"] == 30
    assert tree["video"]["trimBefore"] == 300
    assert tree["video"]["trimAfter"] == 330


def test_ordinary_full_tail_and_no_clock_controls_remain_distinct(tmp_path: Path) -> None:
    """The bounded regression does not broaden ordinary tail behavior."""

    raw = _timeline()
    ordinary = _root_probe(tmp_path, _props(raw))
    assert ordinary["metadata"]["durationInFrames"] == 330
    assert ordinary["sequence"]["from"] == 300
    assert ordinary["video"]["trimBefore"] == 300
    assert ordinary["video"]["trimAfter"] == 330

    no_clock = copy.deepcopy(raw)
    del no_clock["app"]
    no_tail = copy.deepcopy(_props(no_clock))
    no_tail["assets"] = {"assets": {"base": {"file": "base.mp4"}}}
    ordinary_no_tail = _root_probe(tmp_path, no_tail)
    assert ordinary_no_tail["metadata"]["durationInFrames"] == 300
    assert ordinary_no_tail["sequence"] is None
    assert ordinary_no_tail["video"] is None


@pytest.mark.parametrize("start,end", [(0, 30), (150, 165)])
def test_legacy_service_keeps_covered_pre_tail_output(tmp_path: Path, start: int, end: int) -> None:
    _, adapted, selected = _segment(tmp_path, start=start, end=end, native=False)
    assert adapted.window is None
    assert _EXTENT_KEY not in adapted.metadata
    assert remotion._native_window_argument(None, selected, _profile()) is None
    assert len(selected["clips"]) == 1
    clip = selected["clips"][0]
    assert (clip["at"], clip["from"], clip["to"]) == (0, 0, (end - start) / 30)
    tree = _root_probe(tmp_path, _props(selected))
    assert tree["metadata"]["durationInFrames"] == end - start
    assert tree["sequence"]["from"] == end - start
    assert tree["video"]["trimBefore"] == 300
    assert tree["video"]["trimAfter"] == 330


def test_middle_pre_tail_window_keeps_absolute_clips_and_tail_outside_output(tmp_path: Path) -> None:
    request, adapted, selected = _segment(tmp_path, start=150, end=165)
    assert adapted.window is request.window
    assert adapted.metadata[_EXTENT_KEY] == "330"
    assert selected["clips"] == [
        {"id": "top", "track": "top", "asset": "base", "at": 5, "from": 0, "to": 0.5},
    ]
    assert selected["app"]["astrid_render_clock"] == _clock()
    assert remotion._native_window_argument(
        adapted.window, selected, _profile(),
        composition_duration_frames=remotion._composition_duration_from_request(adapted),
    ) == "--frames=150-164"
    tree = _root_probe(tmp_path, _props(selected, extent=330))
    assert tree["metadata"]["durationInFrames"] == 330
    assert tree["sequence"]["from"] == 165
    assert tree["video"]["trimBefore"] == 300
    assert tree["video"]["trimAfter"] == 330


@pytest.mark.parametrize("fault", ["authored", "rendered", "tail", "source", "policy", "malformed"])
def test_original_clock_is_validated_before_layer_filter(tmp_path: Path, fault: str) -> None:
    raw = _timeline()
    clock = raw["app"]["astrid_render_clock"]
    if fault == "authored":
        clock["authored_duration_frames"] = 30
    elif fault == "rendered":
        clock["render_duration_frames"] = 300
    elif fault == "tail":
        clock["tail"]["start_frame"] = 30
    elif fault == "source":
        clock["tail"]["source_end_frame"] = 331
    elif fault == "policy":
        clock["tail"]["policy"] = "other"
    else:
        raw["app"]["astrid_render_clock"] = None
    with pytest.raises(ValueError, match="render clock"):
        _segment(tmp_path, raw=raw)


@pytest.mark.parametrize("fault", ["extent", "authored", "malformed", "legacy", "gap"])
def test_native_tail_extent_conflicts_still_refused(tmp_path: Path, fault: str) -> None:
    _, adapted, selected = _segment(tmp_path)
    extent = 330
    if fault == "extent":
        extent = 331
    elif fault == "authored":
        selected["app"]["astrid_render_clock"]["authored_duration_frames"] = 29
    elif fault == "malformed":
        selected["app"]["astrid_render_clock"] = None
    elif fault == "legacy":
        selected["metadata"]["source_window_start_seconds"] = 0
    else:
        selected["clips"] = []
    with pytest.raises(ValueError, match="conflicts with an existing render clock"):
        remotion._native_window_argument(
            adapted.window, selected, _profile(), composition_duration_frames=extent,
        )


@pytest.mark.parametrize("start,end", [(0, 30), (150, 165), (300, 330)])
def test_unlayered_original_clock_native_windows_unchanged(tmp_path: Path, start: int, end: int) -> None:
    raw = _timeline()
    assert remotion._native_window_argument(FrameWindow(start, end, (30, 1)), raw, _profile()) == (
        f"--frames={start}-{end - 1}"
    )
    assert _root_probe(tmp_path, _props(raw))["metadata"]["durationInFrames"] == 330


@pytest.mark.parametrize("start,end", [(0, 30), (150, 165)])
def test_support_and_locked_props_keep_pre_tail_positive(tmp_path: Path, monkeypatch, start: int, end: int) -> None:
    from tests.packs.rendering.test_native_layer_extent import _support_setup

    _, request, selected = _segment(tmp_path, start=start, end=end)
    assets = tmp_path / "assets.json"
    source = tmp_path / "media.mp4"
    payload = b"source bytes for props assembly only; no decoder is invoked"
    source.write_bytes(payload)
    assets.write_text(json.dumps({"assets": {
        key: {"object_id": key, "digest": hashlib.sha256(payload).hexdigest(), "type": "video/mp4"}
        for key in ("base", "tail")
    }}))
    objects = {key: str(source) for key in ("base", "tail")}
    request = replace(request, assets_registry_path=str(assets), materialized_root=str(tmp_path),
                      materialized_objects=objects)
    _support_setup(monkeypatch, _profile())
    report = remotion.support(request, workspace=tmp_path)
    assert report.supported, report.reasons
    project = tmp_path / "project"
    project.mkdir()
    output = tmp_path / "out.mov"
    runtime = SimpleNamespace(
        node_executable=Path("/unused/node"), remotion_cli=Path("/unused/cli"), node_version="v20.19.4",
    )
    monkeypatch.setattr(remotion, "_validate_project_dir", lambda *a: runtime)
    monkeypatch.setattr(remotion, "_regenerate_element_registries", lambda *a: None)
    monkeypatch.setattr(remotion, "_validate_renderer_effect_registry", lambda *a: None)
    monkeypatch.setattr(remotion, "_effective_registry_state", lambda *a: {"hash": "fixture"})
    monkeypatch.setattr(remotion, "_resolved_theme_for_render", lambda *a: {
        "id": "banodoco-default", "visual": {"canvas": {"width": 320, "height": 180, "fps": 30}},
    })
    monkeypatch.setattr(remotion, "_stage_effect_assets_for_timeline", lambda *a, **k: {"effects": []})
    monkeypatch.setattr(remotion, "_available_remotion_port", lambda: 32101)
    # Keep real managed materialization; replace only the socket-bound URL resolver.
    monkeypatch.setattr(remotion, "InvocationAssetServer", lambda *a, **k: nullcontext(
        SimpleNamespace(local_url=lambda path: path.as_uri()),
    ))
    monkeypatch.setenv("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES", "0")
    monkeypatch.delenv("ASTRID_PROGRESS_PATH", raising=False)
    captured = {}

    class CommandObserved(Exception):
        pass

    def command(argv, **kwargs):
        captured.update(json.loads(Path(argv[argv.index("--props") + 1]).read_text()))
        assert [arg for arg in argv if arg.startswith("--frames=")] == [f"--frames={start}-{end - 1}"]
        assert "--codec=prores" in argv
        raise CommandObserved()

    with monkeypatch.context() as command_patch:
        command_patch.setattr(remotion.subprocess, "run", command)
        with pytest.raises(CommandObserved):
            remotion._execute_remotion_locked(
                Path(request.timeline_path), assets, output, provenance_out_path=output,
                project_dir=project, composition_id="TimelineComposition", theme_path=None,
                min_free_gb=None, window=request.window,
                composition_duration_frames=remotion._composition_duration_from_request(request),
                materialized_root=tmp_path, materialized_objects=objects,
            )
    assert captured["astridCompositionDurationInFrames"] == 330
    assert captured["timeline"] == selected
    assert captured["timeline"]["app"]["astrid_render_clock"] == _clock()
    assert captured["theme"]["visual"]["color"]["bg"] == "transparent"
    tree = _root_probe(tmp_path, captured)
    assert tree["metadata"]["durationInFrames"] == 330
    assert tree["sequence"]["from"] == end
    assert (tree["video"]["trimBefore"], tree["video"]["trimAfter"]) == (300, 330)
    assert not (tmp_path / ".remotion-props.json").exists()
    assert not list(tmp_path.glob(".remotion-runtime-*"))
