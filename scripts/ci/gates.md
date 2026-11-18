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

The packet's Makefile gates and CI gate/evidence commands start with
`python -P -m`, with `PYTHONPATH` set to the protected `scripts` directory only.
Their module names are therefore `reshape.identity_gate`, `ci.run_pytest`, etc.
The repository root is not on their startup import path: a root `argparse.py`
or `tomllib.py` cannot exit successfully before a gate checks anything.

The same installed tool scans both source trees. Ruff and mypy run with
`python -I -m` so neither the working directory nor `PYTHONPATH` can replace the
installed tool. Task changes cannot add or alter top-level `ruff`, `mypy`,
`pytest` or `_pytest` packages/modules. A nonzero base followed by zero candidate
findings fails closed and requires scope review, even for an intended complete
cleanup. An identity contains the path,
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
side of the comparison. The gate also scans `.py` and `.pyi` suppression comments
by path, attached statement and symbol, and counts `no_type_check` uses including
import aliases. A new suppression, or one moved onto another statement, fails.
New or changed `.pyi` interfaces also fail: a sibling stub can hide an unchecked
implementation without an explicit ignore comment. A function or class defined
in both a `TYPE_CHECKING` branch and its `else` is also a suppression: the typed
stub can replace the runtime body in mypy's view. The controls gate does not
replace or enlarge any baseline. Structure uses the separately authorized
ceiling and trusted comparison described below.

## Structure ceiling (GL.2)

The owner froze exactly 102 existing structure findings in
`scripts/reshape/structure_ceiling.json` under
[decision 6979176022](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-6979176022).
The initial records are the original 23 entries plus the exact 79 strings from
the [P39-07 proof](https://github.com/banodoco/astrid-agent/pull/397#issuecomment-6976181544).
The file records their source and hashes. The old
`scripts/reshape/baselines/structure_astrid.json` is retained byte for byte:
SHA256 `07b193211dccf453249af6810ce89838d2e2cf0d42b56b0de9456d97609dc232`.
This is one authorized freeze, not permission to regenerate the ceiling later.

`make structure` reports current findings against that ceiling. It rejects
`--write-baseline` for the approved ceiling. The separate **Structure ceiling
policy from merge base** step is also mandatory: it exports `ASTRID_GATE_BASE`
and uses `python -I` to run that snapshot's `check_structure_ceiling.py`, with
that snapshot's structure scanner and its imports, against the candidate tree.
The proposed JSON is data. The candidate cannot supply its own checker, scanner
or accepted record list. The accepted list is always the merge-base copy, so a
record removed in one merge cannot be restored in the next.

The check rejects additions, edited records, altered provenance, and removal
of a record whose finding remains. A real fix can remove its matching entry.
Fixing one finding never buys allowance for another. Missing, empty, truncated,
duplicate or invalid ceiling data fails, as does an incomplete source root or
a scanner that exits before returning. The scan executes directly in trusted
code; no log or result file is parsed as a successful scan, so missing or
truncated output cannot supply a pass. A genuinely empty completed scan is
allowed when no findings remain.

The five packet injections and completeness/source-selection checks run in
`tests/reshape/test_structure_ceiling.py`, included in the existing hosted gate
probes. Their product mutations use private fixture trees. All previous tests
and assertions remain in place.

**Initial installation:** the introducing PR's merge base has neither this
checker nor the new ceiling. Its trusted-policy step therefore fails once with
the missing base checker, under Release 14 Standing ruling 5. There is no
fallback to candidate code and no exit override. The ordinary structure step
can demonstrate the exact 102-record freeze on that PR. After the Lead lands
the owner-approved exact head, subsequent comparisons use the accepted checker
and ceiling. The landing push still compares to the previous head; the Lead's
qualification on the new plan head must name that plan branch as its destination
to demonstrate the installed policy. Both steps remain required.

All changed gate paths require the exact-head `ASTRID_GATE_OWNERS` approval
below, including later ceiling reductions. The Lead owns review, integration,
trusted workflow installation and required checks. Deliberately crafted
runner/import/build-hook tampering and code executed by candidate packaging or
metadata discovery remain limits covered by path protection and human review;
GL.2 adds no defences for those attacks. Gate review is at most two rounds,
with round two checking only the round-one fixes.

## Trusted gate policy and one-time installation

The separate `Gate integrity` workflow executes `scripts/ci/protect_gate_paths.py`
from the trusted default branch with `python -I`. It uses only the standard
library, has read-only repository/PR permissions, installs no project code,
and never checks out or imports the PR tree. It reads Git blobs between the
PR's merge base and exact head SHA. The following changes require approval:

- all of `scripts/**`, since that directory supplies gate startup imports;
- `astrid/core/structure.py`, which defines the structure scanner's rules;
- top-level modules/packages named in `sys.stdlib_module_names`, including
  root `.py`/`.pyi` files and paths inside a matching top-level directory;
- `tests/lanes.py`, `Makefile`, all workflow files, and CODEOWNERS files;
- requirement/lock files, the Python version file, and gate-tool dependencies
  in pyproject (Ruff, mypy, pytest/plugins, coverage, YAML stubs and NumPy);
- `setup.py`, both install scripts, `[build-system]`, and
  `[tool.setuptools.cmdclass]`, which can execute code during installation;
- any `pytest11` table in a `pyproject.toml` or `pytest11` option in a
  `setup.cfg`, including declarations made through other packaging tools.

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
PYTHONPATH="$PWD/scripts" python -P -m ci.run_pytest -q -rA --junitxml=/tmp/evidence/unit.xml tests/test_example.py
PYTHONPATH="$PWD/scripts" python -P -m ci.check_results /tmp/evidence --expected 1
```

Both this wrapper and the collection-error gate launch the protected absolute
`pytest_entrypoint.py` with `python -I -u`. It imports installed pytest before
restoring project/dependency paths for tests. A checkout's `pytest/__main__.py`
cannot rewrite the command before pytest captures its arguments. `-u` keeps
output unbuffered even though isolated Python ignores `PYTHONUNBUFFERED`.

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
Declared `-k` and `-m` come from the protected ini bytes, startup environment
options and arguments captured by the parent wrapper before pytest starts,
in pytest's precedence order. Repeated
flags, argument files and explicit `-o addopts=` retain their normal meaning.
The guard checks `config.invocation_params` against the parent-supplied child
arguments and directory, and never adopts rewritten live `config.option`
selectors. The parent imports no candidate code, retains its declaration and
reference manifest in memory, and checks both manifests against those values.
A mismatch fails even when the lane registry would otherwise omit a whole
module. The executing pass must have exactly the same census.
Explicit `--deselect`, `--ignore`,
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

## Threat model and limits

These in-job checks detect weakening through configuration, suppressions,
markers, collection, selection and inconsistent result evidence. They fail
closed on those paths. They do not isolate arbitrary code executing inside the
same Python process. In particular, a candidate conftest that rewrites outcomes
in `pytest_runtest_makereport`, or replaces test execution with a no-op runtest
hook, can forge internally consistent reports. Protected-path owner review and
later trusted-runner separation address that deliberate code-execution threat;
the census and result checks do not claim to prevent it.

Round 3 closes the four variants named in the Lead's review of Agent #397.
Further variants belong to GL.3's follow-up, including separation into a trusted
job that runs the gates from the base; this packet does not expand the in-job
checks into an arbitrary-code sandbox.

Moving a test into a valid, source-declared lane that this CI job does not run is
legitimate deselection. The gate records that selection but cannot establish
that another job covers the lane. K1a's lane rows own that coverage obligation.
