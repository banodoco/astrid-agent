from pathlib import Path

import pytest

from PIL import Image

from astrid.packs.rendering.actions.timeline_visualize.composed_frame import (
    FrameCaptureCache,
    FrameCaptureWorker,
    RemotionFrameProvider,
    frame_cache_key,
)
from astrid.packs.rendering.rendering.renderers.remotion.run import _resize_frame_outputs
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import (
    build_filmstrip_pack,
    plan_filmstrip,
)
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_options import filmstrip_options


def _snapshot():
    return {
        "project_slug": "demo",
        "timeline_id": "timeline-1",
        "render_run_id": "capture-1",
        "video_digest": "sha256:" + "a" * 64,
        "fps_rational": [24, 1],
        "duration_frames": 48,
        "clips": [],
        "scripts": [],
        "metadata": {"capture_identity": "sha256:revision-a"},
    }


def test_exact_frame_planning_reports_one_frame_and_rejects_out_of_range():
    options = filmstrip_options({"frame": 7, "resolution": "320x180"})
    index = plan_filmstrip(_snapshot(), options)
    assert [card["frame"] for card in index["cards"]] == [7]
    assert index["cards"][0]["image"].endswith(".jpg")
    assert index["sampling"]["requested_frame"] == 7
    assert options["request"]["frame"] == 7

    try:
        plan_filmstrip(_snapshot(), {"frame": 48})
    except ValueError as exc:
        assert "outside" in str(exc)
    else:
        raise AssertionError("out-of-range frame was accepted")


def test_worker_is_serialized_and_holds_its_renderer_until_closed(tmp_path):
    calls = []

    def render(frames, output_dir, resolution):
        calls.append((tuple(frames), tuple(resolution or ())))
        output = Path(output_dir)
        result = {}
        for frame in frames:
            path = output / f"frame-{frame:06d}.png"
            Image.new("RGB", (32, 18), (frame, 0, 0)).save(path)
            result[frame] = path
        return result

    worker = FrameCaptureWorker(render)
    output = tmp_path / "frames"
    assert worker.capture([2, 1], output, [32, 18])
    assert calls == [((1, 2), (32, 18))]
    # The worker keeps no idle timer: the host's warm hold idles out the owner.
    assert worker.started is True
    worker.close()
    assert worker.started is False
    with pytest.raises(RuntimeError, match="closed"):
        worker.capture([3], output, [32, 18])


def test_worker_closes_after_renderer_failure(tmp_path):
    def render(_frames, _output_dir, _resolution):
        raise RuntimeError("renderer failed")

    worker = FrameCaptureWorker(render)
    try:
        worker.capture([1], tmp_path / "frames", [32, 18])
    except RuntimeError as exc:
        assert str(exc) == "renderer failed"
    else:
        raise AssertionError("renderer failure was swallowed")
    assert worker.started is False
    assert worker._closed is True


def test_frame_outputs_are_resized_after_native_render(tmp_path):
    output = tmp_path / "frame-000000.png"
    Image.new("RGB", (1920, 1080), (12, 34, 56)).save(output)

    _resize_frame_outputs(tmp_path, (320, 180))

    with Image.open(output) as image:
        assert image.size == (320, 180)


