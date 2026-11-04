"""A1: Astrid intent lives under ``app.astrid`` (clip) and ``parent.config.app.astrid`` (film).

Dual read (old locations still read), single write (new location only), migration of a working copy
only, and a published/fetched revision is never rewritten.
"""
from __future__ import annotations

import copy
import json

from pathlib import Path

from astrid.core.timeline import astrid_ns as ns
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline_checkout import Checkout

TINY = Path(__file__).resolve().parents[1] / "fixtures" / "timeline_editing" / "tiny.json"
LEGACY_CLIP_KEYS = ns.CLIP_INTENT_KEYS
LEGACY_FILM_KEYS = ns.FILM_INTENT_KEYS


def _legacy_bundle() -> dict:
    """tiny.json with intent in the OLD places only."""
    bundle = json.loads(TINY.read_text(encoding="utf-8"))
    clips = bundle["shots"]["A"]["internal_timeline"]["clips"]
    clips[0]["app"] = {"on": 'start of "viral"', "cut": "c1", "layer": "hero", "kind": "keep-me",
                       "formulas": {"params.x": {"word": "viral", "field": "start"}}}
    config = bundle.setdefault("parent", {}).setdefault("config", {})
    config["chapters"] = [{"name": "01 OPEN", "from": "c1"}]
    config["slots"] = {"hero": {"kind": "footage"}}
    config["cut_notes"] = {"c1": {"why": "the opener"}}
    return bundle


def _flat_legacy_keys(bundle: dict) -> list[str]:
    found = []
    for shot in bundle["shots"].values():
        for clip in (shot.get("internal_timeline") or {}).get("clips") or []:
            found += [k for k in (clip.get("app") or {}) if k in LEGACY_CLIP_KEYS]
    found += [k for k in bundle["parent"]["config"] if k in LEGACY_FILM_KEYS]
    return found


def test_old_only_film_reads_correctly_without_migration():
    bundle = _legacy_bundle()
    before = copy.deepcopy(bundle)
    clip = bundle["shots"]["A"]["internal_timeline"]["clips"][0]
    assert intent.on(clip) == 'start of "viral"'
    assert intent.cut_of(clip) == "c1" and intent.layer_of(clip) == "hero"
    assert intent.formulas(clip) == {"params.x": {"word": "viral", "field": "start"}}
    assert intent.chapters(bundle) == [{"name": "01 OPEN", "from": "c1"}]
    assert intent.cut_notes(bundle) == {"c1": {"why": "the opener"}}
    assert bundle == before  # reading never rewrites


def test_namespace_wins_per_key_over_legacy():
    clip = {"id": "x", "app": {"on": "old", "cut": "c9", "astrid": {"on": "new"}}}
    assert intent.on(clip) == "new"
    assert intent.cut_of(clip) == "c9"  # the legacy key still reads where the namespace is silent


def test_write_goes_to_namespace_and_moves_legacy_in():
    bundle = _legacy_bundle()
    clip = bundle["shots"]["A"]["internal_timeline"]["clips"][0]
    intent.set_until(clip, 'end of "viral"')
    assert clip["app"]["astrid"]["until"] == 'end of "viral"'
    assert clip["app"]["astrid"]["on"] == 'start of "viral"'  # the legacy neighbours moved in
    assert clip["app"]["kind"] == "keep-me"  # another owner's key stays put
    assert not [k for k in clip["app"] if k in LEGACY_CLIP_KEYS]
    intent.set_chapters(bundle, [{"name": "02 NEXT", "from": "c2"}])
    intent.set_cut_note(bundle, "c2", why="later")
    config = bundle["parent"]["config"]
    assert not [k for k in config if k in LEGACY_FILM_KEYS]
    assert config["app"]["astrid"]["chapters"] == [{"name": "02 NEXT", "from": "c2"}]
    assert config["app"]["astrid"]["slots"] == {"hero": {"kind": "footage"}}  # moved in, not lost
    assert set(config["app"]["astrid"]["cut_notes"]) == {"c1", "c2"}


def test_new_film_has_no_legacy_keys():
    bundle = json.loads(TINY.read_text(encoding="utf-8"))
    clip = bundle["shots"]["A"]["internal_timeline"]["clips"][0]
    intent.set_on(clip, "a")
    intent.set_cut(clip, "c1")
    intent.set_formula(clip, "params.x", {"word": "w", "field": "start"})
    intent.set_chapters(bundle, [{"name": "01", "from": "c1"}])
    intent.slots(bundle)["s"] = {"kind": "footage"}
    intent.set_cut_note(bundle, "c1", why="w")
    assert [k for k in clip.get("app", {}) if k in LEGACY_CLIP_KEYS] == []  # written clip: namespace only
    assert [k for k in bundle["parent"]["config"] if k in LEGACY_FILM_KEYS] == []
    assert {"on", "cut", "formulas"} <= set(clip["app"]["astrid"])
    intent.migrate(bundle)  # the fixture's other clips are old-style; a checkout moves them too
    assert _flat_legacy_keys(bundle) == []


