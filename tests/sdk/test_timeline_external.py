"""Disposable exact-document proofs for sheet and external import boundaries."""
import copy
import json
from pathlib import Path

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline import commands, journal, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.external import AmbiguousImport, find_external, import_external
from astrid.sdk.timeline.rebase import rebase
from astrid.sdk.timeline.revisions import Revision
from astrid.sdk.timeline_sheet import apply_sheet, render_sheet, sheet_commands

ROOT = Path(__file__).parents[1] / 'fixtures/timeline_editing'


def document():
    return Checkout(json.loads((ROOT / 'tiny_publishable.json').read_text())).document()


def records(path):
    return journal.read(path.with_suffix(path.suffix + '.journal.jsonl'))


def edit(tl, **values):
    return commands.run(tl, dict(op='clip.set', select='a-rocket', args=values))


def test_external_checkpoint_retains_original_provenance_and_rebases_pending(tmp_path, monkeypatch):
    base = document()
    tl = Checkout(copy.deepcopy(base))
    pending = edit(tl, x=240).calls
    tl.save(tmp_path / 'draft.json', quiet=True)
    other = Checkout(copy.deepcopy(base))
    edit(other, y=48)
    external = other.document()
    external['base_parent']['revision_id'] = 'original-external-id'
    revision = Revision('original-external-id', 'app', '2026-10-15T00:00:00Z', 'rev-0',
                        'outside', provenance={'source': {'via': 'sheet-elsewhere'}, 'opaque': [None, 7]})
    monkeypatch.setattr(store, 'three_way', lambda *a, **k: pytest.fail('pending document merge'))
    plan = import_external(tl, base, external, revision, pending=pending)
    assert [call.op for call in plan.calls] == ['timeline.import-external', 'clip.set']
    assert tl.base_revision == revision.id
    assert (tl.clip('a-rocket').get('x'), tl.clip('a-rocket').get('y')) == (240, 48)
    rec = records(tl.path)[-1]
    assert rec.calls[0]['args']['provenance']['revision']['provenance'] == revision.provenance
    assert rec.calls[0]['args']['provenance']['revision_id'] == revision.id
    assert len([r for r in records(tl.path) if any(c['op'] == 'timeline.import-external' for c in r.calls)]) == 1
    assert Checkout.load(tl.path).document() == tl.document()


def test_pending_conflict_preserves_draft_and_disk(tmp_path):
    base = document()
    tl, other = Checkout(copy.deepcopy(base)), Checkout(copy.deepcopy(base))
    pending = edit(tl, x=240).calls
    edit(other, x=180)
    tl.save(tmp_path / 'draft.json', quiet=True)
    state, text, history = tl.document(), tl.path.read_bytes(), list(store._history_dir(tl.path).glob('*.json'))
    with pytest.raises(commands.CommandError, match='same-property'):
        import_external(tl, base, other.document(), Revision('external', 'app', None, 'rev-0', None), pending=pending)
    assert tl.document() == state and tl.path.read_bytes() == text
    assert list(store._history_dir(tl.path).glob('*.json')) == history


def test_unrecorded_local_edits_are_not_silently_replaced():
    base = document()
    tl = Checkout(copy.deepcopy(base))
    edit(tl, x=240)
    with pytest.raises(commands.CommandError, match='pending edits'):
        import_external(tl, base, base, Revision('external', 'app', None, 'rev-0', None))


def test_hand_edited_draft_load_is_one_durable_import_and_undo(tmp_path):
    tl = Checkout(document())
    tl.save(tmp_path / 'draft.json', quiet=True)
    before = tl.document()
    raw = json.loads(tl.path.read_text())
    raw['shots']['A']['internal_timeline']['clips'][1]['params']['x'] = 31
    tl.path.write_text(json.dumps(raw))
    loaded = Checkout.load(tl.path)
    assert loaded.clip('a-rocket').params['x'] == 31
    imported = records(tl.path)
    assert len(imported) == 1 and len(imported[0].calls) == 1
    call = imported[0].calls[0]
    assert call['op'] == 'timeline.import-external'
    assert call['args']['provenance']['adapter'] == 'hand-edited-draft'
    assert call['args']['provenance']['revision_id'] == 'rev-0'
    assert Checkout.load(tl.path).document() == loaded.document()
    assert records(tl.path) == imported
    loaded.undo()
    assert Checkout.load(tl.path).document() == before


def test_hand_edit_of_timeline_helper_is_imported_using_existing_absorption(tmp_path):
    tl = Checkout(document())
    # A literal clip has no relation to guess. Its helper edit retains its end.
    clip = tl.clip('b-type')
    intent.set_for(clip.data, None)
    tl.save(tmp_path / 'draft.json', quiet=True)
    raw = json.loads(tl.path.read_text())
    stamp = raw['shots']['B']['internal_timeline']['clips'][1]['_timeline']
    stamp['start'] += .1
    stamp['end'] += .1
    tl.path.write_text(json.dumps(raw))
    loaded = Checkout.load(tl.path)
    assert loaded.clip('b-type').start == pytest.approx(clip.start + .1)
    assert records(tl.path)[-1].calls[0]['op'] == 'timeline.import-external'


def test_hand_relation_edit_refuses_without_touching_file_or_journal(tmp_path):
    tl = Checkout(document())
    tl.save(tmp_path / 'draft.json', quiet=True)
    raw = json.loads(tl.path.read_text())
    raw['shots']['A']['internal_timeline']['clips'][1]['_timeline']['start'] += .5
    tl.path.write_text(json.dumps(raw))
    text = tl.path.read_bytes()
    with pytest.raises(AmbiguousImport, match='Next:.*offset'):
        Checkout.load(tl.path)
    assert tl.path.read_bytes() == text and records(tl.path) == []


