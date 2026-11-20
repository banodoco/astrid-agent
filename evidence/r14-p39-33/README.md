# R14-P39-33 — distinct native names for both Boolean recommendation cases

## Result and scope

The only source change is the declaration at
`astrid/chat/sessionConfigOptions.test.ts:20`. It still iterates `[false, true]`
in that order, with a separate lexical Boolean binding for each original body.
The rendered titles now end in `false` and `true`. The setup, body, assertions,
line count and every other test declaration are byte-identical to the destination.
Replacing the one changed declaration restores the entire original file.

The full original Linux Chat command on the local proposal produced **210 native
observations, 210 distinct identities, 208 PASS, two original SKIP, 445 assertions
and raw exit 0**. All 208 unaffected observations retain their exact native IDs,
assertion counts, outcomes and native diagnostics. The two renamed cases each
passed with two assertions. The original 210 observations and ambiguous pair
remain in the retained archive and comparison; no historical Boolean attribution
is invented.

This is submitted for Lead review. Exact published-head hosted proof and its
source/execution custody are reported on the draft PR. The local run is explicitly
identified below and is not a hosted qualification claim.

## Authority and source custody

- Packet, Authority/Standing rulings, workstream and Plan39 K2/K3 rows: dev
  `a12d9e891d54992fa480515027f464829bdb5e9f`.
- Lead decisions: [7025981759](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7025981759)
  and [7026592076](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7026592076).
- Orchestrator released-slot/sole assignment:
  [7027597459](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7027597459).
- Writer `r14-P39-33-1`, topic `p39/chat-recommendation-case-identity`, destination
  `release/14/plan-39`. No other open Agent PR touched this file at startup.
- Verified destination source/execution:
  `3a50507ceef9bf3520f44938e2e8ef6d4ef43a42`, tree
  `4df3dbc6625400c873d2eb08de89accab5a6a648`.
- Published source-only commit: `3ee361a5c50af79e1b7ac8d98f44e6cbc88cb964`, tree
  `0d1c31f7ea36d42cf9d2fcda6c601d19f093cbe7`.
- Local full execution commit: `c3f25128fac5fc493046c9e9807046c611819b8a`, with
  that **exact same tree**. Native Git publication lacked credentials; the
  authenticated GitHub connector published the identical source tree under the
  preceding source commit. Both raw commit objects and the unchanged native
  receipt preserve this distinction. The evidence-only child changes no source.
- Original test blob `aecd048f540f915ccd253af1156c9e86481c0452`; proposal test blob
  `9d5a1ced4d4eaf16b8092594e27d4f3fd1aa811d`.
- P39-30 #444 remains frozen at `eff1c195a5ae9fad83baa4310e6288c30e93fc9b`.

No product, selector, reporter, workflow, comparator, inventory, omission,
dependency, Plan38/55 test, destination or board change is included. The
comparison script here parses evidence only; all production collector and policy
files are byte-identical to the destination. See `source-preservation.json`.

## Original affected file before and after

Both executions used Bun **1.3.14 (0d9b296a)** and the original complete file:

```sh
bun test ./astrid/chat/sessionConfigOptions.test.ts --reporter=junit --reporter-outfile=<retained-path>/bun-results.xml
```

Every native ID has file `astrid/chat/sessionConfigOptions.test.ts` and classname
`ACP session config options`. The table retains both ambiguous observations;
the sequence is unchanged, but the original receipt itself does not identify
which Boolean produced either old observation.

| Line | Original rendered name | Proposed rendered name | Assertions before/after | Outcome before/after |
| --- | --- | --- | --- | --- |
| 9 | reads Codex base model and reasoning effort, ahead of its legacy model ID with effort suffix | unchanged | 2 / 2 | PASS / PASS |
| 20 | reads Claude effort with recommendation capability %s — occurrence 1 | reads Claude effort with recommendation capability false | 2 / 2 | PASS / PASS |
| 20 | reads Claude effort with recommendation capability %s — occurrence 2 | reads Claude effort with recommendation capability true | 2 / 2 | PASS / PASS |
| 31 | identifies categories with opaque IDs and handles OMP grouped models and thinking | unchanged | 1 / 1 | PASS / PASS |
| 38 | names levels in sentence case | unchanged | 1 / 1 | PASS / PASS |
| 44 | does not invent levels for Hermes without a thought_level option | unchanged | 2 / 2 | PASS / PASS |
| 44 | does not invent levels for Astrid without a thought_level option | unchanged | 2 / 2 | PASS / PASS |

