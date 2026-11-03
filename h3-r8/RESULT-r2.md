# H3 R8 exact installer canary r2 — RESULT (Elrond)

Verdict: NOT READY. Stopped at the disk reserve during the official installer. Not a product result. Classified BLOCKING (reserve breach).

## Build and manifest (this run)
- Candidate 18d928fca8b7dd83f52cc5f1b978850b06f90bfd; six sources at the route table commits and trees (fetched anonymously, checked out detached).
- rebuild.py: passed; non_pack_byte_identical true. Strict verifier before regeneration: six index-pack --strict passed.
- Regenerated sources.json pack fields and fresh SHA256SUMS.json (12 entries), verified against the payload bytes (0 mismatches).
- Pack bytes, original vs rebuilt (size / sha256 prefix):
  - agent: 41465046 / aa575256 -> 41465097 / ba93f704 (commit ad130b68, tree 8861d42c)
  - runtime: 1768882 / c668ced7 -> 1768882 / c668ced7 (match)
  - vibecomfy: 82363135 / ef1680ec -> 82357415 / 08c89344 (commit ac0ee1b8, tree 749cd2ca)
  - harness: 41111 / dc2d4f1 -> 41111 / dc2d4f1 (match)
  - lifecycle: 514286 / 8ce3cc49 -> 514286 / 8ce3cc49 (match)
  - runpod: 643618 / 3219a8b0 -> 643577 / 9daa3dd6 (commit 0786b3d5, tree 8e42fe3e)
- Post-regeneration verifier run correctly fails on sources.json (non-pack bytes differ by design); the r2 route's manifest check passed instead.

## Canary runs
- Runs in job3, job4, job5 with separate roots. Each reached official-installer-start.
- job4: installer exit 1. Cause: port 19291 served by an Astrid Runtime from job3's root3 (same realm), left running by job3. Stopped job3's runtime with its canonical down (status stopped).
- Lane fix in place (log: logs/lane-fix.diff): the driver runs the lane with its own interpreter, so the route's lane could not import astrid. lane2.py re-execs under installed-version.json python. Harness fix only; no product or package change.
- job5: installer started; free disk fell from ~7.0 GB to below the reserve. Guard stopped the canary at free=6267836 KB; driver and installer were then stopped. Free stayed at 6103276 KB and then recovered to 22154556 KB after removing the failed root3/root4/root5 installs (run artifacts kept in job folders).
- Peak: the installer consumed about 16.5 GB (23 GB free at start to about 6.1 GB). The reserve is 6262359040 bytes (~6.1 GiB). Start needed is therefore about 22.6 GB free. This host had 22.3 GB before the cleanup.
- No model step started. No pod, no GPU, no provider call. canary-gpu-boundary not reached.

## Findings
- BLOCKING: disk reserve breach. The official installer's peak exceeds the free space this host can keep above the reserve. Needs more disk on Elrond or a different host.
- BLOCKING (harness/route): the route's sample lane runs under the launcher's Python, not the installed one. Fixed in place with lane2.py (see diff).
- DEFERRED: a previous attempt's runtime left on port 19291 is a manager/process hygiene issue, not a product defect.
