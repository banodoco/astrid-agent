# `shot.set`: shot name and settings edits

`shot.set` is the native EditCommand for the two fields the App edits directly on a shot revision:
the shot **name** and a bounded slice of its generation **settings**. It follows `track.set` /
`clip.set`: `args = {values, clear}`, closed schema, every write recorded (including writes of an
unchanged value), original raw guards, and a guarded exact inverse. There is no whole-settings
replace.

```
select  occurrence:<occurrence_id>/shot
args    values: { name?: string, settings?: { <supported root>: <scalar | null> } }
        clear:  ["name" | "settings.<supported root>", ...]
intent  { "ownership": "occurrence" }
```

## Ownership

Occurrence-only. The selector names the occurrence; the write lands in that occurrence's own shot
root in the authoring bundle (already a per-occurrence copy when its source shot is shared, the
source shot itself when unshared). A sibling occurrence of the same source shot is never touched, and
the compiler publishes the new shot revision and repins only this occurrence. Ownership is never
inferred from display IDs, and `intent.ownership: "shared"` is refused. A shared-shot content edit
needs its own command with explicit membership/order guards; it does not exist yet.

The command binds `shot.owner` and records reads of the occurrence identity and its shot
identity, plus the source revision pin, so a changed owner or source refuses on replay.

## Clear policy

Explicit clear removes the field. It is distinct from setting `""` or `null` (both are values) and
absent means none. Before/after presence is recorded exactly, so the inverse restores missing vs
null vs value. A set of a non-object settings root (array, string, false, 0) refuses rather than
replacing it; an absent or null root is created as `{}` with the creation recorded.

## Supported slice

Scalars only (each also accepts `null`):

- strings: `prompt`, `negativePrompt`, `textBeforePrompts`, `textAfterPrompts`, `videoControlMode`,
  `dimensionSource`, `generationMode`, `selectedModel`, `motionMode`, `selectedPhasePresetId`,
  `generationTypeMode`
- numbers: `batchVideoFrames`, `batchVideoSteps`, `customWidth`, `customHeight`, `guidanceScale`,
  `amountOfMotion`
- booleans: `enhancePrompt`, `turboMode`, `advancedMode`, `smoothContinuations`, `ltxHdResolution`

Several roots may change in one command (for example `selectedModel` with `generationMode` and
`motionMode`) and are then one atomic, one-undo-step edit. Compound or identity-bearing roots are
**unsupported** and refused, set or clear: `steerableMotionSettings`, `phaseConfig`, `pairConfigs`,
`shotImageIds`, `loras`, `structureVideo`, `modelSettingsByModel`. Any mixed batch containing one
refuses atomically. Extending the slice means adding the key with its type; arrays need explicit
identity/order semantics first.

## Name rule (decided)

**The canonical shot name is the root field `payload.name`** (`revision.name` in App terms).

- Why: every Agent reader already uses it (`timeline_cuts`, `timeline_filmstrip`,
  `timeline_restructure`, `timeline_structure`, `invocation`), and the App already displays it before
  any other field, so it is the only name that already masks the others.
- `shot.set` writes and clears only `payload.name`. `name: ""` is a value; `clear: ["name"]` removes it.
- Legacy names are **read-time compatibility only**: `provenance.name`, then `provenance.title`, then
  `metadata.name` (the App's current fallback order, behind `payload.name`). No command ever rewrites
  or deletes them. After a clear, a reader that still falls back will show the legacy value.
- Migration of existing data is explicit and recorded, never silent: the App/agent reads the effective
  legacy name and submits `shot.set {name: <legacy name>}`. That is an ordinary guarded, invertible
  command; the legacy field stays as inert fallback.
- Existing stored shots with both `name` and `provenance.name` keep displaying `name`; the App's
  current rename (which wrote `provenance.name`) was masked by it, so after switching to
  `shot.set` renames are visible again.

## What App and Runtime must do

- **App**: replace the direct settings and rename publishes with `shot.set` commits. Settings must
  send only touched roots (`values.settings` / `clear`), not a normalized complete form; an
  unchanged-but-explicit value is a write. Compound roots stay on the existing guarded fallback
  until the slice grows, and must be refused as unsupported rather than split. Rename must write
  `name`, not `provenance.name`. Pass `select = occurrence:<id>/shot` and capture before editing.
- **Runtime**: the new op/args are additive to `edit-command.schema.json` (`ShotSetArgs`, a new
  op block); regenerate the schema-derived Runtime artifact/clients and run the provider
  compatibility check so the Runtime loads the provider that knows `shot.set`. No new DTO or
  endpoint.

## Tests

`tests/editing_conformance/test_recipe_shot_set.py`: exact round-trip per presence state (absent,
null, empty, value; container absent/null/object), clear vs empty vs null, atomic name plus coupled
settings, occurrence isolation for shared and unshared shots including compiled revisions,
unchanged-write and A→B→A (rebase allowed, inverse refused), disjoint rebase and same-leaf conflict,
original-guard refusals, closed-argument refusals, unsupported roots, and the golden
`fixtures/recipe_m2_shot_set.json` (request, command, record result, after/head/rebased bundles,
compiled shot revisions).
