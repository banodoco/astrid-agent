"""Shot duplication after chapter restructuring on a scratch Runtime."""
from astrid.sdk.timeline import Checkout, Timeline
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_duplicate import capture_occurrence_duplicate
from tests.sdk.test_timeline_restructure import restructure_runtime  # noqa: F401


def test_restructured_shot_duplicate_and_arrange(restructure_runtime, tmp_path):
    env = restructure_runtime
    Checkout.open(env.project, 't', client=env.client).publish(
        'Restructure', restructure=True, client=env.client, idempotency_key='shots-restructure')
    bundle = bundle_from_document(env.document())
    source = bundle['placements'][0]
    timeline = Timeline.open(env.project, 't', client=env.client, outbox_path=tmp_path / 'outbox.json')
    command = capture_occurrence_duplicate(
        bundle, 'occurrence:' + source['occurrence_id'], 'copy-occurrence', 'copy-shot')
    move = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
            'args': {'changes': [{'op': 'set', 'ref': 'parent',
                'path': ['occurrences', 'occurrence_id:copy-occurrence', 'placement', 'start_ms'],
                'before': {'state': 'present', 'value': source['placement']['start_ms']}, 'value': 12000}]}}
    timeline.commit([command, move],
                    label='Duplicate shot', key='shots-duplicate')
    timeline.commit(lambda t: t.arrange(['copy-occurrence'], at=16),
                    label='Arrange copy', key='shots-arrange')
    landed = bundle_from_document(env.document())
    copied = next(row for row in landed['placements'] if row['occurrence_id'] == 'copy-occurrence')
    assert copied['shot_id'] == 'copy-shot'
    assert copied['placement']['start_ms'] == 16000
    original_cut = landed['shots'][source['shot_id']]['internal_timeline']['clips'][0]['app']['astrid']['cut']
    copied_cut = landed['shots']['copy-shot']['internal_timeline']['clips'][0]['app']['astrid']['cut']
    assert original_cut != copied_cut


def test_duplicate_retargets_only_local_cut_moments():
    from astrid.sdk.timeline.native_occurrence_duplicate import remap_copy_cuts

    original = {'clips': [
        {'id': 'a', 'app': {'astrid': {'cut': 'c1', 'on': 'c01 +2f', 'until': 'end of c1'}}},
        {'id': 'b', 'app': {'cut': 'c1', 'on': 'c2', 'until': 'beat 2 after c1'}},
        {'id': 'c', 'app': {'astrid': {'on': '"c1"'}}},
    ]}
    result = remap_copy_cuts(original, 'copy-occurrence')
    cut = result['clips'][0]['app']['astrid']['cut']
    assert cut != 'c1'
    assert result['clips'][0]['app']['astrid']['on'] == f'{cut} +2f'
    assert result['clips'][0]['app']['astrid']['until'] == f'end of {cut}'
    assert result['clips'][1]['app']['astrid']['cut'] == cut
    assert result['clips'][1]['app']['astrid']['on'] == 'c2'
    assert result['clips'][1]['app']['astrid']['until'] == f'beat 2 after {cut}'
    assert result['clips'][2] == original['clips'][2]


def test_duplicate_prefers_exact_cut_spelling_and_keeps_ambiguous_alias():
    from astrid.sdk.timeline.native_occurrence_duplicate import remap_copy_cuts

    original = {'clips': [
        {'id': 'a', 'app': {'astrid': {'cut': 'c1', 'on': 'c1'}}},
        {'id': 'b', 'app': {'astrid': {'cut': 'c01'}}},
        {'id': 'c', 'app': {'astrid': {'cut': 'c2', 'on': 'c02'}}},
        {'id': 'd', 'app': {'astrid': {'cut': 'c2', 'until': 'end of c001'}}},
    ]}

    result = remap_copy_cuts(original, 'copy-occurrence')
    clips = {clip['id']: clip for clip in result['clips']}
    exact_c1 = clips['a']['app']['astrid']['cut']
    exact_c01 = clips['b']['app']['astrid']['cut']
    exact_c2 = clips['c']['app']['astrid']['cut']
    assert exact_c1 != exact_c01
    assert clips['a']['app']['astrid']['on'] == exact_c1
    assert clips['c']['app']['astrid']['on'] == exact_c2
    assert clips['d']['app']['astrid']['until'] == 'end of c001'


def test_duplicate_retargets_microsecond_cut_offset_and_compiles():
    from fractions import Fraction

    from astrid.core.timeline import moments
    from astrid.sdk.timeline.compiler import TimingInputs, compile_timing
    from astrid.sdk.timeline.native_occurrence_duplicate import remap_copy_cuts

    internal = {
        'tracks': [{'id': 'plate', 'kind': 'visual'}, {'id': 'type', 'kind': 'visual'}],
        'clips': [
            {'id': 'one', 'track': 'plate', 'at': 0, 'hold': 1,
             'app': {'astrid': {'cut': 'c1'}}},
            {'id': 'two', 'track': 'plate', 'at': 1, 'hold': 1,
             'app': {'astrid': {'cut': 'c2'}}},
            {'id': 'layer', 'track': 'type', 'at': 0, 'hold': 0.5,
             'app': {'astrid': {'on': 'c1 +0.000001s'}}},
        ],
    }
    bundle = {
        'project_id': 'project', 'timeline_id': 'timeline',
        'parent': {'clips': [], 'config': {}},
        'placements': [{'occurrence_id': 'o', 'shot_id': 's',
                        'placement': {'start_ms': 0}, 'duration_ms': 10000}],
        'shots': {'s': {'internal_timeline': internal}},
    }

    assert compile_timing(TimingInputs(bundle)).timings['o', 's', 'layer']
    copied = remap_copy_cuts(internal, 'copy-occurrence')
    copied_bundle = {
        **bundle,
        'placements': [{'occurrence_id': 'copy-occurrence', 'shot_id': 'copy-shot',
                        'placement': {'start_ms': 0}, 'duration_ms': 10000}],
        'shots': {'copy-shot': {'internal_timeline': copied}},
    }
    assert compile_timing(TimingInputs(copied_bundle)).timings['copy-occurrence', 'copy-shot', 'layer']

    moment = moments.parse(copied['clips'][2]['app']['astrid']['on'])
    assert moment.cut != 'c1'
    assert moment.offset_s == 0.000001
    assert Fraction(str(moment.offset_s)) == Fraction(1, 1_000_000)
