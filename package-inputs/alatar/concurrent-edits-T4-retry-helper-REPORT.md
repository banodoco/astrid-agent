# 11_T4_RETRY_HELPER — candidate hosted green; paired hosted proof still BLOCKING

The unchanged full hosted T4 selector passes **13/13 on the candidate alone**. App 73/73, Runtime 31/31 and Agent 27/27 final focused heads also pass with NEW=[] against their exact bases. **Full frozen acceptance remains unproven because paired hosted execution did not complete.** Focused green is not acceptance.

**Strict joined qualification remains BLOCKING:** the unchanged pinned-env guard rejects consumer-lock association with the joined Runtime, and the App has a base-identical declared generated-artifact hash mismatch. These are not waived or repaired outside scope. No direct push or publication request was made. Manager publication evidence is prepared only if the full frozen behavioral comparison completes.

## Repair and exact provenance

Task 08's earlier helper mislabelled a preview as accepted. The frozen task 10 helper already classified it truthfully as `preview`, but never called the operation that could accept it. Task 11 completes the supported public chain: `timeline.retry(pending_id)` → require `preview` before `timeline.commit(review)` → validate actual accepted/merged/rebased status, receipt/key/publication/base and exact revision/publication readback. Refused and re_derive results remain actionable; structured refusal/conflict during commit retains its pending id and guidance.

The shared transport observer records every initial and resumed **non-dry-run** commit POST, including its active durable fence. Explicit `dry_run=true` preview probes have a separate trace and are retained in the helper result; they cannot satisfy mutation acceptance or immutable-flight assertions. No outbox state is edited by the helper, no new key/body is manufactured, and no preview head is used as an accepted revision.

Only `tests/e2e/local/support/astrid.ts` changes in this task. The spec and every frozen assertion remain byte-identical to task 08 input `bd49c6943a68efe32f4e128373a2dbc5bb93a0cb`. No production App, Runtime, Agent, consumer lock, tuple pin, acceptance line or non-goal changed.

| Product | Exact frozen base | Input candidate | Final head |
|---|---|---|---|
| App | `7b097db66df6bf9f507281e12e083966b3c5219a` | `bd49c6943a68efe32f4e128373a2dbc5bb93a0cb` | `fecf5b3d0d1072d1f55a3b88a89dfc9650dc899c` |
| Runtime | `4edf06f375d598c4e44561dbb50242d15454bd86` | `3ecfdff0b88556c515595a0c31981ee484f3d6bf` | `3ecfdff0b88556c515595a0c31981ee484f3d6bf` |
| Agent | `c16843078bbe4df7b568825bdaf7137961087051` | `499122e970020a63d4c5c0c2eac89b4d9fc073e3` | `499122e970020a63d4c5c0c2eac89b4d9fc073e3` |

App lane `lane/U/11-T4-RETRY-HELPER` has two task commits (`2e27151885d57f3d91a584d8667c6ff06cbe18c7`, then `fecf5b3d0d1072d1f55a3b88a89dfc9650dc899c`), retaining the entire task 08 ancestry. There are 11 App commits after the published frozen base; Runtime has two and Agent one. All subjects end `[skip ci]`; [commit-policy.json](logs/commit-policy.json) and `local-commits-*.txt` preserve the chains. All work used task-local shared-object clones; shared `work/` and prior-task sources were read-only.

Input bundles verified before import: task 08 App/Runtime incremental bundles require their exact frozen bases; task 09 published Agent bundle requires `be4962a1a87f4e383846777fa955a1f19deb2a0f`, and the required local base ancestry was checked. No moving ref was fetched or rebased. See [reconstruction.log](logs/reconstruction.log), [source-tuple.json](logs/source-tuple.json), and [input-sha256.txt](logs/input-sha256.txt).

The explicitly requested single Luna helper completed independent read-only provenance/selector mapping with exit 0. Its [REPORT](sub/1/REPORT.md) was read before tests. [helper-poll.log](logs/helper-poll.log) records exact 30-second polling. No source edits or tests overlapped with the helper.

