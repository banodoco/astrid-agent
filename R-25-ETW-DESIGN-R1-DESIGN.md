# Windows ETW continuous-custody observer — design only

Status: design-only, read-only, not implemented, not dispatched, and not a qualification or receipt-qualification claim.

## Decision

Windows ETW is a feasible candidate source for the continuous-custody obligations, but this profile is **not yet an unconditional R-30 §2.3 match**. ETW documents process lifetime events, session-level loss counters, and a process-start key. It does not document a gap-detecting, globally monotonic source sequence for the kernel process events. The observer can assign a contiguous consumer sequence and fail closed on every documented loss/consumer error, but R-30 still needs to approve that sequence/loss interpretation and the hosted-runner isolation profile.

The current Agent custody provider remains correctly closed: at `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`, `scripts/m4_6_pinokio_custody_provider.py:664-666` explicitly refuses to turn polling into continuity. The Windows snapshot collector is also census-only (`windows-cim-task-service-snapshot-v1`) and is not changed or reinterpreted here.

## Exact source and session

The selected profile is the Windows system-process provider path, not a caller-selected provider:

| Layer | Exact identity | Use |
|---|---|---|
| ETW control provider | `SystemProcessProviderGuid = {151f55dc-467d-471f-83b5-5f889d46ff66}` | Enable with `EnableTraceEx2`; `SYSTEM_PROCESS_KW_GENERAL` is the process-lifetime keyword and maps to `PERF_PROCESS` / `EVENT_TRACE_FLAG_PROCESS` (`0x00000001`). |
| Emitted process event class | `ProcessGuid = {3d6fa8d0-fe05-11d0-9dda-00c04fd7ba7c}` | Validate this class GUID in every event before parsing the classic kernel payload. |
| Session | One named, run-owned system logger, proposed name `Astrid ETW Custody` | Start with `EVENT_TRACE_SYSTEM_LOGGER_MODE`, real-time consumption, a sequential ETL backing file, a fresh session GUID, QPC clock (`ClientContext=1`), and a private ACL. Never attach to an ambient session. |

The session is a system logger because Microsoft documents that System Providers require `EVENT_TRACE_SYSTEM_LOGGER_MODE`. A private system logger can use a descriptive name and a newly assigned GUID; it need not use the reserved `NT Kernel Logger` name. The legacy alternative is the single reserved `NT Kernel Logger` session (`KERNEL_LOGGER_NAME`, `SystemTraceControlGuid`) with `EnableFlags=EVENT_TRACE_FLAG_PROCESS`; it is not preferred for a hosted runner because it is globally shared and can already be in use.

The name `Microsoft-Windows-Kernel-Process` and GUID `{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}` are real Windows ETW metadata, but they must not be silently substituted for the selected control profile. If an implementation chooses that manifest provider instead, it must bind the exact provider GUID, keyword and manifest version observed on the runner. The Microsoft system-provider documentation says the events generated through the System Trace Provider are not marked as emitted by the individual system providers; therefore the event class identity above is the authoritative parser discriminator for this profile.

## Events and identity

Consume with `OpenTrace`/`ProcessTrace` in real time using `EVENT_RECORD`/`EventRecordCallback`, and decode the approved MOF class. Required event types are:

| Event type | Meaning | Required handling |
|---:|---|---|
| `1` | `Start` | Add the process incarnation. Use payload `ProcessId`, `ParentId`, `SessionId`, `ImageFileName`, `CommandLine`, `UserSID`, and the process-start key when present. |
| `2` | `End` | Close the exact `(ProcessId, process-start-key)` incarnation and retain exit status. |
| `3` | `DCStart` | ETW's initial rundown of processes already running when the session starts. |
| `4` | `DCEnd` | ETW's final rundown of processes still running when the session ends. |
| `39` | `Defunct` | Accept only if the observed class/version profile explicitly includes it; it is diagnostic, not a substitute for `End`. |

Do not identify a process by PID alone. Request `EVENT_ENABLE_PROPERTY_PROCESS_START_KEY` in `ENABLE_TRACE_PARAMETERS`; Microsoft documents that this key is unique during the current boot even when PIDs are reused. Retain PID, parent PID, process-start key, session ID, SID, image path/name, command line, event timestamp, class version, event type, runner boot identity, and ETW session identity. A missing or malformed process-start key, unknown class version, malformed payload, duplicate start/end, end without an open incarnation, or parent identity that cannot be reconciled is an observer failure.

The ETW event header is not sufficient to identify the created process: Microsoft notes that process-start events may be logged in the parent context, so the payload's process identifier must be used rather than the header PID. Parse and hash the exact provider metadata and parser profile used for each run.

## Lifecycle and authenticated interval

The supervisor, not the candidate or journey driver, owns this state machine:

`issued → authenticated → launched → session-started → consumer-attached → initial-censused → custody-bound → observing → final-censused → session-stopped-and-drained → sealed`.

Any error goes to terminal `aborted`; an aborted session cannot be resumed or reused.

