"""The timeline SDK's facade (astrid.sdk.timeline_checkout) after the P0-C2 split.

The name lists are copied from the split map (review/v8/audit/split-map.json) so this test
does not read the audit folder. The map is the spec; if it changes, copy it here again.
"""
from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

import astrid.sdk.timeline.store as store
from astrid.sdk import timeline_checkout as tc
from astrid.sdk.timeline.checkout import Checkout

PRE_SPLIT_MODULE_NAMES = ['BASE_KEYS', 'SHOT_BASE_PREFIX', 'DERIVED_PREFIX', 'EDITABLE_NOTE', 'ELEMENT_TRACK', 'AUDIO_KINDS',
 'TIME_RE', 'WORD_ID_RE', 'CANVAS', 'SAFE_MARGIN', 'LOGICAL_PX', 'POSITION_KEYS', 'element_units',
 '_unit_path', 'is_logical', 'param_unit', '_number', 'to_canvas', 'from_canvas', '_FORMULA_VALUE',
 'parse_moment_value', 'parse_formula_value', 'formula_from_spec', 'mark_offset_canvas', 'formula_short',
 'TimelineEditError', '_length_seconds', '_Suggest', '_norm', '_num', '_r', 'Word', '_LazyParams', 'Clip',
 'Cut', 'StepSequence', 'Voice', '_MomentContext', 'tl_cache', 'Checkout', 'CheckReport', '_end_rule',
 '_text_where_number', 'text_fit_safe_width', '_span_frames', '_lint_key', '_new_findings',
 'describe_changes', '_post', '_first_word_lead', '_row_start', '_set_row_start', '_set_length', '_trim_end',
 '_trim_front', '_filter_words', '_split_audio', '_read_words', '_PATH_RE', '_get_path', '_set_path',
 '_strip_derived', '_serialize', 'drafts_root', '_clock', '_free_key', 'resolve_handle_entry', '_cli_running',
 '_with_client', 'load_beats', 'handle_beats', 'file_beats', 'media_seconds', '_snapshot', '_draft_text',
 '_stage_step', '_write_step', '_discard_half_written', '_read_step', '_redo_dir', '_journaled',
 'LOCK_WAIT_ENV', 'LOCK_WAIT_SECONDS', '_lock_wait', '_lock_holder', '_file_lock', '_acquire_lock',
 '_is_draft', '_history_dir', 'draft_path', 'resolve_ids', 'find_draft', 'current_draft', 'set_current_draft',
 'fetch_bundle', '_units', 'three_way', 'publish_bundle', 'import_media', 'mo', 'intent', 'bundle_fps',
 'clip_duration', 'occurrences_from_bundle', 'picture_cuts']

# W2: _music_seam is gone (the bed is cut film-wide by _cut_beds, keep_end); it was private, with no caller left.
PRE_SPLIT_CHECKOUT_MEMBERS = ['open', 'load', 'HISTORY', 'SAY_SAVED', 'save', '_save_locked', '_merge_writer', 'undo', 'redo', '_reload',
 'step', 'draft', 'adopt', 'discard', 'base_revision', 'edits', 'changed_cut_ids', 'changes', 'quantize',
 'time', 'frame', 'to_shot_seconds', 'duration', 'cuts', 'cut', 'clips', 'clip', '_matches', 'words', 'word',
 'voice', 'at', 'add', 'add_cut', '_split_moment', '_next_cut_id', '_split_cut', 'set_cut_note', 'cut_note',
 'cut_notes_all', '_mirror_holds', '_text_overflow', 'FULL_FRAME', 'picture_gaps', '_stale_whys',
 '_note_bare_word', 'verify', 'sequence', 'find', 'cut_count', '_cut_on_screen', 'ripple_delete', 'close_gap',
 'insert_time', 'lines', '_pieces', '_mark', '_piece_gaps', 'gaps', 'declare_gaps', 'reflow', 'insert_line',
 'remove_line', 'apply_script', 'retime', '_frame', '_moment_text', '_moment_time', '_resolve_note',
 '_pin_nearest', '_place_on', '_own_cut_span', 'orphans', '_cut_clips', 'cut_picture', '_cut_id', '_no_cut',
 '_cut_spans', '_cut_groups', '_is_picture', '_move_within_shot', '_place', '_fit_sequences', 'resolve',
 '_evaluate', 'slots', 'slot_clips', 'fill_slot', 'fill_standins', 'lint', '_type_mismatches', 'check',
 'publish', '_publish_narration', 'narration', 'narration_changes', 'pin_narration', 'document', '_shot_ids',
 '_row', '_shot_start', '_shot_length', '_shot_id', '_internal', '_track_kind', '_is_audio', '_shot_at',
 '_cut_at', '_as_word', '_pick', '_new_id', '_registry', '_follow_media', '_attach_beats', '_stale_beats',
 '_register_asset', '_corner', '_shift_after', '_shot_sequences', '_relay_steps', '_sequences_across',
 '_close_in_shot', '_open_in_shot', '_is_music', '_shift_parent', '_annotated', '_absorb']