Both raw exits are zero: **7 PASS, 12 assertions**. Distinct native IDs change
from six to seven. `focused-before/` and `focused-after/` contain raw JUnit,
terminal logs and execution metadata. `focused-comparison.json` retains the
complete seven-observation comparison and unchanged trusted suite replay.

## Full original Linux command

The exact checked-in command bytes are retained, with SHA256
`6cef40b0fcaace2de4cc5fecbe2824901c8cb70983c1fba8b9c6d08827b8edba`:

```sh
bun run test:chat --reporter=junit --reporter-outfile="$CEILING_STEP_DIR/bun-results.xml"
```

| Native measurement | Original hosted destination | Local proposal |
| --- | --- | --- |
| Observations / distinct IDs | 210 / 209 | 210 / 210 |
| PASS / SKIP / FAIL | 208 / 2 / 0 | 208 / 2 / 0 |
| Assertions | 445 | 445 |
| Raw exit | 0 | 0 |
| Unaffected observations | 208 | All 208 exact IDs/assertions/outcomes/diagnostics retained |
| Recommendation pair | Two ambiguous PASS observations | Two distinct PASS observations, two assertions each |

Full terminal rosters equal the native JUnit multisets on both sides. After only
the two proposed titles are substituted back to the historical literal, terminal
rosters and result order within every file match. Global file traversal order
can differ; the original JUnit and terminal already have different serialization
orders. No observation is discarded or deduplicated.

The original archive is [run 44635819742, artifact 14583582456](https://github.com/banodoco/astrid-agent/actions/runs/44635819742/artifacts/14583582456),
Linux job `135953917442`, with source/execution `3a50507c`. Its exact 19,895 bytes
are retained in `original/original-linux-chat-14583582456.zip`; SHA256
`550111b2a33dd976ec61e41e9ada226ff1889f95fdb80930e901932443d240e3`
matches GitHub's artifact digest. The hosted environment used Ubuntu 26.04.1,
image `20261111.196.1`, Node 20.19.4, npm 10.8.2 and Bun 1.3.14.

The local proposal used the unchanged capture shell and original command on Linux,
Bun 1.3.14, Node 24.22.3, Python 3.12.14 and locked zod 4.3.6 installed with
`npm ci --ignore-scripts --offline`. `local-full/execution.json` distinguishes this
local environment. `local-full/failure-ceiling/chat/step-06/` retains the unchanged
capture receipt, raw log, command and JUnit. `local-full-comparison.json.gz`
contains both complete native rosters and comparison; `local-full-summary.json`
contains the compact conclusions.

The two original skips remain zero-assertion skips, with unchanged native IDs:

- `astrid/chat/discoveryProbe.test.ts:23`, empty classname,
  `native Windows discovery launches a harmless .cmd probe through cmd.exe`.
- `astrid/chat/astridAcpLauncher.windows.test.ts:8`, empty classname,
  `native Windows ACP profile and context lookups can launch a .cmd Astrid shim`.

## Unchanged trusted collector and remaining policy

The unchanged `bun_junit` adapter retains all 210 original observations. Its
unchanged `verify_suite` refuses the historical stream with
`empty/duplicate collected IDs: Chat service/chat/step-06`. The corrected stream
passes that bounded suite check. This is not a full trusted hosted verdict.

The original policy has **zero seeded Chat service/chat inventory rows**, and
this lane is absent from both tier loss lists. This packet does not admit the
210 identities or approve either skip. Full trusted evaluation, remaining
inventory/omission findings, protected decisions and release qualification stay
with the Lead and #984. Original Windows selection findings and #444 remain
unchanged.

## Reproduce the comparison

Extract the retained original ZIP to a temporary directory, then run:

```sh
python3 -I evidence/r14-p39-33/compare_native.py \
  <original-extraction>/step-06 \
  evidence/r14-p39-33/local-full/failure-ceiling/chat/step-06 \
  /tmp/p39-33-native-comparison.json
```

For exact published-head hosted proof, use the candidate archive's `step-06` as
the second argument. The PR records immutable run/job/artifact IDs and the actual
execution commit/tree. `manifest.json` records SHA256 hashes for this evidence
pack; the original raw records remain unchanged.