## Commands, collection and paired counts

| Selection | Candidate alone | Exact base + identical test overlay | Final paired head | New failures |
|---|---|---|---|---|
| Hosted frozen 13 | 13 passed; exit 0 | 1 failed, 12 did not run; exit 1 | Not completed; interrupted launcher / pre-start retry cancellation | Not evaluated |
| App four files / 73 | 73 passed, 0 failed, 0 skipped; exit 0 | 70 passed, 3 failed, 0 skipped; exit 1 | 73 passed, 0 failed, 0 skipped; exit 0 | [] |
| Runtime exact 31 | 31 passed; exit 0 | 25 passed, 6 failed; exit 1 | 31 passed; exit 0 | [] |
| Agent exact 27 | 27 passed; exit 0 | 17 passed, 10 failed; exit 1 | 27 passed; exit 0 | [] |

Every selection collected nonzero before execution. [selector-identity.json](logs/selector-identity.json) records identical complete node lists and fingerprints: 13 hosted, 73 App, 31 Runtime, 27 Agent on candidate/base/head. [comparison.json](logs/comparison.json) retains counts and exact failing-base node census; `*-base.log`/JSON retain assertion signatures. Base-only failures are not candidate failures. Serially unrun hosted base cases, if any, are reported as unrun, never passed/skipped.

Exact frozen hosted commands, unchanged:

```sh
npm run test:e2e:three-products -- --grep 'hosted-loopback.*(J1|J2|J3|J4|J5|recovery|interleaving)' --list --reporter=line
npm run test:e2e:three-products -- --grep 'hosted-loopback.*(J1|J2|J3|J4|J5|recovery|interleaving)' --reporter=line
```

App uses `vitest list`, then `vitest run --maxWorkers=1` on exactly:

```text
src/integrations/runtime/client.timelineEditing.test.ts
src/integrations/runtime/timelineCommit.test.ts
src/tools/video-editor/hooks/useTimelineTrackManagement.persistence.test.tsx
src/tools/video-editor/commands/localSaveSession.test.ts
```

Runtime uses the unchanged task 08 [runtime-selectors.txt](logs/runtime-selectors.txt), expanded to the exact 31 nodes in the identity manifest. Agent uses task 09's unchanged [27 nodes](logs/agent-frozen-nodes.txt), including the 11 saved-flight regressions. Each Python lane uses collection then `-q -o addopts=` with unique task-local `--basetemp`; exact expanded invocations, cwd and timestamps are in [commands.jsonl](logs/commands.jsonl), [execution-order.txt](logs/execution-order.txt), and the `run-*.sh` launchers. No broad suite, xfail, test deletion or weakened assertion was introduced.

Base clones remain at exact base production SHAs. Only identical candidate test/support files are overlaid: App four selected unit files plus frozen spec, astrid/harness/bootstrap support; Runtime three selected changed test files; Agent `test_timeline_retry.py`. [base-overlays.json](logs/base-overlays.json) records identical test hashes and zero changed production paths; full `*-base-test-overlay.patch` files include the added bootstrap. Final heads are clean.

**SDK coverage limit:** unchanged Agent fixtures require consumer-lock Runtime/SDK `33e9195a8ac57c4ba61f372ef6f25139c37eaa97`; standalone 27-node runs use that explicit root on both Agent base/head. They are direct SDK regressions, not a relabelled joined Runtime pass. The real hosted tests separately exercise Agent `499122e9` against the same real Runtime `3ecfdff0` used by the App.

## Real hosted proof and frozen acceptance map

