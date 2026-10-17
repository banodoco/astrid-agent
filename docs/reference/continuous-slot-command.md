# Clip continuous effect slot

`clip.set-continuous` authors the complete top-level `continuous` property of
one exact clip ScopedId. It uses the existing EditCommand runner, transaction,
commit receipt, rebase and publication inverse. It does not write element
`params.continuous`.

```json
{
  "op": "clip.set-continuous",
  "select": "occurrence:occ-A/clip:clip-1",
  "args": {
    "continuous": {
      "type": "custom:orbit",
      "intensity": 0.5,
      "params": {"amount": 2, "opaque": [null, true]}
    }
  },
  "expect": {"continuous": {"state": "absent"}}
}
```

Supply exactly one argument mode:

- `continuous: object` sets/replaces the complete captured App value, including
  the App's spread-and-replace parameter update and reset-defaults shapes.
- `params: object` shallow-merges a nonempty parameter patch into an existing
  effect, retaining its type, intensity, other parameters and opaque siblings.
  Each parameter value is replaced whole; nested objects are not deep-merged.
- `clear: true` removes the slot. App `continuous: undefined` maps to this mode;
  JSON null is not a removal request.

The effect requires a nonempty string `type`. Optional `intensity` is a finite
number (booleans refuse); optional `params` is an object. Values remain finite
JSON through the existing command envelope. No effect implementation lookup,
resource admission or rendering capability is inferred from the type string.

Wire calls require the full tagged before-state in `expect.continuous`, or
`expect["<ScopedId>::continuous"]`. A present state contains `value`, including
all captured type, intensity, params and opaque data. Stale state, malformed
mode/value, missing/ambiguous owner, alias selection and selector/resolved-owner
disagreement refuse the transaction. `Clip.set_continuous(...)` captures the
same expectation at its SDK boundary and records the exact scoped owner.

The changed slot emits one direct write at `<ScopedId>::continuous`, with exact
tagged before/after values or absence. Unchanged writes emit no receipt. Existing
`timeline.inverse` reverses this received write with its publication identity;
the accepted Undo receipt supplies Redo. Both guard the whole slot and preserve
unrelated clip fields. App command-writer adoption and real Runtime/render proof
remain separate integration work.
