"""STORY source behavior on disposable inputs, never named-film admission."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_timeline_reflow_golden import film

from astrid.core.timeline import astrid_ns
from astrid.core.timeline.compatibility import require_writable_parent
from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline.story import (
    StoryRangeError,
    migrate_story_ranges,
    open_story_overview,
    set_ranges,
    story_overview,
)
from astrid.sdk.timeline_checkout import Checkout

TINY = Path(__file__).resolve().parents[1] / "fixtures/timeline_editing/tiny.json"


def labelled():
    bundle = film()
    bundle["parent"]["config"]["chapters"] = [
        {"name": "01 OPEN", "from": "c1"},
        {"name": "02 LIVE", "from": "c2"},
        {"name": "03 WORK", "from": "c3"},
    ]
    return bundle


def ns(bundle):
    return bundle["parent"]["config"]["app"]["astrid"]


def starts(bundle):
    return [r["start_frame"] for r in story_overview(bundle, revision_id="rev-0")["ranges"]
            if r["range"]["kind"] == "chapter"]


def row(name="chapter", kind="chapter", start="c1", end=None):
    return {"name": name, "kind": kind, "from": start, "to": end}


def test_migration_preserves_all_disposable_chapter_frames_and_picture_target_kind():
    bundle = labelled()
    before = Checkout(film())
    expected = [before.frame(before.cut(cid).start) for cid in ("c1", "c2", "c3")]
    assert expected == [0, 120, 180]
    assert before.frame(before.time('"It"')) == 3  # word onset differs from picture's take origin
    media = copy.deepcopy((bundle["shots"], bundle["placements"], bundle["base_parent"]))
    assert migrate_story_ranges(bundle) == []
    assert intent.chapters(bundle) == []
    assert intent.ranges(bundle) == [row("01 OPEN", end="c2"), row("02 LIVE", start="c2", end="c3"), row("03 WORK", start="c3")]
    assert starts(bundle) == expected
    assert (bundle["shots"], bundle["placements"], bundle["base_parent"]) != media  # namespace-only clip normalization
    for sid, shot in bundle["shots"].items():
        for migrated, original in zip(shot["internal_timeline"]["clips"], media[0][sid]["internal_timeline"]["clips"]):
            assert {k: v for k, v in migrated.items() if k != "app"} == {k: v for k, v in original.items() if k != "app"}
    assert bundle["placements"] == media[1] and bundle["base_parent"] == media[2]
    assert ns(bundle)["v"] == 2


def test_trim_speed_and_take_origin_remain_the_picture_target_after_reflow():
    bundle = film()
    clips = bundle["shots"]["F"]["internal_timeline"]["clips"]
    voice = next(c for c in clips if c["id"] == "vo-s2")
    voice.update(at=4.5, speed=2.0)
    voice["from"] = 1.0
    voice["to"] = 2.6
    # This exact compiler's transcript coordinates are clip-relative; source
    # trim/rate affect the take-origin exception, not those word coordinates.
    voice["app"]["words"] = [[0.2, 0.7, "Live"]]
    bundle["parent"]["config"]["chapters"] = [{"name": "LIVE", "from": "c2"}]
    assert migrate_story_ranges(bundle) == []
    tl = Checkout(bundle)
    tl.resolve()
    assert starts(tl.bundle) == [120]
    assert tl.frame(tl.time('"Live"')) == 141
    tl.voice("s1").set_gap_after(1.0)
    projected = starts(tl.bundle)[0]
    assert projected == tl.frame(tl.cut("c2").start)
    assert projected != tl.frame(tl.time('"Live"'))


def test_swap_asset_keeps_chapters_and_moving_line_anchor_moves_only_its_chapter():
    tl = Checkout(labelled())
    before = starts(tl.bundle)
    tl.clip("c2.field").swap_asset("P")
    assert starts(tl.bundle) == before
    # Use the actual registered clip move/nudge path on a disposable narration
    # line named n20a. No claim that this is the unavailable real-film n20a.
    voice = tl.voice("s2").clips[0]
    intent.set_line(voice.data, "n20a", intent.words(voice.data))
    voice.nudge(0.5)
    assert starts(tl.bundle) == [before[0], before[1] + 15, before[2]]


def test_unanchored_tiny_keeps_exact_legacy_labels_and_reports_a_finding():
    bundle = json.loads(TINY.read_text())
    labels = [{"name": "01 OPEN", "from": "c1"}, {"name": "02 LIVE", "from": "c2"}]
    bundle["parent"]["config"]["chapters"] = copy.deepcopy(labels)
    findings = migrate_story_ranges(bundle)
    assert len(findings) == 2 and all(f["code"] == "legacy-range" for f in findings)
    assert intent.chapters(bundle) == labels
    assert "ranges" not in ns(bundle)
    once = copy.deepcopy(bundle)
    assert migrate_story_ranges(bundle) == findings and bundle == once
    view = story_overview(bundle, revision_id="rev-0")
    assert view["ranges"] == [] and view["legacy_chapters"] == labels
    assert len(view["findings"]) == 2


def test_atomic_legacy_conversion_preserves_original_boundaries_on_retry():
    bundle = labelled()
    intent.set_on(bundle["shots"]["F"]["internal_timeline"]["clips"][2], None)
    assert migrate_story_ranges(bundle)
    assert len(intent.chapters(bundle)) == 3 and intent.ranges(bundle) == []
    intent.set_on(bundle["shots"]["F"]["internal_timeline"]["clips"][2], '"Live"')
    assert migrate_story_ranges(bundle) == []
    once = copy.deepcopy(bundle)
    assert migrate_story_ranges(bundle) == [] and bundle == once
    assert starts(bundle) == [0, 120, 180]


def test_stale_cut_cache_is_not_rewritten_by_storage_migration():
    bundle = labelled()
    bundle["shots"]["F"]["internal_timeline"]["clips"][2]["at"] = 4.5
    assert "cache frame" in migrate_story_ranges(bundle)[0]["message"]
    assert intent.chapters(bundle) and not intent.ranges(bundle)
    assert bundle["shots"]["F"]["internal_timeline"]["clips"][2]["at"] == 4.5


def test_cyclic_picture_anchor_keeps_legacy_table_without_fabricating_a_resolution():
    bundle = labelled()
    intent.set_on(bundle["shots"]["F"]["internal_timeline"]["clips"][0], "c1")
    assert "CYCLE" in migrate_story_ranges(bundle)[0]["message"]
    assert len(intent.chapters(bundle)) == 3 and not intent.ranges(bundle)


def test_same_frame_chapter_boundaries_keep_legacy_table_instead_of_collapsed_ranges():
    bundle = labelled()
    clips = bundle["shots"]["F"]["internal_timeline"]["clips"]
    voice = next(c for c in clips if c["id"] == "vo-s2")
    voice["at"] = 0.0
    next(c for c in clips if c["id"] == "c2-plate")["at"] = 0.0
    assert "collapse" in migrate_story_ranges(bundle)[0]["message"]
    assert len(intent.chapters(bundle)) == 3 and not intent.ranges(bundle)


def test_same_name_consecutive_labels_coalesce_and_unsorted_labels_follow_cut_order():
    bundle = labelled()
    labels = bundle["parent"]["config"]["chapters"]
    labels[1]["name"] = labels[0]["name"]
    labels.reverse()
    assert migrate_story_ranges(bundle) == []
    assert intent.ranges(bundle) == [row("01 OPEN", end="c3"), row("03 WORK", start="c3")]


def test_canonical_chapters_and_beats_share_resolver_and_half_open_boundaries():
    bundle = film()
    set_ranges(bundle, [row("01 OPEN", end="c2"), row("02 LIVE", start="c2"), row("viral", "beat", '"viral"', 'after "viral"')])
    view = story_overview(bundle, revision_id="rev-0")
    assert view["findings"] == []
    assert [(r["start_frame"], r["end_frame"]) for r in view["ranges"]] == [(0, 120), (120, 270), (39, 57)]
    tl = Checkout(bundle)
    assert tl.clip("c2.live").id in [c.id for c in tl.clips(chapter="02")]
    assert tl.clip("c2.live").id not in [c.id for c in tl.clips(chapter="01")]
    assert [c.id for c in tl.cuts(chapter="02")] == ["c2", "c3"]
    from astrid.sdk.timeline_sheet import render_sheet
    sheet = render_sheet(tl)
    assert "# 01 OPEN" in sheet and "# 02 LIVE" in sheet
    detached = intent.ranges(bundle)
    detached[0]["name"] = "output-only"
    assert intent.ranges(bundle)[0]["name"] == "01 OPEN"


def test_plain_first_word_and_picture_timerefs_keep_different_targets_and_follow_their_anchor():
    bundle = film()
    set_ranges(bundle, [row("picture", start="c2", end="c3"), row("word", "beat", '"Live"', 'after "Live"')])
    before = story_overview(bundle, revision_id="rev-0")
    assert [r["start_frame"] for r in before["ranges"]] == [120, 123]
    assert migrate_story_ranges(bundle) == []
    once = copy.deepcopy(bundle)
    assert migrate_story_ranges(bundle) == [] and bundle == once
    tl = Checkout(bundle)
    tl.voice("s2").clips[0].nudge(0.5)
    assert [r["start_frame"] for r in story_overview(tl.bundle, revision_id="rev-0")["ranges"]] == [135, 138]


def test_opening_cut_boundary_stays_pinned_while_first_word_beat_follows_narration():
    bundle = film()
    bundle["parent"]["config"]["chapters"] = [{"name": "opening", "from": "c1"}]
    assert migrate_story_ranges(bundle) == []
    assert intent.chapters(bundle) == []
    set_ranges(bundle, [*intent.ranges(bundle), row("word", "beat", '"It"', 'after "It"')])
    # Normalize through the actual migration path before taking the idempotence
    # snapshot: set_ranges alone validates a copy, not legacy clip app fields.
    once = copy.deepcopy(bundle)
    assert migrate_story_ranges(bundle) == [] and bundle == once
    before = story_overview(bundle, revision_id="rev-0")
    assert [r["start_frame"] for r in before["ranges"]] == [0, 3]
    assert not any(f["code"] == "unresolved-anchor" for f in before["findings"])

    tl = Checkout(bundle)
    tl.voice("s1").clips[0].nudge(0.5)
    group = next(g for g in tl.cut_index.groups(tl) if g["id"] == "c1")
    assert group["start"] == 0.0 and not group["orphan"]
    assert group["picture"] is not None and not intent.orphan(group["picture"].data)
    assert tl.frame(tl._moment_time('"It"', group["picture"], in_point=True)) == 15
    assert tl.frame(tl.time('"It"')) == 18
    after = story_overview(tl.bundle, revision_id="rev-0")
    assert [r["start_frame"] for r in after["ranges"]] == [0, 18]
    assert not any(f["code"] == "unresolved-anchor" for f in after["findings"])


def test_invalid_canonical_ranges_block_checks_and_refuse_selector_fallback():
    from astrid.sdk.timeline.api import SelectorError
    bundle = film()
    intent._film_w(bundle).update(ranges=[row(end="c3"), row("overlap", start="c2")], v=2)
    tl = Checkout(bundle)
    with pytest.raises(SelectorError, match="overlaps"):
        tl.clips(chapter="chapter")
    assert any("story" in line and "overlaps" in line for line in tl.check().blocking)


@pytest.mark.parametrize("rows,code", [
    ([row(end="c3"), row("overlap", start="c2")], "chapter-overlap"),
    ([row(start="c2", end="c1")], "invalid-span"),
    ([row(start="c1", end="c1")], "invalid-span"),
    ([row(start="+2f")], "unresolved-anchor"),
    ([row(start='"missing word"')], "unresolved-anchor"),
    ([row(start="c99")], "unresolved-anchor"),
    ([row(start={"word": "guessed"})], "invalid-range"),
    ([dict(row(), id="invented")], "invalid-range"),
])
def test_unsupported_unresolved_and_invalid_rows_are_findings_and_writes_are_atomic(rows, code):
    bundle = film()
    intent._film_w(bundle)["ranges"] = rows
    ns(bundle)["v"] = 2
    view = story_overview(bundle, revision_id="rev-0")
    assert code in [f["code"] for f in view["findings"]]
    original = film()
    before = copy.deepcopy(original)
    with pytest.raises((StoryRangeError, ValueError)):
        set_ranges(original, rows)
    assert original == before


def test_orphan_cut_does_not_expose_cached_seconds_as_resolved_frames():
    bundle = labelled()
    migrate_story_ranges(bundle)
    # Remove the spoken word without deleting cached clips.
    clips = bundle["shots"]["F"]["internal_timeline"]["clips"]
    clips[:] = [c for c in clips if intent.line(c) != "s2"]
    view = story_overview(bundle, revision_id="rev-0")
    assert view["ranges"][1]["start_frame"] is None
    assert any(f["code"] == "unresolved-anchor" for f in view["findings"])


def test_projection_compiler_cycle_failure_never_falls_back_to_cached_frames():
    bundle = film()
    intent._film_w(bundle).update(ranges=[row()], v=2)
    intent.set_on(bundle["shots"]["F"]["internal_timeline"]["clips"][0], "c1")
    before = copy.deepcopy(bundle)
    view = story_overview(bundle, revision_id="rev-0")
    assert view["ranges"][0]["start_frame"] is None
    assert view["findings"][0]["code"] == "timing-unresolved"
    assert bundle == before


def test_read_model_is_pinned_detached_and_has_no_checkout_or_journal_effects(monkeypatch):
    bundle = labelled()
    before = copy.deepcopy(bundle)
    from astrid.sdk.timeline import journal
    def forbidden(*args, **kwargs):
        pytest.fail("read projection attempted to write")
    monkeypatch.setattr(journal, "record_or_note", forbidden)
    monkeypatch.setattr(Checkout, "save", forbidden)
    monkeypatch.setattr(Checkout, "publish", forbidden)
    view = story_overview(bundle, revision_id="rev-0")
    assert bundle == before
    assert view == story_overview(bundle, revision_id="rev-0")
    view["ranges"][0]["range"]["name"] = "detached"
    assert bundle == before
    with pytest.raises(StoryRangeError, match="pinned revision"):
        story_overview(bundle, revision_id="rev-other")
    with pytest.raises(StoryRangeError):
        story_overview(bundle, revision_id="")


def test_real_host_consumer_fetches_exact_revision_and_refuses_mismatched_closure():
    bundle = labelled()
    calls = []
    def open_bundle(project, timeline, *, revision_id):
        calls.append((project, timeline, revision_id))
        return SimpleNamespace(ok=True, data={"bundle": bundle})
    client = SimpleNamespace(timelines=SimpleNamespace(open_bundle=open_bundle))
    view = open_story_overview("p", "t", revision_id="rev-0", client=client)
    assert calls == [("p", "t", "rev-0")]
    assert view["project_id"] == "p" and view["timeline_id"] == "t" and view["revision_id"] == "rev-0"
    assert starts(bundle) == [0, 120, 180]
    with pytest.raises(StoryRangeError, match="pinned revision"):
        open_story_overview("p", "t", revision_id="changed-head", client=client)
    bundle["project_id"] = "foreign"
    with pytest.raises(StoryRangeError, match="identity"):
        open_story_overview("p", "t", revision_id="rev-0", client=client)


@pytest.mark.parametrize("opaque", [{"app": "opaque"}, {"app": {"astrid": "opaque"}}])
def test_opaque_namespace_collision_is_preserved_and_reported(opaque):
    bundle = labelled()
    bundle["parent"]["config"].update(copy.deepcopy(opaque))
    before = copy.deepcopy(bundle["parent"])
    findings = migrate_story_ranges(bundle)
    assert findings and bundle["parent"] == before


def test_namespace_foreign_data_collisions_and_base_payload_are_preserved():
    bundle = labelled()
    config = bundle["parent"]["config"]
    canonical = copy.deepcopy(config["chapters"])
    foreign = {"renderer": {"opaque": [1, True]}, "astrid": {"chapters": canonical, "custom": {"keep": "yes"}}}
    config["app"] = copy.deepcopy(foreign)
    config["chapters"] = [{"name": "loose conflict", "from": "c99"}]
    bundle["base_parent_payload"] = copy.deepcopy(bundle["parent"])
    assert migrate_story_ranges(bundle) == []
    assert config["app"]["renderer"] == foreign["renderer"]
    assert ns(bundle)["custom"] == {"keep": "yes"}
    assert ns(bundle)["_legacy"]["chapters"] == [{"name": "loose conflict", "from": "c99"}]
    assert "chapters" in bundle["base_parent_payload"]["config"]["app"]["astrid"]
    once = copy.deepcopy(bundle)
    assert migrate_story_ranges(bundle) == [] and once == bundle


def test_opaque_collision_archive_does_not_reveal_a_different_table_on_second_migration():
    bundle = labelled()
    config = bundle["parent"]["config"]
    canonical = copy.deepcopy(config["chapters"])
    config["chapters"] = [{"name": "preserved conflict", "from": "c99"}]
    config["app"] = {"astrid": {"chapters": canonical, "_legacy": "opaque"}}
    assert migrate_story_ranges(bundle)[0]["code"] == "legacy-conflict"
    once = copy.deepcopy(bundle)
    assert migrate_story_ranges(bundle) and bundle == once
    assert config["chapters"] == [{"name": "preserved conflict", "from": "c99"}]


def test_existing_canonical_chapter_collision_retains_both_sources():
    bundle = labelled()
    intent._film_w(bundle).update(ranges=[row("authored")], v=2)
    findings = migrate_story_ranges(bundle)
    assert findings[0]["code"] == "legacy-conflict"
    assert intent.chapters(bundle) and intent.ranges(bundle) == [row("authored")]


def test_legacy_writer_replaces_migrated_chapters_and_preserves_beats():
    bundle = labelled()
    migrate_story_ranges(bundle)
    ns(bundle)["ranges"].append(row("word", "beat", '"went"', 'after "went"'))
    intent.set_chapters(bundle, [{"name": "replacement", "from": "c2"}])
    assert intent.chapters(bundle) == []  # compatibility writes migrate immediately
    assert migrate_story_ranges(bundle) == []
    assert [r["name"] for r in intent.ranges(bundle)] == ["word", "replacement"]


def test_legacy_writer_on_live_checkout_keeps_unanchored_labels_visible_beside_beats():
    bundle = json.loads(TINY.read_text())
    set_ranges(bundle, [row("word", "beat", '"viral"', 'after "viral"')])
    tl = Checkout(bundle)
    intent.set_chapters(bundle, [{"name": "01 OPEN", "from": "c1"}, {"name": "02 LIVE", "from": "c2"}])
    assert [c.id for c in tl.cuts(chapter="02")] == ["c2"]
    assert intent.ranges(bundle)[0]["kind"] == "beat"
    assert len(intent.chapters(bundle)) == 2


def test_range_version_declaration_and_unknown_version_preservation():
    bundle = labelled()
    intent._film_w(bundle)["v"] = 99
    assert migrate_story_ranges(bundle) and ns(bundle)["v"] == 99
    before = copy.deepcopy(bundle)
    with pytest.raises(ValueError, match="unknown Astrid"):
        set_ranges(bundle, [row()])
    assert bundle == before
    parent = {"config": {"app": {"astrid": {"ranges": [row()]}}}}
    with pytest.raises(ValueError, match="ranges requires"):
        require_writable_parent(parent)
    parent["config"]["app"]["astrid"]["v"] = 2
    require_writable_parent(parent)


def test_ranges_are_recognized_by_shared_namespace_normalization_without_foreign_loss():
    bundle = film()
    rows = [row()]
    bundle["parent"]["config"].update(ranges=rows, app={"renderer": "keep"})
    normalized = astrid_ns.normalized(bundle)
    assert ns(normalized)["ranges"] == rows
    assert normalized["parent"]["config"]["app"]["renderer"] == "keep"
    assert "ranges" in bundle["parent"]["config"]
