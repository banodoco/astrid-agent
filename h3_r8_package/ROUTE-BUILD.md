# Build inputs and publication handoff (not an executable rehearsal route)

The host must first record this lane's READY-TO-PUSH entry as PUSHED, identifying
the exact immutable candidate SHA. Every future route input must then be either
a GitHub SHA or a committed docs file. Local task paths, packets and bundles are
handoff evidence only. This document authorizes package build/verification only.
A portable installed lane and guarded rehearsal caller still require separate
work and authorization; none is published or executed here.

The six frozen source identities and counts are in `expected-sources.json`.
Their anonymous GitHub URLs are in `repositories.json`. Acquire each into its own
clean checkout with `git init`, `git -c credential.helper= fetch --no-tags
--depth=1 URL FROZEN_SHA`, and `git checkout --detach FETCH_HEAD`. Use a clean HOME,
selected child environment, no credentials/global or system Git configuration,
and no inherited replacement objects, alternates or Python paths. The harness
source is the original frozen `1aec95e158159949a9d71636e768c53b190902c3`, separate
from the newer packaging candidate checkout. Never package the candidate itself
as the frozen harness source.

After selecting the pushed candidate and fetching the six sources, the build
interface is one CPU-only invocation (replace absolute placeholders):

```sh
python3 -I -B CANDIDATE/h3_r8_package/rebuild.py \
  --source agent=ABS_AGENT --source runtime=ABS_RUNTIME \
  --source vibecomfy=ABS_VIBECOMFY --source harness=ABS_FROZEN_HARNESS \
  --source lifecycle=ABS_LIFECYCLE --source runpod=ABS_RUNPOD \
  --inputs ABS_PUBLISHED_DOCS/h3-r8-content-identity/inputs \
  --output ABS_NEW_PACKAGE --work ABS_NEW_BUILD_WORK
```

Output and work paths must be absolute and absent. `--inputs` selects exactly the
five docs snapshots from the table in README. It defaults to this candidate's
`inputs/` for local validation; local validation does not claim docs publication.
The builder verifies snapshots before creating output, builds six packs, copies
all immutable non-pack bytes, and verifies the assembled package through the
same real Git content verifier. A failure exits nonzero. Do not execute any
payload member. Successful `WORK/comparison.json` contains strict-index results,
selected refs, commit/tree/object identities, non-pack hashes and diagnostic
pack-byte differences. It stays outside the package and is not a new SHA256SUMS.

For an independently received content package, use the same pushed candidate's
verifier with a fresh work directory and the published snapshots:

```sh
python3 -I -B CANDIDATE/h3_r8_package/rebuild.py \
  --verify-package ABS_CONTENT_PACKAGE --work ABS_NEW_VERIFY_WORK \
  --inputs ABS_PUBLISHED_DOCS/h3-r8-content-identity/inputs
```

The authoritative docs checkout was read-only in task 94. **Host action is
required** to publish the five exact text files. After recording PUSHED, copy
`h3_r8_package/inputs/<basename>` at that candidate SHA to
`docs/merge-plan/handoff/M-GPU/h3-r8-content-identity/inputs/<basename>` in the
docs repository for all five names in README; preserve bytes and verify each
SHA256 against that table. Publish this README and ROUTE-BUILD alongside the
inputs and `PRODUCTION-ACCEPTANCE.md` alongside them, then commit those docs and
record the docs commit SHA. Adjust relative links only in the published docs;
never edit the snapshot files. No `.pack`, retained package, job with private
paths, private media or credential is part of this handoff. No route may become
claimable until both the pushed candidate and committed docs inputs exist.

Before any separately authorized executable route, preserve these gates:

| Gate | Required evidence |
|---|---|
| B1 | One uninterrupted exact `run.sh --canary` from a fresh root, through support.lock sync, CUDA wheel/RECORD verification and environment probe to the exact canary-gpu-boundary and gpu_sanity argv; no model step started. |
| B2 (amended) | Cross-machine source-content identity and non-pack byte identity pass; the original retained machine-1 package and immutable job retain their original byte hashes before submission, after transfer and on pod receipt. No rebuilt pack replaces the original submission package. |
| B3 | Machine-1 submit-time guard snapshot: all four STOP markers absent, fresh heartbeat and zero active/unresolved pods. |
| B4 | Reviewed rehearsal/pod mode table; every delta is host-only and installer/source/lock/wheel/model/custody integrity is preserved. |
| B5 | Full frozen production acceptance present before any pod; the exact bytes are in PRODUCTION-ACCEPTANCE.md. |

Use per-phase STOP/disk guards, clean HOME and explicit child environment, ten
second disk samples, measured peak, and at least 16 GB start space or measured
peak plus required reserve for a future rehearsal. Stop below the specified
reserve, on STOP or integrity failure. Machine-1 pod submissions additionally
retain the original SHA256SUMS check. No B1–B4 completion, R8 completion, installed
composition or GPU result is claimed by a source-content build. B5 preserves
written acceptance only. The frozen production document SHA256 is
`c1e875c4faed4b84b2e06bd47c8aaf1224b05dbdad62df070c5682560c0c8506`.
