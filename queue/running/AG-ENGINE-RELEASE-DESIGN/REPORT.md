# AG-ENGINE-RELEASE-DESIGN report

**Design packet complete; review requested. No implementation or HANDOFF.**

The packet specifies an Agent-issued, release-only capability for the exact
readiness owner/engine, targeted to one authenticated host incarnation and
associated with the complete later SessionBinding/generation. The effect is
fenced under supervisor `self.work` against start/replacement/stop/cleanup,
with host admission locks held in a defined order. It specifies attributable
busy/completed/failed/unresolved results, one fixed bounded deadline,
idempotent request records and retained custody on timeout or replacement
uncertainty. Models unloaded does not mean engine stopped or all memory returned.
A checked-release reservation preserves the original generation across dispatch:
the existing manager's fence-before-adapter callback cannot be used unchanged.
Busy preserves G; completed retires once; unresolved retains the original G and
the poisoned local fence, allowing readback to resolve only that old slot.

## Provenance and files

- Repository: `banodoco/astrid-agent`.
- Design lane: `lane/M-AG/AG-ENGINE-RELEASE-DESIGN-design`.
- Immutable Agent base: `b54117c9312165e2799554ec27480665d5cad2b1`.
- Base parents: `ef8df6eb25c5de1691c357c2a6d0e3b87f5074c2`,
  `890ca2347bdc8c9377e178fd176a9eca3152e00f`.
