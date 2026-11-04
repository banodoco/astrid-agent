from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from PIL import Image

from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commit_session import Timeline


def test_runtime_timeline_preserves_legacy_add_dispatch(monkeypatch):
    expected = object()
    captured = {}

    def legacy_add(self, element, **kwargs):
        captured.update(element=element, **kwargs)
        return expected

    monkeypatch.setattr(Checkout, "add", legacy_add)
    timeline = object.__new__(Timeline)
    timeline._evaluating = False
    timeline._identity_ordinal = 0
    timeline._identity_cache = {}
    timeline._edit_depth = 1

    result = timeline.add("am-text", at=0, asset="managed-key", params={"text": "Title"}, clip_id="title")

    assert result is expected
    assert captured == {"element": "am-text", "at": 0, "hold": None, "track": None,
                        "asset": "managed-key", "params": {"text": "Title"}, "clip_id": "title"}


def test_media_add_refuses_file_changed_between_digest_and_import(tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    Image.new("RGB", (4, 4), "red").save(source)
    before = source.read_bytes()
    before_id = "sha256:" + hashlib.sha256(before).hexdigest()
    timeline = object.__new__(Timeline)
    timeline._evaluating = True
    timeline._identity_ordinal = 0
    timeline._identity_cache = {}
    timeline._media_cache = {}
    timeline._verified_media = []
    timeline.bundle = {}
    timeline.session = SimpleNamespace(
        project_id="project",
        client=SimpleNamespace(_remote=SimpleNamespace(_transport=SimpleNamespace(
            list_generations=lambda *_a, **_k: ([], None)))),
    )

    from astrid.sdk.timeline import media_add

    def import_changed_file(_session, _bundle, _source):
        changed = Image.new("RGB", (4, 4), "blue")
        changed.save(source)
        digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        return ({"asset": "asset", "generation_id": "generation", "variant_id": "variant",
                 "object_id": digest, "catalog_digest": digest}, {"media_type": "image/png"})

    monkeypatch.setattr(media_add, "_selection", import_changed_file)
    with pytest.raises(TimelineEditError, match="changed during import"):
        timeline.add(source, at=0, id="image")
    assert before_id != "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()


def test_missing_media_path_cannot_fall_back_to_an_element(tmp_path, monkeypatch):
    from astrid.sdk.timeline import media_add

    timeline = object.__new__(Timeline)
    timeline._evaluating = True
    timeline._identity_ordinal = 0
    timeline._identity_cache = {}
    timeline._media_cache = {}
    timeline.bundle = {}
    timeline.session = object()

    def missing_file(*args):
        raise TimelineEditError("missing media")

    monkeypatch.setattr(media_add, "_selection", missing_file)
    monkeypatch.setattr(Checkout, "add", lambda *a, **kw: pytest.fail("treated a path as an element"))
    with pytest.raises(TimelineEditError, match="missing media"):
        timeline.add(str(tmp_path / "missing.png"), at=0)
