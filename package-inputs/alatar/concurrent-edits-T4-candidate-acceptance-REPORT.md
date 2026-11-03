# 10_T4_CANDIDATE_ACCEPTANCE — PASSED-FOCUSED T4

**BLOCKING: full frozen hosted T4 is not accepted.** On the exact joined candidate, the unchanged hosted selector collects 13 and finishes **2 passed, 1 failed, 10 serially unrun**, exit 1. J1/J2 pass. Interleaving-1 fails at `tests/e2e/local/three-products.spec.ts:659`: expected `accepted`, received `preview`. The frozen App helper calls `timeline.retry(pending_id)` but never commits the returned preview. Agent candidate `499122e9` preserves the original flight when that preview is committed; the helper does not reach that operation.

App **73/73**, Runtime **31/31**, Agent new **11/11**, and Agent frozen focused **27/27** pass. These focused results do not establish full T4. Candidate-alone full green was not achieved, so **no exact-base/candidate paired execution occurred**, and no no-new-failures claim is made. Verification stopped after the hosted failure. No product source, test, contract, tuple pin or frozen assertion was edited; no fix loop or push request was started.

## Exact source provenance

| Product | Exact base | Exercised candidate | Candidate commits after base |
|---|---|---|---:|
| App | `7b097db66df6bf9f507281e12e083966b3c5219a` | `bd49c6943a68efe32f4e128373a2dbc5bb93a0cb` | 9 |
| Runtime | `4edf06f375d598c4e44561dbb50242d15454bd86` | `3ecfdff0b88556c515595a0c31981ee484f3d6bf` | 2 |
| Agent | `c16843078bbe4df7b568825bdaf7137961087051` | `499122e970020a63d4c5c0c2eac89b4d9fc073e3` | 1 |

All work was in this task's `work/` shared-object clones. Exact prerequisite objects came from read-only `work/03/app`, `work/04-r3/runtime`, and `work/05/agent`; candidates were imported from the retained task 08 incremental App/Runtime bundles and task 09 Agent publication bundle. Every bundle verified before import, each candidate SHA and ancestry from its frozen base were checked, and source working trees were clean. App reconstructed tree is `d9bc4c0b886539abf4c56b4dc9fadfeb4572cf14`; Runtime `28cf726470f787d0d26c3b7dd3fdb0d2fc5b6c7a`; Agent `03220d0312ef81c0c0676226a76a36427089985c`.

[reconstruction.log](logs/reconstruction.log), [tuple.json](logs/tuple.json), [input-sha256.txt](logs/input-sha256.txt), and [source-status.json](logs/source-status.json) retain commands, full parent chains, hashes, exact heads, clean statuses and diff checks. No moving branch was fetched or rebased. Existing bundles remain in their original task folders; this task creates no product patch or bundle.

The requested single Luna helper performed independent read-only provenance/selector preparation, without tests, installs or source edits. [sub/1/REPORT.md](sub/1/REPORT.md) was read after its exit 0 and before acceptance execution; [helper-poll.log](logs/helper-poll.log) records polling. Peak task workers: two including the Sol and helper; helper completed before testing.

## Candidate-only execution and paired matrix

All node collections were nonzero and recorded before execution. Exact selected cases are in [app-unit-nodes.txt](logs/app-unit-nodes.txt), [runtime-concrete-nodes.txt](logs/runtime-concrete-nodes.txt), [agent-frozen-nodes.txt](logs/agent-frozen-nodes.txt), [agent-new-nodes.txt](logs/agent-new-nodes.txt), and [hosted-candidate-collect.log](logs/hosted-candidate-collect.log). Execution order is in [execution-order.txt](logs/execution-order.txt).

| Frozen selection | Candidate alone | Exact-base paired lane | Candidate paired lane |
|---|---|---|---|
| App four unit files, 73 nodes | 73 passed, 0 failed/skipped; exit 0; 11.32 s | Not run: full candidate gate failed | Not run |
| Runtime exact 31 nodes | 31 passed, 0 failed/skipped; exit 0; 7.59 s | Not run | Not run |
| Agent new 11, first | 11 passed, 0 failed/skipped; exit 0; 46.11 s | Not run | Not run |
| Agent frozen 27 | 27 passed, 0 failed/skipped; exit 0; 55.02 s | Not run; no test overlay applied | Not run |
| Hosted frozen full 13 | 2 passed, 1 failed, 10 unrun; exit 1; reported 1.4 min | Not run | Not run |

