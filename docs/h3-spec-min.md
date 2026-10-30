# H3 Phase B canonical view

`normalize_request(document)` still accepts exactly the retained v1/v2 documents
and computes their historical normalized value and SHA-256. Its result exposes
`request.canonical`, an `H3Spec` describing the common, bounded Phase B subset.
The view is lazy: legacy requests outside this subset remain valid legacy
requests, but asking for their canonical view fails explicitly.

Canonical callers construct the typed `H3Spec`, `Source`, `Reference`, `Region`,
and `Operation` records in `astrid.packs.h3_av.shared.spec`, then call
`normalize_spec(spec)`. This adapter lowers to the existing v2 normalizer for
request, asset, clock, mask, reference, and settings validation. There is no new
JSON/YAML parser, schema version, digest, CLI, or execution route.

```python
from astrid.packs.h3_av.shared.spec import (
    H3Spec, Source, Region, Operation, normalize_spec,
)
spec = normalize_spec(H3Spec(
    prompt="Make the selected area blue.", duration=4,
    source=Source("clip", (0, 4)),
    regions=(Region("area", box=(10, 10, 100, 100)),),
    ops=(Operation("regenerate", (1, 2), region="area"),),
))
passes = spec.sampling_passes()
```

Sources are optional for generation and singular otherwise. A source is a bounded
video placed at frame zero; its range addresses input time, while operation
intervals address the output starting at zero. Intervals are half open and use
the existing 24 fps clock. A `box` is `[x, y, width, height]` in pixels, with a
nonnegative origin and positive size. A mask asset declares a static image;
normalization does not open or inspect it. Pass inspected `asset_modalities` to
`normalize_spec` or `canonical_spec` to reject known nonimage masks. Actual pixel
validation/rasterization belongs to later preparation work.

The canonical names (`ref-1`, `region-1`, ...) are assigned by occurrence order,
so caller labels do not alter canonical meaning. References are untimed appearance
images; an omitted/empty operation reference list uses the global references.
Each region must select exactly one mask asset or box. A missing operation region
selects the full frame. Unused regions do not contribute semantics.

`generate` is one source-free full-output `av` operation. `extend` is one full-frame
`av` suffix beginning at the source span's end. Ordered `regenerate` operations
change video inside that source window. `preserve` constrains the original input
for its declared interval and region, including audio/av preservation; it adds no
sampling pass. The adapter rejects overlapping edit/preserve permissions. It can
prove spatial disjointness for boxes, but refuses simultaneous file-mask
permissions whose disjointness needs media inspection. Two regenerations may
overlap: their order is retained and later editable pixels win.

`sampling_passes()` declares the first source-backed pass's `reads="source"` and
later passes' `reads="previous_verified_output"`. Every preservation constraint
has `preserve_reads="original_source"`. This is a plan dependency, not an executor
or a claim that verification ran. Generation/extension cannot combine with source
regeneration in this slice. Existing single-window bounds apply; there is no
window splitting, future-frame region lookup, tracking, polygon resolution,
dialogue conditioning, or new conditioning role.

Canonical equality includes prompt bytes, source range, permissions, operation
order, references, regions, and effective sampling settings. V1 prompt whitespace
is retained; v2 has already trimmed its prompt. V1 edit's historical FL2VA/20/0
model/steps/seed defaults are retained in its view; explicit equivalent settings
are needed to compare it with v2's Ref2VA defaults. Equal views do not imply equal
legacy hashes or migrate either execution route.

Request errors remain catchable as `H3RequestError`/`ValueError` and now reuse
`AstridValueError.to_envelope()`, including the G21 fields `code`, `message`,
`field`, and `fix`. Canonical errors use `h3.spec.*` codes. Every canonical
capability defaults to `available: false`, pending recorded GPU proof. Neither
normalization nor these CPU fixtures qualifies GPU behavior.
