# Agent gate evidence

## Lint, types and protected controls

`make ruff` and `make mypy` compare diagnostic identities against the Git merge
base named by `ASTRID_GATE_BASE`. For a task branch, use its destination branch:

```sh
ASTRID_GATE_BASE=origin/release/14/plan-39 make ruff
ASTRID_GATE_BASE=origin/release/14/plan-39 make mypy
```

CI uses the pull request's base SHA, or the previous head for a push. A manual
dispatch requires `target_branch`, the destination branch for the whole task.
`scripts/ci/gate_base.py` fetches that branch and resolves its merge base with
HEAD. There is no `HEAD^1` fallback: a defect in an earlier topic commit must
still fail. Missing/zero event SHAs and missing/invalid manual destinations
fail. The checkout includes history. Both CI and the integration workflow
cover `release/*/plan-*`, in addition to the existing release/main targets.

The same installed tool scans both source trees. An identity contains the path,
diagnostic code, normalized message and enclosing qualified symbol with a
statement fingerprint. That fingerprint also distinguishes equal-message
errors in different assignments inside the same function. Duplicate
identities retain their counts. Line shifts do not create findings; fixing one
finding cannot pay for a different finding. Every code blocks a new identity.
The legacy `compare_*_baseline` count commands remain available for debt reports;
they do not drive the Ruff or mypy CI gates.

The controls gate rejects task changes to `scripts/reshape/baselines/`, Ruff or
mypy configuration, and pytest's collection scope settings. Alternate and nested
`ruff.toml`, `.ruff.toml`, `pytest.ini`, `.pytest.ini`, `pytest.toml`, `.pytest.toml`,
`mypy.ini`, `.mypy.ini`, `tox.ini`, `setup.cfg`, and `pyproject.toml` files are
protected as well. Root pyproject package metadata can still change; its tool
controls remain protected. Ruff discovers its configured scope with Git ignores
disabled, then checks committed paths in that scope. A committed ignored file is
linted, while an untracked dependency checkout cannot add findings to only one
side of the comparison. The gate also counts
suppression comments by path, attached statement and symbol. A new suppression,
or one moved onto another statement, fails. Existing structure violations are
still compared with the committed structure baseline by `make structure`; the
controls gate does not replace or enlarge that baseline.

## Trusted gate policy and one-time installation

The separate `Gate integrity` workflow executes `scripts/ci/protect_gate_paths.py`
from the trusted default branch with `python -I`. It uses only the standard
library, has read-only repository/PR permissions, installs no project code,
and never checks out or imports the PR tree. It reads Git blobs between the
PR's merge base and exact head SHA. The following changes require approval:

- `scripts/reshape/**`, `scripts/ci/**`, their top-level package initializer,
  and the collection-check entrypoint/error allowance;
- `tests/lanes.py`, `Makefile`, all workflow files, and CODEOWNERS files;
- requirement/lock files, the Python version file, and gate-tool dependencies
  in pyproject (Ruff, mypy, pytest/plugins, coverage, YAML stubs and NumPy).

Approval must be a current `APPROVED` GitHub review of the **exact head SHA**
from an explicit login in the repository variable `ASTRID_GATE_OWNERS`.
An old-head approval, another reviewer's approval, a later change request or
dismissal cannot authorize changed gate bytes. No configured owners means
protected changes fail closed. Ordinary source-only changes need no such review.

**Bootstrap is a Lead integration action, not a capability of this task PR.**
The packet's base has no trusted policy. The Lead must approve/install this
workflow and script on the default branch, configure the named owner logins,
and require this particular trusted workflow through an organization ruleset
for the release destination branches. A candidate-defined check with the same
name is not a substitute for that workflow requirement. Confirm the repository's
Actions event policy allows this narrowly scoped `pull_request_target` workflow.
This packet does not change repository settings or push the default branch.

After owner review, rerun the latest PR-triggered integrity job so it reads the
new review without changing the approved commit. A `workflow_dispatch` run does
not satisfy required PR checks. For local negative probes, run the trusted copy
with `python -I /trusted/scripts/ci/protect_gate_paths.py --base-ref <destination>`;
local mode never grants approval. These deployment requirements follow GitHub's
[ruleset workflow events](https://docs.github.com/en/enterprise-cloud@latest/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/troubleshooting-rules#supported-ruleset-workflow-events),
[required-check eligibility](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks#checks-from-some-workflow-jobs-are-not-evaluated)
and [trusted trigger guidance](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).

## Collected tests and stored results

CI's existing test commands run through a wrapper, retaining their selectors,
timeouts and shard parameters:

```sh
python -m scripts.ci.run_pytest -q -rA --junitxml=/tmp/evidence/unit.xml tests/test_example.py
python -m scripts.ci.check_results /tmp/evidence --expected 1
```

Before imports, the collection guard parses the requested test source files.
It counts module/class test definitions (including local inherited methods),
respects configured naming and explicit file/method selectors, and compares
those definitions with the collected cases. Parameterization expands one source
definition into several cases, all retained in the runtime inventory. Missing
definitions fail even when a hook returned `[]` or code assigned `__test__ = False`.

The wrapper first runs an independent `--collect-only` census. A plugin records
every item before selection, freezes its keyword/marker selectors, recomputes
the declared marker, keyword and shard selection, and verifies the selected and
deselected inventory. Lane markers must be source-declared; the protected
registry's `INTEGRATION_FIXTURES` rule remains supported. A hook cannot add a
lane marker after collection to silently exclude a previously unmarked test.
The executing
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