The Agent's new 11 passed before its full 27, and every focused candidate selection passed before the real hosted run. Agent focused tests use the report-prescribed lock-selected Runtime/SDK `33e9195a8ac57c4ba61f372ef6f25139c37eaa97`, not the joined hosted Runtime. They are standalone diagnostic evidence. The incomplete selected VibeComfy dependency described below further limits environment qualification; raw green counts are retained without claiming a fully reconstructed dependency environment. Runtime focused tests and hosted execution use candidate `3ecfdff0...`. Historical task 09 base/head results have not been relabelled as this task's paired results. Base-identical failing test-node census and new candidate failure comparison are **not available**, because no base test execution was authorized after the failed candidate gate.

Verbatim test logs and exit files: [app-unit-candidate.log](logs/app-unit-candidate.log) / [JSON](logs/app-unit-candidate.json), [runtime-candidate.log](logs/runtime-candidate.log), [agent-new-candidate.log](logs/agent-new-candidate.log), [agent-candidate.log](logs/agent-candidate.log), [hosted-candidate-final.log](logs/hosted-candidate-final.log). No broad suite, test deletion, assertion weakening, xfail or skip directive was introduced. The ten hosted unrun cases are the unchanged serial suite's stop-after-failure behavior.

## Exact selectors and runners

Hosted collection and execution used the requested identical grep through [run-browser.sh](logs/run-browser.sh), with execution through `/home/user/astrid/ops/heavy.sh`:

```sh
npm run test:e2e:three-products -- --grep 'hosted-loopback.*(J1|J2|J3|J4|J5|recovery|interleaving)' --list --reporter=line
npm run test:e2e:three-products -- --grep 'hosted-loopback.*(J1|J2|J3|J4|J5|recovery|interleaving)' --reporter=line
```

The App `vitest list` collection then `vitest run --maxWorkers=1` selected exactly:

```text
src/integrations/runtime/client.timelineEditing.test.ts
src/integrations/runtime/timelineCommit.test.ts
src/tools/video-editor/hooks/useTimelineTrackManagement.persistence.test.tsx
src/tools/video-editor/commands/localSaveSession.test.ts
```

Runtime copied the unchanged [runtime-selectors.txt](logs/runtime-selectors.txt) from task 08 and used [run-runtime.sh](logs/run-runtime.sh) with `--collect-only -q`, then `-q`; this expands to the recorded exact 31 nodes. Agent copied task 09's exact 11/27 node lists and used [run-agent.sh](logs/run-agent.sh), first collecting/executing the 11 and then collecting/executing the 27. The mandatory fenced-flight and saved-pending/cancellation sync nodes both pass. All checked-in changed journey support is loaded through the frozen hosted spec, including `support/astrid.ts`, `support/harness.ts`, and `support/t4-acceptance-bootstrap/sitecustomize.py`. The independent helper report maps these and all directly exercised changed production paths.

## Real hosted failure and partial evidence

[hosted-facts.json](logs/hosted-facts.json) is the compact factual projection; [hosted-evidence.json](logs/hosted-evidence.json) retains requests/outcomes. The complete harness summary, failure trace/error context and Runtime streams are under [logs/hosted-candidate-final/](logs/hosted-candidate-final/).

The exercised composition is the production hosted App build -> Runtime browser door -> real Runtime, plus direct Agent SDK -> **that same Runtime**. All three executed cases attest realm `a9a8bce47fd44bf29322998f15b5cca3`, Runtime instance `397dbd02617b483882ea63d77f636caa`, and Agent import from this task's exact candidate checkout. App/Agent/Runtime share digest `sha256:8550fe508770301e1157dee1237e595db3a17c106ac9666313d485bd5bc9ebe1`. Summary captures the exact three candidate heads. No mocked App or Runtime substitutes for the exercised path.

- **J1 passed:** acknowledged App presence precedes save; held Agent requests receive `user_editing`. After user save/release, same-field Agent work conflicts softly, retains `Hosted user wins J1`, and provides useful re-derive guidance.
- **J2 passed:** held disjoint Agent work is delayed, then accepted after App save/release; both values survive. This verifies compatible post-save work in the ordinary active-flight path.
- **BLOCKING — interleaving-1 failed:** the real serialized acceptance barrier rechecks the lease after provider preparation; the App draft and original head survive refusal. App save and release succeed. The durable Agent pending reason is `user_editing`. There are **97 identical non-dry-run original-body/key POSTs**, all 409, and **zero accepted non-dry-run POSTs**; one later 200 POST is a dry-run clash probe, not an accepted replay. Retry returns `status=preview`, `next=tl.commit(r)`, and the unchanged assertion fails at line 659 before accepted replay/readback assertions.