1. Redeem the signed R-30 `RunGrant` and verify the complete tuple, inventory, obligation, attempt, nonce, origin and execution profile before creating the ETW output directory.
2. Verify the observer executable/import closure and record its hash, PID, process-start identity, parent incarnation, principal, working directory, command/input digest, runner/image/build, boot identity and isolation profile.
3. Start exactly one fresh `Astrid ETW Custody` system logger. Record the session GUID/handle, requested and effective `EVENT_TRACE_PROPERTIES`, enabled provider GUID, keyword, level, enable properties, buffer counts/sizes, log path ACL, and all return codes. `ERROR_ALREADY_EXISTS`, an occupied session, any effective-setting drift, or any ambient-session reuse aborts.
4. Open the same named real-time session and prove the consumer is attached before the action begins. Query the session counters and capture an initial counter baseline.
5. Capture an independent initial process census using a separate OS API path (not ETW parsing and not candidate-supplied `Win32_Process` JSON). Reconcile it with the ETW `DCStart` rundown. Differences, missing rundown, or an unbounded census are `OBSERVER_INCOMPLETE`.
6. Bind the exact controller, Runtime and descendants to the authenticated action/owner using process-start keys, executable digests, parent incarnations, listener owners, roots/locks and manager definition. The ETW source observes the OS; it does not authorize a caller-supplied removal set.
7. Record each accepted ETW event with a supervisor-local sequence, append-only hash chain, QPC/UTC timestamp, raw event digest and source counter snapshot. Poll `ControlTrace(..., EVENT_TRACE_CONTROL_QUERY)` during the interval; the query is a health check, not an event source.
8. At finalization, stop product mutation, take the independent final census, request ETW stop, drain all buffered events, and require/validate `DCEnd`; then query final counters, close the consumer, and reap the exact observer child. The final census and ETW rundown must reconcile under the approved end-boundary rule. Any event or counter arriving after the declared terminal boundary aborts rather than being silently appended.

The bound interval is the supervisor-recorded pair `(t0,t1)` with both QPC and UTC projections, where `t0` is after the consumer and both initial census records are ready and before the bound action, and `t1` is the supervisor's finalization barrier/ETW stop boundary. The receipt binds `(qualification_id, tuple_sha256, inventory_sha256, obligation_id, attempt_id, nonce, origin_sha256, execution_sha256, run_id, run_attempt, numeric_job_id, runner_instance, session_guid, provider_profile_sha256)`; environment variables and artifact filenames are comparison inputs only.

## Sequence, completeness and lost events

The export should contain both kinds of sequence and all available loss evidence:

```text
event.sequence              supervisor-local 1..N, no duplicates or gaps
event.source_timestamp      ETW event timestamp
event.source_class/type     ProcessGuid, class version, event type
session.query_counters      EventsLost, LogBuffersLost, RealTimeBuffersLost,
                            BuffersWritten, NumberOfBuffers, FreeBuffers
consumer.buffer_callbacks   per-buffer delivery/processing records
trace_header                TRACE_LOGFILE_HEADER.EventsLost/BuffersLost
initial/final_counters      exact query snapshots before and after the interval
```

The local sequence is useful for export integrity only; it is not evidence that Windows emitted every event. `EVENT_TRACE_PROPERTIES` supplies session counters for events not recorded, log buffers not written, and real-time buffers not delivered. `BufferCallback` supplies per-buffer loss information, and the trace header supplies final `EventsLost`/`BuffersLost`. A real-time consumer must remain attached; Microsoft documents that a real-time session without a consumer can fill its bounded backing buffer and lose events.

The success predicate is strict:

`complete = attached throughout ∧ ProcessTrace terminal-success ∧ no callback/error/EOF ∧ all counter deltas = 0 ∧ all buffer/header loss = 0 ∧ local sequences contiguous ∧ DCStart and DCEnd present/reconciled ∧ independent initial/final censuses reconcile ∧ exact tuple/owner binding holds`.

If any loss counter is unavailable, nonzero, decreases unexpectedly, cannot be correlated to the same session, or is only represented by an unsigned child claim, the result is `unknown`/`aborted`, never pass. A hash chain, two equal snapshots, or a zero counter alone does not prove continuity. The unresolved R-30 gap is that the Microsoft sources reviewed here do not expose a documented source event sequence whose absence would detect a silent omission when all loss counters remain zero. R-30 must either accept the local sequence plus independent census/loss-counter profile or require a stronger source sequence/validation experiment on the target Windows image.

## Isolation and privileges

Microsoft documents that trace control is available to elevated administrators, Performance Log Users, and LocalSystem/LocalService/NetworkService, while the reserved NT Kernel Logger requires administrators or LocalSystem. A system-wide trace also needs the effective system-profile right `SeSystemProfilePrivilege` unless the adopted access-control profile grants an equivalent approved route. The observer must record token SID, elevation, group membership, enabled privileges, session/provider ACLs, and ETW API return codes before admission; it must not assume that a GitHub-hosted Windows runner's default account is sufficient.

