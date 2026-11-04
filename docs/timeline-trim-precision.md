# Trim source precision

`clip.trim` v1 and `Clip.trim` share `units.trim_source_interval`. New trims
author six-decimal **source seconds** and keep the existing speed rule:
playback duration is `hold / speed`. Registry duration remains authoritative
and unrounded. Reading, compiling, restoring or undoing existing films does
not normalize their authored windows or holds.

Resolve null endpoints from the current source window, defaulting missing
`from` to zero and missing `to` to the raw file duration `D`. Before rounding
or writing anything, require finite endpoints and `0 <= in < out <= D`.
A genuinely overrange raw request is refused even if rounding would fit.
Preserve integer inputs exactly for this check, including the registry bound,
explicit requests and retained/default endpoints. Do not convert them to
binary64 first: integer `9007199254740993` exceeds binary64 `9007199254740992.0`
even though converting the integer to binary64 makes them equal.
Existing native source identity, unsupported source-map/window, provenance,
capture, replay and inverse guards remain in force.

The exact policy (all binary64 conversions use nearest, ties to even):

```text
Q = 1_000_000
E(x) = the exact rational value of the input binary64 number x
V(x) = x exactly for an integer, E(x) for a binary64 input
N(x) = binary64(nearest_even_integer(Q * E(binary64(x))) / Q)
I = N(in)
O = N(out)
if E(O) > V(D):
    F = floor(Q * V(D)) / Q                 # exact rational division
    O = binary64(F)
    if E(O) > V(D):
        O = predecessor_binary64(O)
H = N(binary64(O - I))
refuse if O <= I or H <= 0
write from=I, to=O, hold=H
```

`N` is the existing Python `round(float(x), 6)`; unrelated `_r` calls are
unchanged. The interior floor uses exact multiplication, with no epsilon.
In particular, `D=1.0485929999999999` has an interior endpoint `1.048592`;
binary64 `D * Q` rounds up to `1048593.0`, so `Math.floor(D * Q) / Q` is
incorrect. Decimal-string ties and `Math.round(x * Q)` also do not reproduce
Python's binary64 nearest rule; the vectors include both tie directions.

`predecessor_binary64` means the immediately preceding finite IEEE-754
binary64 value toward negative infinity (`math.nextafter(O, -math.inf)` in
Python). This is a portable numeric rule, independent of host formatting or
rounding modes. For positive finite numbers, App can decrement the unsigned
64-bit IEEE-754 bit pattern by one. One step suffices: `F <= V(D)`, and the
predecessor of a nearest binary64 value that exceeds `F` is below `F`.
The additional step is needed for integer bounds beyond binary64's exact
integer range; that predecessor is itself an integer, hence on the
six-decimal grid. A binary64 bound is already representable, so converting
`F <= D` with nearest rounding cannot exceed it.

For the review's integer `D=9007199254740995`, both the nearest endpoint and
the binary64 conversion of its exact floor are `9007199254740996.0`. The
predecessor is `9007199254740994.0`, which is the authored endpoint and (for
`in=0`) hold. Raw `out=9007199254740996` or `9007199254740996.0` refuses before
writes. A float duration `9007199254740996.0` instead admits that exact end.
Hold always comes from the actual chosen endpoints, using the existing
six-decimal rule; speed applies once to playback.

The policy refuses a positive raw interval if its applied endpoints coincide,
including after moving the out point inward. It never stretches a collapsed
interval to manufacture a minimum duration. A file shorter than one
microsecond has no positive six-decimal source window. This is source
precision only: rational fps and half-open playback boundaries are unchanged.

Portable vectors are in
`tests/editing_conformance/fixtures/recipe_m2_clip_trim_boundaries.json`.
Start each case with the unchanged sibling `recipe_m2_clip_trim.json`'s
`input`; replace only `parent_revision.payload.registry.assets.picture.duration`
and recompute that payload's required content digest. Apply the case's `args`.
`applied` gives exact authored numeric values; `error` requires refusal before
writes. The 34 vectors retain the original 17 cases and add integer/float
boundaries around `2**53`, the review's exact end, just-below and overrange
controls, a positive interior interval and a collapsed interior interval.
JSON integer tokens must be read losslessly for raw checks (for example with
BigInt); ordinary `JSON.parse` into Number loses the distinction these cases
test. Decimal/exponent tokens denote binary64 inputs. Applied outputs are
finite binary64 numbers and remain bounded after JSON round-trip. The
original golden's commands, records and compiled output still
match exactly. Native tests exercise model parity, publication mirrors,
opaque data, child pins, exact replay/retry and guarded undo/redo.

| Raw duration | Requested out (`in=0`) | Authored out / hold |
| --- | --- | --- |
| 9.9999999 | 9.99999989 | 9.999999 |
| 9.9999999 | 9.9999999 | 9.999999 |
| 9.9999999 | 9.999999900000002 or 10 | Refused before writes |
| 0.0000009 | 0.0000009 | Refused: no positive authored window |
| 9007199254740995 (integer) | 9007199254740995 | 9007199254740994.0 |
| 9007199254740995 (integer) | 9007199254740996 | Refused before writes |
| 9007199254740996.0 (binary64) | 9007199254740996.0 | 9007199254740996.0 |

Source identity for the App handoff: candidate base commit
`e4bec724bec39865b9b6b73b8754abd82d4f4449`; the commit containing this fix is
assigned by the worker harness. Exact changed source **Git blob SHAs**:

| File | Blob SHA |
| --- | --- |
| `astrid/sdk/timeline/units.py` | `11bb609c6116fe2d2a50f880fbd3f92c60ee1d43` |
| `astrid/sdk/timeline/_trim_commands.py` | `c5dd666a676d09c2a232e3f81031b736feb50916` |
| `astrid/sdk/timeline/model.py` | `f8115879a7cd666ea9b99bd5e1884ddbec8b7ab8` |

The original golden's SHA-256 is
`4d71a9d184db1b85c220de10842b2dc68e5eb9b48afee180b6c29cf0d5b06152`.
The escalation's pinned native source commit
`76737ca2f9c86a075e7e03dfc8990e3090acc234` reproduces `to=hold=10` for
both below-end and exact-end requests; the corrected source and model both
produce `to=hold=9.999999`, preserving raw duration `9.9999999`.

Mirror this policy in App, then qualify its native capture parity against
these vectors and the untouched original golden. Runtime receipt retry,
browser preview and rendering require their own qualification; these native
domain tests make no claim about those paths.
