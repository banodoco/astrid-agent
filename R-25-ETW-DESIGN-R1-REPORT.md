# R-25 ETW design report

Recorded: 2026-11-02T18:52:15Z

## Outcome

Delivered a Windows-only ETW continuous-custody design in [DESIGN.md](./DESIGN.md). It identifies the exact system-process provider control GUID, process event class GUID/event types, named system-logger session profile, privileges, isolation boundary, authenticated interval/tuple, initial/final census protocol, sequence/loss records, and fail-closed cases.

Decision: ETW is feasible for the Windows continuity gap, but this design is **not an implementation, hosted-run result, accepted R-30 profile, qualified receipt, or release claim**. Strict §2.3 remains conditional because the reviewed Microsoft documentation provides loss counters but no documented global source sequence for these kernel process events, and hosted-runner supervisor isolation/privilege availability is unresolved.

## Inputs inspected

| Input | Result |
|---|---|
| `ORCHESTRATOR.md` entries `2026-11-02T18:44:58Z` and `18:47:24Z` | Read. The 18:44 correction says snapshots cannot satisfy continuous custody; the 18:47 note says Windows continuity remains ETW on hosted runners. |
| `/home/user/merge-docs/docs/merge-plan/handoff/inputs/M-QUAL-R-30-verifier-interface.md` | Read, SHA-256 `2f685c8aec21bef116f95645920717e4b12ef33c1ca820fe6185eb01ffea115d`; §2.3 is the governing lifecycle/completeness rule. |
| Agent `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` | Read. `scripts/m4_6_pinokio_custody_provider.py:664-666` refuses polling-only continuity. Commit parent `fb222b3a54e9ffe2fa4eb2c382bc25033e773949`. |
| Existing snapshot work | Read `queue/done/R-25-SNAPSHOT-COLLECTORS-R1/REPORT.md` and receipt. Windows profile is `windows-cim-task-service-snapshot-v1`, explicitly no ETW; it is non-continuous. |
| Requested source `4a8bca56d672a99bb73bd2b2f2ea07948443b841` | Not available in the checked-out Agent repository. `git show` and direct fetch failed; no claims are made about its contents. |

## Exact design disposition

The recommended binding is:

```text
control provider: SystemProcessProviderGuid
control GUID:     151f55dc-467d-471f-83b5-5f889d46ff66
keyword:          SYSTEM_PROCESS_KW_GENERAL
legacy mapping:   EVENT_TRACE_FLAG_PROCESS = 0x00000001
event class:      ProcessGuid
event GUID:       3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c
session:          one fresh run-owned named system logger, "Astrid ETW Custody"
mode:             EVENT_TRACE_SYSTEM_LOGGER_MODE + real-time consumer + sequential ETL
events:           Process Start/End/DCStart/DCEnd (types 1/2/3/4)
identity:         payload ProcessId/ParentId + EVENT_ENABLE_PROPERTY_PROCESS_START_KEY
```

`Microsoft-Windows-Kernel-Process` `{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}` is recorded as a separate Windows manifest-provider identity, not silently conflated with the selected System Provider control path. The design explains why the emitted Process class GUID must be checked.

## Coverage

- Authenticated interval and full R-30 tuple/obligation binding: designed; not deployed.
- Session lifecycle and exact child ownership: designed; not implemented.
- Controller/Runtime/descendant incarnation tracking: designed with process-start key; not implemented.
- Independent initial and final censuses: required as separate OS observations and ETW DCStart/DCEnd reconciliation; not run.
- Event ordering: supervisor-local contiguous sequence plus ETW timestamp/class/type; source sequence gap remains unresolved.
- Loss detection: `EventsLost`, `LogBuffersLost`, `RealTimeBuffersLost`, `TRACE_LOGFILE_HEADER` fields, buffer callback and query baselines; all nonzero/unavailable paths fail closed.
- Privilege/isolation: admin/Performance Log Users/approved service route and `SeSystemProfilePrivilege` must be measured and authenticated; hosted availability unknown.
- R-30 qualification: no claim; no receipt qualification.

## Commands and exits

Read-only commands executed in `/home/user/merge/ws/M-PIN`:

| Command | Exit | Purpose |
|---|---:|---|
| `rg --files ...` | 0 | Locate orchestrator, handoff, queue reports and source files. |
| `sed`/`tail` on `ORCHESTRATOR.md`, handoff interface and snapshot report | 0 | Read the two requested orchestrator entries, §2.3 and prior-art limitations. |
| `git show --stat ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` | 0 | Verify the published Agent commit. |
| `git show ab43...:scripts/m4_6_pinokio_custody_provider.py` | 0 | Inspect the provider refusal and fail-closed semantics. |
| `git show 4a8bca56d672a99bb73bd2b2f2ea07948443b841:...` | 128 | Requested source object is absent locally. |
| `git -C repos/astrid-agent fetch --no-tags origin 4a8bca56d672a99bb73bd2b2f2ea07948443b841` | 128 | Remote returned `upload-pack: not our ref`; source remains unresolved. |
| `sha256sum` on handoff/orchestrator/prior report/receipt | 0 | Record input byte identities. |
| Microsoft Learn searches/opens | successful | Consult primary ETW provider, session, event, counter and privilege documentation; no implementation or dispatch. |

No tests, hosted dispatch, implementation, checker/product change, commit, branch push, handoff, or qualification action was performed.

## Limitations and next binding requirements

1. R-30/M-QUAL must decide whether a supervisor-local contiguous sequence plus zero ETW loss counters and independent census reconciliation satisfies “source sequence/loss counters.” If not, a documented source sequence or target-image experiment is required.
2. A hosted Windows runner must prove the named system logger can be created and controlled without candidate code sharing the supervisor's authority. A same-account administrator job is not enough for §2.3 isolation.
3. The exact Windows image must be checked for provider metadata, event class/version, process-start-key extended data, requested/effective buffer configuration, and existing system-logger conflicts.
4. ETW process events do not prove filesystem writes, data invariants, engine settlement, or exact removal authorization; those remain separate authenticated producer claims.
5. The unavailable `4a8b...` source must be supplied or its absence explicitly accepted before source-level reconciliation.

The design intentionally preserves the current provider's refusal until all of those bindings are independently supplied.

See [DESIGN.md](./DESIGN.md) for the complete profile and Microsoft primary-source links.