def test_provider_uses_cache_on_second_request(tmp_path):
    calls = []

    def render(frames, output_dir, resolution):
        calls.append(tuple(frames))
        result = {}
        for frame in frames:
            path = Path(output_dir) / f"frame-{frame:06d}.png"
            Image.new("RGB", (32, 18), (frame, 0, 0)).save(path)
            result[frame] = path
        return result

    snapshot = _snapshot()
    cache = FrameCaptureCache(tmp_path / "cache")
    provider = RemotionFrameProvider(
        snapshot,
        timeline_path=tmp_path / "timeline.json",
        assets_path=tmp_path / "assets.json",
        project_dir=tmp_path,
        cache=cache,
        renderer=render,
    )
    cards = [{"frame": 1, "image": "frames/frame-000000001.png"}]
    first = provider.capture(cards, tmp_path / "first", [32, 18])
    assert first["evidence_source"] == "fresh_capture"
    second_provider = RemotionFrameProvider(
        snapshot,
        timeline_path=tmp_path / "timeline.json",
        assets_path=tmp_path / "assets.json",
        project_dir=tmp_path,
        cache=cache,
        renderer=render,
    )
    second = second_provider.capture(cards, tmp_path / "second", [32, 18])
    assert second["evidence_source"] == "cache"
    assert calls == [(1,)]
    assert (tmp_path / "second" / cards[0]["image"]).is_file()
    assert frame_cache_key(snapshot, 1, [32, 18]) != frame_cache_key(snapshot, 2, [32, 18])
    assert provider.worker.started is True
    provider.close()
    assert provider.worker.started is False


def test_cache_identity_changes_when_renderer_source_changes(tmp_path, monkeypatch):
    source_root = tmp_path / "renderer-source"
    source_root.mkdir()
    source_file = source_root / "effect.tsx"
    source_file.write_text("export const value = 1;", encoding="utf-8")
    monkeypatch.setenv("ASTRID_RENDERER_SOURCE_ROOT", str(source_root))
    first = frame_cache_key(_snapshot(), 1, [32, 18])
    source_file.write_text("export const value = 2;", encoding="utf-8")
    second = frame_cache_key(_snapshot(), 1, [32, 18])
    assert first != second


@pytest.mark.parametrize("relative", [
    "astrid/packs/rendering/rendering/renderers/remotion/run.py",
    "astrid/packs/rendering/rendering/renderers/remotion/root_preparation.py",
])
def test_cache_identity_tracks_root_preparation_source(tmp_path, monkeypatch, relative):
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame

    source = tmp_path / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# before\n")
    monkeypatch.setattr(composed_frame, "_checkout_root", lambda: tmp_path)
    first = frame_cache_key(_snapshot(), 1, [32, 18])
    source.write_text("# changed preparation\n")
    assert frame_cache_key(_snapshot(), 1, [32, 18]) != first


def test_cache_identity_ignores_ephemeral_attempt_directory_paths():
    first = _snapshot()
    second = _snapshot()
    first["metadata"]["asset_integrity"] = {
        "clip": {
            "observed_sha256": "abc",
            "path": "/tmp/astrid-attempt-first/managed-objects/0000-content",
        }
    }
    second["metadata"]["asset_integrity"] = {
        "clip": {
            "observed_sha256": "abc",
            "path": "/tmp/astrid-attempt-second/managed-objects/0000-content",
        }
    }
    assert frame_cache_key(first, 1, [32, 18]) == frame_cache_key(second, 1, [32, 18])


def test_cache_identity_changes_when_remotion_source_changes(tmp_path, monkeypatch):
    project = tmp_path / "remotion"
    source = project / "src"
    source.mkdir(parents=True)
    worker = source / "astrid-frame-worker.mjs"
    worker.write_text("const version = 1;", encoding="utf-8")
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(project))
    first = frame_cache_key(_snapshot(), 1, [32, 18])
    worker.write_text("const version = 2;", encoding="utf-8")
    second = frame_cache_key(_snapshot(), 1, [32, 18])
    assert first != second


