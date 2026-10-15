"""Source-only contract checks; no browser, media, or dependency installation."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from jsonschema import ValidationError

from astrid.core import timeline
from astrid.core.timeline import banodoco_schema
from astrid.packs.rendering.actions.timeline_visualize import composed_frame
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion
from scripts import gen_remotion_types

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("role", ["output", "source"])
def test_track_role_roundtrips_to_renderer(tmp_path, role):
    config = {
        "tracks": [{"id": "v1", "kind": "visual", "label": "Video", "role": role}],
        "clips": [],
    }
    path = tmp_path / "role.timeline.json"
    timeline.save_timeline(config, path)
    assert timeline.load_timeline(path) == config
    assert remotion._serialize_timeline(path)["tracks"][0]["role"] == role
    assert "role" in timeline._TRACK_ALLOWED
    assert "role?: 'output' | 'source';" in gen_remotion_types.generate()


def test_generated_track_allowlist_is_current():
    source = gen_remotion_types.generate()
    assert (ROOT / "remotion/src/types.generated.ts").read_text() == source
    assert set(timeline.TrackDefinition.__annotations__) == timeline._TRACK_ALLOWED
    shared = banodoco_schema._shared_load_schema()["properties"]["tracks"]["items"]["properties"]
    assert set(shared) == timeline._TRACK_ALLOWED
    assert shared["role"]["enum"] == ["output", "source"]
    assert shared["role"]["default"] == "output"


@pytest.mark.parametrize("role", ["preview", None, 1])
def test_invalid_track_roles_are_rejected(role):
    with pytest.raises(ValidationError):
        timeline.validate_timeline({
            "tracks": [{"id": "v1", "kind": "visual", "label": "Video", "role": role}],
            "clips": [],
        }, strict=False)


def test_omitted_track_role_retains_legacy_output_default():
    config = {"tracks": [{"id": "v1", "kind": "visual", "label": "Video"}], "clips": []}
    assert timeline.canonical_timeline_config(config) == config


@pytest.fixture
def renderer_tree(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    project = tmp_path / "selected-project"
    files = {
        checkout / "astrid/packs/demo/elements/example/component.tsx": b"component=one",
        project / "src/index.ts": b"entry=one",
        project / "node_modules/@banodoco/timeline-composition/package.json": b'{"version":"0.0.7","main":"dist/index.js"}',
        project / "node_modules/@banodoco/timeline-composition/typescript/src/index.ts": b"composition=one",
        project / "node_modules/@banodoco/timeline-composition/dist/index.js": b"compiled=one",
        project / "public/fonts/example.woff2": b"synthetic-font=one",
        checkout / "unrelated.md": b"notes=one",
    }
    for path, data in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    monkeypatch.setattr(composed_frame, "_checkout_root", lambda: checkout)
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(project))
    monkeypatch.delenv("ASTRID_RENDERER_SOURCE_ROOT", raising=False)
    composed_frame._DIGEST_MEMO.clear()
    return checkout, project


@pytest.mark.parametrize("relative,project_file", [
    ("astrid/packs/demo/elements/example/component.tsx", False),
    ("node_modules/@banodoco/timeline-composition/typescript/src/index.ts", True),
    ("node_modules/@banodoco/timeline-composition/dist/index.js", True),
    ("public/fonts/example.woff2", True),
])
def test_frame_and_owner_identity_follow_consumed_bytes(renderer_tree, relative, project_file):
    checkout, project = renderer_tree
    path = (project if project_file else checkout) / relative
    before_owner = composed_frame.renderer_environment_identity()
    before_frame = composed_frame.frame_cache_key({"clips": []}, 1, [32, 18])
    original = path.read_bytes()
    stat = path.stat()
    path.write_bytes(original.replace(b"one", b"two"))
    # A package extraction may preserve the file's size and mtime.
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert composed_frame.renderer_environment_identity() != before_owner
    assert composed_frame.frame_cache_key({"clips": []}, 1, [32, 18]) != before_frame
    composed_frame._DIGEST_MEMO.clear()
    cold_frame = composed_frame.frame_cache_key({"clips": []}, 1, [32, 18])
    assert cold_frame == composed_frame.frame_cache_key({"clips": []}, 1, [32, 18])
    path.write_bytes(original)
    assert composed_frame.renderer_environment_identity() == before_owner
    assert composed_frame.frame_cache_key({"clips": []}, 1, [32, 18]) == before_frame


def test_unrelated_edit_keeps_frame_and_owner_identity(renderer_tree):
    checkout, _ = renderer_tree
    before_owner = composed_frame.renderer_environment_identity()
    before_frame = composed_frame.frame_cache_key({"clips": []}, 1, [32, 18])
    (checkout / "unrelated.md").write_text("notes=two")
    assert composed_frame.renderer_environment_identity() == before_owner
    assert composed_frame.frame_cache_key({"clips": []}, 1, [32, 18]) == before_frame
