# Agent gate evidence

## Lint, types and protected controls

`make ruff` and `make mypy` compare diagnostic identities against the Git merge
base named by `ASTRID_GATE_BASE`. For a task branch, use its destination branch:

```sh
ASTRID_GATE_BASE=origin/release/14/plan-39 make ruff
ASTRID_GATE_BASE=origin/release/14/plan-39 make mypy
```

CI uses the pull request's base SHA, or the previous head for a push. An explicit
workflow dispatch without those event fields compares the previous commit.
The checkout includes history so an absent base fails instead of silently
comparing the candidate to itself.

The same installed tool scans both source trees. An identity contains the path,
diagnostic code, normalized message and enclosing qualified symbol with a
statement fingerprint. That fingerprint also distinguishes equal-message
errors in different assignments inside the same function. Duplicate
identities retain their counts. Line shifts do not create findings; fixing one
finding cannot pay for a different finding. Every code blocks a new identity.
The legacy `compare_*_baseline` count commands remain available for debt reports;
they do not drive the Ruff or mypy CI gates.

The controls gate rejects task changes to `scripts/reshape/baselines/`, Ruff or
mypy configuration, and pytest's collection scope settings. It also counts
suppression comments by path, attached statement and symbol. A new suppression,
or one moved onto another statement, fails. Existing structure violations are
still compared with the committed structure baseline by `make structure`; the
controls gate does not replace or enlarge that baseline.

## Collected tests and stored results

CI's existing test commands run through a wrapper, retaining their selectors,
timeouts and shard parameters:

```sh
python -m scripts.ci.run_pytest -q -rA --junitxml=/tmp/evidence/unit.xml tests/test_example.py
python -m scripts.ci.check_results /tmp/evidence --expected 1
```

The wrapper first runs an independent `--collect-only` census. A plugin records
every item before selection, recomputes the declared marker, keyword and shard
selection, and verifies the selected and deselected inventory. The executing
pass must have exactly the same census. Explicit `--deselect`, `--ignore`,
`collect_ignore` and `collect_ignore_glob` exclusions are refused. A whole-module
omission is allowed only through the existing lane registry. Focused file
selectors do not claim coverage of neighboring files.

Beside each XML file, the wrapper retains:

- `.reference.json` and `.reference.log`: the independent collection pass;
- `.collection.json`: the executing pass's inventory;
- `.jsonl`: one flushed record per test report, plus collection and completion;
- `.log`: the executing pass's actual console output and `-rA` summary.

A killed process leaves the reports flushed so far. Without completion, a final
summary and every selected test's teardown report, that evidence is partial and
fails validation. An existing evidence path cannot be reused by a later run.

Validation counts actual `<testcase>` elements and matches their identities and
outcomes to reports and the terminal summary. It accounts explicitly for
subtests and separate call/teardown failures; pytest's header count need not
equal its testcase count in those cases. Headers must agree with those reports,
so an inflated header cannot conceal missing cases. Missing, empty, malformed,
truncated or incomplete result sets fail. All four artifact types are uploaded
even when tests fail, under the existing artifact names and retention period.
