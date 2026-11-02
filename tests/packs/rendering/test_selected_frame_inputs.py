"""Frame cache identities consume the same selected elements as the renderer."""
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from astrid.core.element.registry import clear_default_registry_cache, load_default_registry
from astrid.core.pack import loader
from astrid.packs.rendering.actions.timeline_visualize import composed_frame as capture


def write_pack(collection, owner, *, element_id="sample", enabled=True):
    pack = collection / owner
    element = pack / "elements/effects" / element_id
    element.mkdir(parents=True)
    (element / "component.tsx").write_text("export const value = 1;\n")
    (element / "geometry.json").write_text('{"safeArea":0.9}\n')
    (element / "helper.ts").write_text("export const help = 1;\n")
    (element / "element.yaml").write_text(json.dumps({
        "id": element_id, "kind": "effect", "runtime": {"adapter": "remotion"},
        "assets": {"geometry": "geometry.json"},
    }))
    prefix = f"elements/effects/{element_id}"
    (pack / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": owner, "name": owner, "version": "1.0.0",
        "status": "active", "documentation": {"kind": "none", "reason": "Test fixture."},
        "rendering": {} if not enabled else {f"effects/{element_id}": {
            "type": "element", "path": f"{prefix}/element.yaml", "resources": [
                {"path": f"{prefix}/component.tsx", "kind": "implementation"},
                {"path": f"{prefix}/helper.ts", "kind": "support"},
                {"path": f"{prefix}/geometry.json", "kind": "asset"},
            ],
        }},
    }))
    clear_default_registry_cache()
    return pack, element


def config_for(element_id, **extra):
    return {"clips": [{"id": "clip", "clipType": element_id, "at": 0, "hold": 1,
                       "track": "v", **extra}]}


@pytest.fixture(autouse=True)
def registry_cache():
    clear_default_registry_cache()
    yield
    clear_default_registry_cache()


@pytest.mark.parametrize("relative", ["component.tsx", "helper.ts", "geometry.json", "element.yaml"])
def test_selected_edit_forces_second_capture_but_unselected_payloads_are_never_inspected(
    tmp_path, monkeypatch, relative,
):
    checkout = tmp_path / "installed"
    packs = checkout / "astrid/packs"
    monkeypatch.setattr(loader, "packs_root", lambda: packs)
    _, selected = write_pack(packs, "motion")
    unused_pack, _ = write_pack(packs, "unused", element_id="unused")
    disabled_pack, _ = write_pack(packs, "disabled", element_id="disabled", enabled=False)
    env_collection = tmp_path / "env-packs"
    env_pack, _ = write_pack(env_collection, "unselected-env", element_id="env-unused")
    foreign = tmp_path / "foreign-staged"
    foreign.mkdir()
    (foreign / "pack.yaml").write_text("must never open")
    (selected / "undeclared.bin").write_text("unselected even inside the selected element")
    monkeypatch.setattr(capture, "_checkout_root", lambda: checkout)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(env_collection))
    monkeypatch.setenv("ASTRID_RENDER_PACK_ROOTS", json.dumps([{"root": str(foreign)}]))
    # Real discovery admits the fixture; access guards below audit cache acquisition,
    # not metadata enumeration owned by the shared discovery implementation.
    admitted = load_default_registry(project_root=checkout)
    assert admitted.get("effects", "sample").root == selected
    denied = (unused_pack, disabled_pack, env_pack, foreign, selected / "undeclared.bin")
    reads, walks = [], []
    real_open, real_stat, real_lstat = Path.open, Path.stat, Path.lstat
    real_iterdir, real_rglob, real_walk = Path.iterdir, Path.rglob, os.walk

    def check(path):
        path = Path(path)
        assert not any(path == root or path.is_relative_to(root) for root in denied), path

    def opening(path, *args, **kwargs):
        check(path)
        reads.append(path)
        return real_open(path, *args, **kwargs)

    def stating(path, *args, **kwargs):
        check(path)
        return real_stat(path, *args, **kwargs)

    def lstating(path, *args, **kwargs):
        check(path)
        return real_lstat(path, *args, **kwargs)

    def listing(path):
        check(path)
        assert path != packs, "cache must not enumerate the pack collection"
        walks.append(path)
        return real_iterdir(path)

    def globbing(path, *args, **kwargs):
        check(path)
        assert path != packs and not path.is_relative_to(packs), path
        return real_rglob(path, *args, **kwargs)

    def walking(path, *args, **kwargs):
        check(path)
        assert not Path(path).is_relative_to(packs), path
        return real_walk(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opening)
    monkeypatch.setattr(Path, "stat", stating)
    monkeypatch.setattr(Path, "lstat", lstating)
    monkeypatch.setattr(Path, "iterdir", listing)
    monkeypatch.setattr(Path, "rglob", globbing)
    monkeypatch.setattr(os, "walk", walking)
    calls = []

    def native_edge(frames, output_dir, resolution):
        calls.append(tuple(frames))
        result = {}
        for frame in frames:
            path = output_dir / f"{frame}.png"
            Image.new("RGB", (32, 18), "red").save(path)
            result[frame] = path
        return result

    snapshot = {"config": config_for("sample"), "clips": [], "metadata": {}}
    provider = capture.RemotionFrameProvider(
        snapshot, timeline_path=tmp_path / "absent.timeline", assets_path=tmp_path / "assets",
        project_dir=tmp_path, renderer=native_edge, cache=capture.FrameCaptureCache(tmp_path / "cache"),
    )
    cards = [{"frame": 0, "image": "frames/0.png"}]
    assert provider.capture(cards, tmp_path / "first")["evidence_source"] == "fresh_capture"
    # Mutate denied bytes through the saved filesystem edge. The cache must
    # neither acquire those bytes nor lose the selected composition's entry.
    for path in (unused_pack / "elements/effects/unused/geometry.json",
                 env_pack / "elements/effects/env-unused/geometry.json",
                 disabled_pack / "elements/effects/disabled/geometry.json",
                 foreign / "pack.yaml", selected / "undeclared.bin"):
        with real_open(path, "ab") as stream:
            stream.write(b" ")
    assert provider.capture(cards, tmp_path / "warm")["evidence_source"] == "cache"
    target = selected / relative
    before = target.stat()
    data = target.read_bytes()
    # Restore mtime; component/helper edits also retain their original size.
    target.write_bytes(data.replace(b"1", b"2", 1) if b"1" in data else data + b" ")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert provider.capture(cards, tmp_path / "changed")["evidence_source"] == "fresh_capture"
    assert calls == [(0,), (0,)]
    assert selected / "component.tsx" in reads and selected / "geometry.json" in reads
    provider.close()


def test_missing_selected_element_refuses_before_cache_or_native_edge(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "_checkout_root", lambda: tmp_path)
    with pytest.raises(ValueError, match="unregistered effect clip type.*absent-motion"):
        capture.frame_cache_key({"config": config_for("absent-motion")}, 0, None)


def test_empty_generic_composition_never_discovers_or_walks_pack_roots(tmp_path, monkeypatch):
    packs = tmp_path / "astrid/packs"
    packs.mkdir(parents=True)
    monkeypatch.setattr(capture, "_checkout_root", lambda: tmp_path)
    real_iterdir = Path.iterdir

    def guarded(path):
        assert path != packs
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", guarded)
    assert capture.frame_cache_key({"clips": []}, 0, None)