**BLOCKING — verified composition cause (R4):** App `tests/e2e/local/support/astrid.ts:371` only calls `timeline.retry(pending_id)`. Agent `astrid/sdk/timeline/retry.py:611-627` returns a reviewable preview for compatible work; its new exact-flight resume is dispatched by `commit_preview` at lines 738-739 when the caller commits that preview. The frozen helper never does so. This task's 11/27 standalone passes exercise that public commit operation, but cannot cause the hosted helper to call it. J4 uses the same helper and remains unrun. [helper-source-evidence.txt](logs/helper-source-evidence.txt) retains exact frozen snippets. This is not evidence of a failure inside the Agent's resume implementation. Manager must separately scope any helper/source/test change; this task performs none.

**BLOCKING, resolved — runner setup (R4):** the initial launch failed before Runtime startup because its log directory used a full path while TMPDIR used a short alias. The harness correctly required a lexical private child of TMPDIR. Only task-local `run-browser.sh` was corrected to use `$TMPDIR/runtime-logs`; the unchanged full selector then ran as above. Initial fixture failure (0 passed, 1 failed, 12 unrun) and trace are retained in [hosted-candidate.log](logs/hosted-candidate.log) and [hosted-infrastructure/](logs/hosted-infrastructure/). This was an infrastructure correction, not a product fix or a second candidate acceptance variant.

## Frozen acceptance mapping

| Acceptance line | Current evidence and disposition |
|---|---|
| 1: J1/J2 | Verified current joined passes above. |
| 1: unchanged draft recovery | **DEFERRED:** historical accepted task 04-r4 evidence applies only to unchanged portions. Current full selector stops before recovery. |
| 1: J3 command-list and commit-fn saved stale base | **BLOCKING:** both collected, neither executed after serial stop; no current joined pass claimed. |
| 1: J4 configured 15-second bounded wait and cancellation | **BLOCKING:** J4 collected but unrun. Strict race uses the configured 15-second bound and saves a pending original flight; this does not substitute for J4's full assertions. Agent standalone cancellation nodes pass. |
| 1: J5 App/App same-field recovery | **BLOCKING:** hosted J5 unrun; mounted four-file App selection passes its live-draft/original-basis regression. |
| 2.1: admission vs serialized acceptance | **BLOCKING:** real barrier/gate/draft/save assertions reach pending retry; final acceptance fails with preview. |
| 2.2: lost-response replay | **BLOCKING:** hosted case unrun; selected Runtime receipt/replay and Agent fenced-flight nodes pass as focused evidence. |
| 2.3: independent two-tab release and stale release/renewal fencing | **BLOCKING:** full hosted coverage incomplete; Runtime authority/sequence tests and App ACK/release tests pass. |
| 2.4: crash/TTL | **BLOCKING:** hosted TTL case unrun; Runtime expiry nodes pass. |
| 2.5: process restart limit | **DEFERRED:** documented process-local boundary remains; hosted restart case collected but not reverified here. |
| 2.6: spoofed source.via=app and authenticated App exemption | **BLOCKING:** focused Runtime active actor/scope/credential/transport nodes pass and real J1/J2 App edits remain usable; spoof/copy-credential hosted case unrun. |
| 2.7: retry cancellation | **BLOCKING:** hosted cancellation case unrun; unchanged Agent cancellation and new resume-cancellation nodes pass. |
| 3: no silent draft replacement; compatible work lands; conflict soft/rederive | J1/J2 pass current joined ordinary path; mounted App and Agent conflict selectors pass. **BLOCKING:** post-deadline hosted pending resume is not completed, J3/J5/recovery remain unrun. |
| 3: original pending body/key/base/fence; transient refusal nonterminal | Standalone Agent exact-flight tests and Runtime transient/replay nodes pass; strict hosted pending body/key remain identical across refusals. **BLOCKING:** hosted resumed acceptance not observed. |
| 4: candidate full green before identical paired selectors/no new failures | **BLOCKING:** candidate full hosted red; no paired base/head runs or no-new-failures claim. |

Focused green and historical green are not combined into an accepted T4 result.

## Environment, strict preflight and remaining findings

App dependencies came from checked-lock `npm ci` (exit 0), Node 22.23.4/npm 10.9.9. Agent uses CPython 3.12.3/uv 0.11.32 with `uv pip sync --require-hashes` on the exact proof/build locks: 67 locked distributions, no loose installs. Agent pin/import/package checks collect 27 and pass: [agent-candidate-pinned.log](logs/agent-candidate-pinned.log). Runtime focused and real hosted execution reuse task 08's documented CPython 3.13.18 interpreter, pinned Chromium `/opt/pw-browsers/chromium-1243/chrome-linux64/chrome`, and explicit candidate roots. VibeComfy selected HEAD is `2967033970cbb5884651c9565265778482d3b44e` (incomplete retained objects; see BLOCKING finding below), lifecycle `29dae06e0ce8f350aac6586b178db6bebeae44b1`. Dependency reconstruction, lock install and distribution inventory are in [dependency-tuple.json](logs/dependency-tuple.json), [python-env.log](logs/python-env.log), and [environment-versions.txt](logs/environment-versions.txt).

