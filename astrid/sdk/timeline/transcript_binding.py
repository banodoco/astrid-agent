"""Select source words from an explicitly frozen transcript dependency.

This is a pure compiler-input check, not source admission or a resolver. The
command owner supplies the reference from authenticated accepted history and
the exact CAS bytes already resolved for it. Publication, shared schema/media
extent validation and document-key authority belong to the source producer.
No document, latest-version, network, path or derived word cache is consulted.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from astrid.sdk.managed_transcript import _digest
from astrid.sdk.timeline._common import TimelineEditError

_PINS = {
    "document": ("id", "version", "content_digest"),
    "snapshot": ("object_id", "sha256"),
    "subject": ("media_object_id", "media_sha256"),
    "source": ("producer_id", "source_id", "lineage_id", "revision_id", "producer_digest"),
    "clock": ("kind", "unit", "origin_seconds"),
}


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TimelineEditError(f"transcript {label} must be an object")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TimelineEditError(f"transcript snapshot has duplicate JSON key {key!r}")
        result[key] = value
    return result


def _media_clock(clock):
    if (clock.get("kind") != "media" or clock.get("unit") != "seconds"
            or type(clock.get("origin_seconds")) not in (int, float) or clock["origin_seconds"] != 0):
        raise TimelineEditError("transcript binding requires the media-seconds clock")


def bound_source_words(
    binding: Mapping[str, Any],
    *,
    frozen_reference: Mapping[str, Any],
    snapshot_bytes: bytes,
    occurrence_id: str | None,
) -> tuple[dict[str, Any], ...]:
    """Return detached selected rows in source order, retaining opaque word IDs.

    ``frozen_reference`` carries schema_version and the five immutable pin
    objects in a binding, without occurrence_id/word_ids. It MUST come from the
    accepted command dependency set, not from the caller's proposed binding.
    Equality here cannot authenticate a caller-created reference. A missing
    dependency is a refusal, including during offline replay.

    Rows retain media seconds. Feed them to the existing word projection and
    its source_to_timeline_time conversion, never through a second clock. This
    helper neither mints IDs nor infers correspondence from text/segmentation.

    The caller supplies the selected target's occurrence: explicit null for a
    parent clip, a nonempty ID for a nested occurrence. The binding must carry
    that exact value; an absent field is never an implicit parent selector.
    """
    binding = _object(binding, "binding")
    frozen_reference = _object(frozen_reference, "frozen reference")
    if (type(binding.get("schema_version")) is not int or binding["schema_version"] != 1
            or type(frozen_reference.get("schema_version")) is not int
            or frozen_reference["schema_version"] != 1):
        raise TimelineEditError("transcript binding requires schema_version 1")
    if ("occurrence_id" not in binding
            or (occurrence_id is not None and
                (not isinstance(occurrence_id, str) or not occurrence_id))
            or binding["occurrence_id"] != occurrence_id):
        raise TimelineEditError("transcript binding occurrence does not match its target")
    for name, fields in _PINS.items():
        proposed = _object(binding.get(name), name)
        frozen = _object(frozen_reference.get(name), f"frozen {name}")
        if (any(field not in proposed or field not in frozen for field in fields)
                or proposed != frozen
                or any(type(proposed[field]) is not type(frozen[field]) for field in fields)):
            raise TimelineEditError(f"transcript {name} differs from frozen dependency")
        for field in fields:
            value = proposed[field]
            if name == "clock" and field == "origin_seconds":
                continue
            if name == "document" and field == "version":
                if type(value) is int and value > 0 or isinstance(value, str) and value:
                    continue
            elif isinstance(value, str) and value:
                continue
            raise TimelineEditError(f"transcript {name}.{field} is invalid")
    _media_clock(binding["clock"])
    if not isinstance(snapshot_bytes, bytes):
        raise TimelineEditError("transcript frozen snapshot bytes are required")
    try:
        actual = hashlib.sha256(snapshot_bytes).hexdigest()
        if any(_digest(value, label=label) != actual for label, value in (
                ("snapshot.sha256", binding["snapshot"]["sha256"]),
                ("document.content_digest", binding["document"]["content_digest"]))):
            raise TimelineEditError("transcript frozen snapshot digest mismatch")
        content = _object(json.loads(snapshot_bytes, object_pairs_hook=_unique_object), "snapshot")
    except (ValueError, UnicodeError) as exc:
        raise TimelineEditError(f"invalid transcript frozen snapshot: {exc}") from None
    if type(content.get("schema_version")) is not int or content["schema_version"] != 1:
        raise TimelineEditError("transcript snapshot requires schema_version 1")
    for name in ("subject", "source", "clock"):
        actual = _object(content.get(name), f"snapshot {name}")
        if any(actual.get(field) != binding[name][field] for field in _PINS[name]):
            raise TimelineEditError(f"transcript snapshot {name} differs from binding")
        if name == "clock":
            _media_clock(actual)
    rows = content.get("words")
    selected = binding.get("word_ids")
    if not isinstance(rows, list) or not isinstance(selected, list):
        raise TimelineEditError("transcript words and selected word_ids must be arrays")
    by_id = {}
    for row in rows:
        row = _object(row, "word")
        word_id = row.get("id")
        if not isinstance(word_id, str) or not word_id or word_id in by_id:
            raise TimelineEditError("transcript source word IDs must be nonempty and unique")
        start, end = row.get("start_s"), row.get("end_s")
        if (not isinstance(row.get("text"), str)
                or any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end))
                or not 0 <= start <= end):
            raise TimelineEditError("transcript words require finite source-media intervals and text")
        by_id[word_id] = row
    if (any(not isinstance(key, str) or key not in by_id for key in selected)
            or len(set(selected)) != len(selected)):
        raise TimelineEditError("transcript selection contains missing or duplicate word IDs")
    wanted = set(selected)
    if [key for key in by_id if key in wanted] != selected:
        raise TimelineEditError("transcript selected word IDs must retain source order")
    return tuple(copy.deepcopy(dict(by_id[key])) for key in selected)


# These values are request-bound compiler inputs, never a persisted document or
# history schema. The runner owns admission and restores the union of accepted
# before/after dependencies for replay and inverse. No resolver is stored here.


@dataclass(frozen=True)
class FrozenTranscript:
    reference: Mapping[str, Any]
    snapshot_bytes: bytes


def binding_of(clip):
    app = clip.get("app")
    declaration = ({} if app is None else app).get("transcript", {})
    if not isinstance(declaration, Mapping):
        raise TimelineEditError("clip.app.transcript must be an object")
    if "binding" not in declaration:
        return None
    if type(declaration.get("v")) is not int or declaration["v"] != 1:
        raise TimelineEditError("transcript namespace requires v=1")
    return _object(declaration["binding"], "binding")


def source_words(binding, dependencies, occurrence_id):
    """Select only an explicitly supplied accepted dependency, without discovery."""
    binding = _object(binding, "binding")
    document = _object(binding.get("document"), "document")
    matches = [item for item in dependencies if isinstance(item, FrozenTranscript)
               and item.reference.get("document", {}).get("id") == document.get("id")
               and item.reference.get("document", {}).get("version") == document.get("version")]
    if len(matches) != 1:
        raise TimelineEditError("transcript requires one exact frozen dependency")
    item = matches[0]
    return bound_source_words(binding, frozen_reference=item.reference,
                              snapshot_bytes=item.snapshot_bytes, occurrence_id=occurrence_id)


def word_source(binding, clip_id, *, parent=False):
    """Lossless identity components, never a generated word ID."""
    owner = f"parent/clip:{clip_id}" if parent else f"occurrence:{binding['occurrence_id']}/clip:{clip_id}"
    return (binding["document"]["id"], binding["source"]["lineage_id"],
            binding["source"]["revision_id"], owner, _digest(binding["snapshot"]["sha256"], label="snapshot.sha256"))


def resolve_source_word(expr, words):
    """The word-formula face of the same exact identity used by TimeRef."""
    source = expr.get("source")
    if not isinstance(source, (list, tuple)) or len(source) != 5 or any(not isinstance(v, str) or not v for v in source):
        raise TimelineEditError("bound word formula requires exact source/occurrence identity")
    from astrid.core.timeline import moments as mo
    hits = mo.find_words(mo.Moment("word", source=tuple(source), word_id=expr.get("word")), words)
    if len(hits) != 1:
        raise TimelineEditError("ORPHAN: bound word is unavailable in its exact source/occurrence")
    return hits[0][0]


def follow_bound_anchors(tl, *, clip_id, occurrence_id, previous_binding, next_binding, correspondence=()):
    """D's accepted bind handler calls this after its exact guarded binding write.

    Both immutable dependencies must already be present in the request. Their
    verified bytes are provenance for mapping endpoints, not authentication of
    the caller: only the command owner can admit the mapping. This helper never
    publishes/adopts a source or authorizes a caller-created dependency. Failed
    endpoint/ancestry checks occur before any anchor write; the existing outer
    transaction owns rollback of the binding itself.
    """
    from dataclasses import replace

    from astrid.core.timeline import moments as mo
    from astrid.sdk import timeline_intent as intent
    from astrid.sdk.timeline.commands import scoped_clip

    previous = _object(previous_binding, "previous binding")
    old_rows = source_words(previous, tl._transcript_dependencies, occurrence_id)
    old_source = word_source(previous, clip_id, parent=occurrence_id is None)
    owner = next((c for c in tl.clips() if scoped_clip(tl, c) == old_source[3]), None)
    if owner is None or binding_of(owner.data) != next_binding:
        raise TimelineEditError("transcript continuation target differs from accepted binding")
    old = {r["id"] for r in old_rows}
    new = set()
    new_source = None
    if next_binding is not None:
        new_rows = source_words(next_binding, tl._transcript_dependencies, occurrence_id)
        new = {r["id"] for r in new_rows}
        new_source = word_source(next_binding, clip_id, parent=occurrence_id is None)
    pairs = {}
    destinations = set()
    if not isinstance(correspondence, (tuple, list)):
        raise TimelineEditError("transcript correspondence must be explicit one-to-one endpoints")
    for pair in correspondence:
        pair = _object(pair, "correspondence")
        before, after = _object(pair.get("from"), "from endpoint"), _object(pair.get("to"), "to endpoint")
        a, b = before.get("word_id"), after.get("word_id")
        if (new_source is None or before.get("lineage_id") != old_source[1]
                or after.get("lineage_id") != new_source[1] or not isinstance(a, str) or not isinstance(b, str)
                or a not in old or b not in new or a in pairs or b in destinations):
            raise TimelineEditError("transcript correspondence must be provenance-bearing and one-to-one")
        pairs[a] = b
        destinations.add(b)
    if new_source == old_source:
        if any(a != b for a, b in pairs.items()):
            raise TimelineEditError("one immutable source cannot remap its retained word IDs")
        # Selecting/resegmenting the same immutable source needs no revision map.
        pairs.update({wid: wid for wid in old & new if wid not in pairs and wid not in destinations})
    elif new_source and old_source[1] == new_source[1]:
        # A same-name lineage alone cannot authorize continuity. Require immutable
        # ancestry, stable media/stream/document identity, and explicit retained IDs.
        item = next(d for d in tl._transcript_dependencies if d.reference["document"] == next_binding["document"])
        record = json.loads(item.snapshot_bytes)
        if (previous["subject"] != next_binding["subject"]
                or previous["document"]["id"] != next_binding["document"]["id"]
                or any(previous["source"][k] != next_binding["source"][k] for k in ("producer_id", "source_id"))
                or new_source[2] == old_source[2]
                or record["source"].get("parent_revision_id") != old_source[2]
                or any(a != b for a, b in pairs.items())):
            raise TimelineEditError("same-lineage continuity requires explicit retained IDs and revision provenance")

    def follow(moment):
        if moment.base is not None:
            base = follow(moment.base)
            return None if base is None else replace(moment, base=base)
        if moment.source != old_source:
            return moment
        target = pairs.get(moment.word_id)
        return replace(moment, source=new_source, word_id=target) if target is not None else None

    def expression(expr):
        if expr.get("orphan"):
            return expr
        if "moment" in expr:
            moment = follow(mo.parse(expr["moment"]))
            return None if moment is None else {**expr, "moment": mo.format_moment(moment)}
        if isinstance(expr.get("source"), (tuple, list)) and tuple(expr["source"]) == old_source:
            target = pairs.get(expr.get("word"))
            return None if target is None else {**expr, "word": target, "source": list(new_source)}
        return expr

    for clip in tl.clips():
        for read, write in ((intent.on, intent.set_on), (intent.until, intent.set_until)):
            text = read(clip.data)
            if not text:
                continue
            try:
                updated = follow(mo.parse(text))
            except mo.MomentError:
                continue
            if updated is None:
                for mate in clip._sequence_mates():
                    intent.set_orphan(mate.data, f"ORPHAN: {text} lost its bound source word")
            else:
                write(clip.data, mo.format_moment(updated))
        fit = intent.sequence_fit(clip.data)
        if fit and fit.get("land") is not None:
            land = fit["land"]
            updated = expression({"moment": land} if isinstance(land, str) else land)
            if updated is None:
                for mate in clip._sequence_mates():
                    intent.set_orphan(mate.data, "ORPHAN: fit lost its bound source word")
            else:
                intent.set_sequence_fit(clip.data, {**fit, "land": updated["moment"] if isinstance(land, str) else updated})
        for path, expr in intent.formulas(clip.data).items():
            if not isinstance(expr, Mapping):
                continue
            updated = expression(expr)
            intent.set_formula(clip.data, path, updated if updated is not None else
                               {**expr, "orphan": "ORPHAN: formula lost its bound source word"})
    tl._mcache = None