The candidate run's [complete summary and streams](logs/hosted-candidate/) and [compact facts](logs/hosted-candidate-facts.json) capture actual production hosted App build → Runtime browser door → real Runtime, with Agent SDK → that same Runtime. Exact final App/Runtime/Agent SHAs and common digest `sha256:8550fe508770301e1157dee1237e595db3a17c106ac9666313d485bd5bc9ebe1` are recorded. Realm is `46bf84c369d74a48878b2761b973edbb`; two observed Runtime instance ids (`418624ab468a437fb7311e3fa0fd4607`, `66340dcaa7a3466794c19b61dfd7e889`) reflect the intentional restart case. Every same-realm assertion binds both clients to the actual instance for that case. There is no mock App or Runtime substitute.

- **Serialized acceptance:** preview committed to a real merged receipt after App save/release. 93 non-dry-run mutation POSTs, exactly one accepted response, all original body/key/fence identical. Readback pair is revision `authoring-parent-revision-0be61d541d0a34903d96860070e137d8c9763fcd1e10d09fccf5190182adf7cb`, publication `txn-b3c60e40174444f2b6cc0de1a446d911`. Latest user draft/save and disjoint Agent value survive.
- **J4:** configured SDK timeout 15 seconds, observed helper wall wait 19,081 ms including setup/transport; 111 mutation POSTs end in exactly one merged response with original body/key/base/fence. Exact readback pair is revision `authoring-parent-revision-9f9e9f352749a406ab5abd6a13be2865d0b0c4aadfa0b4c2ab046cff5d022e5e`, publication `txn-2442bd47803c4f24ba1547c738e55072`. One successful dry-run preview probe is retained separately in each pending-retry case.

The authoritative [frozen acceptance brief](/home/user/astrid/ws-U/queue/failed/10_T4_CANDIDATE_ACCEPTANCE/brief.md) remains unchanged; its hash is retained with inputs.

| Frozen line / requirement | Evidence |
|---|---|
| 1: J1/J2 | Hosted J1 holds the Agent behind acknowledged App editing, then softly conflicts with useful user-value/re-derive guidance; J2 lands compatible work after save/release. Both candidate cases pass. |
| 1: draft recovery | Last frozen hosted recovery case names the conflict, retains the live draft, and restores it after reload; candidate passed. |
| 1: J3 command-list and function stale-base | Both selected real hosted forms pass with saved pending intent and actionable re-derive guidance; no live user value replacement. |
| 1: J4 configured 15-second wait/cancellation | J4's unchanged deadline, persisted original flight, explicit fenced release, accepted resume/receipt/readback assertions pass; interleaving-7 and SDK cancellation nodes pass. |
| 1: J5 App/App same-field recovery | Hosted J5/interleaving-3 and mounted App persistence regression pass, preserving the second tab's draft and original basis. |
| 2.1: admission / serialized acceptance | Real acceptance barrier rechecks after preparation, refuses while lease is live, then exact saved-flight resume succeeds; 93-POST evidence above. |
| 2.2: lost-response replay | Hosted interleaving-2 replays identical request/key and accepted receipt without another publication; Runtime replay nodes also pass. |
| 2.3: two-tab fencing | Hosted J5/interleaving-3 verifies independent lease owners plus stale release/renewal fencing, with original draft recovery. |
| 2.4: crash/TTL | Hosted interleaving-4 stops renewal, verifies TTL expiry, then lands the SDK flight; Runtime expiry nodes pass. |
| 2.5: documented restart boundary | Hosted interleaving-5 verifies process-local lease loss and documented best-effort limit; no strict restart/offline priority claimed. |
| 2.6: spoofed source.via app | Hosted interleaving-6 holds spoofed/copy-credential Agent attempts; authenticated, active actor/scope-bound App exemption works. Runtime authority/credential/CORS nodes pass. |
| 2.7: retry cancellation | Hosted interleaving-7 and exact saved-pending/resume cancellation SDK nodes pass; saved body/key/fence remain intact. |
| 3: draft safety, compatible post-save work, soft actionable conflicts | J1/J2, both J3 forms, J5/recovery and mounted App selection provide current joined/UI evidence; pending retries in interleaving-1/J4 now have actual accepted receipts and readback. |
| 3: original pending body/key/base/fence; transient refusal nonterminal | Every observed mutation POST in both pending-resume cases retains saved identity; actual merged acceptance follows repeated soft refusals with the same key. SDK exact-flight regressions and Runtime transient/replay selectors pass. |
| 4: candidate full green first; exact paired selectors; no new failures | **BLOCKING:** candidate full green preceded paired work and focused comparisons are green, but hosted base/head comparison is not complete. No full T4 or complete no-new-failures claim. |

