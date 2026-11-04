"""Edit a timeline in one obvious step.

    from astrid.sdk.timeline_checkout import Checkout

    tl = Checkout.open("almost-ready", "01M4GGKMHZ3J8V402C8CWSCE95")   # or Checkout.load("film.json")
    tl.clip("ROCKET").enter_at("viral")          # a clip by asset, id, element or text; a word by text or id
    tl.close_gap(before="Live.")                 # ripple: everything after moves, VO/music/SFX stay in sync
    print(tl.check())                            # validate + what moved (timeline seconds) + lint on the changed cuts
    tl.publish("rocket on 'viral'")

Units, once: every time this API takes or returns is TIMELINE SECONDS (the numbers
``timelines show``, ``visualize --at`` and ``lint`` print), unless the argument is
called ``frames``. The document stores shot-relative seconds (clip ``at``) and
milliseconds (placements); the API converts, so you never do.

Intent: moving a clip *to a word* stores an anchor ``{word, text, offset_s}`` on
the clip. ``retime()`` (or a VO take swap) moves every anchored clip back onto its
word, so a re-recorded line never strands its overlays. Moving a clip to a plain
time drops its anchor; ``nudge`` keeps it and changes its offset.

Addresses are the ones the CLI prints: cut numbers (``tl.cut(17)``, the same
numbering as show, lint and visualize), clip ids or their prefix (``c22-02``),
asset keys (``ROCKET``), element ids (``am-tweet``), on-screen text, and words
(``"viral"``, ``"n20b:20"``, ``tl.word("tool", n=2)``).

The code lives in astrid/sdk/timeline/; this module is the stable import path.
"""
from __future__ import annotations

import contextlib  # noqa: F401
import copy  # noqa: F401
import dataclasses  # noqa: F401
import difflib  # noqa: F401
import json  # noqa: F401
import math  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
import uuid  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Iterable, Mapping, Sequence  # noqa: F401

from astrid.core.timeline import moments as mo  # noqa: F401
from astrid.sdk import timeline_intent as intent  # noqa: F401
from astrid.core.timeline.cuts import (
    bundle_fps,  # noqa: F401
    clip_duration,  # noqa: F401
    occurrences_from_bundle,  # noqa: F401
    picture_cuts,  # noqa: F401
)

__all__ = [
    "Checkout", "Clip", "Cut", "Word", "Voice", "TimelineEditError", "EDITABLE_NOTE",
    "draft_path", "drafts_root", "find_draft", "resolve_ids",
]

