# Generation boundary ratchet handoff

Source: `r13/engine-br`, starting at `5cca0bcae`. Comparison parent:
`cef4da11^` = `ddebc6246c6ecd8992823ff6a5ac41d91c797c80`.
The parent checker passes with 313 findings. The starting HEAD has 242 findings,
but fails its per-file/kind/value allowances: 34 excess keys, 73 occurrences.
The checked-in baseline allows 321 occurrences. A lower total does not excuse
growth in an individual key.

## Inventory

Locations below refer to the starting HEAD. Counts are positive normalized
differences against the exact parent, not every surviving historical crossing.
The checker also treats a string equal to any non-core pack identity as coupling.
Consequently, the execution selector `local` collides with the unrelated
rendering-elements pack named `local`.

| Location | Added finding | Purpose and disposition |
| --- | --- | --- |
| `astrid/core/generation/placement.py:227` | Import `journal_leases` from `astrid.packs.runpod.machine`, +1 | Supplies live-machine validation. No existing contribution method lists leases; deferred interface below. |
| `astrid/core/generation/placement.py:23,67,80,135,150,151,166,244` | Literal `local`, +8 | Built-in placement identity, default, selector normalization, and validation. These do not refer to the rendering pack. Left intact. |
| `astrid/core/generation/placement.py:26,101,103,225` | Literal `runpod`, +5 (two occurrences at line 103) | Provider fallback, provider predicate, and journal adapter dispatch. Lease observation needs the interface below; the provider predicate is already reused by callers. |
| `astrid/core/generation/cli.py:739` | Import `failure_receipt` from `astrid.packs.runpod`, +1 | Finds and renders stored failure receipts and orders their logs. No matching neutral reader or CLI extension exists. Deferred. |
| `astrid/core/generation/cli.py:777,810` | Import `journal_leases` from `astrid.packs.runpod.machine`, +2 | Snapshots resource identities and reports machines retained after a command fails. Deferred diagnostic interface below. |
| `astrid/core/generation/cli.py:504` | Literal `local`, +1 | Restricts an on-machine job command to local execution; unrelated to the rendering pack. Left intact. |
| `astrid/core/generation/managed_preparation.py:156,158,267,354,356` | Literal `local`, +5 | Receipt locator, receipt-derived plan, and local backend construction. No pack import is added to the checker inventory. Existing recipe/runner/backend seams already accept placement. No one-line import move resolves these findings; file untouched as instructed. |
| `astrid/core/generation/engine_bootstrap.py:36` | Pack declaration path under `astrid/packs/wan2gp/extensions/generation.engine/`, +1 | Hardcoded bootstrap data closure. A prototype using existing declaration and recipe-reference discovery could not pass the existing source cap and was reverted. |
| `astrid/core/generation/engine_endpoint.py:367` | Literal `local`, +1 | Reconstructs the on-target installation plan from its receipt, preserving its original placement. Left intact. |
| `astrid/core/generation/engine_local_server.py:49,53,55` | Literal `local`, +3 | Local backend, receipt locator, and receipt-derived plan. Left intact. |
| `astrid/core/generation/engine_readiness.py:747` | Literal `local`, +1 | Reconstructs the local receipt plan for readiness verification. Left intact. |
| `astrid/core/generation/engine_startup.py:177,180,253,375` | Literal `local`, net +1 across current sites | Local receipt validation, installer command, and cleanup backend. The added receipt reconstruction is at line 180. Left intact. |

These named files account for 30 added occurrences. Other starting violations
exist in execution, machine CLI, SDK, generation settings/targets/alignment/pins,
and other modules. Those paths were not changed by this repair.

Imports from the bundled `generation` and `vibecomfy` core packs are exempt under
the existing checker policy. No exemption, checker rule, or baseline allowance
was added here.

## Attempted change, reverted

The bootstrap prototype used the existing `engine_recipe.declaration_paths()`,
`load_recipe_index()`, `load_recipe()`, and `pin_set_refs()` seams to assemble its
data closure instead of enumerating pack declaration, recipe, pin, and model-lock
paths. The explicit code closure, archive hashes, symlink refusal, metadata
checks, deterministic encoding, and size caps were preserved.

Recipe source-patch payloads must travel with the recipe: loading a recipe reads
and verifies them. Their omission was a preexisting bootstrap defect. Environment
requirements lock payloads remain outside this planning-only closure; loading a
recipe records their references without reading those payloads.

The correct full closure, including source patches, measured 308,372 archive
bytes and 83,352 encoded source bytes before distribution metadata. The existing
encoded source limit is 65,536 bytes. Three budget-failing variants were reached
(including requirements locks, selecting a recipe, then the complete closure);
work stopped without changing limits. The unsuccessful source and test edits
were restored to HEAD. This handoff changes documentation only and does not
claim to have removed any crossing.

Before implementation, the first bootstrap test also failed because this clone
has no local distribution metadata. Temporary metadata used for diagnosis was
removed. The source-patch omission and complete-closure size need separate
release-manager routing; no replacement transport or second implementation was
built here.