JOURNALED = {'Checkout': ['add', 'ripple_delete', 'close_gap', 'insert_time', 'insert_line', 'remove_line',
              'apply_script', 'fill_slot', 'fill_standins', 'declare_gaps', 'adopt', 'add_cut',
              'set_cut_note'],
 'Clip': ['on', 'until', 'enter_at', 'nudge', 'hold_for', 'set_duration', 'extend', 'end_at', 'set',
          'set_track', 'clear_asset', 'swap_asset', 'set_beats', 'keyframe_at', 'remove', 'remove_layer',
          'keep'],
 'Cut': ['split'],
 'StepSequence': ['fit'],
 'Voice': ['replace', 'set_gap_after']}

MIXINS = ("_LookupMixin", "_EditMixin", "_ReflowMixin", "_CheckMixin", "_PublishMixin", "_Suggest")

PACKAGE = pathlib.Path(importlib.import_module("astrid.sdk.timeline").__file__).parent


def _defined_at_top_level(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


def test_every_pre_split_name_is_importable_from_the_facade():
    missing = [n for n in PRE_SPLIT_MODULE_NAMES if not hasattr(tc, n)]
    assert missing == []


def test_facade_names_are_the_same_objects_as_their_new_homes():
    checked = 0
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "__init__.py":
            continue
        mod = importlib.import_module(f"astrid.sdk.timeline.{path.stem}")
        for name in _defined_at_top_level(path):
            if name in PRE_SPLIT_MODULE_NAMES:
                assert getattr(tc, name) is getattr(mod, name), (path.name, name)
                checked += 1
    # the six names the facade imports itself (mo, intent, bundle_fps, clip_duration, occurrences_from_bundle,
    # picture_cuts) are not defined in the package, so they are not counted here
    assert checked == len(PRE_SPLIT_MODULE_NAMES) - 6


def test_public_api_module_reexports_the_facade_objects():
    from astrid.sdk.timeline import api

    assert api.API_VERSION == 1
    assert api.Checkout is tc.Checkout
    assert api.TimelineEditError is tc.TimelineEditError
    assert api.describe_changes is tc.describe_changes


def test_all_is_unchanged():
    assert "Checkout" in tc.__all__ and "TimelineEditError" in tc.__all__
    assert set(tc.__all__) == {"Checkout", "Clip", "Cut", "Word", "Voice", "TimelineEditError", "EDITABLE_NOTE",
                               "draft_path", "drafts_root", "find_draft", "resolve_ids"}


def test_checkout_keeps_its_members_and_mixins():
    missing = [n for n in PRE_SPLIT_CHECKOUT_MEMBERS if not hasattr(Checkout, n)]
    assert missing == []
    mro_names = [c.__name__ for c in Checkout.__mro__]
    for mixin in MIXINS:
        assert mixin in mro_names, mixin


@pytest.mark.parametrize("owner", sorted(JOURNALED))
def test_journaled_methods_are_still_wrapped(owner):
    cls = {"Checkout": Checkout, "Clip": tc.Clip, "Cut": tc.Cut, "StepSequence": tc.StepSequence, "Voice": tc.Voice}[owner]
    for name in JOURNALED[owner]:
        assert hasattr(getattr(cls, name), "__wrapped__"), f"{owner}.{name} is not journaled"


class _Reached(Exception):
    pass


def test_monkeypatch_on_the_facade_reaches_the_code_that_calls_it(tmp_path, monkeypatch):
    original_fetch = store.fetch_bundle
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(tc, "resolve_ids", lambda project, timeline, client=None: ("p", "t", "rev-0"))
    calls = []

    def fake_fetch(project, timeline, revision_id=None, client=None):
        calls.append((project, timeline))
        raise _Reached()

    monkeypatch.setattr(tc, "fetch_bundle", fake_fetch)
    assert store.fetch_bundle is fake_fetch
    with pytest.raises(_Reached):
        Checkout.draft("P", "t")
    assert calls == [("P", "t")]

    monkeypatch.undo()
    assert tc.fetch_bundle is original_fetch
    assert store.fetch_bundle is original_fetch