def test_unchanged_sheet_emits_zero_commands_and_moves_no_head_or_journal(tmp_path):
    tl = Checkout(document())
    tl.resolve()
    tl.save(tmp_path / 'draft.json', quiet=True)
    before, text = tl.document(), tl.path.read_bytes()
    sheet = render_sheet(tl)
    assert sheet_commands(tl, sheet) == ()
    assert apply_sheet(tl, sheet) == []
    assert tl.document() == before and tl.path.read_bytes() == text
    assert tl._pending_calls == [] and records(tl.path) == []


def test_sheet_edit_has_executable_commands_and_replays_on_its_exact_base():
    tl = Checkout(document())
    tl.resolve()
    base = tl.document()
    text = render_sheet(tl).replace('x=60', 'x=120').replace('"Live."', '"Live!"')
    calls = sheet_commands(tl, text)
    assert calls and all(c.expect is not None and c.authorship is not None for c in calls)
    assert tl.document() == base
    applied = apply_sheet(tl, text)
    result = rebase(calls, base)
    assert applied and not result.conflicts
    assert result.document == tl.document()


def test_find_external_requires_commit_evidence_not_revision_prefix():
    rows = [Revision('authoring-parent-revision-external', 'astrid', None, 'base', None),
            Revision('semantic', 'app', None, 'authoring-parent-revision-external', None,
                     evidence={'commands': [], 'digests': {'before': 'a', 'after': 'b'}, 'toolchain': {}}),
            Revision('head', 'app', None, 'semantic', None)]
    assert [r.id for r in find_external('base', 'head', rows)] == ['authoring-parent-revision-external', 'head']
    assert [r.id for r in find_external('base', 'head', rows, imported=['head'])] == ['authoring-parent-revision-external']
    with pytest.raises(Exception, match='ancestry'):
        find_external('missing', 'head', rows)


@pytest.mark.parametrize('path', sorted((ROOT / 'external_ambiguities').glob('*.json')), ids=lambda p: p.stem)
def test_relation_ambiguity_fixture_is_refused_with_next_step(path):
    fixture = json.loads(path.read_text())
    base, external = fixture['base'], fixture['external']
    originals = copy.deepcopy((base, external))
    tl = Checkout(copy.deepcopy(base))
    with pytest.raises(AmbiguousImport) as exc:
        import_external(tl, base, external, Revision(fixture['revision_id'], 'app', None, 'rev-0', None))
    assert exc.value.entities and 'Next:' in str(exc.value)
    assert fixture['next_step_contains'] in exc.value.next_step
    assert (base, external) == originals and tl.document() == base


def test_legacy_hand_edit_imports_against_exact_pinned_base(tmp_path):
    tl = Checkout(document())
    # A legacy file has pinned provenance but no new saved-snapshot helper.
    raw = tl._annotated()
    raw['shots']['A']['internal_timeline']['clips'][1]['params']['x'] = 31
    path = tmp_path / 'legacy.json'
    path.write_text(json.dumps(raw))
    loaded = Checkout.load(path)
    assert loaded.clip('a-rocket').params['x'] == 31
    assert len(records(path)) == 1 and records(path)[0].calls[0]['op'] == 'timeline.import-external'
    assert Checkout.load(path).document() == loaded.document()
    assert len(records(path)) == 1


def test_legacy_runner_save_bypasses_import_using_its_verified_digest(tmp_path):
    tl = Checkout(document())
    tl.save(tmp_path / 'legacy.json', quiet=True)
    with tl.edit('literal', quiet=True):
        edit(tl, x=240)
    (store._history_dir(tl.path) / '.saved' / 'current.json').unlink()
    before = records(tl.path)
    assert Checkout.load(tl.path).document() == tl.document()
    assert records(tl.path) == before


def test_revision_only_external_checkpoint_is_not_lost_as_a_noop(tmp_path):
    base = document()
    external = copy.deepcopy(base)
    external['base_parent']['revision_id'] = 'outside-metadata-only'
    tl = Checkout(copy.deepcopy(base))
    tl.save(tmp_path / 'draft.json', quiet=True)
    plan = import_external(tl, base, external, Revision('outside-metadata-only', 'other', None, 'rev-0', 'metadata'))
    assert plan.changed and len(plan.calls) == 1
    assert records(tl.path)[-1].calls[0]['op'] == 'timeline.import-external'
    assert tl.document() == external


def test_external_checkpoint_requires_matching_original_revision_id():
    base = document()
    tl = Checkout(copy.deepcopy(base))
    with pytest.raises(commands.CommandError, match='original document'):
        import_external(tl, base, base, Revision('wrong-revision', 'app', None, 'rev-0', None))
    assert tl.document() == base


def test_imported_checkpoint_replay_is_guarded_by_exact_document_digest():
    base = document()
    external = copy.deepcopy(base)
    external['base_parent']['revision_id'] = 'outside'
    tl = Checkout(copy.deepcopy(base))
    plan = import_external(tl, base, external, Revision('outside', 'app', None, 'rev-0', None))
    assert rebase(plan.calls, base).document == external
    changed = Checkout(copy.deepcopy(base))
    edit(changed, x=240)
    result = rebase(plan.calls, changed.document())
    assert result.conflicts and 'base changed' in result.conflicts[0]['ours']


def test_repeated_publications_are_not_collapsed_into_a_guessed_revision():
    rows = [Revision('same', 'app', None, 'base', None, publication_id='p1'),
            Revision('same', 'restore', None, 'other', None, publication_id='p2')]
    with pytest.raises(Exception, match='publication ancestry'):
        find_external('base', 'same', rows)