All changed helper callers are within the frozen 13 cases; [helper-callers.txt](logs/helper-callers.txt) records the complete caller census. Full changed path map: `support/astrid.ts` retry/transport → `three-products.spec.ts` interleaving-1/J4 and other held-flight cases → real `support/harness.ts` plus `support/t4-acceptance-bootstrap/sitecustomize.py` → Runtime acceptance/store and Agent public retry/commit. The independent helper maps the App client/local-save/useTimelineSave changes, Runtime door/server/service/editing/execution/port/store paths, and Agent retry/sync dispatch to the exact selectors.

## Environment, findings and retention

Checked-lock `npm ci` passed; Node/npm/uv and all 67 hash-locked Python distributions are in [environment-versions.txt](logs/environment-versions.txt). Agent focused env follows the exact proof/build lock recipe (CPython 3.12.3); real hosted/Runtime execution reuses the prescribed shared CPython 3.13.18 interpreter and pinned Chromium `/opt/pw-browsers/chromium-1243/chrome-linux64/chrome`. Explicit roots and cleared ambient Python paths/user site are in each launcher. [dependency-commands.txt](logs/dependency-commands.txt) records complete setup and limitations.

Task-local TMPDIR, pytest basetemp, npm/uv cache and Playwright outputs are unique by run. `/tmp/t4r11-*` are compatibility symlinks only; heavy data stays under this task. Browser execution goes through unchanged `ops/heavy.sh`, one heavy command at a time. The first hosted wrapper took 2442.08 seconds including about 35 minutes queued; Playwright reports 5.9 minutes for the passing run. An initial paired-base queue was cancelled before any output/test execution, then base/head were combined sequentially under one heavy acquisition; [queue adjustment](logs/heavy-queue-adjustment.json) and [exact outer command](logs/paired-driver-command.txt) retain this infrastructure change. No product/acceptance source changed after candidate freeze.

Findings are fully labelled in [BUGS.md](logs/BUGS.md):

- **BLOCKING, repaired/proved (R4):** pending preview was never committed; current candidate full hosted proof observes actual saved-flight acceptance.
- **BLOCKING (R5):** strict joined consumer-lock association remains red. Consumers declare Runtime `33e9195a...`, joined candidate is `3ecfdff0...`. Unchanged guard exits 1 on both private/shared interpreter audits; roots, imports, versions and collection pass. Standalone SDK preflight passes with lock-selected Runtime. See `pinned-env-joined.log`, `pinned-env-shared-joined.log`, and their exit files. No guard or lock was weakened.
- **BLOCKING (R5), pre-existing/base-identical:** App `generated.ts` actual SHA-256 `daeac9d049adcae0bfdceae9ef60d91d4d3bd65aa4f58fb0e69ca074411a3c5c` differs from declared `f233523999066e0ef2b9bea434eb369d580894b9894ab7e9e9727d469891f4d4`. Remaining 13 App and all 16 Agent declared artifacts match. [consumer-lock-artifacts.json](logs/consumer-lock-artifacts.json) includes exact base identity and locked-source checks. Outside allowed scope.
- **BLOCKING, resolved before use (R5):** retained VibeComfy selected objects were incomplete. Exact-SHA fetch/refetch populated the selected snapshot despite missing-history diagnostics. Before tests, [source integrity](logs/vibe-source-integrity.json) verified every one of 3,876 tracked file blob hashes and a clean checkout at `2967033970cbb5884651c9565265778482d3b44e`. Importability is not used as integrity proof. Lifecycle stays at `29dae06e0ce8f350aac6586b178db6bebeae44b1`.
- **BLOCKING, resolved before tests (R5 setup):** uv's initial read-only ambient cache failed; task-local UV_CACHE_DIR corrected setup before hash-locked sync. No loose installs.
- **DEFERRED:** cooperative process-local lease limits across restart/offline, TTL expiry and pre-ACK gaps; Pinokio, toast/waiting UI, periodic refetch, strict restart/offline priority and broad cleanup remain outside scope.