The supervisor must be a separately measured, protected process or service with exclusive control/query/consume access to the session and output directory. Candidate code must not be able to stop/reconfigure the session, change its ACL, replace the observer, read signing material, or rewrite raw ETW evidence. The ETW log parent must have a restrictive ACL, and the supervisor must hash/revalidate files before sealing. A same-account hosted job in which candidate code has administrator control is not an established R-30 trust boundary; until a hosted runner profile proves separation, the observer is a diagnostic source only.

## Failure-closed cases

Abort and retain diagnostics for: failed grant or tuple binding; observer hash/import drift; missing required privilege; session conflict; provider/session/keyword/version mismatch; `StartTrace`, `EnableTraceEx2`, `OpenTrace`, `ProcessTrace`, `ControlTrace`, or `TDH` failure; consumer disconnect, fork, timeout or unknown exit; any nonzero loss/buffer counter; unknown or malformed event; missing process-start key; PID/start-key reuse; census mismatch; manager/controller/descendant drift; finalization before drain; late events; output mutation; or failed exact-child reap. No fallback to `Get-CimInstance`, Task Scheduler, service snapshots, PID-only polling, or a native-service observer may satisfy a Pinokio continuous-custody claim.

ETW process lifetime does not prove filesystem writes, application data preservation, Pinokio engine settlement, or exact removal authorization by itself. Those remain separate authenticated observations joined by R-30/R-36.

## Source-backed facts versus hypotheses/open gaps

Documented by Microsoft: the system-provider GUID/keyword mapping; system-logger requirement; Process class GUID and event types; process payload fields; DCStart/DCEnd rundown semantics; Process Start Key uniqueness within a boot; real-time consumer and `OpenTrace`/`ProcessTrace` lifecycle; ETW session loss counters; buffer callback loss reporting; session-control privilege classes; and `SeSystemProfilePrivilege` naming.

Design hypotheses requiring target-runner validation: that the hosted Windows image permits a run-owned system logger with the required privilege; that the selected System Process Provider emits the expected Process class versions and process-start-key extended data on that image; that the independent census can run under the same protected supervisor; that requested buffers are honored; that no platform monitoring session causes conflict; and that the verifier accepts local sequence plus zero loss counters as the required source-sequence proof.

Unresolved input: `git show 4a8bca56d672a99bb73bd2b2f2ea07948443b841` was unavailable in the checked-out Agent repository, and a direct fetch reported `upload-pack: not our ref`. Therefore this report does not claim to have inspected or approved that historical Windows observer source. The current snapshot report records a different, polling-only Windows implementation and explicitly says it has no ETW provider/session.

## Microsoft primary sources

- [System Providers](https://learn.microsoft.com/en-us/windows/win32/etw/system-providers) — `SystemProcessProviderGuid`, `SYSTEM_PROCESS_KW_GENERAL`, and system-provider/session rules.
- [Configuring and Starting a SystemTraceProvider Session](https://learn.microsoft.com/en-us/windows/win32/etw/configuring-and-starting-a-systemtraceprovider-session) — named system logger configuration and `EVENT_TRACE_SYSTEM_LOGGER_MODE`.
- [Process class](https://learn.microsoft.com/en-us/windows/win32/etw/process) and [Process_TypeGroup1](https://learn.microsoft.com/en-us/windows/win32/etw/process-typegroup1) — Process class GUID, fields and event types.
- [ENABLE_TRACE_PARAMETERS](https://learn.microsoft.com/en-us/windows/win32/api/evntprov/ns-evntprov-enable_trace_parameters) — `EVENT_ENABLE_PROPERTY_PROCESS_START_KEY` and its boot-scoped uniqueness.
- [EVENT_TRACE_PROPERTIES](https://learn.microsoft.com/en-us/windows/win32/api/evntrace/ns-evntrace-event_trace_properties) — flags, buffer settings, `EventsLost`, `LogBuffersLost` and `RealTimeBuffersLost`.
- [Consuming Events](https://learn.microsoft.com/en-us/windows/win32/etw/consuming-events) and [EVENT_TRACE_LOGFILEW](https://learn.microsoft.com/en-us/windows/win32/api/evntrace/ns-evntrace-event_trace_logfilew) — real-time consumption, `BufferCallback`, and callback/session behavior.
- [TRACE_LOGFILE_HEADER](https://learn.microsoft.com/en-us/windows/win32/api/evntrace/ns-evntrace-trace_logfile_header) — final event/buffer loss fields.
- [EVENT_RECORD](https://learn.microsoft.com/en-us/windows/win32/api/evntcons/ns-evntcons-event_record) — delivered event and extended-data shape.
- [Controlling Event Tracing Sessions](https://learn.microsoft.com/en-us/windows/win32/etw/controlling-event-tracing-sessions), [EventAccessControl](https://learn.microsoft.com/en-us/windows/win32/api/evntcons/nf-evntcons-eventaccesscontrol), and [Privilege Constants](https://learn.microsoft.com/en-us/windows/win32/secauthz/privilege-constants) — control/ACL privilege facts.
- [DTrace ETW](https://learn.microsoft.com/en-us/windows-hardware/drivers/devtest/dtrace-etw) — the separate `Microsoft-Windows-Kernel-Process` name/GUID seen on Windows images.
