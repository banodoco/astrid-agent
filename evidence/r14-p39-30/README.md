# R14-P39-30: Windows Bun selection and execution

The proposed decoder accepts the complete original step-04 receipt while keeping all eleven identities visible. It records one selected/executed pass and ten deliberately unselected identities. The existing comparator still treats their native skipped outcomes as non-execution requiring trusted disposition. No inventory row, omission allowance, failure ceiling, test, fixture, dependency pin or original Chat command is changed.

Baseline: Agent `3a50507ceef9bf3520f44938e2e8ef6d4ef43a42`. Native source: `b1e53baf5facbb340dfc42b1688f7152c255e351`. The only changes between those revisions are the previously accepted Windows journey driver and its test; Chat source, commands, dependencies and gate policy are identical. The branch and draft PR are `p39/bun-launcher-inventory` → `release/14/plan-39`, [Agent #444](https://github.com/banodoco/astrid-agent/pull/444).

## Observed before and after

| Evidence | Original decoder | Proposed decoder | Native outcomes retained |
| --- | --- | --- | --- |
| Windows step 03, original b1e53baf | Accepted | Accepted | 24 identities: 23 executed PASS, one declared SKIP; raw0 |
| Windows step 04, original b1e53baf | Refused: terminal summary missing/truncated | Accepted as complete native accounting | 11 identities: one executed PASS, ten deliberately unselected; raw0 |
| Earlier official trusted report, e84bbd52 | FAIL/raw1; four collection unknowns | Offline verdict replay stays FAIL; three original collection unknowns | Only the Bun summary refusal clears; original hosted raw1, incomplete CI and both Windows journey custody findings remain recorded |
| Original Linux Chat | 210 observations, 209 unique identities | Same 208 PASS and two SKIP; duplicate still refused by the unchanged comparator | All eleven model-discovery declarations executed and passed in the separate Linux lane; raw0 |
| Retained native Bun failure fixture | One PASS, one FAIL, raw1 | Exact same identities, diagnostic and raw1 | This is a genuine earlier runner fixture, not product qualification |

These after results replay the original archived native executions through the proposed gate code. They are not a new Windows execution or an official hosted verdict on the packet head. The exact published source and current hosted run inventory are in the PR and mailbox submission.

## Correction and trust boundary

The collector reads the named Bun step from the immutable workflow snapshot exported from the comparison base. The command receipt must match that original command exactly; a candidate command and its newly computed digest cannot authorize a different selector. The workflow export now includes `.github/workflows` so the trusted collector can perform that comparison. It refuses unsupported command forms and selector regexes rather than approximating them.

The adapter reconciles JUnit with terminal results by file, nested title and outcome. It checks one terminal finish, all selected outcomes, the filtered count, root and suite counts, and zero selected tests. Windows log path normalization is used only for matching; canonical JUnit IDs retain their original backslashes and line/class/title fields. The normalized `summary_count` remains the full retained identity census; `native_summary_count` separately retains Bun's selected-test count. `selection` lists selected, executed, declared-skipped and deliberately-unselected identities. The ten filtered rows keep outcome `skipped`, with explicit `selection_status: deliberately-unselected`, so the unchanged omission checks still refuse them without a trusted allowance.

The original filter was introduced with the Windows-capable HTTP regression in [2905fd50](https://github.com/banodoco/astrid-agent/commit/2905fd50c00986be03fe67006f77547a7e64a3b5). Several other declarations use POSIX process fixtures. Removing that filter or changing those fixtures would be a separate Windows coverage decision. Their existing Linux execution does not qualify the Windows lane.

## Proof and remaining decisions

All 49 focused controls pass under Python 3.11.18: 19 new named Bun selection controls, the unchanged 27 comparator controls and three unchanged hosted workflow controls. Raw logs and commands are in `focused-proof.json`. The narrow F-code lint comparison retains the identical two pre-existing unused imports in `ceiling_hosted.py` (raw1 on both base and candidate); see `targeted-lint.json`. It preserves a real archived failing native result and rejects changed selectors, digest mismatches, missing trusted workflow, unsupported regexes, every one of eleven removed JUnit identities, missing/duplicate finish, missing/wrong selected terminal identity, inconsistent filtered/outcome counts, a filtered row marked executed, zero selected tests, and inconsistent/interrupted exits. Existing duplicate-ID and omission refusals remain.

The unchanged production policy has no seeded Windows launcher rows or these two lanes in its tier loss lists. A Windows-only diagnostic input exposes 35 unseeded observations and the first declared-skip refusal; other required jobs are deliberately absent in that diagnostic. It is never a qualification result. The later ten filtered omission checks are not credited as reached. Lead owns inventory admission, exact omission/selection policy and tier enforcement, as well as the independent review required for protected gate code.

The existing Linux native collision is `["astrid/chat/sessionConfigOptions.test.ts","20","ACP session config options","reads Claude effort with recommendation capability %s"]`, occurring twice. Both original observations remain. `linux-duplicate.json` retains the two original cases, command, receipt, source blob and verified execution tree; `linux-duplicate-source.ts.txt` is the exact original source. No test rename, suffix, deduplication or assertion edit is proposed.

## Reproduce and inspect

```sh
python -I scripts/ci/test_bun_selection.py -v
python -I scripts/ci/test_failure_ceiling.py -v
python -I scripts/ci/test_ceiling_hosted.py -v
python -I evidence/r14-p39-30/replay.py --output /tmp/p39-30-replay
```

`proposed-windows-job.json` contains the complete proposed native interpretation of all 35 original identities. `per-id-ledger.json` retains their original source, command, native outcome and receipt. `replay.json.gz` retains before/after suites, the unchanged official verdict, the proposed frozen-report verdict, Linux control and real failure fixture. `earlier-proposed-observed.json.gz` is the frozen original aggregate with only the now-decodable Windows job added. `summary.json` is the compact replay result.

The reproduction command also emits exhaustive `windows-only-merge.json.gz` and `windows-only-release.json.gz` diagnostic verdicts. Their unrelated missing lanes are intentional probe setup, not observed product failures; the compact committed replay includes their hashes and the packet's 36 findings. The original official/native archives are preserved byte for byte below. The source snapshot archive contains source and metadata only; generated bytecode from the initial capture was removed without altering any source byte or original receipt ZIP.

## Original Windows receipts

Exact landing: `b1e53baf5facbb340dfc42b1688f7152c255e351`. Native artifact ZIP SHA256 `d09886c2c17d4215f829ee15aad20aa097023547815ccda9297f50abcde5a315`.

Both source command receipts declare `complete: true`, `raw_exit: 0`, shell `pwsh`, matrix index 0, attempt 1. Native runner: Bun 1.3.14 (0d9b296a).

## step-03

```powershell
bun test --reporter=junit --reporter-outfile="$env:CEILING_STEP_DIR/bun-results.xml" ./astrid/chat/discoveryProbe.test.ts ./astrid/chat/astridAcpLauncher.windows.test.ts ./astrid/chat/acpProcessHost.test.ts
```

| File | Line | Test identity | Native outcome |
|---|---:|---|---|
| astrid/chat/discoveryProbe.test.ts | 9 | Windows discovery probes route npm command shims through cmd.exe | executed_pass |
| astrid/chat/discoveryProbe.test.ts | 23 | native Windows discovery launches a harmless .cmd probe through cmd.exe | executed_pass |
| astrid/chat/discoveryProbe.test.ts | 35 | a discovery deadline kills a wrapper and its hanging CLI child | executed_pass |
| astrid/chat/discoveryProbe.test.ts | 65 | discovery preserves nonzero status diagnostics for explicit signed-out detection | executed_pass |
| astrid/chat/discoveryProbe.test.ts | 73 | an unavailable discovery command rejects promptly | executed_pass |
| astrid/chat/astridAcpLauncher.windows.test.ts | 8 | native Windows ACP profile and context lookups can launch a .cmd Astrid shim | executed_pass |
| astrid/chat/astridAcpLauncher.windows.test.ts | 43 | profile lookup reports safe status without returning child stderr | declared_skip |
| astrid/chat/acpProcessHost.test.ts | 84 | AcpProcessHost > keeps a long prompt active and deduplicated past the control deadline, then delivers its result | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 124 | AcpProcessHost > can cancel a prompt after the control deadline and waits for the agent cancellation response | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 142 | AcpProcessHost > rejects a long prompt when its process exits instead of leaving it pending forever | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 157 | AcpProcessHost > runs Windows command shims through cmd.exe with a quoted entry and arguments | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 171 | AcpProcessHost > forwards initialize/new/close and preserves the opaque OMP session id | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 202 | AcpProcessHost > surfaces the installed agent session-not-found detail instead of generic Internal error | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 217 | AcpProcessHost > explains an unavailable saved model on session/load and permits a new chat | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 217 | AcpProcessHost > explains an unavailable saved model on session/resume and permits a new chat | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 237 | AcpProcessHost > forwards notifications and answers explicit permission callbacks without ambient authority | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 271 | AcpProcessHost > fails closed when a required filesystem callback is absent | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 289 | AcpProcessHost > rejects pending calls when the owned process disconnects and builds the explicit omp acp launch | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 316 | AcpProcessHost > builds the real Astrid launcher command with identity, cwd, and session custody | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 356 | AcpProcessHost > resolves the packaged Astrid profile through the installed command without a user-home seed | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 386 | AcpProcessHost > omits the session override so OMP owns canonical cwd-derived storage | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 421 | AcpProcessHost > pins the hosted installed package profile despite ambient named mode while preserving appended instructions and OMP custody | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 431 | AcpProcessHost > rejects a validation-only custom base override rather than pretending to execute it | executed_pass |
| astrid/chat/acpProcessHost.test.ts | 435 | AcpProcessHost > fails closed before spawn when the canonical prompt file is unavailable | executed_pass |

Original JUnit, command/exit receipt, command text, terminal output and PowerShell executor remain under `step-03/` in `original-launchers-14581372377.zip`.

## step-04

```powershell
bun test --reporter=junit --reporter-outfile="$env:CEILING_STEP_DIR/bun-results.xml" ./astrid/chat/astrid-agent-models.test.ts -t "model discovery allows sequential OMP startup"
```

| File | Line | Test identity | Native outcome |
|---|---:|---|---|
| astrid/chat/astrid-agent-models.test.ts | 85 | model discovery allows sequential OMP startup to exceed the old ten-second cutoff | executed_pass |
| astrid/chat/astrid-agent-models.test.ts | 114 | agent model catalog maps the CLI response to the safe ACP projection and preserves CLI state | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 147 | agent catalog discovery never invokes the global default mutation | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 161 | agent model adapter rejects unsupported schemas and malformed JSON, and redacts CLI diagnostics | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 190 | agent model launch logs ENOENT without exposing child output | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 204 | agent catalog discovery kills a hanging CLI and rejects its partial output | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 238 | the Python Astrid model wrapper and its nested OMP CLI exit on discovery timeout | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 286 | agents endpoint survives a hanging catalog and profile bootstrap, and reaps discovery children | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 286 | agents endpoint survives a hanging catalog and initialize bootstrap, and reaps discovery children | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 286 | agents endpoint survives a hanging catalog and session/new bootstrap, and reaps discovery children | deliberately_unselected |
| astrid/chat/astrid-agent-models.test.ts | 286 | agents endpoint survives a hanging catalog and session/close bootstrap, and reaps discovery children | deliberately_unselected |

Original JUnit, command/exit receipt, command text, terminal output and PowerShell executor remain under `step-04/` in `original-launchers-14581372377.zip`.

## Trusted refusal

Earlier official verdict source is `e84bbd52725cba1da7ac655771f840cb7af25fc2`, run 44580348107, not the exact later landing. It is FAIL with `aggregate collection did not complete` and unknown `('Chat service', 'windows-launchers'): Bun terminal summary missing/truncated`; it also retains unfinished CI and two Windows journey custody unknowns. The prior embedded Windows artifact 14559687499 has byte-identical commands and the same 35 JUnit identity/status tuples as the exact landing.

The ten step-04 JUnit `<skipped />` entries are command-filtered observations; none is credited as executed. Step-03 has 23 PASS plus one platform-declared SKIP. The native source uses `test.skipIf(process.platform === 'win32')` for that skip.

## Exact source and local adapter replay

Original source bytes and their Git blob/SHA256 index are retained under `source-b1e53baf/` in `original-source-and-metadata.zip`. The checked-in workflow selects only the sequential-OMP HTTP test in step-04. All eleven declarations are ordinary tests; ten do not match its `-t` substring. Their Windows behavior is unexecuted in this receipt. Several use POSIX shebang fixtures, so removing the filter is a coverage/portability decision requiring actual Windows execution, not an accounting-only fix.

`original-adapter-replay.json` records a local, unchanged-original-adapter replay: step-03 accepts all 24 native observations; step-04 refuses with `Bun terminal summary missing/truncated`. This replay is an adapter diagnostic and does not constitute an official full trusted verdict on b1e53baf.

The official trusted job 135758987828 preserves `Process completed with exit code 1` at 2026-11-19T19:19:07.8734676Z; its decoded complete log is retained as `trusted-job-135758987828.log` in `original-source-and-metadata.zip`. The original native Chat workflow is terminal success; the earlier trusted workflow is terminal failure.