## Interfaces for release-manager routing

No new neutral interface was built.

1. **Read-only live leases on `generation.machine`.** A contribution should
   expose normalized live lease identities without contacting its provider.
   The existing remote machine contribution would implement this using its
   current journal reader. `validate_placement()` would resolve the contribution
   and supply the existing `LiveLeases` callback. The current machine protocol
   only supports claim/start/poll/cancel/fetch/release. Moving the import into
   SDK callers or calling the private machine CLI wrapper merely relocates the
   same dependency.
2. **Stored generation failure evidence reader.** A provider implementation
   should find latest/by-task evidence and expose validated failure information
   and ordered named logs. The existing receipt pack implements storage and
   schema interpretation; the generation failure command consumes the neutral
   reader. `run_logs` only declares and collects files; engine and facade-verb
   registries do not provide this reader or a CLI subcommand extension.
3. **Command failure resource diagnostics.** A provider implementation should
   capture a pre-command identity and describe newly retained paid resources
   after failure. The generic CLI calls that hook; the remote machine pack
   interprets its own journal rows, costs, expiry, and reuse guidance. Deleting
   the current diagnostic would lose useful paid-resource information.

The `local` selector collisions require a separate checker-policy decision,
not a provider interface. Factoring arbitrary constants or assembling strings
only to hide these literals would not repair a core-to-pack dependency.

## Proof

No tests were converted, deleted, skipped, or weakened. All prototype test edits
were reverted with the implementation. The full suite was not run.

Bootstrap runs all used `-x -q` on
`tests/core/generation/test_engine_bootstrap.py`, with `TMPDIR` and `--basetemp`
inside this clone's `.tmp-tests`. Every run stopped at the same test ID:
`tests/core/generation/test_engine_bootstrap.py::test_console_dry_run_and_help_are_isolated_and_exact`.

| Attempt | Failure | Caused by this repair? |
| --- | --- | --- |
| Initial, before edits | Missing or ambiguous distribution metadata | No; checkout environment. |
| First prototype | Source patch replacement treated as a reference object rather than a string | Yes; prototype error, subsequently corrected and ultimately reverted. |
| Including environment lock payloads | Archive budget exceeded | Yes; prototype expanded the payload, ultimately reverted. |
| Selecting recipe data and necessary patches | Source budget exceeded | Yes; prototype payload, ultimately reverted. |
| Complete recipe closure and necessary patches | Source budget exceeded | Yes; prototype payload, ultimately reverted. |

The checker guard test
`tests/test_core_pack_boundary_guard.py::test_core_pack_boundary_ratchet_holds`
fails on the existing 34 excess keys / 73 occurrences. This repair does not cause
it. A requested historical checker-test path,
`tests/stage1/test_core_pack_boundary.py`, does not exist in this checkout; the
attempt to collect it exited 4, with no test IDs run.

Commands used `/home/user/astrid-agent.env/venv/bin/python`, with
`PYTHONPATH="$PWD:/home/user/wt/c-vcroot"` and `TMPDIR="$PWD/.tmp-tests"`.
The parent was extracted under this clone and its own checker executed with
that extracted checkout as the working directory.

| Check | Final result | Change attribution |
| --- | --- | --- |
| Parent `scripts/check_core_pack_boundary.py --check` | PASS, exit 0, 313 findings | Exact requested parent. |
| HEAD `scripts/check_core_pack_boundary.py --check` | FAIL, exit 1, 242 total findings; 34 excess keys / 73 excess occurrences | Same as starting HEAD; no product changes retained. |
| `scripts/check_core_pack_boundary.py --write-baseline` | FAIL, exit 1, refuses growth in those same keys | Baseline unchanged. Its own writer cannot shrink while any key exceeds its allowance. |
| `-m scripts.reshape.compare_ruff_baseline` | PASS, exit 0, 2,117 before and after, baseline 2,117 | No increase. |
| `-m scripts.reshape.compare_mypy_baseline` | NO-RESULT, exit 2 before and after | Both runs stop at 15 missing `yaml` stub errors and one dependency stub syntax error under the configured Python 3.11 target. Full-tree finding count unavailable. |
| `git diff --check` | PASS | Documentation-only final diff. |

An adjusted full-tree mypy diagnostic run targeting Python 3.12 crashed inside
mypy with an unresolved placeholder type. A Python 3.11 target without cache
still stopped on the dependency stub syntax error. Neither establishes a
full-tree type-check result. A separate targeted comparison of the bootstrap
prototype had one preexisting `Path(SimplePath)` argument finding before and
after; that prototype was reverted. The final Python source is byte-for-byte
HEAD, so this handoff introduces no Python type changes, but the normal mypy
gate remains unqualified in this environment.

Temporary tests, archived checkouts, caches, diagnostic logs, and fixture
metadata were removed after recording these results. No commits, pushes, live
generation commands, or provider endpoint calls were made.