- Runtime context: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`.
- Packet commit: Git HEAD on this design lane; exact post-commit head, parent,
  remote ref and artifact hashes are recorded in task-local `RECEIPT.json`.
  A report cannot embed its own commit hash; the outer task report also records
  that metadata after commit.
- Committed paths are this task's `DESIGN.md`, `REPORT.md` and the two integrated
  `sub/{1,2}/REPORT.md` files, preserved under
  `queue/running/AG-ENGINE-RELEASE-DESIGN/` in the Agent checkout. No root-level
  design alias was added: the task's explicit write scope and artifact paths
  take precedence over the manager's generic root-name convention.

`DESIGN.md` is the normative proposed sequence/state/API/test contract. Both
sub-reports completed with exit 0 and were read by the lead. Sub/1 maps exact
source, readiness lifetime and four launch/attachment modes. Sub/2 supplies
consumer correlation, deadline and custody requirements. Their baseline
findings are integrated, not treated as implementation proof. The design
supersedes the older framing that Runtime must own the release operation;
either a reviewed Agent private transfer or a Runtime adapter may carry
Agent-issued authority. It also distinguishes local pending from verified busy
and native unload success from VRAM availability.

## Decisions and consumer boundary

The proposed `BoundEngineRelease.release(...)` and read-only `observe(...)`
never resolve or start an owner. Supporting staged issue/delegate/claim/bind
operations and `ReleaseHandoff` are new Agent design interfaces. The existing
worker credential, profile, endpoint, activation ACK and `{}` supervisor release
are insufficient authority. The design does not invent a published Runtime API.

The design retains four separate identities: readiness engine owner, selected
host executor, actual Runtime execution instance/epoch, and complete later
SessionBinding plus managed generation. It identifies the existing overloaded
profile `runtime_instance_id`, missing readiness `launch_generation`, source
revision/content distinction and attached-hook Runtime epoch refusal. Future
implementation must resolve these seams with verified evidence, not weaken the
checks. Runtime epoch transition fencing stronger than local binding identity
is an explicit adapter requirement, not a property attributed to `self.work`.

Intended modes are local Linux detached spawn, supervised foreground, resident
attachment and parked activation, each with an explicit private transfer gate.
None has this release guarantee on the pinned tuple. Resident attachment needs
a new exact-host receiver/claim path; parked activation must retain its frozen
grant shape and use a separate channel. Untracked HTTP writers cannot be
excluded by queue polling, so shared/external ingress is unsupported without
an admission fence. Remote/RunPod and non-Linux custody are outside v1.

## Inspection and verification

Read R4 `REPORT.md`/`RECEIPT.json`, I-AG-03a `DESIGN.md`, current STATUS peer
lines and the three exact ORCHESTRATOR requests. Inspected the pinned Agent
source paths required by the brief plus `managed_tool_session.py`, the relevant
tests, and Runtime bootstrap at the context SHA. The historical 03a approval
conditions were checked in ORCHESTRATOR; their preservation is described in
the design. No new cross-owner approval is claimed.

Core commands (workspace `W=/home/user/merge/ws/M-AG`; task `T=W/queue/running/AG-ENGINE-RELEASE-DESIGN`):

```bash
CODEX_WORKSPACE=/home/user/merge/ws/M-AG /home/user/merge/kit/codex/task-clone.sh astrid-agent /home/user/merge/ws/M-AG/queue/running/AG-ENGINE-RELEASE-DESIGN/agent b54117c9312165e2799554ec27480665d5cad2b1
# Following git/source commands run in T/agent, unless an absolute path is given.
git switch -c lane/M-AG/AG-ENGINE-RELEASE-DESIGN-design
git rev-list --parents -n 1 HEAD
sed -n '803,962p' astrid/core/generation/engine_custody.py
sed -n '6610,6675p' astrid/core/execution/generic_host.py
sed -n '78,146p;474,508p' astrid/core/execution/managed_tool_session.py
sed -n '613,637p;684,796p;829,865p' astrid/core/generation/engine_readiness.py
sed -n '11935,12007p;16795,16865p' astrid/core/execution/generic_host.py
sed -n '1297,1409p;1430,1503p;1760,1816p;1830,1899p;1946,1977p;2066,2125p;2249,2273p' astrid/sdk/host_bootstrap.py
git -C /tmp/i-ag-08-runtime rev-parse HEAD
git -C /tmp/i-ag-08-runtime show 2776ba973b6c8f3555b5629b0840d9691a98d3c2:astrid_runtime/bootstrap.py | sed -n '300,325p;427,460p;1120,1145p;1215,1255p'
git diff --check b54117c9312165e2799554ec27480665d5cad2b1
git diff --name-status b54117c9312165e2799554ec27480665d5cad2b1
git rev-list --parents -n 1 HEAD
git push origin HEAD:refs/heads/lane/M-AG/AG-ENGINE-RELEASE-DESIGN-design
git ls-remote origin refs/heads/lane/M-AG/AG-ENGINE-RELEASE-DESIGN-design
```

Additional source/test inventory used `rg`/`rg --files` and bounded `sed` reads;
design copying, documentation validation, commit and push commands/results are
recorded in the final receipt. Early navigation tried git in the non-repository
manager directory, one source search used a nonexistent `backends/comfy.py`,
and one ORCHESTRATOR read used the clone directory. These returned errors;
the reads were corrected to the pinned clone, `backends/vibecomfy.py` test
anchors, and manager ORCHESTRATOR. No test was executed.

Validation is documentation/scope only: parse receipt, check all cited existing
test nodes against source, ensure proposed nodes are labeled, audit the design
diff and compare product tree/lock against the base. No generated-file hash is
new: `runtime.lock.json` remains
`859d67343dccbe71b216d114360e1f614fefd6629358038e4488c5b2a232e2a7`.
No code, test, workflow, generated client, pin, journey harness, frozen schema,
bridge contract or manager file is changed.

## Remaining decisions and routing

NEEDS: AG-ENGINE-RELEASE-DESIGN design review

The task-local `DECISIONS.md` contains one concise SHA-specific request each
for M-AG2 and M-RT after publication. The task worker is restricted to its
folder; only the manager STATUS header is polled by peer-router. The lead
manager must relay those exact lines there. Prepared peer notes and a pushed
branch are not evidence of delivery, review or approval; the final receipt
records that distinction.

M-AG2 is asked to confirm consumer fit and its adapter seam. M-RT is asked to
choose active-executor/epoch attestation and side-channel carriage, or name a
separately scoped adapter addition. A future Agent implementation needs
independent protected Agent review by M-AG2; a changed Runtime/frozen contract
needs its owner review and any workflow/harness change needs M-CI. No such
implementation or approval is claimed here.

## Limits

No tests were run, as required for this design-only task. The exact future
test-node plan is in DESIGN section 8, including real non-root Linux P19
coverage for identity, process/channel ownership, replacement and ACK loss.
R4's 42 root-run baseline tests and failed non-root venue attempt do not close
that gate. Native Comfy/GPU evidence is required before claiming unload/VRAM
behavior. The design branch is for review and is not a product candidate,
implementation SHA, qualified release tuple or landable HANDOFF.
