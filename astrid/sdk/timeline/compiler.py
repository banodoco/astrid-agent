"""Pure timing projection of the canonical authoring bundle (first M2 slice).

``compile_timing(TimingInputs(bundle, sources=rows, bindings=pins))`` returns an
owned, unchanged authored bundle plus derived half-open intervals keyed by
``(occurrence_id, shot_id, clip_id)``; parent clips use ``('parent', '', id)``.
Intervals contain exact Fraction seconds and a separately quantized frame view.
No declaration, source window, explicit clear, command, or opaque field is rewritten.

Captured-input adapter still required before provider integration: capture authorized
Runtime VerifiedDocuments at EXACT document_id/version/content_digest, and record a
WordBinding per on/until declaration, including the selected source clip/occurrence
and stable v1 word IDs. Existing source_mapping.pinned_documents can supply these
VerifiedDocuments; the adapter must still capture the word/occurrence binding.
A phrase binds all its consecutive IDs. A changed declaration needs a new binding.
Missing/revoked inputs must be omitted, never fetched as latest; authority and exact
historical reads belong to the caller. The native authored-reference adapter must derive these bindings from the
revision itself, never from an echo or accepted history. These records are consumed
inputs, not a new stored authoring format.
Inputs snapshot their JSON at construction, including captured documents.

Supported: fixed clips (including fixed_start), on/until/for, main word/cut/end/offset
Moments, the existing numeric Main beat/downbeat grids, linear source windows/rates,
occurrence isolation and rational fps. Explicit time maps, sequence fits, formulas,
orphan caches, until_film_end bases, generic markers and authored reflow operations
are refused. Nontrivial placement source maps and clip millisecond codecs require an
explicit main authoring adapter first.
The graph walk comes from R4's compiler; Moment/cut rules and exact-time arithmetic
come from main. This bounded slice targets <=600 product lines across two modules.

This is NOT yet the only derived writer. Checkout.retime/resolve/reflow, model timing
setters, narration/music/sequence writers and their consumers still require migration
and parity before integration can claim one compiler. Reflow is untouched here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from astrid.sdk.timeline._common import TimelineEditError

Owner = tuple[str, str, str]


class TimingCompileError(TimelineEditError):
    """The entire projection was refused; no partial result is returned."""


class TimingCycleError(TimingCompileError):
    code = "CYCLE"

    def __init__(self, path):
        self.path = tuple(path)
        super().__init__("CYCLE: " + " -> ".join(map(str, self.path)))


@dataclass(frozen=True)
class WordBinding:
    target: Owner
    field: str
    declaration: str
    source: Owner
    document_id: str
    version: int
    content_digest: str
    word_ids: tuple[str, ...]

    def __post_init__(self):
        for name in ("target", "source", "word_ids"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if (len(self.target) != 3 or len(self.source) != 3 or not self.word_ids
                or self.field not in ("on", "until") or isinstance(self.version, bool)
                or not isinstance(self.version, int) or self.version < 1
                or not isinstance(self.declaration, str) or not isinstance(self.document_id, str)
                or not isinstance(self.content_digest, str)
                or any(not isinstance(x, str) for x in (*self.target, *self.source))
                or any(not isinstance(x, str) or not x for x in self.word_ids)):
            raise TimingCompileError("invalid captured word binding")


@dataclass(frozen=True, init=False)
class TimingInputs:
    _bundle: str = field(repr=False)
    _sources: str = field(repr=False)
    bindings: tuple[WordBinding, ...]
    legacy_declarations: tuple[tuple[tuple[str, str, str], str, str], ...]
    policy: str

    def __init__(self, bundle: Mapping[str, Any], *, sources=(), bindings=(),
                 legacy_declarations=(), policy="nearest"):
        if policy not in ("nearest", "floor", "ceil"):
            raise TimingCompileError("invalid frame projection policy")
        object.__setattr__(self, "_bundle", json.dumps(bundle, allow_nan=False))
        object.__setattr__(self, "_sources", json.dumps(list(sources), allow_nan=False))
        pins = tuple(bindings)
        if any(not isinstance(pin, WordBinding) for pin in pins):
            raise TimingCompileError("bindings must be captured WordBinding values")
        object.__setattr__(self, "bindings", pins)
        legacy = tuple(legacy_declarations)
        if any(
            not isinstance(row, tuple)
            or len(row) != 3
            or not isinstance(row[0], tuple)
            or len(row[0]) != 3
            or any(not isinstance(value, str) for value in (*row[0], row[1], row[2]))
            for row in legacy
        ):
            raise TimingCompileError("invalid legacy timing declaration")
        object.__setattr__(self, "legacy_declarations", legacy)
        object.__setattr__(self, "policy", policy)

    @property
    def bundle(self):
        return json.loads(self._bundle)

    @property
    def sources(self):
        return json.loads(self._sources)


@dataclass(frozen=True)
class TimingResult:
    bundle: dict[str, Any]
    timings: Mapping[Owner, Mapping[str, Any]]


def compile_timing(inputs: TimingInputs) -> TimingResult:
    """Compile all supported declarations atomically, without IO or authored edits."""
    from astrid.sdk.timeline._timing_evaluators import evaluate

    if not isinstance(inputs, TimingInputs):
        raise TypeError("compile_timing requires captured TimingInputs")
    bundle = inputs.bundle
    try:
        timings = evaluate(bundle, inputs.sources, inputs.bindings, inputs.policy,
                           inputs.legacy_declarations)
    except TimingCompileError:
        raise
    except (ValueError, TypeError, KeyError, ZeroDivisionError, OverflowError) as exc:
        raise TimingCompileError(f"invalid timing input: {exc}") from exc
    return TimingResult(bundle, MappingProxyType(timings))
