"""Authored envelopes survive the existing command/save/history/compiler path."""
import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline import commands, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.compiler import compile_timing


def bundle():
    value = json.loads((Path(__file__).parents[1] / "fixtures/timeline_editing/tiny_publishable.json").read_text())
    clip = next(c for c in value["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-music")
    clip["audio"] = {"envelope": {"keyframes": [{"at": 0.123456789, "value": 1.5}], "duck": True}}
    clip.setdefault("app", {})["vendor"] = {"opaque": [None, False, {"keep": "bytes"}]}
    clip.setdefault("params", {}).update(fadeIn=0.73, fadeOut=0.83, opaque=[None, {"keep": True}])
    return value


def row(value):
    return next(c for c in value["shots"]["A"]["internal_timeline"]["clips"] if c["id"] == "a-music")


def test_sdk_command_save_load_projection_undo_redo(tmp_path, monkeypatch):
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    source = bundle()
    original = copy.deepcopy(row(source))
    for seam in (tc, store):
        monkeypatch.setattr(seam, "resolve_ids", lambda *a, **k: ("p", "t", "rev-0"))
        monkeypatch.setattr(seam, "fetch_bundle", lambda *a, **k: copy.deepcopy(source))
    tl = Checkout.draft("P", "t")
    selector = next(c.address for c in tl.clips() if c.id == "a-music")
    commands.run(tl, {"op": "clip.set-look", "select": selector, "args": {"volume": 0.7}})
    loaded = Checkout.draft("P", "t")
    assert row(loaded.document())["volume"] == 0.7
    projected = compile_timing(loaded.document()).bundle
    for doc in (loaded.document(), projected):
        for key in ("audio", "params"):
            assert row(doc)[key] == original[key]
        assert row(doc)["app"]["vendor"] == original["app"]["vendor"]
    assert loaded.undo(1)
    assert row(loaded.document()).get("volume") == original.get("volume")
    assert row(loaded.document())["audio"] == original["audio"]
    assert loaded.redo(1)
    assert row(Checkout.draft("P", "t").document())["audio"] == original["audio"]
    # Compiler must not mirror the canonical film track into child definitions.
    assert projected["shots"]["A"]["internal_timeline"]["tracks"] == loaded.document()["shots"]["A"]["internal_timeline"]["tracks"]


@pytest.mark.parametrize("audio", [None, {}, {"envelope": {"keyframes": [{"at": 0, "value": True}]}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1}], "duck": 1}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1}, {"at": 0, "value": 2}]}},
    {"envelope": {"keyframes": [{"at": 0, "value": 1}]}, "render_source": {"file": "https://forged"}}])
def test_checkout_refuses_invalid_authored_audio_on_load(audio):
    value = bundle()
    row(value)["audio"] = audio
    with pytest.raises(ValueError):
        Checkout(value)


def test_one_canonical_film_track_definition_survives_timing_projection():
    from astrid.core.timeline.authoring_bundle import _film_track_view
    canonical = _film_track_view(bundle())
    tracks = copy.deepcopy(canonical["parent"]["config"]["tracks"])
    assert sum(track["id"] == "music" for track in tracks) == 1
    assert all(shot["internal_timeline"]["tracks"] == [] for shot in canonical["shots"].values())
    result = compile_timing(Checkout(canonical).document()).bundle
    assert result["parent"]["config"]["tracks"] == tracks
    assert all(shot["internal_timeline"]["tracks"] == [] for shot in result["shots"].values())
    assert row(result)["audio"] == row(canonical)["audio"]