def test_cache_identity_changes_when_remotion_config_changes(tmp_path, monkeypatch):
    project = tmp_path / "remotion"
    (project / "src").mkdir(parents=True)
    config = project / "remotion.config.ts"
    config.write_text("export default {};\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(project))
    first = frame_cache_key(_snapshot(), 1, [32, 18])
    config.write_text("export default { enableTailwind: true };\n", encoding="utf-8")
    second = frame_cache_key(_snapshot(), 1, [32, 18])
    assert first != second


def test_shared_owner_force_release_forwards_force_to_session(monkeypatch):
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame

    class Session:
        def __init__(self):
            self.calls = []

        def close(self, *, force=False):
            self.calls.append(force)

    session = Session()
    owner = object.__new__(composed_frame._SharedRemotionOwner)
    owner.session = session
    owner.lock = __import__("threading").RLock()
    owner.references = 1
    owner.closed = False
    owner.release(force=True)
    assert session.calls == [True]


def test_shared_owner_leaves_idle_release_to_the_host(monkeypatch):
    from astrid.packs.rendering.actions.timeline_visualize import composed_frame

    class Session:
        calls = []

        def close(self, *, force=False):
            self.calls.append(force)

    monkeypatch.setattr("astrid.packs.rendering.rendering.renderers.remotion.run.PersistentRemotionFrameSession", Session)
    owner = composed_frame._SharedRemotionOwner()
    handle = owner.acquire({})
    handle.close()
    # An ordinary last release never closes the host's frame owner from inside the task.
    assert Session.calls == [] and not owner.closed


def test_filmstrip_pack_accepts_capture_provider_without_video(tmp_path):
    snapshot = _snapshot()
    snapshot["duration_frames"] = 3
    options = filmstrip_options({"frame": 1, "resolution": "32x18"})
    options["frame_extension"] = "png"

    class Provider:
        def capture(self, cards, out_root, resolution):
            for card in cards:
                path = Path(out_root) / card["image"]
                path.parent.mkdir(parents=True, exist_ok=True)
                Image.new("RGB", (32, 18), "#204050").save(path)
            return {"evidence_source": "fresh_capture", "fresh_frames": len(cards), "cached_frames": 0}

    result = build_filmstrip_pack(
        out_root=tmp_path / "filmstrip-view",
        snapshot=snapshot,
        options=options,
        frame_provider=Provider(),
    )
    assert result["frame_index"]["frame_capture"]["evidence_source"] == "fresh_capture"
    assert result["cards"][0]["image"].endswith(".png")
    assert Path(result["paths"]["png"][0]).is_file()


@pytest.mark.parametrize("boundary", ["worker", "provider"])
def test_native_render_primary_survives_secondary_cleanup_failure(tmp_path, boundary):
    primary = ValueError("original native render connection failed")
    cleanup = RuntimeError("original owner release remains uncertain")
    class Renderer:
        calls = 0
        def __call__(self, *_args):
            self.calls += 1
            raise primary
        def close(self, *, force=False):
            assert force is True
            raise cleanup
    renderer = Renderer()
    if boundary == "worker":
        worker = FrameCaptureWorker(renderer)
        invoke = lambda: worker.capture([1], tmp_path / "frames", [32, 18])
    else:
        provider = RemotionFrameProvider(
            _snapshot(), timeline_path=tmp_path / "timeline.json",
            assets_path=tmp_path / "assets.json", project_dir=tmp_path,
            cache=FrameCaptureCache(tmp_path / "cache"), renderer=renderer,
        )
        worker = provider.worker
        invoke = lambda: provider.capture(
            [{"frame": 1, "image": "frames/frame-000000001.png"}],
            tmp_path / "out", [32, 18],
        )
    with pytest.raises(ValueError) as caught:
        invoke()
    assert caught.value is primary and caught.value.__cause__ is cleanup
    assert renderer.calls == 1 and worker._closed and not worker.started
    assert not list(tmp_path.glob("astrid-frame-capture-*"))


@pytest.mark.parametrize("relative", [
    "node_modules/@banodoco/timeline-composition/dist/index.js",
    "src/effects.generated.ts",
])
def test_cache_identity_tracks_built_package_and_host_registry(tmp_path, monkeypatch, relative):
    project = tmp_path / "remotion"
    source = project / relative
    source.parent.mkdir(parents=True)
    source.write_text("export const version = 1;\n")
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(project))
    first = frame_cache_key(_snapshot(), 1, [32, 18])
    source.write_text("export const version = 2;\n")
    assert first != frame_cache_key(_snapshot(), 1, [32, 18])