def test_migrate_moves_working_copy_and_is_idempotent():
    bundle = _legacy_bundle()
    intent.migrate(bundle)  # main's migrate returns the bundle and moves the legacy keys in place
    assert _flat_legacy_keys(bundle) == []
    clip = bundle["shots"]["A"]["internal_timeline"]["clips"][0]
    assert clip["app"]["astrid"]["cut"] == "c1" and clip["app"]["kind"] == "keep-me"
    assert intent.chapters(bundle) == [{"name": "01 OPEN", "from": "c1"}]
    once = copy.deepcopy(bundle)
    intent.migrate(bundle)
    assert bundle == once


def test_normalized_leaves_the_published_input_untouched():
    head = _legacy_bundle()
    snapshot = copy.deepcopy(head)
    out = ns.normalized(head)
    assert head == snapshot and _flat_legacy_keys(head)  # published stays legacy
    assert _flat_legacy_keys(out) == []


def test_three_way_legacy_head_is_not_a_conflict_and_keeps_intent(tmp_path):
    base = _legacy_bundle()
    head = copy.deepcopy(base)
    candidate = copy.deepcopy(base)
    intent.migrate(candidate)  # the working copy was migrated on checkout
    intent.set_chapters(candidate, [{"name": "01 OPEN", "from": "c1"}, {"name": "02 NEXT", "from": "c2"}])
    path = tmp_path / "legacy.json"
    Checkout(base).save(path, quiet=True)
    local = Checkout.load(path)
    Checkout(head).save(path, quiet=True)
    local.bundle = candidate
    local.save(quiet=True)
    merged = Checkout.load(path).bundle
    assert [r["name"] for r in intent.chapters(merged)] == ["01 OPEN", "02 NEXT"]
    assert intent.slots(copy.deepcopy(merged)) == {"hero": {"kind": "footage"}}
    assert intent.cut_notes(merged) == {"c1": {"why": "the opener"}}
    assert [k for k in merged["parent"]["config"] if k in LEGACY_FILM_KEYS] == []


# ---- RS-2 fixes: nothing legacy is dropped, address reads the namespace, unknown film keys merge in the namespace
def test_migrating_a_clash_keeps_the_legacy_value_recoverably_and_survives_a_second_migration():
    bundle = _legacy_bundle()
    config = bundle["parent"]["config"]
    config["app"] = {"astrid": {"slots": {"hero": {"kind": "footage"}}, "cut_notes": {"c1": {"why": "new"}}}}
    config["slots"] = {"broll": {"kind": "footage"}}
    config["cut_notes"] = {"c1": {"why": "old"}}
    clip = bundle["shots"]["A"]["internal_timeline"]["clips"][0]
    clip["app"] = {"why": "old why", "astrid": {"why": "new why"}}
    assert ns.migrate_bundle(bundle) is True
    film = config["app"]["astrid"]
    assert film["slots"] == {"hero": {"kind": "footage"}} and film["_legacy"]["slots"] == {"broll": {"kind": "footage"}}
    assert film["cut_notes"] == {"c1": {"why": "new"}} and film["_legacy"]["cut_notes"] == {"c1": {"why": "old"}}
    assert clip["app"]["astrid"]["why"] == "new why" and clip["app"]["astrid"]["_legacy"]["why"] == "old why"
    assert "slots" not in config and "why" not in clip["app"]
    once = copy.deepcopy(bundle)
    assert ns.migrate_bundle(bundle) is False and bundle == once  # a second migration loses nothing


def test_equal_values_on_both_sides_leave_no_legacy_copy():
    clip = {"id": "x", "app": {"why": "same", "astrid": {"why": "same"}}}
    ns.migrate_clip(clip)
    assert clip["app"]["astrid"] == {"why": "same"}


def test_onscreen_address_reads_a_migrated_clip():
    from astrid.core.timeline.onscreen import _address

    clip = {"id": "a:sparks-id", "app": {"cut": "c41", "layer": "sparks"}}
    assert _address(clip) == "c41.sparks"
    ns.migrate_clip(clip)
    assert "cut" not in clip["app"] and _address(clip) == "c41.sparks"


def test_merge_keeps_a_film_namespace_key_outside_the_known_list_in_the_namespace(tmp_path):
    base = json.loads(TINY.read_text(encoding="utf-8"))
    head = copy.deepcopy(base)
    candidate = copy.deepcopy(base)
    candidate.setdefault("parent", {}).setdefault("config", {}).setdefault("app", {}).setdefault("astrid", {})["groups"] = {"g": ["c1"]}
    path = tmp_path / "namespace.json"
    Checkout(base).save(path, quiet=True)
    local = Checkout.load(path)
    Checkout(head).save(path, quiet=True)
    local.bundle = candidate
    local.save(quiet=True)
    merged, lines = Checkout.load(path).bundle, local.merged
    assert lines == []
    assert ns.film_intent(merged["parent"]["config"])["groups"] == {"g": ["c1"]}
    assert "groups" not in merged["parent"]["config"]
    newer = copy.deepcopy(candidate)
    newer["parent"]["config"]["app"]["astrid"]["groups"] = {"g": ["c1", "c2"]}
    local = Checkout.load(path)
    local.bundle = newer
    local.save(quiet=True)
    merged2 = Checkout.load(path).bundle
    assert ns.film_intent(merged2["parent"]["config"])["groups"] == {"g": ["c1", "c2"]}