Runtime `generators/generate.py --check` and `scripts/artifact_manifest.py --check` pass exit 0 before hosted execution. Candidate sources and `git diff --check` are clean. `git apply --check` succeeds on exact App input candidate; cached application produces exact final tree `b8f36e204fffd6861682c9fce638475d81ff335e`. [patch-apply-check.log](logs/patch-apply-check.log) retains all checks.

Deliverables: [task patch](patches/11_T4_RETRY_HELPER.patch), [two-commit incremental bundle](patches/11_T4_RETRY_HELPER.bundle), and first-commit incremental retention bundle. The task bundle requires exact `bd49c6943a68efe32f4e128373a2dbc5bb93a0cb` and retains named lane `lane/U/11-T4-RETRY-HELPER`; bundle verification passes. Artifact hashes are in [artifact-sha256.txt](logs/artifact-sha256.txt). No full-history bundle, direct push, external credential, pod or `~/.codex` use occurred.

Cleanup is recorded separately after final results: task-created basetemps/TMPDIR/environments, source/base/patch-check clones, build/Playwright scratch and short aliases are removed only after preserving logs/artifacts and checking live references/alternates. Shared interpreters, shared product repositories and prior-task artifacts are retained. STOP-DISPATCH was absent and free disk stayed above 2 GB. Timebox started 2026-11-03 05:35 UTC; final status is recorded at cleanup.

## Final timebox disposition — full frozen T4 NOT ACCEPTED

Exact-base hosted execution collected 13 and finished 1 failed / 12 serially unrun, exit 1. J1 fails the frozen assertion that the actual App save carries its acknowledged `x-timeline-editing-session` header: expected the live tab session, received undefined. Actual captured source tuple is App `7b097db66df6bf9f507281e12e083966b3c5219a`, Runtime `4edf06f375d598c4e44561dbb50242d15454bd86`, Agent `c16843078bbe4df7b568825bdaf7137961087051`; see `hosted-base/` and `hosted-base-facts.json`.

The paired final head reached the last recovery case but was interrupted by my task-private queue deadline. Observed parent exit is 124; child exit and final verdict were not recorded. Its raw log/streams are retained as `hosted-head-interrupted*`; no full pass or invented child exit is claimed. I attempted a fresh complete run of the identical 13-case head selector under heavy.sh, but it remained queued and was cancelled pre-start (observed outer exit 130, zero cases) because it could not finish within the user two-hour window. This was a launcher planning error after prolonged slot contention; it does not establish a product failure or a complete paired pass.

**Acceptance line 4 and full frozen T4 remain BLOCKING / UNPROVEN.** Candidate-only hosted 13/13 and focused paired green are retained without substituting them for the missing completed paired hosted head. Hosted NEW is NOT EVALUATED; App/Runtime/Agent focused NEW=[] and there are no shared failing focused nodes. Strict consumer-lock association and base-identical declared App artifact drift also remain BLOCKING. No publication request or manager publication package was issued. [final-hosted-status.json](logs/final-hosted-status.json) records exact disposition.

Cleanup completed at 2026-11-03T07:35:03Z: task work/ and tmp/, all basetemps/environments/build/browser scratch, and seven owned short aliases were deleted after preserving evidence and verifying no external live references/alternates. Free disk after cleanup: 17.49 GB. See [cleanup.json](logs/cleanup.json), [cleanup-reference-check.json](logs/cleanup-reference-check.json), and [source-status-final.json](logs/source-status-final.json). Shared interpreters/repositories and prior-task folders remain untouched.
