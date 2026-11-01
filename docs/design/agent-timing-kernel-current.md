# Pure timing kernel on current integration

The opt-in compiler lives in `astrid.sdk.timeline.compiler`. It performs no IO,
command execution, authored writes, history lookup, renderer work or latest-source
reads. This port does not activate it in the default provider or Checkout.

Source: kernel `b8139a1acbb6d286fff791f95f09276e5492ea9f`;
authoritative-ID evaluator and authored-reference experiments
`fd27d5ebdb795b4855e42f5389793eb99637ae7e`; reviewed distinct-revision test
`a3ad4f967b1299f9f46db64bbf50b58cd38d717f`.
Port base: `df2739f903edac02bd36b4446deeea847524c040`.
Current `timeline.units`, public Composition 0.0.12 pins, commands and DocumentPin
contracts are retained. The pure time helpers already have the required current
signatures; the evaluator imports them once from `timeline.units`.

## API for the native word adapter

```python
Owner = tuple[str, str, str]  # occurrence_id, authoring shot_id, raw clip_id
# Parent clip owner: ("parent", "", raw_clip_id).

WordBinding(
    target: Owner,
    field: str,              # "on" or "until"
    declaration: str,        # exact kernel Moment spelling for these selected IDs
    source: Owner,
    document_id: str,
    version: int,
    content_digest: str,
    word_ids: tuple[str, ...],
)
TimingInputs(bundle: Mapping[str, Any], *, sources=(), bindings=(), policy="nearest")
compile_timing(inputs: TimingInputs) -> TimingResult
# TimingResult.bundle: owned unchanged dict
# TimingResult.timings: Mapping[Owner, Mapping[str, Any]] (read-only)
```

`TimingInputs` captures JSON copies of the bundle and source rows at construction,
plus frozen WordBindings. `policy` is `nearest`, `floor` or `ceil`. Each `sources`
entry is `{"pin": DocumentPin, "document": VerifiedDocument}`. A DocumentPin is
exactly `{document_id, version, content_digest}`. The document must contain matching
`document_id`, positive integer `version`, scoped `project_id`, `kind="media.transcript"`
and `content`. Content must use `media.transcript/v1`, match the raw clip's registered
media and project/source-derived transcript document ID, and hash to the pin digest.
Capture authorization and historical reads remain the caller's responsibility.
Unavailable or revoked captures must be omitted; compilation then refuses.

The native adapter must extract the stable source owner, exact transcript pin,
word IDs, edge and offsets from the authored revision. IDs resolve only within the
exact captured document's segments. A phrase must use consecutive IDs in one segment.
The evaluator validates selected text against its private Moment spelling, then uses
the selected edge directly; it never calls `find_words` to select those IDs again.
The adapter must generate this private spelling from the IDs, not from the display
echo, and return the original authored bundle unchanged. Never reconstruct a choice
from accepted history, current text, or an ordinal after a source move.

`tests/editing_conformance/word_binding_recipe.py` demonstrates that boundary as a
**test-only parent-clip adapter and recipe**. Its `app.astrid.word_anchors` experiment
is not a product registration, a default adapter, or a new contract in this port.
It preserves same-echo/different-ID canonical revisions, restore/undo/rebase and
opaque fields. Legacy text is accepted only when exactly one source phrase matches;
ambiguous text (including an ordinal without a stored choice) refuses with
`ambiguous anchor; choose occurrence`. The unique-text control remains supported.

Each interval returns exact Fraction seconds `start`, `end`, `duration`, plus
`requested_start`, `requested_end`, `applied_start`, `applied_end`, `fps`,
`start_frame`, `end_frame`, `rounded` and `policy`. Source windows are half-open:
word onset at source-out refuses; a word ending exactly there is allowed. The clock
reads canvas FPS from parent/base-parent theme overrides, accepts numeric FPS,
`{numerator, denominator}` or `[numerator, denominator]`, and defaults to 30.
Intervals that collapse on quantization refuse. `TimingCompileError` refuses the
entire result; `TimingCycleError` adds `code="CYCLE"` and a deterministic closed
`path`. Input objects stay unchanged even on refusal.

## Remaining wiring and parity

- Native recorded word-reference command/capture adapter, owning the authored stable
  IDs and pins for all supported owners, including occurrence isolation.
- Default provider/Recording compilation, derived-write recording and consumers of
  the returned intervals. The existing provider still uses `authoring-bundle-1`.
- Checkout retime/resolve/reflow, model setters, narration/music/sequence writers,
  CLI/sheet compatibility and parity before removing their existing writers.
- Explicit maps, nontrivial placement source offsets/rates, millisecond clips,
  shared-shot materialization and any additional source-window codecs need a
  dedicated adapter; this kernel refuses the unsupported shapes it recognizes.
- Sequence/sequence-fit, formulas, orphan caches, until_film_end, gap/reflow
  declarations and beats/markers remain explicit unsupported declarations here.
  Existing working default paths stay in place pending their dedicated adapters.
- No Runtime default-acceptance, renderer/frame/audio parity, one-writer migration,
  full-suite or cross-product claim follows from these targeted compiler tests.
