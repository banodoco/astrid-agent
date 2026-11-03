# H3 R8 exact installer canary — RESULT (Elrond)

Verdict: NOT READY. The canary did not reach canary-gpu-boundary. First failing gate: bootstrap pin assertion in the unchanged driver. Classified BLOCKING.

## Candidate and sources
- Candidate: banodoco/astrid-agent 18d928fca8b7dd83f52cc5f1b978850b06f90bfd (tree e31f59f9af9372e2fe03e41221155777091ed922), lane lane/M-GPU/p1-h3-r8-content-identity. Fetched and checked out as a detached worktree.
- Frozen harness: 1aec95e158159949a9d71636e768c53b190902c3 (tree 36c8fa74282f55b82f2157e27c21c60e06c7b35b).
- agent ad130b68268cbb71949d5988dca1b50795b5e3c3 tree 8861d42c2e32cecb3f6abfc2e5681c56ee320693
- runtime 67fad49ad202f18b5207fb70abcf344461b1a5cc tree 4829c0ea0275bbc4f60622c5de6e3a4690375e68
- vibecomfy ac0ee1b80991f2414dae27d264af4b5e2890a7a6 tree 749cd2ca1d994924b16bfcc19e0caacbf5697f3e
- lifecycle 96360a27a53c4608f780f7b370f747a2aa3c7ca8 tree 24e3562181f0ad64003d5cef5403b15d16415be6
- runpod 0786b3d51855a66176fea1e7443b92e7ef2d01b5 tree 8e42fe3e9b2876b71270f2f6256bece8dc5dc651
All six fetched anonymously (git init, fetch --depth=1, checkout --detach FETCH_HEAD). Trees match the route table.

## Build and verify
- rebuild.py (candidate) with --inputs at the published docs snapshots: status passed; non_pack_byte_identical true.
- --verify-package (fresh work): status passed; six index_pack_strict passed.
- Fresh SHA256SUMS.json sha256 797c8d792addd420c332271dede7ec97e71fcfe4e94eae6c6346eebbe65036cb (matches the retained expected value).

## The failure (BLOCKING)
The unchanged driver.py (bootstrap-start, line 105 → line 307) asserts for each source: pack size and SHA-256 equal the pins in sources.json. Three rebuilt packs do not match:
| source | built pack bytes | pinned pack bytes | built sha256 (prefix) | pinned sha256 (prefix) | match |
|---|---:|---:|---|---|---|
| agent | 41465097 | 41465046 | ba93f70469c58c83 | aa575256ad279236 | no |
| runtime | 1768882 | 1768882 | c668ced720f57dc8 | c668ced720f57dc8 | yes |
| vibecomfy | 82357415 | 82363135 | 08c893446e9a8a5e | ef1680ec9cdd7f58 | no |
| harness | 41111 | 41111 | dc2d4f1792c108de | dc2d4f1792c108de | yes |
| lifecycle | 514286 | 514286 | 8ce3cc49049207cc | 8ce3cc49049207cc | yes |
| runpod | 643577 | 643618 | 9daa3dd6d93fc194 | 3219a8b0e9c77697 | no |

The strict verifier passes because it checks git content, not the retained pack bytes. The retained sources.json is a non-pack member that must stay byte-identical, so its pack pins describe the retained packs. The rebuilt packs carry the same git content but different bytes, so the driver's pin check fails. This is a packaging defect: rebuilt packs cannot pass the unchanged driver. Fixing it means changing sources.json or the driver, which this route forbids. Owner decision needed.

Error at the failure: driver.py line 307 in main, reported at bootstrap-start, error field empty.

## Command and environment
- Heavy wrapper: /home/user/astrid/ops/heavy.sh, under env -i HOME=h3-r8/home TMPDIR=h3-r8/tmp PATH=/usr/local/bin:/usr/bin:/bin.
- Command: bash PACKAGE/run.sh --canary /usr/bin/python3 ROOT JOB LANE (LANE = installed-artifact smoke, h3-r8/lane.py).
- Python: /usr/bin/python3 is the launcher; the driver selects the installed application Python.
- git 2.43.0.

## Limits and guards
- No model step started. The canary stopped at bootstrap-start. No pod, no GPU, no provider call.
- STOP markers absent at start (ops/STOP-DISPATCH, STOP-PODS, STOP-OPERATIONS; h3-r8/STOP; ws-R workspace STOP).
- Disk: start free 23G; minimum sampled free 22304764 KB during the run (10-second samples in logs/disk-samples.log). Reserve not reached.
- No retry was run. No package, driver or pin was edited.

## Findings
- BLOCKING: packaging defect. Rebuilt agent, vibecomfy and runpod packs do not match the pinned sizes and hashes in sources.json, so the unchanged driver rejects the package at bootstrap.
- DEFERRED: none recorded in this run.