from astrid.sdk.timeline._common import BASE_KEYS  # noqa: F401
from astrid.sdk.timeline._common import SHOT_BASE_PREFIX  # noqa: F401
from astrid.sdk.timeline._common import DERIVED_PREFIX  # noqa: F401
from astrid.sdk.timeline._common import EDITABLE_NOTE  # noqa: F401
from astrid.sdk.timeline._common import ELEMENT_TRACK  # noqa: F401
from astrid.sdk.timeline._common import AUDIO_KINDS  # noqa: F401
from astrid.sdk.timeline._common import TIME_RE  # noqa: F401
from astrid.sdk.timeline._common import WORD_ID_RE  # noqa: F401
from astrid.sdk.timeline._common import CANVAS  # noqa: F401
from astrid.sdk.timeline._common import SAFE_MARGIN  # noqa: F401
from astrid.sdk.timeline._common import LOGICAL_PX  # noqa: F401
from astrid.sdk.timeline._common import POSITION_KEYS  # noqa: F401
from astrid.sdk.timeline.units import element_units  # noqa: F401
from astrid.sdk.timeline.units import _unit_path  # noqa: F401
from astrid.sdk.timeline.units import is_logical  # noqa: F401
from astrid.sdk.timeline.units import param_unit  # noqa: F401
from astrid.sdk.timeline.units import _number  # noqa: F401
from astrid.sdk.timeline.units import to_canvas  # noqa: F401
from astrid.sdk.timeline.units import from_canvas  # noqa: F401
from astrid.sdk.timeline.units import _FORMULA_VALUE  # noqa: F401
from astrid.sdk.timeline.units import parse_moment_value  # noqa: F401
from astrid.sdk.timeline.units import parse_formula_value  # noqa: F401
from astrid.sdk.timeline.units import formula_from_spec  # noqa: F401
from astrid.sdk.timeline.units import mark_offset_canvas  # noqa: F401
from astrid.sdk.timeline.units import formula_short  # noqa: F401
from astrid.sdk.timeline._common import TimelineEditError  # noqa: F401
from astrid.sdk.timeline._common import _length_seconds  # noqa: F401
from astrid.sdk.timeline._common import _Suggest  # noqa: F401
from astrid.sdk.timeline._common import _norm  # noqa: F401
from astrid.sdk.timeline._common import _num  # noqa: F401
from astrid.sdk.timeline._common import _r  # noqa: F401
from astrid.sdk.timeline.model import Word  # noqa: F401
from astrid.sdk.timeline.model import _LazyParams  # noqa: F401
from astrid.sdk.timeline.model import Clip  # noqa: F401
from astrid.sdk.timeline.model import Cut  # noqa: F401
from astrid.sdk.timeline.model import StepSequence  # noqa: F401
from astrid.sdk.timeline.model import Voice  # noqa: F401
from astrid.sdk.timeline.model import _MomentContext  # noqa: F401
from astrid.sdk.timeline.model import tl_cache  # noqa: F401
from astrid.sdk.timeline.checkout import Checkout  # noqa: F401
from astrid.sdk.timeline.check import CheckReport  # noqa: F401
from astrid.sdk.timeline.check import _end_rule  # noqa: F401
from astrid.sdk.timeline.check import _text_where_number  # noqa: F401
from astrid.sdk.timeline.check import text_fit_safe_width  # noqa: F401
from astrid.sdk.timeline.check import _span_frames  # noqa: F401
from astrid.sdk.timeline.check import _lint_key  # noqa: F401
from astrid.sdk.timeline.check import _new_findings  # noqa: F401
from astrid.sdk.timeline.check import describe_changes  # noqa: F401
from astrid.sdk.timeline.doc import _post  # noqa: F401
from astrid.sdk.timeline.doc import _first_word_lead  # noqa: F401
from astrid.sdk.timeline.doc import _row_start  # noqa: F401
from astrid.sdk.timeline.doc import _set_row_start  # noqa: F401
from astrid.sdk.timeline.doc import _set_length  # noqa: F401
from astrid.sdk.timeline.doc import _trim_end  # noqa: F401
from astrid.sdk.timeline.doc import _trim_front  # noqa: F401
from astrid.sdk.timeline.doc import _filter_words  # noqa: F401
from astrid.sdk.timeline.doc import _split_audio  # noqa: F401
from astrid.sdk.timeline.doc import _read_words  # noqa: F401
from astrid.sdk.timeline.doc import _PATH_RE  # noqa: F401
from astrid.sdk.timeline.doc import _get_path  # noqa: F401
from astrid.sdk.timeline.doc import _set_path  # noqa: F401
from astrid.sdk.timeline.doc import _strip_derived  # noqa: F401
from astrid.sdk.timeline.doc import _serialize  # noqa: F401
from astrid.sdk.timeline.store import drafts_root  # noqa: F401
from astrid.sdk.timeline.doc import _clock  # noqa: F401
from astrid.sdk.timeline.doc import _free_key  # noqa: F401
from astrid.sdk.timeline.store import resolve_handle_entry  # noqa: F401
from astrid.sdk.timeline.store import _cli_running  # noqa: F401
from astrid.sdk.timeline.store import _with_client  # noqa: F401
from astrid.sdk.timeline.store import load_beats  # noqa: F401
from astrid.sdk.timeline.store import handle_beats  # noqa: F401
from astrid.sdk.timeline.store import file_beats  # noqa: F401
from astrid.sdk.timeline.store import media_seconds  # noqa: F401
from astrid.sdk.timeline.store import _snapshot  # noqa: F401
from astrid.sdk.timeline.store import _draft_text  # noqa: F401
from astrid.sdk.timeline.store import _stage_step  # noqa: F401
from astrid.sdk.timeline.store import _write_step  # noqa: F401
from astrid.sdk.timeline.store import _discard_half_written  # noqa: F401
from astrid.sdk.timeline.store import _read_step  # noqa: F401
from astrid.sdk.timeline.store import _redo_dir  # noqa: F401
from astrid.sdk.timeline.store import _journaled  # noqa: F401
from astrid.sdk.timeline.store import LOCK_WAIT_ENV  # noqa: F401
from astrid.sdk.timeline.store import LOCK_WAIT_SECONDS  # noqa: F401
from astrid.sdk.timeline.store import _lock_wait  # noqa: F401
from astrid.sdk.timeline.store import _lock_holder  # noqa: F401
from astrid.sdk.timeline.store import _file_lock  # noqa: F401
from astrid.sdk.timeline.store import _acquire_lock  # noqa: F401
from astrid.sdk.timeline.store import _is_draft  # noqa: F401
from astrid.sdk.timeline.store import _history_dir  # noqa: F401
from astrid.sdk.timeline.store import draft_path  # noqa: F401
from astrid.sdk.timeline.store import resolve_ids  # noqa: F401
from astrid.sdk.timeline.store import find_draft  # noqa: F401
from astrid.sdk.timeline.store import current_draft  # noqa: F401
from astrid.sdk.timeline.store import set_current_draft  # noqa: F401
from astrid.sdk.timeline.store import fetch_bundle  # noqa: F401
from astrid.sdk.timeline.store import _units  # noqa: F401
from astrid.sdk.timeline.store import import_media  # noqa: F401

from astrid.sdk.timeline import store as _store  # noqa: F401
import sys as _sys, types as _types
_STORE_FUNCTIONS = frozenset(n for n, v in vars(_store).items()
                             if callable(v) and getattr(v, "__module__", None) == _store.__name__)

class _Facade(_types.ModuleType):
    """Setting a store function here (a test's monkeypatch) sets it where the code calls it."""
    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name in _STORE_FUNCTIONS:
            setattr(_store, name, value)

_sys.modules[__name__].__class__ = _Facade   # last line of the facade