Launchers clear ambient PYTHONPATH or build a declared list, select every source root, disable user site packages, and use explicit import checks. Runtime pytest uses `-I` and explicit selected roots/plugin; Agent uses `env -i`/safe path and lock-selected Runtime. Each run has its own task-local TMPDIR, pytest basetemp or Playwright output. `/tmp/t4r10-*` entries are short aliases only; all data stayed task-local. One hosted heavy command ran at a time through the established runner. No pods, external credential access, shared clone mutation or direct pushes occurred.

**BLOCKING — known strict joined consumer-lock association (R5):** unchanged `ops/pinned-env.sh` exits **1**, even though exact roots, imports, versions and collection pass. Agent/App locks select Runtime `33e9195a...`; hosted candidate is `3ecfdff0...`. This task does not weaken the guard or claim it passed. See [pinned-env-joined.log](logs/pinned-env-joined.log) / [exit](logs/pinned-env-joined.exit). Focused diagnostic passes do not cure the strict provenance failure.

**BLOCKING — pre-existing App declared artifact drift (R5):** direct consumer-byte checking finds `src/integrations/runtime/generated.ts` hash `daeac9d049adcae0bfdceae9ef60d91d4d3bd65aa4f58fb0e69ca074411a3c5c`, while its lock declares `f233523999066e0ef2b9bea434eb369d580894b9894ab7e9e9727d469891f4d4`. This exact consumer file is byte-identical on frozen App base and candidate; it is not a new candidate regression. Other 13 App and all 16 Agent declared artifacts match their lock hashes. No lock/generated file changed. [consumer-lock-artifacts.json](logs/consumer-lock-artifacts.json) records declared, locked-source, actual, joined-source and base-identical checks; task 08's reported artifact count does not replace this direct consumer-byte evidence.

Runtime `generators/generate.py --check` and `scripts/artifact_manifest.py --check` both pass exit 0 before hosted freeze. The three product candidates have clean statuses and `git diff --check` exit 0. The separately selected VibeComfy dependency checkout is incomplete as described below; its diff check exits 128. Findings and R4/R5 roots are consolidated in [BUGS.md](logs/BUGS.md); no new R1/R2/R3 issue was identified by this bounded verification.

**BLOCKING — missing VibeComfy prerequisite blobs (R5):** cleanup status review found 23 missing tracked files in the task-local dependency checkout, including `pyproject.toml` (blob `250c0f50df8b9d5607d46e5376fc506dedf6558d`). The retained source store has the requested commit/tree but lacks these blobs. `git checkout --detach 29670339...` emitted `unable to read sha1 file` errors while returning exit 0; the main worker failed to inspect those diagnostics before proceeding. Pin/import checks only established HEAD/import availability, not full checkout integrity. The error was already present in [dependency-reconstruction.log](logs/dependency-reconstruction.log), and [source-status.json](logs/source-status.json) records it before hosted execution. No tests were rerun or source repaired after discovery. All three App/Runtime/Agent candidates are complete and clean; all passing counts remain diagnostic, with this additional dependency provenance limitation. [source-status-final.json](logs/source-status-final.json) retains the exact final status.

**DEFERRED — authorized limits:** lease protection is cooperative and process-local. Runtime restart, offline periods, expiry and pre-ACK gaps cannot guarantee strict user priority. Source attribution does not authenticate App type; exemption depends on the active actor/scope-bound lease credential. A shared bearer holder can intentionally acquire a lease. No stronger application-type identity, restart/offline priority, Pinokio, waiting UI/toast, periodic freshness refetch or broad cleanup is claimed.

## Retention

STOP-DISPATCH was absent and free disk remained above 2 GB throughout (about 16 GB before cleanup). Sources, isolated Python/npm environments/caches, basetemps, TMPDIRs, build/Playwright scratch and short aliases are removed after preserving evidence and checking live references/alternates. Original retained bundles are untouched. [cleanup.json](logs/cleanup.json) records the final cleanup, clean source heads and reference checks. Report/logs/helper evidence and scheduler-owned control files remain. Work completed within the two-hour timebox, starting 2026-11-03 05:19 UTC.
