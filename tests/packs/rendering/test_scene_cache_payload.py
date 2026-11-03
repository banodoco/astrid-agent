"""Source-bound cache reuse with a transport double; no pixel parity claim."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import zlib

import pytest
from PIL import Image

from astrid.core.rendering import scene_answers
from astrid.core.timeline import Timeline
from astrid.packs.rendering.actions.timeline_visualize import composed_frame
from astrid.packs.rendering.actions.timeline_visualize.filmstrip_cards import attach_frame_addresses
from tests.packs.rendering.test_scene_source_carry import (  # noqa: F401
    cards,
    provider,
    saved,
    source_query,
)

pytestmark = [pytest.mark.usefixtures("source_query"),
              pytest.mark.parametrize("source_query", ["transport"], indirect=True)]


@pytest.fixture
def captured(tmp_path, monkeypatch):
    monkeypatch.setattr(composed_frame, "renderer_environment_identity", lambda: "stable-renderer")
    snapshot, _ = saved(monkeypatch)
    p, calls = provider(tmp_path, monkeypatch, snapshot)
    p.capture(cards(0, 24), tmp_path / "first")
    yield p, calls
    p.close()


_DEFAULT_CACHE = object()


def another(p, *, snapshot=None, cache=_DEFAULT_CACHE):
    return composed_frame.RemotionFrameProvider(
        copy.deepcopy(p.snapshot if snapshot is None else snapshot),
        timeline_path=p.timeline_path, assets_path=p.assets_path, project_dir=p.project_dir,
        materialized_root=p.materialized_root, materialized_objects=p.materialized_objects,
        theme_path=p.theme_path, cache=p.cache if cache is _DEFAULT_CACHE else cache,
    )


def entry(p, frame=0):
    key = composed_frame.frame_cache_key(p.snapshot, frame, None)
    path = p.cache.path_for(key)
    png, chunk = composed_frame._split_scene_entry(path.read_bytes())
    return key, path, png, json.loads(chunk)


def plant(path, png, payload):
    # Rebuild a valid PNG chunk, including its CRC. Disk hashes and syntactic
    # validity alone cannot establish that these rows came from the callback.
    chunk = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    packet = (struct.pack(">I", len(chunk)) + b"asTr" + chunk
              + struct.pack(">I", zlib.crc32(b"asTr" + chunk)))
    path.write_bytes(png[:-12] + packet + png[-12:])


def test_new_provider_reuses_rows_without_query_or_private_visibility(captured, tmp_path, monkeypatch):
    p, calls = captured
    key, path, png, payload = entry(p)
    assert payload["key"] == key
    assert payload["png_sha256"] == hashlib.sha256(png).hexdigest()
    assert payload["scene"]["root_source"] == hashlib.sha256(json.dumps(
        calls[0][1], sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()
    assert png == (tmp_path / "first/frames/0.png").read_bytes()
    assert sorted(item.suffix for item in p.cache.root.iterdir()) == [".png", ".png"]
    with Image.open(path) as image:
        assert image.size == (100, 50)
        image.load()
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("cached query"))
    monkeypatch.setattr("astrid.core.timeline.onscreen.visible_at", lambda *a, **k: pytest.fail("private visibility"))
    q = another(p)
    info = q.capture(cards(0, 24), tmp_path / "second")
    assert info["cached_frames"] == 2 and info["fresh_frames"] == 0
    assert len(calls) == 1
    assert q.scene_capture is None and not q.fresh_scene_frames
    receipt = attach_frame_addresses({"cards": [{}, {}]}, q.snapshot, cards(0, 24), frame_provider=q)
    assert [row["addresses"] for row in receipt["cards"]] == [["c08.title"], ["c08.title"]]
    assert q.scene_for_frame(0).frame(0, source_identity=q.source_identity) == payload["scene"]["answer"]
    with pytest.raises(scene_answers.SceneQueryUnavailable, match="cannot mint"):
        q.cache.write(key, png, scene=q.scene_for_frame(0), frame=0)
    q.close()


def test_default_provider_keeps_png_only_cache_reusable_across_owners(tmp_path, monkeypatch):
    snapshot, _ = saved(monkeypatch)
    monkeypatch.setenv("ASTRID_TIMELINE_FRAME_CACHE", str(tmp_path / "cache"))
    p, calls = provider(tmp_path, monkeypatch, snapshot, cache=None)
    first = tmp_path / "first"
    p.capture(cards(0), first)
    key = composed_frame.frame_cache_key(p.snapshot, 0, None)
    path = p.cache.path_for(key)
    png, payload = composed_frame._split_scene_entry(path.read_bytes())
    assert payload is None
    p.close()

    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("default cache query"))
    q = another(p, cache=None)
    second = tmp_path / "second"
    info = q.capture(cards(0), second)
    assert info["cached_frames"] == 1 and info["fresh_frames"] == 0
    assert len(calls) == 1
    assert (first / "frames/0.png").read_bytes() == png == (second / "frames/0.png").read_bytes()
    assert q.scene_for_frame(0) is None
    receipt = attach_frame_addresses({"cards": [{}]}, q.snapshot, cards(0), frame_provider=q)
    assert receipt["cards"][0]["addresses"] == ["c08.title"]
    q.close()


def test_ephemeral_attempt_retry_reuses_the_same_entry(captured, tmp_path, monkeypatch):
    p, calls = captured
    first = copy.deepcopy(p.snapshot)
    first["metadata"]["asset_integrity"] = "/tmp/astrid-attempt-one/managed-objects/image"
    q = another(p, snapshot=first)
    q.capture(cards(0), tmp_path / "attempt-one")
    second = copy.deepcopy(first)
    second["metadata"]["asset_integrity"] = "/tmp/astrid-attempt-two/managed-objects/image"
    r = another(p, snapshot=second)
    monkeypatch.setattr(scene_answers, "host_frames_at", lambda *a: pytest.fail("retry query"))
    assert r.capture(cards(0), tmp_path / "attempt-two")["cached_frames"] == 1
    assert r.scene_for_frame(0) is not None and len(calls) == 2
    q.close()
    r.close()


@pytest.mark.parametrize("changed", ["revision", "candidate", "compiled_map", "profile", "assets", "source", "frame", "resolution", "renderer"])
def test_changed_capture_input_cannot_reuse_rows(captured, tmp_path, monkeypatch, changed):
    p, calls = captured
    snapshot = copy.deepcopy(p.snapshot)
    if changed == "revision":
        snapshot["metadata"]["capture_authority"]["parent_revision_id"] = "parent:new"
    elif changed == "candidate":
        snapshot["metadata"]["working_copy"] = {"candidate_digest": "sha256:" + "c" * 64}
    elif changed == "compiled_map":
        clip = snapshot["config"]["clips"][0]
        clip["app"]["astrid"]["cut"] = "c99"
        snapshot["metadata"]["scene_clips"][clip["id"]]["address"] = "c99.title"
    elif changed == "profile":
        snapshot["config"]["theme_overrides"]["visual"]["canvas"]["width"] = 300
    elif changed == "assets":
        snapshot["registry"]["assets"]["asset:image"] = {
            "object_id": "object-image", "digest": "sha256:" + hashlib.sha256(b"image bytes").hexdigest(),
            "type": "image", "resolution": "100x50",
        }
    elif changed == "source":
        snapshot["metadata"]["capture_authority"]["parent_content_digest"] = "sha256:" + "f" * 64
    elif changed == "renderer":
        monkeypatch.setattr(composed_frame, "renderer_environment_identity", lambda: "new-renderer-source")
    retained = {"authority": snapshot["metadata"]["capture_authority"],
                "config": snapshot["config"], "registry": snapshot["registry"]}
    snapshot["metadata"]["capture_identity"] = "sha256:" + hashlib.sha256(json.dumps(
        retained, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()
    Timeline.from_config(snapshot["config"]).dump(p.timeline_path)
    p.assets_path.write_text(json.dumps(snapshot["registry"]))
    q = another(p, snapshot=snapshot)
    frame = 29 if changed == "frame" else 0
    info = q.capture(cards(frame), tmp_path / "changed", (50, 25) if changed == "resolution" else None)
    assert info["fresh_frames"] == 1 and info["cached_frames"] == 0
    assert q.scene_for_frame(frame) is not None and len(calls) == 2
    q.close()


def test_changed_renderer_identity_is_a_cache_miss(captured):
    p, _ = captured
    key = composed_frame.frame_cache_key(p.snapshot, 0, None, renderer_identity="other-renderer")
    assert p.cache.read_entry(key) is None


@pytest.mark.parametrize("changed", [
    "key", "source_identity", "root_source", "basis", "addresses", "frame", "forged_row", "png", "malformed",
])
def test_planted_stale_or_forged_entry_renders_fresh(captured, tmp_path, changed):
    p, calls = captured
    key, path, png, payload = entry(p)
    if changed == "key":
        payload["key"] = "0" * 64
    elif changed == "source_identity":
        payload["scene"][changed] = "stale-source"
    elif changed == "root_source":
        payload["scene"][changed] = "0" * 64
    elif changed == "basis":
        payload["scene"][changed]["width"] += 1
    elif changed == "addresses":
        payload["scene"][changed]["opaque:occ:A:opaque:source:title"] = "c99.forged"
    elif changed == "frame":
        payload["scene"]["answer"]["frame"] = 24
    elif changed == "forged_row":
        # Every source/key/PNG metadata hash still matches. Alter only an
        # already-public answer, then recompute the on-disk chunk CRC.
        payload["scene"]["answer"]["layers"][0]["visible"] = False
    elif changed == "png":
        image = io.BytesIO()
        Image.new("RGB", (100, 50), "red").save(image, format="PNG")
        png = image.getvalue()
        payload["png_sha256"] = hashlib.sha256(png).hexdigest()
    else:
        payload = ["malformed"]
    plant(path, png, payload)
    assert p.cache.read_entry(key) is None
    q = another(p)
    info = q.capture(cards(0), tmp_path / "recovered")
    assert info["fresh_frames"] == 1 and info["cached_frames"] == 0
    assert len(calls) == 2
    assert q.scene_for_frame(0).frame(0, source_identity=q.source_identity)["layers"][0]["visible"] is True
    q.close()


@pytest.mark.parametrize("changed", ["key", "png_sha256", "source_identity", "basis", "addresses", "frame", "malformed"])
def test_payload_bindings_checked_after_owner_integrity(captured, tmp_path, changed):
    p, calls = captured
    key, path, png, payload = entry(p)
    if changed in {"key", "png_sha256"}:
        payload[changed] = "0" * 64
    elif changed == "source_identity":
        payload["scene"][changed] = "wrong-source"
    elif changed in {"basis", "addresses"}:
        payload["scene"][changed] = {}
    elif changed == "frame":
        payload["scene"]["answer"]["frame"] = 24
    else:
        payload["scene"]["answer"]["layers"] = [None]
    plant(path, png, payload)
    # Isolate the semantic checks from the whole-entry integrity check above.
    # This privileged test-only mutation is unavailable to an on-disk forger.
    p.cache._scene_digests[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    q = another(p)
    assert q.capture(cards(0), tmp_path / "mismatch")["fresh_frames"] == 1
    assert len(calls) == 2
    q.close()


def test_mixed_payload_batch_keeps_fresh_observations_separate(captured, tmp_path, monkeypatch):
    p, calls = captured
    key, path, _, _ = entry(p)
    original = path.read_bytes()
    writes = []
    write = p.cache.write

    def record(key, data, **kwargs):
        writes.append(kwargs["frame"])
        return write(key, data, **kwargs)
    monkeypatch.setattr(p.cache, "write", record)
    info = p.capture(cards(0, 48), tmp_path / "mixed")
    assert info["evidence_source"] == "mixed" and writes == [48]
    assert path.read_bytes() == original
    assert p.fresh_scene_frames == {48} and p.scene_for_frame(0) is not p.scene_capture
    assert p.scene_for_frame(48).frame(48, source_identity=p.source_identity)["coverage"] == "GAP"
    with pytest.raises(scene_answers.SceneQueryUnavailable):
        p.scene_capture.frame(0, source_identity=p.source_identity)
    assert [call[0] for call in calls] == [(0, 24), (48,)]


def test_untrusted_cache_owner_renders_fresh_even_with_matching_disk_hashes(captured, tmp_path):
    p, calls = captured
    key, _path, _png, _payload = entry(p)
    cache = composed_frame.FrameCaptureCache(p.cache.root)
    assert cache.read_entry(key) is None
    q = another(p, cache=cache)
    assert q.capture(cards(0), tmp_path / "untrusted")["fresh_frames"] == 1
    assert len(calls) == 2
    q.close()


def test_planted_other_frame_entry_is_a_miss(captured, tmp_path):
    p, calls = captured
    key, path, _png, _payload = entry(p)
    _, other, _, _ = entry(p, 24)
    path.write_bytes(other.read_bytes())
    assert p.cache.read_entry(key) is None
    assert p.capture(cards(0), tmp_path / "stale-frame")["fresh_frames"] == 1
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["capture", "missing", "copy", "no_callback", "extra"])
def test_failed_batch_never_mints_cache_payload(tmp_path, monkeypatch, failure):
    snapshot, _ = saved(monkeypatch)
    p, _ = provider(tmp_path, monkeypatch, snapshot, fail=failure)
    if failure == "copy":
        from pathlib import Path
        write = Path.write_bytes

        def failed_copy(path, data):
            if path == tmp_path / "failed/frames/24.png":
                raise OSError("copy failed")
            return write(path, data)
        monkeypatch.setattr(Path, "write_bytes", failed_copy)
    with pytest.raises((RuntimeError, KeyError, OSError)):
        p.capture(cards(0, 24), tmp_path / "failed")
    assert not list(p.cache.root.iterdir()) and not p.cache._scene_digests
    assert not p.fresh_scene_frames and p.scene_for_frame(0) is None


def test_payload_counts_toward_existing_cache_caps(captured):
    p, _ = captured
    key, path, png, _ = entry(p)
    assert path.stat().st_size > len(png)
    p.cache.max_bytes = len(png)
    p.cache.prune()
    assert p.cache.read_entry(key) is None
    assert not list(p.cache.root.iterdir()) and not p.cache._scene_digests
