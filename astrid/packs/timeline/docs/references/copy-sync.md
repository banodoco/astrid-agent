# Copy inventory and explicit quotation intent

`checkout.copy_inventory()` returns deterministic JSON-compatible rows for every
parent and actual occurrence-scoped visual layer, without selecting layer IDs.
Text slots come from existing element `metadata.text`: all declared fields are
read, including `am-type` text, both `am-callout` fields, and `text-card` content.
Other strings, including emphasis selectors, are not treated as displayed copy.
Unsupported components, native text without a slot declaration, dynamic fields,
hidden tracks and source-only tracks remain explicit coverage limitations.
Authored visual support is not proof of rendered visibility.

Every row includes the existing ScopedId, component/resource, exact field value,
Unicode-code-point token positions, display support, captured declarations, and
all consecutive whole-word source candidates. Candidates are `unique`,
`ambiguous`, `unmatched`, `empty`, or `unavailable`; even a unique candidate is
unannotated until the author declares intent. No timing-based candidate selection,
fragment search, fuzzy matching, number conversion, OCR or LLM matching occurs.

`copy-token-v1` applies NFC, Unicode casefold, NFC, curly-apostrophe normalization,
Unicode whitespace splitting, and the specified sentence punctuation/quote/bracket
edge trimming. Internal punctuation, accents, hyphens, slashes, signs, currency,
percent symbols and numeric spelling remain distinct. Candidate tokens retain
source word IDs and source code-point spans, alongside display token spans.

## Source evidence

`astrid.sdk.timeline_script.read_detached_copy_source(bundle)` reads the supplied
bundle's script binding descriptors and actual word/take producers. It performs
no remote or media reads. `snapshot_kind: detached-document-sha256` explicitly
identifies the hash of this local document; it is not Runtime's independently
read `snapshot_digest`. `revision_id` identifies the checkout's pinned base.
Missing revision, binding identity, transcript, or occurrence identity produces
unavailable evidence. Authored script text awaiting a matching transcript also
reports unavailable. A separately obtained `read_composition_script` result is
never asserted to share the word read's snapshot.

A source association includes the binding/content pin and `word_producer`:
existing scoped speech clip, exact word-row digest, normalized whole-line digest,
and the actual asset dependency from this same document. Word IDs use F's
`segment:index` generator over the actual occurrence's time-ordered word read;
occurrence, producer clip, line and captured text are essential parts of identity.
The legacy global lookup's collapsed repeated-placement clocks are not used to
choose an occurrence. These ordinals are not durable cross-edit identities.

## Explicit declaration and rebinding

After the author chooses a field and one of its candidates, construct the adopted
shape from that row and candidate and use the ordinary command runner:

```python
quote = {
    "version": 1,
    "normalization": "copy-token-v1",
    "display": {"path": row["path"], "text": row["text"]},
    "source": candidate["source"],
}
checkout.batch([{
    "op": "clip.declare-quotes",
    "select": row["layer"],
    "args": {"declarations": [quote]},
}])
checkout.save()
```

For ordinary unshared clip handles, `clip.declare_quotes([...])` uses the same
operation. The array replaces that clip's declarations; pass `[]` to clear it.
Each declaration captures the complete display string; optional `display.range`
is a validated half-open Unicode-code-point interval selecting complete words.
Use `astrid.core.timeline.copy_sync.runs` on that explicit substring to enumerate its candidates before
authoring a partial declaration. Duplicate field/range associations are refused.
The command validates current display/source expectations, preserves opaque
siblings, records a narrow declaration-field expectation, and supports undo.
Reconfirm/rebind explicitly through this operation after inspecting new evidence.

## Source changes and lint

`checkout.copy_lint(before=old_checkout)` returns findings aggregated once per
ScopedId with field/span reasons. Without `before`, it uses an actual stored
checkout base when available; otherwise it checks declarations against current
source evidence. `checkout.lint()` and `checkout.check()` expose these findings.

`apply_script` compares the real old/new source boundary in the private timing
workspace, scanning every declaration regardless of G's visual change intervals.
Its return report and compiler diagnostic state retain the findings. Its existing
new-take predicate also compares exact raw text so legacy moment normalization
cannot discard a source punctuation change before the comparison.

Logical runs follow only forced correspondence across all optimal exact-word
alignments. A unique insertion before a phrase can shift its word indices safely.
Replacement, deletion, broken contiguity, changed occurrence/line, or lexical
display changes report `COPY_STALE`; unresolved repetitions require rebinding.
`copy_quote_tracking` stores command-owned correspondence/invalidation evidence
keyed by the declaration's content digest. It never rewrites the declaration or
its captured basis. Durable invalidation prevents a later reintroduced phrase
from resurrecting the old association. Undo restores both through the same edit.

`COPY_RAW_CHANGE` reports normalized-equivalent raw source/display changes.
Timing and revision/digest changes alone do not imply lexical staleness.
`COPY_SOURCE_UNAVAILABLE` prevents a missing source from appearing clean.
`COPY_SYNC` identifies an unresolved quotation or loss of half-open overlap
between the source line's speech interval and the display support.
An undeclared previously unique overlapping candidate lost across a comparison
reports `COPY_SYNC_INFERRED`, separately from declared staleness. Unmatched
headings and labels remain ordinary inventory rows.

Callers with shared evaluated support may supply a ScopedId-to-interval-list map
as `evaluated_support`; otherwise results explicitly say `authored-support`.
No distance threshold is guessed. Inventory and lint never change the document,
create declarations, advance provenance, move layers or rewrite copy.

Historical v11 copy and agent-goal inventory remains pending its exact authorized,
authenticated input. Synthetic v11-named fixtures prove the tool behavior only.
