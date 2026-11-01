---
name: releasing
short_description: "Astrid's release gates and qualification, from candidate to go/no-go."
description: "Astrid's release gates and qualification: the integrated Runtime/Agent/App tuple, protected journeys, the frozen POST /v1/update bridge, the App update guard, coverage and the known-failure ledger, freeze, RC evidence, and the go/no-go checklist, plus where the release recipe and runbooks live. Use when preparing, qualifying, freezing or publishing an Astrid release, when deciding whether a change may land or needs requalification, or when judging release evidence."
metadata:
  astrid:
    audience: developer
---

# Releasing Astrid

A release is one **integrated tuple**: a Runtime, an Agent and an App SHA
qualified together. Repository branches qualified on their own don't count,
and no line of work is dropped to save the date: a slipping line moves the
forecast. Day-to-day iteration is in [Fast iteration](../fast-iteration/SKILL.md);
the repositories and their build commands are in [Developing Astrid](../SKILL.md).

## Where the procedures live

This skill holds the gates. The step-by-step material stays in the
repositories, so update it there rather than copying it here:

| Document | Holds |
| --- | --- |
| `docs/setup/release-recipe.md` (Agent) | The paired-release recipe: release manifest, artifact bundle, installed identity, predecessor transitions and optional native checks. |
| `docs/runbooks/reproducible-release.md` (Agent) | Build, runtime and proof locks, and how to record the toolchain. |
| `docs/testing/NATIVE-TESTS.md` (Agent) | The optional hands-on checks CI cannot reproduce (Pinokio Stop and Quit, Windows consumer setup, logout and reboot). |
| `docs/release/release-1-notes.md` (Agent) | What users are told about supported platforms and updates. |
| `docs/extensions/extension-release-runbook.md`, `docs/video-editor/extension-platform-release-checklist.md` (App) | The App extension ship-quality procedure and its machine-checked checklist; `make release-check` runs the App gate. |

## Never waivable

These surfaces are protected. No coverage policy, waiver, speed rule or
deadline removes them:

- **Protected journeys**: install, update, rollback, keep-data uninstall,
  data migration and the bridge, proved end to end.
- **The frozen `POST /v1/update` bridge.** Its signatures, error vocabulary,
  stop payload, proxy policy and journal linkage are frozen for
  qualification; changing any of them means requalifying. A terminal update
  job without its journal linkage is not evidence. Lost-acknowledgement
  recovery needs a stable operation ID, positive evidence that nothing is
  running, one continuation, journal precedence, and refusal when the state
  is unknown. Recovery never sends an update POST by itself.
- **The App update guard.** The order is: resolve the update actions, flush
  guarded persistence, then start the update. A failed or delayed save or
  flush, a recovery-needed state, edits during the flush, a pending identity,
  an owner change, an unmount, an IndexedDB failure, stale markers and a
  restart replay all mean **no POST**; a successful save means **exactly one
  POST**. Status GETs only observe. A local recovery snapshot is not a saved
  draft.
- **Timeline persistence**, rerun on the integrated tuple.
- **Live GPU evidence at RC.** Advertise only GPU capabilities with live
  evidence; prepared-only runs never satisfy a live gate.
- **Honest evidence.** Nothing fabricated, nothing inferred from a wrapper
  status, a preparation receipt or a historical baseline.

## Coverage and failures

- **Regression-green** means the full declared coverage inventory ran (no
  missing shards, collection aborts or timeouts), contracts and pins are
  consistent, and every remaining failure has an admitted ledger entry.
- Coverage is compared by exact test identity: node ID, parameters, phase,
  platform, failure fingerprint, semantics, tuple and matched-parent
  environment, never by failure counts.
- The **known-failure ledger** records repository, lane or platform, full node
  ID, phase, fingerprint, semantics, baseline tuple, owner, rationale and
  expiry. Only demonstrably inherited, non-protected failures can be waived,
  with matched-parent evidence, an owner and an expiry. A renamed test keeps an
  allowance only through a reviewed old-ID → new-ID mapping.
- Blocking: unexplained test removals, setup or collection failures, missing
  evidence, new regressions and changed semantics.
- Cadence: change-directed coverage at intermediate checkpoints, **one complete
  run of the full suite before freeze**, and the complete journey matrix on the
  RC (below).

## CI tiers

| Tier | Runs |
| --- | --- |
| T0 | Changed-file format and lint, Pinokio syntax. Not the full suite as a pre-push hook. |
| T1 | Affected Agent tests and quality, Runtime unit and TypeScript checks, App Vitest and typecheck, Pinokio Linux units. |
| T2 | Exact tuple SHAs and workflow revisions, full coverage, generated and pin freshness, installed-artifact checks, the fail-closed regression aggregate. |
| T4 | The checkpoint journeys and GPU evidence, run formally on the RC tuple. |

Qualification matrices use explicit timeouts and `fail-fast: false`, and keep
named logs and receipts as artifacts.

## Freeze

Freeze needs every gating task complete, regression-green, and a coherent
tuple whose generated artifacts and installed identities are fresh.
Finalisation runs Runtime → Agent → App with merge commits (qualified SHAs are
never rebased or squashed): it raises the public release number, generates the
public manifest once, and rebuilds the stand-in next release from that
manifest. **Any change after freeze** to the contract, manifest, installed
artifact, harness or workflow revision creates a replacement tuple and
invalidates the affected receipts. Review blockers are closed on the frozen
tuple.

## RC evidence

Every RC receipt binds the exact repository SHAs, tuple, manifest hash,
workflow revision, installed artifact identities, commands, inner exit codes,
coverage, hardware, output digests, cleanup and limitations. The trusted
evaluator rejects missing, skipped, forged, duplicate, stale-tuple,
wrong-artifact and failed evidence. The RC runs:

- the upgrade from the previous release on macOS, Windows and Linux through
  each channel (direct, Pinokio backend, Pinokio fully local): nine journeys;
- interruption handling, rollback to the authentic previous executable,
  keep-data uninstall, and the upgrade to a stand-in next release;
- all six authentic Pinokio slots on the real action engine (reducing them is
  an explicit decision that narrows the release);
- migration of populated previous-release data with backup, interrupted
  migration recovery, restore, rollback and provider registration checks;
- Timeline acceptance and an installed-pack proof through the official
  installer, rendering without a checkout;
- one live GPU batch (not one per lane) and a CPU-only Windows journey.

Carry-forward never applies to protected proof: it stays exact-head.

## Go/no-go

Go only when all of these hold:

1. The exact RC manifest is qualified: all nine journeys, the required Pinokio
   slots and the feature evidence pass, and the aggregate is green with no
   missing evidence.
2. Review blockers are closed on the frozen tuple.
3. Publication evidence exists: qualified launcher and deployment identities,
   served bootstrap and manifest hashes, canary evidence, a clean install, and
   an authentic upgrade from the served previous bootstrap.
4. The final merges land in pin order, Runtime → Agent → App.

Otherwise it is no-go: fix, produce a replacement tuple and rerun what the
change affects.
