# Original Windows Bun native receipts

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
