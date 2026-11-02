# R-25-ESLOGGER-DESIGN-R1 report

Recorded: 2026-11-02T18:50:32Z

## Outcome

Decision-ready design only. The proposed profile is `macos-eslogger-continuity-v1`
and is documented in [DESIGN.md](./DESIGN.md). It specifies the R-30 §2.3
interval, authentic `exec`/`fork`/`exit` observations, Apple sequence/loss
counters, independent initial/final censuses, lifecycle/failure behavior, and
runner/tuple/provider binding.

No implementation, test, live run, dispatch, checker change, product change,
contract change, handoff, acceptance claim, or access to Hannah's Mac occurred.
The current evidence status is **unproven**.

## Governing inputs and exact source refs

| Input | Ref / digest / relevant fact |
|---|---|
| Manager correction | `ORCHESTRATOR.md`, entries `2026-11-02T18:44:58Z` and `2026-11-02T18:47:24Z`; file SHA-256 at report time `1c8695df0639b0c2f1de0d45c96a3dd473f37d299d89aae28800d3ca81461cbc`. The file is append-only manager state and may change later. |
| R-30 interface | `/home/user/merge-docs/docs/merge-plan/handoff/inputs/M-QUAL-R-30-verifier-interface.md`, SHA-256 `2f685c8aec21bef116f95645920717e4b12ef33c1ca820fe6185eb01ffea115d`; §2.3 lines 62–72; schema continuity lines 136–182. |
| Mac runner setup | `/home/user/merge-docs/docs/merge-plan/admin/MAC-RUNNER-SETUP.md`, SHA-256 `29234c4e138a2b48479232fdf04800323f5bde9e2ebcb84908994563c2ff8e30`; dedicated user/runner name/label and online confirmation steps. |
| Published Agent input | `banodoco/astrid-agent` commit `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`, parent `fb222b3a54e9ffe2fa4eb2c382bc25033e773949`; custody provider blob `a22d8840f764a85dd2356b05d788a52449c05e4b`; process observer blob `241ac8baa883f97eaba2ad760f0693b2e89b578c`. Provider lines 664–666 refuse continuity; observer lines 1–13 and 209–216 identify polling-only snapshots. |
| Prior snapshot collector | `queue/done/R-25-SNAPSHOT-COLLECTORS-R1/REPORT.md`, SHA-256 `1991e92e25893e58c448804ebb234a775549e5653859471a9db285052398cad3`; canonical receipt SHA-256 `e6a197cf91ab14a4bcc5d4c2866e7a7b3e648044bff33195fa9c5f077185bfa0`. It explicitly says snapshots do not prove gap-free continuity. |
| Continuity task | `queue/running/R-25-CONTINUITY-OBLIGATIONS-R1/brief.md`; no accepted continuity receipt was found in that task folder. |

## Research result

Apple's public Endpoint Security documentation supplies the required native
facts: `exec`, `fork`, and `exit` notification types; process identity fields;
`seq_num` per client/event type; and `global_seq_num` per client. Apple gives
the exact dropped-event formulas and version gates (`version > 2` for
`seq_num`, `version > 4` for `global_seq_num`).

The macOS `eslogger(1)` manual documents JSON Lines on stdout and the
`exec fork exit` invocation, but explicitly warns that `eslogger` is not an API,
has no formal/stable JSON schema, may change without warning, is notify-only,
and suppresses its own process group. Those facts are why the design requires
an exact parser/build profile, separate process groups, both counters, source
markers, and fail-closed handling.

Primary references used:

