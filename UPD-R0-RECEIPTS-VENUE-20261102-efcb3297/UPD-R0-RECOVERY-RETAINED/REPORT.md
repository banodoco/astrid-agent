# UPD-R0-RECOVERY-RETAINED — exact retained R0 recovery

## Disposition

The protected-review defect is fixed and the scoped Agent lane is pushed at
`6614cbe44bc950e239b0c7d3bd86a74c47818696`. Focused local proof is green.
This is a worker-complete candidate, not a HANDOFF, protected approval, or
hosted/native proof.

M-INT added a later 20:20Z requirement that this same Agent candidate pin
Runtime `255ecda21ba904520eb867936ef173cc5f7d3ae4` in `runtime.lock.json`.
The lock still names `2776ba973b6c8f3555b5629b0840d9691a98d3c2` because this task explicitly
forbids writes outside `astrid/installer/**` and installer tests. The compatible
combined tuple therefore needs separately authorized Agent pin work before the
proof-only manifest, protected re-review, and hosted/native dispatch.

## Candidate

| Item | Value |
| --- | --- |
| Repository | `banodoco/astrid-agent` |
| Branch | `lane/M-R1FIX/UPD-R0-RECOVERY-RETAINED` |
| Task base | `839173a36dbd8f4eea3dc6d40f924ed576d59124` |
| Original upstream base | `648f99e122019faf9734a6e2e5a68aff48eaa5ce` |
| Pushed head | `6614cbe44bc950e239b0c7d3bd86a74c47818696` |
| Head parent | `3936c75980123070d4000f7c2871b8b4d4a0c04f` |
| Target Runtime for eventual proof | `255ecda21ba904520eb867936ef173cc5f7d3ae4` |

Changed paths are exactly:

- `astrid/installer/backend.py`
- `astrid/installer/service.py`
- `tests/test_installer_launchd_stop_handoff.py`
- `tests/test_installer_launchd_stop_refusals.py`

## Decision rationale

The prior boolean `bootout_intent` could not say whether a loaded job was the
untouched job admitted before the second check or a job that appeared after an
actual bootout. Static plist/action equality alone cannot preserve the 19:22Z
post-bootout refusal.

The intent record now also retains the first admission's exact live owner:
PID, discovery birth ID, independently observed process birth ID, UID, and
resolved executable. The existing snapshot already binds the installing user
domain, Astrid label, expected plist path, exact plist bytes/digest, and action.
During interrupted recovery, the installer repeats bootout only when all of
those facts still describe the same live process. Current replacement discovery
is never promoted into recovery authority.

Consequently:

- a second-admission refusal or pre-bootout controller interruption can recover
  the unchanged live R0 process, boot it out before C1, and restore/bootstrap
  the original plist bytes;
- a replacement or PID reuse fails the retained PID/birth comparison even when
  its plist/action is byte-identical;
- existing label, path, arguments, executable and UID mismatches still refuse;
- any post-bootout reappearance still refuses without another bootout or signal;
- a stopped/no-PID job stays fail-closed because it has no process-incarnation
  witness that distinguishes it from an identical post-bootout reappearance.

That last case is the only unresolved protected observational ambiguity. The
read-only sub-worker independently identified the same boundary. It must not be
weakened to definition-only matching without M-INT authorizing a durable launchd
generation witness.

## Focused proof

| Selection | Inner exit | Result |
| --- | ---: | --- |
| Named positive retained-original, pre-bootout interruption, replacement, stopped ambiguity, five mismatch classes, and reappearance controls | 0 | 10 passed |
| Complete launchd refusal + real Backend/C1 handoff fixture files | 0 | 59 passed |
| R0 service-action adoption | 0 | 9 passed |
| Bounded non-integration pre-stop recovery | 0 | 8 passed, 1 custody-dependent node deselected |
| `py_compile`, diff check, ancestry, clean worktree, and remote-head equality | 0 | passed |

No full suite was run. An exploratory broad recovery selection was invalid
because clearing addopts selected integration nodes without their declared
fixtures; after restoring the marker exclusion, the remaining failures all
reproduced the unchanged upstream `custody.py` isolated-script relative import
from base `648f99e1`. Those exit-1 attempts and their exact commands are retained
in the receipt and are not counted as proof.

## Receipt and limitations

The canonical record is
[`canonical-receipt.json`](canonical-receipt.json). It contains the exact
commands and inner exits, tuple incompatibility, commit graph, remote observation,
coverage, source/input hashes, sub-worker disposition, failed development runs,
and limitations.

M-AG protected approval, the required Runtime-lock pin, proof-only App manifest,
and exact-tuple hosted/native Mac evidence remain separate. None is claimed here.
