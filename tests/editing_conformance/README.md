# Editing conformance

One scenario (YAML) is applied through three front-ends: the `timelines` CLI, a cut sheet
(export, substitute, `timelines apply`), and a Python snippet (`Checkout.draft(P, t)`, then one save).
Each front-end runs on its own fresh copy of a fixture. The runner proves the documents agree,
the undo steps are right, undo/redo restore exactly, and no op is a silent no-op.

Run: `python -m pytest tests/editing_conformance` (scenarios: `tests/editing_scenarios/**/*.yaml`).

## Scenario format v1

```yaml
id: look.set.rocket-x        # <area>.<op>.<case>; area: timing|structure|look|audio|code; file is <id>.yaml
source: h2 L2                # audit row (AUDIT.md, h1-h4), or "new"
intent: Move the rocket to x=120 (canvas px).
fixture: tiny                # see "Fixtures" below
tool: timeline.opacity       # optional: the registered tool this scenario tests (stored, not used yet)
expect: change               # change | no-change | refused
idempotent: true            # optional, only with expect: change: a second run must be a no-change (see Checks)
cli: edit --clip c1.rocket --set x=120   # one command, or a list; the runner inserts "t --project P" after the verb
sheet:                       # optional: export a sheet, substitute, apply with `timelines apply`
  cuts: [c1]                 # optional: only these cuts; default the whole sheet
  replace:                   # each `old` must occur EXACTLY ONCE in the exported sheet, else ScenarioError
    - ["x=60", "x=120"]
python: |                    # optional: runs with tl = Checkout.draft("P", "t"); the runner then saves once
  tl.clip("c1.rocket").set(x=120)
check:                       # optional: Python expressions; tl is a fresh draft after the edit; each truthy
  - tl.clip("c1.rocket").get("x") == 120
xfail:                       # optional: known mismatches, each with the audit reference
  - {path: python, check: steps, reason: "AUDIT bug 10: a Python save with 2 edits is 2 undo steps"}
```

- A path is absent, `{missing: "<why, with audit ref>"}` (the front-end cannot do it today), or present.
  A missing path is reported as an `exists` xfail. At least one path must be present.
- Unknown keys, a bad `expect`, an unknown xfail path or check, or a missing reason are a `ScenarioError`.

## Fixtures

`fixture: NAME` resolves to `tests/editing_scenarios/fixtures/NAME.json` if it exists, else
`tests/fixtures/timeline_editing/NAME.json`. Neither present is a `ScenarioError`.

## Checks

Each present path is checked against these names (xfail entries use them):

- `runs`: the path ran (every CLI/sheet command exits 0, Python raises nothing). For `refused` it must refuse. For `no-change`, exit 3 (no change) also counts as ran.
- `changes`: the document differs from the base (`change`), equals it (`no-change`, `refused`).
- `reports`: stdout contains "no change" if and only if the document did not change (CLI and sheet only; not for `refused`).
- `equal`: the document equals the reference path's (`change` only).
- `steps`: new undo steps equal the commands (CLI), 1 (sheet, Python), and 0 for `no-change` and `refused`.
- `undo`: undoing the measured steps restores the base exactly (only when steps >= 1).
- `redo`: redoing them restores the after-document exactly (only when steps >= 1).
- `check`: every `check:` expression is truthy.
- `exists`: the path is present (a missing path fails it, and is an expected xfail).
- `idempotent`: only for `idempotent: true` scenarios. After a path's first run (before undo/redo) it runs again
  on the same working copy; this check fails if that second run changed the document, made an undo step,
  or (cli/sheet) did not say "no change". Its steps are taken back, so undo/redo still test the first run.

## Reference path

`equal` compares against the reference: `python` if present, else `cli`, else `sheet`. The reference
itself is not checked against itself.

## Strict xfail

A listed xfail that still fails is reported as an expected xfail. A listed xfail that passes fails the
test with "fixed? remove the xfail", so known bugs cannot silently get fixed or hidden. An unlisted failure
fails the test. Missing paths xfail with their reason.

## Files

- `runner.py`: `load_scenario`, `parse_scenario`, `run_case(scenario, via)` (alias `run_path`), `run_scenario`, `evaluate`, `classify`.
- `test_conformance.py`: one test per (scenario, path).
- `test_runner_selftest.py`: proves the harness catches each class of bug.

RUNNER also accepts `sheet: {text: 'edit {"op":...,"select":...,"args":...}'}`
for a complete sheet whose intent does not fit a layer row. `text` and
`cuts`/`replace` are mutually exclusive. These are the same sheet parser and
`timelines apply` path, using the same OpCall registry as the CLI and SDK.

The `journal` check compares the actual complete journal records across paths.
It removes only `source`, `actor`, `at`, `id`, `seq`, `prev`, and `hash`; labels,
calls, digests, undo-step identity and errors must match. A crash never counts as
a refusal: the harness records a refusal only for a typed editing error.

The admitted `08a6044` source contains 50 YAML scenarios; all 150 paths are
exercised. The card's stated 49-file census is retained as a control discrepancy,
not implemented by dropping a scenario. The remaining three cut-placement
xfails describe the current `Checkout.resolve()` behavior; the RUNNER branch
does not change that shared timing implementation.