- [Apple `es_message_t.seq_num`](https://developer.apple.com/documentation/endpointsecurity/es_message_t/seq_num)
- [Apple `es_message_t.global_seq_num`](https://developer.apple.com/documentation/endpointsecurity/es_message_t/global_seq_num)
- [Apple `es_message_t.version`](https://developer.apple.com/documentation/endpointsecurity/es_message_t/version)
- [Apple `es_message_t`](https://developer.apple.com/documentation/endpointsecurity/es_message_t)
- [Apple `es_process_t`](https://developer.apple.com/documentation/endpointsecurity/es_process_t)
- [Apple event types](https://developer.apple.com/documentation/endpointsecurity/event-types?language=objc)
- [Apple fork event](https://developer.apple.com/documentation/endpointsecurity/es_event_fork_t)
- [Apple exit event](https://developer.apple.com/documentation/endpointsecurity/es_event_exit_t)
- [`eslogger(1)` sections NAME, DESCRIPTION, EVENTS, FORMATS, EXAMPLES, TCC AUTHORIZATION](https://man.freebsd.org/cgi/man.cgi?apropos=0&manpath=macOS+13.6.5&query=eslogger&sektion=1)

## Commands and exits

All commands were read-only inspection commands; no tests or live run were
required or run.

| Command | Result / exit |
|---|---:|
| `rg -n "2026-11-02T18:(44\|47)\|R-30\|ESLOGGER" ORCHESTRATOR.md` | Found both specified manager entries; 0 |
| `sed -n '3400,3455p' ORCHESTRATOR.md` | Read the correction, eslogger option, and no-dispatch gate; 0 |
| `sed -n '1,320p' /home/user/merge-docs/docs/merge-plan/handoff/inputs/M-QUAL-R-30-verifier-interface.md` | Read interface and §2.3/schema; 0 |
| `sed -n '1,300p' /home/user/merge-docs/docs/merge-plan/admin/MAC-RUNNER-SETUP.md` | Read runner/user/label setup; 0 |
| `git show ab43bad818ef65ca1e929b973a2f61cd9fc4bf61:scripts/m4_6_pinokio_custody_provider.py` | Verified refusal at lines 664–666; 0 |
| `git show ab43bad818ef65ca1e929b973a2f61cd9fc4bf61:scripts/m4_6_process_observer.py` | Verified polling-only observer declaration and snapshot source; 0 |
| `sha256sum` over manager/interface/setup/prior report/receipt | Digests recorded above; 0 |
| `command -v eslogger` | Not present on this Linux workspace; 1 |
| Apple documentation and `eslogger(1)` source review | Sources recorded above; no local execution; not applicable |

No `eslogger` binary exists on this Linux workspace, so no macOS command was
attempted. No tests were run, consistent with the design-only brief.

## Coverage and limitations

Covered by the proposal:

- exact interval markers and initial/final census placement;
- authentic ES notification types and PID/audit-token/process identity use;
- `seq_num` and `global_seq_num` gap/dropped-message detection with version
  gates and exact formulas;
- process-group suppression avoidance and stdout JSONL/raw digest handling;
- startup/readiness, marker correlation, action release, finalization, and
  fail-closed shutdown;
- R-30 binding fields for runner, tuple, obligation, attempt, provider,
  collector, workflow, and exact child identities;
- explicit unsupported requirements and unproven deployment dependencies.

Not established:

- target Mac availability, online runner status, root/FDA grants, `eslogger`
  behavior, schema fields, counter presence, capacity, native census behavior,
  or event loss results;
- a secure R-30 supervisor, authenticated origin, signing/sealing, checker
  wiring, or a callable provider integration;
- filesystem/data/engine semantics or evidence outside process lifecycle;
- the universal claim that `exec`/`fork`/`exit` alone covers every process
  lifecycle path relevant to the product;
- qualification of any R-25 slot.

## Direct manager coordination

`PEER: M-QUAL: R-30 §2.3 design proposal is in queue/running/R-25-ESLOGGER-DESIGN-R1/DESIGN.md. The proposed export requires source_profile_sha256, raw JSONL digest, exact `seq_num` and `global_seq_num` continuity from a post-initial-census start marker through a final marker, independent initial/final macOS ps/libproc/launchctl censuses, and authenticated `astrid-mac` runner plus full tuple/obligation/attempt binding. Please review whether this is the accepted continuity contract; no evidence or acceptance is claimed, and all missing target-Mac proof remains fail-closed.`

`NO DISPATCH: astrid-mac/hannah-mac has not been confirmed online.`
