# Proposed macOS `eslogger` continuous-custody profile

Status: design only. This is not an implementation, a live-run result, a
qualification receipt, or an acceptance claim.

## Decision

Use a dedicated, supervisor-owned `eslogger` observer profile for a future
repo-scoped `astrid-mac` self-hosted runner. The profile can satisfy the R-30
§2.3 shape only if the target Mac demonstrates all of the following in the
same bound run:

1. `/usr/bin/eslogger` starts successfully under the approved privilege/TCC
   profile and emits the approved JSON representation for the pinned macOS
   build.
2. `exec`, `fork`, and `exit` notifications are captured from an authenticated
   interval, without process-group suppression of the action under test.
3. Every retained message has usable `global_seq_num` and per-event-type
   `seq_num`; both counters remain contiguous from the start marker through the
   final marker. Any gap, drop, malformed line, source restart, or uncertain
   boundary aborts the claim.
4. An independent initial census anchors the event replay, and an independent
   final census agrees with the replayed process/manager state.
5. The supervisor binds the collector, runner, tuple, slot, operation, grant,
   attempt, and exact child identities before authorizing the action.

This is a conditional design, not evidence that those conditions hold on
Hannah's Mac. No dispatch is authorized until the manager receives confirmation
that `hannah-mac` is online.

## What the source actually exposes

Apple documents `ES_EVENT_TYPE_NOTIFY_EXEC`, `..._FORK`, and `..._EXIT` as
Endpoint Security notification types. The fork event carries the forked child;
the exec event describes the newly executing process; the exit event carries
the process exit status. Endpoint Security process records include audit-token
identity, PID/PPID, process-group/session identity, start time, executable,
code-signing fields, and parent/responsible audit tokens.

The loss evidence is in `es_message_t`, not a new `eslogger` counter:

- `seq_num` is per client and event type. Apple specifies that, when no events
  are dropped, it increments by one for each message of that type, and gives
  `this.seq_num - (previous.seq_num + 1)` as the dropped-message count. It is
  available when message `version > 2`.
- `global_seq_num` is per client across event types. Apple gives the same
  difference formula and says it is available when message `version > 4`.
- `version` must be checked before reading fields. `time` and `mach_time` are
  available for event timing, but are not loss counters.

The macOS `eslogger(1)` manual says stdout is JSON Lines, the JSON is modeled
after `es_message_t`, and `schema_version` is present. It also says there is no
formal schema or stability guarantee, fields may change without warning, only
notify events are supported, and the tool is not intended as an application
API. Therefore this profile pins the macOS build and parser profile and does
not treat an arbitrary `eslogger` output file as qualifying evidence.

Sources:

- [Apple `es_message_t.seq_num`](https://developer.apple.com/documentation/endpointsecurity/es_message_t/seq_num)
- [Apple `es_message_t.global_seq_num`](https://developer.apple.com/documentation/endpointsecurity/es_message_t/global_seq_num)
- [Apple `es_message_t`](https://developer.apple.com/documentation/endpointsecurity/es_message_t)
- [Apple process record `es_process_t`](https://developer.apple.com/documentation/endpointsecurity/es_process_t)
- [Apple event types](https://developer.apple.com/documentation/endpointsecurity/event-types?language=objc)
- [Apple fork event](https://developer.apple.com/documentation/endpointsecurity/es_event_fork_t)
- [Apple exit event](https://developer.apple.com/documentation/endpointsecurity/es_event_exit_t)
- [`eslogger(1)`, FORMATS/EVENTS/LIFECYCLE requirements](https://man.freebsd.org/cgi/man.cgi?apropos=0&manpath=macOS+13.6.5&query=eslogger&sektion=1), which reproduces the macOS manual page.

## Collector profile

Profile ID: `macos-eslogger-continuity-v1`.

The supervisor launches the fixed absolute command below, with no shell,
capturing stdout and stderr separately:

```text
/usr/bin/eslogger --format json exec fork exit
```

The final adopted command must be recorded with the exact macOS version,
architecture, `eslogger` code-signing identity, CDHash, file digest,
`schema_version`, message-version requirements, and parser/profile digest.
The supervisor must verify the signed Apple binary and refuse a substituted
path, writable copy, changed code-signing identity, or changed parser closure.

The output path is a supervisor-owned, exclusive file or pipe with bounded
size and restrictive permissions. `--oslog` is not used: the manual documents
message-size limits and truncation in unified logging, which would be an
unacceptable evidence boundary. Raw JSONL bytes are retained and hashed before
normalization; normalized observations retain the raw-line digest and source
sequence values.

`eslogger` suppresses events for processes in its own process group. The
supervisor consequently gives `eslogger`, the boundary marker, the census
helpers, and the product/action child separate, recorded process-group and
session identities. The action is rejected if it shares the collector's group,
or if the collector's own suppression could include an action process.

Required host preconditions are macOS 13 or newer, root execution for the
observer, Full Disk Access for the responsible process, a usable logged-in
dedicated runner account, and enough protected storage for the bounded raw
stream. The manager's Mac setup names the dedicated `astrid-runner` user,
repo-scoped `hannah-mac` runner, and `astrid-mac` label. Those are binding
inputs, not proof by themselves.

## Interval binding and authentic process events

The supervisor owns a single state machine:

`issued → authenticated → launched → source-ready → initial-census → observing → final-census → finalized`,

with every error entering `aborted`. The exact interval is bounded by source
events, not by wall-clock timestamps alone:

1. Authenticate the R-30 grant, full tuple, inventory obligation, operation,
   runner identity, collector profile, provider contract, and exact source
   hashes. Record supervisor PID/birth identity, boot identity, UID, cwd,
   command digest, process group, and start monotonic time.
2. Start `eslogger` in its isolated group. Parse until a valid message with the
   required schema and both sequence fields is received. A source that never
   becomes ready is `aborted`.
3. Run a fixed, measured boundary helper in a different process group. Match
   its authenticated executable, nonce/argument, PID plus audit-token
   incarnation, and parent identity across `fork`, `exec`, and `exit`. The
   first marker is only an arm/readiness check; it does not close the custody
   interval.
4. Take the initial census using the independent macOS snapshot source already
   used by R-25: direct `/bin/ps`, `libproc` executable identity, and exact
   `launchctl print` target state. This census is not reconstructed from ES
   events. Record the census's own command identities, monotonic times, target,
   owner UID, manager instance, PID/birth identities, executable paths and
   digests.
5. Run a second fixed boundary marker after the initial census. The interval
   starts immediately after that marker's observed `exit` message sequence.
   The marker's final sequence is the baseline `global_seq_num` and the
   per-type baselines. The supervisor then releases the exact Pinokio action
   child. This makes the initial census an independent anchor while ensuring
   every later fork/exec/exit message is checked against a known sequence.
6. Retain every `exec`, `fork`, and `exit` message delivered to this ES client,
   not just messages whose process currently matches the target. Filter only
   after loss checking. Replay from the initial census using PID plus the
   Endpoint Security audit-token incarnation, not PID alone. An exec replaces
   the image identity; a fork adds the child identity and parent relation; an
   exit removes the identity and records its status.
7. Wait for the fixed provider's exact action child and all approved
   descendants to finalize. A reparented, unexpected, reused, or un-reaped
   identity aborts the session; a process census cannot silently repair it.
8. Run the final boundary marker in the separate marker group. Its observed
   `fork`, `exec`, and `exit` close the event interval. Stop accepting interval
   events at the final marker's exit sequence, then take the final independent
   census. The final census must agree with replayed state and the exact
   manager/launchd target. Drain and terminate `eslogger` under the approved
   shutdown rule; EOF before the final marker, unexpected signal/exit, or
   parser failure is not finalization.

The supervisor exports `first_sequence`, `last_sequence`, `event_count`,
`gaps`, `dropped`, `terminal`, and the exact raw event-file digest in the R-30
`continuity` object. The marker events and their sequence values are included
in the raw evidence, so the boundary is independently auditable.

## Sequence and loss algorithm

For every received message after the source is ready:

```text
global_gap = message.global_seq_num - (previous_global_seq_num + 1)
type_gap   = message.seq_num - (previous_seq_num[event_type] + 1)
```

The first interval message is accepted only after the second marker establishes
the previous values. For each event type, the first value after the marker is
the baseline-plus-one expectation. `global_gap` and `type_gap` must both be
zero. Negative, duplicate, out-of-order, non-integer, absent, or version
incompatible values fail closed. `dropped` is the sum of the exact positive
counter differences, reported separately for global and type counters; a
verifier must reject any nonzero value rather than merely record it.

This detects kernel-to-client drops documented by Apple and omissions in a
continued stream. It does not prove that an event was never omitted when the
client died before a later sequence value arrived, so source death/EOF and
missing terminal markers are independently fatal. It also does not prove the
semantic completeness of fields that `eslogger` does not serialize.

## Independent censuses and finalization

The initial and final censuses are separate supervisor observations, made by
the R-25 macOS process/service snapshot profile and never by replaying the ES
file. Each census must include:

- the exact runner UID and launchd target/definition;
- process PID, audit-token/pidversion incarnation where available, PPID and
  parent incarnation;
- executable path, CDHash/signing identity where available, and digest of the
  approved executable;
- manager state, instance identity, runtime PID, and target state;
- source command paths, exit codes, capture times, size limits, and raw/file
  digests.

The finalizer compares the initial census to the state immediately after
replaying the interval, and compares that result to the final census. It also
requires the provider's exact-child finalization, action result, and manager
state to be present. Snapshot equality alone is never enough: the ES interval,
sequence checks, and both boundary censuses are all required.

## Runner, tuple, and custody binding

The approved binding record should contain, at minimum:

```text
qualification_id, tuple_sha256, inventory_sha256, obligation_id,
attempt_id, nonce, origin_sha256, execution_sha256,
repo owner/id, workflow path+commit+digest, run/attempt/job ids,
runner name/label + authenticated instance id, host boot id, OS/build/arch,
dedicated UID, collector profile digest, provider-contract digest,
operation, slot, mode, channel, exact child identities, census digests,
raw stream digest, continuity counters, finalization state
```

`hannah-mac` and `astrid-mac` must be corroborated by authenticated runner
metadata and local supervisor observations; names and labels alone are not
authority. The tuple is the published tuple bound to the selected obligation,
including Agent `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` for this design
input. A receipt from another tuple, run, attempt, slot, runner, or operation
is not reusable.

The existing Agent custody provider remains the fixed provider boundary. It
currently records a polling-only observer and deliberately raises
`PinokioCustodyRefused("Pinokio custody has no approved gap-free observer")`.
This profile is a proposed future observer input to that boundary; it does not
change the provider and does not make the current provider callable.

## Failure handling

The following are terminal `aborted` results, never successful empty receipts:

- missing root/FDA, unsupported macOS/build, unavailable or changed
  `/usr/bin/eslogger`, changed JSON schema, missing sequence field, malformed
  JSON, oversized/truncated line, stdout/stderr I/O error, output overflow,
  source timeout, source restart, EOF before final marker, unexpected exit, or
  an unverified Apple signing identity;
- any global or per-type sequence gap, drop, duplicate, reorder, marker
  mismatch, event-group suppression risk, event-filter ambiguity, or inability
  to correlate an event to an audit-token incarnation;
- initial/final census error, mismatch with replay, manager/definition drift,
  PID reuse, unknown descendant, reparenting outside the approved model,
  exact-child reap failure, action/provider failure, or changed tuple/runner/
  workflow/collector input;
- collector or supervisor crash, protected-file mutation, extra bytes after
  the terminal record, or a finalization record that cannot be independently
  recomputed.

On abort, retain diagnostics and raw bytes if safe, revoke the grant, mark the
continuity result `fail` or `unknown` as appropriate, and never issue a
qualifying `CollectorSeal`. A retry needs a new approved attempt/grant; it does
not reuse the failed session.

## Unsupported or unproven requirements

This design does not claim any of the following is already closed:

- No target Mac has been inspected; no `eslogger` startup, FDA, root,
  Apple-signature, JSON-field, sequence, loss, or native census result exists.
- `eslogger`'s JSON schema is explicitly unstable. A future target build may
  omit or rename the required fields, and this profile must then fail rather
  than infer them.
- Apple documents sequence counters for Endpoint Security messages, but the
  public `eslogger(1)` documentation does not promise that every required
  field is serialized forever. The mapping from JSON to all required native
  fields needs a target-build specimen and parser review.
- Sequence counters detect client-visible drops; they do not by themselves
  prove no loss before the first marker, after client death, in suppressed
  process groups, or in an unmodeled process-creation path.
- `exec`/`fork`/`exit` do not observe filesystem writes, manager configuration
  changes, application data invariants, engine settlement, or all semantic
  action results. Those remain separate R-30/R-36 evidence roles.
- The R-30 supervisor isolation, authenticated platform origin, signing key
  distribution, runner instance attestation, and exact checker acceptance
  wiring are proposal-level bindings here, not implemented or verified.
- The current R-25 snapshot collector remains a bounded observation source and
  cannot be relabeled as continuous custody.
