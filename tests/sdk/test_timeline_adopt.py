"""Adopt another timeline's head: the film's working copy becomes round2 (its narration, cuts, assets, intent)."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline.bed_adoption import (
    adopt_music_bed,
    reconcile_arrangement_aliases,
    switch_windows,
)
from astrid.sdk.timeline.music_beds import MusicBedError, adopted, collection, sources, validate
from astrid.sdk.timeline_adopt import adopt_content
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline_duplicate import copy_content
from tests.packs.timeline.test_edit_verbs import bundle
from tests.sdk.test_music_beds import enrolled, pinned_music, tiny


def _round2():
    """A duplicate of the film (new shot ids), then edited: a new take, a new asset key, a cut moved, beats."""
    film = bundle()
    dup, _ = copy_content(film, copy.deepcopy(film), new_slug="round2")
    dup["timeline_id"] = "round2"
    sid = next(iter(dup["shots"]))
    internal = dup["shots"][sid]["internal_timeline"]
    internal["registry"]["assets"]["music"] = {"media_id": "sha256:" + "9" * 64, "type": "audio/wav"}
    for clip in internal["clips"]:
        if clip["id"] == "a-music":
            clip["asset"] = "music"
            clip["app"] = {"beats": {"beats": [0.0, 0.5, 1.0], "time": "cue_seconds", "source": "run:R2/beats"}}
        if clip["id"] == "a-vo":
            clip["app"]["astrid"]["words"] = [[0.1, 0.3, "it"], [0.4, 0.6, "really"], [0.7, 0.9, "went"], [1.0, 1.4, "viral"]]
        if clip["id"] == "a-rocket":
            clip["app"] = {"on": '"viral"'}
    dup["parent"]["config"]["chapters"] = [{"from": "c1", "name": "01 OPEN"}]
    published = Checkout(dup)
    published.resolve()  # a published head is resolved: every clip already on its moment
    return film, published.document()


def _clips(doc):
    doc = copy.deepcopy(doc)
    intent.migrate(doc)  # compare intent, not where it is kept (a legacy round2 vs the migrated adopted film)
    return sorted((json.dumps(c, sort_keys=True) for shot in doc["shots"].values()
                   for c in shot["internal_timeline"]["clips"]))


def test_adopting_makes_the_film_equal_round2_and_keeps_the_films_identity():
    film, round2 = _round2()
    adopted, notes = adopt_content(round2, film)
    assert set(adopted["shots"]) == set(film["shots"])  # the film's shots, new revisions of them
    assert [r["shot_id"] for r in adopted["placements"]] == [r["shot_id"] for r in film["placements"]]
    film_base = copy.deepcopy(film)
    intent.migrate(film_base)  # a working copy's base is kept migrated (where its intent is kept is not an edit)
    for sid in film["shots"]:  # the film's base is untouched: check/status/publish diff against it
        assert adopted["shots"][sid]["base_internal_timeline"] == film_base["shots"][sid]["base_internal_timeline"]
    assert _clips(adopted) == _clips(round2)  # every clip with its intent, words and beats, as round2 has them
    assert intent.chapters(adopted) == [{"from": "c1", "name": "01 OPEN"}]
    tl = Checkout(adopted)
    music = next(c for c in tl.clips(audio=True) if c.track == "music")
    assert music.asset == "music" and intent.beats_label(music.data) == "run:R2/beats"
    assert [w.text for w in tl.words()][:4] == ["it", "really", "went", "viral"]
    assert tl.edits()["changes"] and not notes


def test_checkout_adopt_through_the_api_and_the_cli(tmp_path, monkeypatch, capsys):
    from astrid.packs.timeline import cli
    from astrid.sdk import timeline_checkout as tc

    film, round2 = _round2()
    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    heads = {"t": film, "round2": round2}
    monkeypatch.setattr(tc, "fetch_bundle", lambda project, timeline, revision_id=None, client=None: copy.deepcopy(heads[timeline]))
    parser = cli.build_parser(object())
    parsed = parser.parse_args(["checkout", "t", "--project", "P", "--from", "round2"])
    assert parsed.handler(parsed) == 0
    out = capsys.readouterr().out
    assert "adopted round2" in out and "narration" in out
    tl = Checkout.draft("P", "t")
    assert _clips(tl.document()) == _clips(round2)
    # a working copy with edits is not replaced silently
    tl.clip("b-type").set(text="Mine.")
    tl.save()
    parsed = parser.parse_args(["checkout", "t", "--project", "P", "--from", "round2"])
    assert parsed.handler(parsed) == 2
    assert "choose a new --draft name before adopting another timeline" in capsys.readouterr().err
    # and undo takes the adopt back in one step
    fresh = Checkout.draft("P", "t", fresh=True)
    fresh.adopt("round2")
    fresh.save()
    assert fresh.undo() == ["adopt(timeline='round2')"]
    assert _clips(fresh.document()) == _clips(film)


def test_adopt_refuses_across_projects():
    film, round2 = _round2()
    round2["project_id"] = "other"
    with pytest.raises(TimelineEditError, match="inside one project"):
        adopt_content(round2, film)

# BED-ADOPT is distinct from adopting another timeline head above.

OWNER = 'occurrence:occ-A/clip:a-music'


def adopt_one(tl, owner=OWNER):
    return Checkout(adopt_music_bed(tl.document(), [owner], alignment={owner: dict(confidence=1, source_offset_s=0)}))


def test_bed_adoption_single_carrier_is_lossless_and_deterministic():
    tl = enrolled()
    source = collection(tl)[OWNER]['source']
    source['opaque'] = {'nested': [None, False, {'future': 4}]}
    # Use the actual source-edit regeneration surface before adoption.
    from astrid.sdk.timeline.music_beds import project
    project(tl)
    before = copy.deepcopy(tl.document())
    result = adopt_one(tl)
    bed = adopted(result)
    assert bed['arrangements'][0]['source'] == source
    assert bed['ripple_basis'] == collection(tl)[OWNER]['ripple_basis']
    assert result._internal('A')['clips'] == tl._internal('A')['clips']
    assert not collection(result)
    assert result.bundle['parent']['config']['app']['astrid']['v'] == 2
    assert tl.document() == before
    from astrid.sdk.timeline.compiler import compile_timing
    assert compile_timing(result.document()).bundle == result.document()
    assert adopt_music_bed(result.document(), [OWNER]) == result.document()
    view = sources(result)[OWNER]
    assert view['source'] is bed['arrangements'][0]['source']
    assert view['ripple_basis'] is bed['ripple_basis']
    view['outputs'] = []
    assert bed['outputs'][bed['arrangements'][0]['id']] == []
    assert not collection(result)  # no serialized compatibility authority


@pytest.mark.parametrize('alignment', [None, {}, {OWNER: dict(confidence=.9, source_offset_s=0)},
                                      {OWNER: dict(confidence=True, source_offset_s=0)},
                                      {OWNER: dict(confidence=1, source_offset_s=5)}])
def test_bed_adoption_alignment_refuses_without_mutation(alignment):
    tl = enrolled()
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError, match='alignment'):
        adopt_music_bed(tl.bundle, [OWNER], alignment=alignment)
    assert tl.bundle == before


def _two_sources():
    tl = tiny()
    source = copy.deepcopy(tl.clip('a-music').data)
    source.update(id='arrangement-two', at=4.)
    tl._internal('A')['clips'].append(source)
    return tl, [OWNER, 'occurrence:occ-A/clip:arrangement-two']


def _alignment(owners):
    return {o: dict(confidence=1, source_offset_s=-4 * i) for i, o in enumerate(owners)}


def _switch(on='6s', duration=4):
    return dict(id='switch-one', on=on, duration=duration, curve='equal-power')


def test_plain_bed_projection_gain_timing_beats_and_media_are_preserved():
    tl, owners = _two_sources()
    next(t for t in tl._internal('A')['tracks'] if t['id'] == 'music')['volume'] = .8  # track fields remain separate
    first = tl.clip('a-music').data
    first.update(volume=.35, speed=1.)
    first['params'] = dict(volume=.8, arbitrary=['keep'])
    first.setdefault('app', {}).setdefault('astrid', {})['beats'] = dict(beats=[0., 1., 2.], time='cue_seconds')
    first['opaque'] = {'media_identity': ['sha256:unchanged', None]}
    before = copy.deepcopy(tl.document())
    after = Checkout(adopt_music_bed(before, owners, switches=[_switch()], alignment=_alignment(owners)))
    bed = adopted(after)
    assert bed['arrangements'][0]['source'] == first
    assert [a['source_offset_s'] for a in bed['arrangements']] == [0., -4.]
    assert bed['arrangements'][0]['gain'] == .35
    assert after._internal('A') == tl._internal('A')
    assert switch_windows(after) == [(4., 8.)]
    assert tl.document() == before


@pytest.mark.parametrize('switches,match', [([_switch('1s')], 'negative'),
    ([_switch('3s')], 'coverage'), ([_switch(duration=-1)], 'positive'),
    ([_switch('"missing word"')], 'ORPHAN'), ([_switch(duration=float('nan'))], 'finite')])
def test_bed_windows_coverage_orphan_atomic_refusal(switches, match):
    tl, owners = _two_sources()
    before = copy.deepcopy(tl.bundle)
    with pytest.raises((MusicBedError, TimelineEditError), match=match):
        adopt_music_bed(tl.bundle, owners, switches=switches, alignment=_alignment(owners))
    assert tl.bundle == before


def test_bed_duplicate_selection_and_generated_collision_refuse_atomically():
    tl = enrolled()
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError, match='duplicate selected'):
        adopt_music_bed(tl.bundle, [OWNER, OWNER])
    assert before == tl.bundle
    tl._internal('A')['clips'].append(copy.deepcopy(next(c for c in tl._internal('A')['clips'] if c['id'] == 'a-music:b')))
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError, match='output|colli'):
        adopt_music_bed(tl.bundle, [OWNER])
    assert before == tl.bundle


def test_older_writer_refuses_adopted_bed_by_capability_even_at_v2():
    from astrid.core.timeline.compatibility import (
        ADOPTED_BED_CAPABILITY,
        TimelineCompatibilityError,
        require_writable_candidate,
    )
    doc = adopt_one(enrolled()).document()
    with pytest.raises(TimelineCompatibilityError, match='capability'):
        require_writable_candidate(doc, capabilities=())
    require_writable_candidate(doc, capabilities=[ADOPTED_BED_CAPABILITY])
    # Pinned heads cannot evade the capability check by deleting the new record.
    doc['base_parent_payload'] = copy.deepcopy(doc['parent'])
    del doc['parent']['config']['app']['astrid']['bed']
    with pytest.raises(TimelineCompatibilityError, match='pinned head.*capability'):
        require_writable_candidate(doc, capabilities=())


def test_alias_edit_regenerates_and_conflicting_source_edit_refuses():
    before = adopt_one(enrolled()).document()
    after = copy.deepcopy(before)
    a = adopted(Checkout(after))['arrangements'][0]
    a['gain'] = .21
    result = Checkout(reconcile_arrangement_aliases(before, after))
    assert sources(result)[OWNER]['source']['volume'] == .21
    assert all(c.data['volume'] == .21 for c in result.clips() if c.track == 'music')
    validate(result)
    a['source']['volume'] = .8
    snapshot = copy.deepcopy(after)
    with pytest.raises(MusicBedError, match='contradictory'):
        reconcile_arrangement_aliases(before, after)
    assert after == snapshot
    with pytest.raises(MusicBedError, match='projections conflict'):
        validate(Checkout(after))


def test_zero_output_adoption_reload_regeneration_and_no_dual_authority():
    from astrid.sdk.timeline.music_beds import project
    tl = enrolled()
    tl.ripple_delete(0, 9)
    assert collection(tl)[OWNER]['outputs'] == []
    adopted_tl = adopt_one(tl)
    restored = Checkout(json.loads(json.dumps(adopted_tl.document())))
    assert sources(restored)[OWNER]['outputs'] == []
    validate(restored)
    sources(restored)[OWNER]['ripple_basis'] = []
    project(restored)
    assert sources(restored)[OWNER]['outputs'] == ['a-music']
    restored.bundle['parent']['config']['app']['astrid']['music_beds'][OWNER] = dict(sources(restored)[OWNER])
    with pytest.raises(MusicBedError, match='duplicate authored music authority'):
        validate(restored)


def test_repeated_occurrence_identity_and_opaque_source_are_isolated():
    tl = pinned_music()
    first = 'occurrence:first/clip:bed'
    source = copy.deepcopy(tl.clip(first).data)
    result = adopt_one(tl, first)
    assert sources(result)[first]['source'] == source
    assert 'occurrence:second/clip:bed' not in sources(result)
    second_row = next(r for r in result.bundle['placements'] if r['occurrence_id'] == 'second')
    assert result._internal(second_row['shot_id'])['clips'] == [source]


def _two_carriers(basis_a, basis_b):
    from astrid.sdk.timeline.music_beds import project
    tl, owners = _two_sources()
    for owner, basis in zip(owners, [basis_a, basis_b]):
        source_id = owner.split('/clip:')[1]
        source = copy.deepcopy(next(c for c in tl._internal('A')['clips'] if c['id'] == source_id))
        collection(tl, create=True)[owner] = dict(owner=owner, source=source, ripple_basis=basis, outputs=[source_id])
    project(tl)
    return tl, owners


def test_multi_carrier_common_basis_is_transferred_once_without_summing():
    basis = [dict(at_s=2., delta_s=1.)]
    tl, owners = _two_carriers(basis, copy.deepcopy(basis))
    before = copy.deepcopy(tl._internal('A')['clips'])
    result = Checkout(adopt_music_bed(tl.document(), owners, switches=[_switch()], alignment=_alignment(owners)))
    bed = adopted(result)
    assert bed['ripple_basis'] == basis
    assert result._internal('A')['clips'] == before
    assert all(v['ripple_basis'] is bed['ripple_basis'] for v in sources(result).values())
    assert not collection(result)


def test_incompatible_known_multi_carrier_mapping_refuses_atomically():
    tl, owners = _two_carriers([dict(at_s=6., delta_s=1.)], [dict(at_s=6., delta_s=-1.)])
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError, match='incompatible ripple mappings'):
        adopt_music_bed(tl.bundle, owners, switches=[_switch()], alignment=_alignment(owners))
    assert tl.bundle == before


def test_switches_three_active_arrangements_and_uncovered_spans_refuse():
    tl, owners = _two_sources()
    third = copy.deepcopy(tl.clip('a-music').data)
    third.update(id='third', at=6.)
    tl._internal('A')['clips'].append(third)
    owners.append('occurrence:occ-A/clip:third')
    evidence = _alignment(owners)
    evidence[owners[-1]]['source_offset_s'] = -6.
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError, match='three active|three simultaneously|coverage'):
        adopt_music_bed(tl.bundle, owners, switches=[_switch(), dict(_switch('8s', 4), id='switch-two')], alignment=evidence)
    assert before == tl.bundle


def test_plain_unadopted_compilation_remains_byte_identical():
    from astrid.sdk.timeline.compiler import compile_timing
    tl = tiny()
    tl.resolve()
    doc = tl.document()
    assert json.dumps(compile_timing(doc).bundle, sort_keys=True) == json.dumps(doc, sort_keys=True)
    assert adopted(tl) is None


def test_segmented_output_cannot_fabricate_a_missing_source():
    tl = enrolled()
    collection(tl).clear()  # simulate a lost carrier; generated output remains
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError, match='segmented output'):
        adopt_music_bed(tl.bundle, [OWNER], alignment=_alignment([OWNER]))
    assert tl.bundle == before


def test_time_reference_resolves_moves_orphans_and_requires_explicit_rehome():
    tl, owners = _two_sources()
    words = next(c for c in tl._internal('A')['clips'] if c['id'] == 'a-vo')['app']['astrid']['words']
    # Actual narration-word API, with a source-covered centered switch.
    words.append([5.9, 6.1, 'SwitchWord'])
    switch = _switch('"SwitchWord" in s1 #1', 2.)
    result = Checkout(adopt_music_bed(tl.document(), owners, switches=[switch], alignment=_alignment(owners)))
    assert switch_windows(result) == [(5.4, 7.4)]
    vo = next(c for c in result._internal('A')['clips'] if c['id'] == 'a-vo')
    vo['app']['astrid']['words'][-1] = [6.2, 6.4, 'SwitchWord']
    result._mcache = None
    assert switch_windows(result) == [(5.7, 7.7)]
    vo['app']['astrid']['words'][-1] = [6.2, 6.4, 'NearbyWord']
    result._mcache = None
    with pytest.raises((MusicBedError, TimelineEditError), match='ORPHAN|not spoken'):
        switch_windows(result)
    # Explicit existing TimeRef grammar; no nearest-word fallback.
    adopted(result)['switches'][0]['on'] = '"NearbyWord" in s1 #1'
    assert switch_windows(result) == [(5.7, 7.7)]


def test_adopt_cli_preflight_and_writer_capability_refusal(tmp_path, capsys):
    from astrid.packs.timeline.cli import build_parser
    tl = enrolled()
    path = tmp_path / 'source.json'
    # Write through the document format read by the actual Checkout.load API.
    tl.save(path, quiet=True)
    evidence = tmp_path / 'alignment.json'
    evidence.write_text(json.dumps(_alignment([OWNER])))
    args = ['adopt', 'music-bed', '--file', str(path), '--source', OWNER, '--alignment', str(evidence)]
    parser = build_parser(object())
    original = path.read_bytes()
    parsed = parser.parse_args(args + ['--dry-run'])
    assert parsed.handler(parsed) == 0
    assert 'dry_run' in capsys.readouterr().out
    assert path.read_bytes() == original
    parsed = parser.parse_args(args)
    assert parsed.handler(parsed) == 2
    assert 'cannot edit adopted music beds' in capsys.readouterr().err
    assert path.read_bytes() == original


def test_source_bounds_negative_or_outside_checked_media_refuse():
    tl = tiny()
    tl.clip('a-music').data['to'] = -1.
    with pytest.raises(MusicBedError, match='negative source'):
        adopt_one(tl)
    tl = tiny()
    tl._registry('A')['Q']['duration'] = 2.
    with pytest.raises(MusicBedError, match='media coverage'):
        adopt_one(tl)


def test_unambiguous_source_timing_expression_is_resolved_before_alignment():
    tl = tiny()
    tl.clip('a-music').data.setdefault('app', {}).setdefault('astrid', {})['on'] = '"It" in s1 #1'
    result = adopt_one(tl)
    source = sources(result)[OWNER]['source']
    assert source['app']['astrid']['on'] == '"It" in s1 #1'
    assert source['at'] == .6
    assert adopted(result)['arrangements'][0]['source_offset_s'] == 0.
    from astrid.sdk.timeline.compiler import compile_timing
    assert compile_timing(result.document()).bundle == result.document()


def test_checked_rate_offset_algebra_and_stable_reference_survive_reorder():
    tl, owners = _two_sources()
    second = tl.clip('arrangement-two').data
    second.update(speed=2., **{'from': 8., 'to': 16.})
    evidence = _alignment(owners)
    evidence[owners[1]]['source_offset_s'] = 0.
    result = Checkout(adopt_music_bed(tl.document(), owners, switches=[_switch()], alignment=evidence))
    bed = adopted(result)
    assert bed['arrangements'][1]['source_offset_s'] == 8. - 2. * (4. - 0.)
    offsets = {a['id']: a['source_offset_s'] for a in bed['arrangements']}
    bed['arrangements'].reverse()
    from astrid.sdk.timeline.bed_adoption import refresh_projections
    refresh_projections(result)
    assert bed['owner'] == owners[0]
    assert offsets == {a['id']: a['source_offset_s'] for a in bed['arrangements']}
    validate(result)


def test_reconciliation_does_not_discard_generated_output_edits():
    before = adopt_one(enrolled()).document()
    after = copy.deepcopy(before)
    next(c for c in after['shots']['A']['internal_timeline']['clips'] if c['id'] == 'a-music:b')['volume'] = .123
    snapshot = copy.deepcopy(after)
    with pytest.raises(MusicBedError, match='read-only'):
        reconcile_arrangement_aliases(before, after)
    assert after == snapshot


def test_actual_clip_source_edit_hook_is_gated_and_regenerates_when_capable(monkeypatch):
    from astrid.core.timeline import compatibility
    tl = adopt_one(enrolled())
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='capability'):
        tl.clip(OWNER).set_look(volume=.2)
    assert tl.bundle == before
    # Explicitly model the arrangement-aware caller; the current default stays
    # closed until D joins semantic recording/replay, which this test cannot prove.
    monkeypatch.setattr(compatibility, 'WRITER_CAPABILITIES', {compatibility.ADOPTED_BED_CAPABILITY})
    tl.clip(OWNER).set_look(volume=.2)
    assert adopted(tl)['arrangements'][0]['gain'] == .2
    assert sources(tl)[OWNER]['source']['volume'] == .2
    assert not collection(tl)
    assert all(c.data['volume'] == .2 for c in tl.clips() if c.track == 'music')
    tl._registry('A')['Q']['duration'] = 20.
    tl.clip(OWNER).trim(.5, 7.)
    assert sources(tl)[OWNER]['source']['from'] == .5
    assert adopted(tl)['arrangements'][0]['source_offset_s'] == .5
    validate(tl)


def test_occurrence_copy_uses_logical_adopted_source_without_second_authority():
    from astrid.sdk.timeline_editing import duplicate_authoring_shot
    tl = adopt_one(pinned_music(), 'occurrence:first/clip:bed')
    row = next(r for r in tl.bundle['placements'] if r['occurrence_id'] == 'first')
    duplicate_authoring_shot(tl.bundle, row['shot_id'], new_shot_id='third-shot',
                             occurrence_id='third', start_ms=16000)
    validate(tl)
    assert 'occurrence:first/clip:bed' not in collection(tl)
    assert 'occurrence:third/clip:bed' in collection(tl)
    assert sources(tl)['occurrence:third/clip:bed']['source'] == sources(tl)['occurrence:first/clip:bed']['source']


def test_explicit_common_basis_preserves_disjoint_known_mapping_constraints():
    basis_a, basis_b = [dict(at_s=2., delta_s=1.)], [dict(at_s=10., delta_s=1.)]
    tl, owners = _two_carriers(basis_a, basis_b)
    before = copy.deepcopy(tl._internal('A')['clips'])
    shared = [dict(at_s=2., delta_s=1.), dict(at_s=10., delta_s=1.)]
    result = Checkout(adopt_music_bed(tl.document(), owners, switches=[_switch()],
                                    alignment=_alignment(owners), common_basis=shared))
    assert adopted(result)['ripple_basis'] == shared
    assert result._internal('A')['clips'] == before
    validate(result)
    with pytest.raises(MusicBedError, match='single-carrier.*exactly'):
        adopt_music_bed(enrolled().document(), [OWNER], alignment=_alignment([OWNER]), common_basis=[])


def test_switch_coverage_uses_actual_ripple_mapping_instead_of_unrippled_bounds():
    basis = [dict(at_s=6., delta_s=-4.)]
    tl, owners = _two_carriers(basis, copy.deepcopy(basis))
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(MusicBedError, match='source coverage'):
        adopt_music_bed(tl.bundle, owners, switches=[_switch()], alignment=_alignment(owners))
    assert tl.bundle == before


def test_adoption_refuses_unknown_version_without_downgrading_it():
    from astrid.core.timeline.compatibility import TimelineCompatibilityError
    tl = enrolled()
    tl.bundle['parent']['config']['app']['astrid']['v'] = 99
    before = copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineCompatibilityError, match='music_beds requires app.astrid.v=2'):
        adopt_one(tl)
    assert tl.bundle == before
