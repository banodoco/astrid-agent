# H3 production composition acceptance (frozen 2026-11-02)

This acceptance is for the post-R8 production run. The R8 install rehearsal is a prerequisite; it does not satisfy any generation row below. Pod work follows the canonical H3 path and is submitted through podq. Private THE CLIP inputs and every output stay in the workspace and on our pods.

## Per-row gates

Before each row is submitted, rehearse that row's exact `run.sh` command locally from a clean HOME and explicit child environment, using its final immutable package and job file. Run through the first GPU-only command, then stop immediately before dispatching it. Record the `canary-gpu-boundary` event and the exact `gpu_sanity` argv. Check the disk/STOP/heartbeat/active-pod guards before each phase and again immediately before submission. Verify the package and job hashes before rehearsal, after transfer, and against the pod's received bytes. Do not weaken installer, source, lock, wheel RECORD, model, or custody checks.

After a row's boundary rehearsal passes, run the same immutable `run.sh` and job on the fresh or approved warm pod through the canonical installer and GPU sanity step. Record the pod tuple and all mode differences from the rehearsal; each difference must be host-only and preserve the checks above. Retain progress, command/result logs, output hashes, setup record, cost, and broker-confirmed release. A successful generation row has a review packet at `outputs/review/<job>/` with its input, output, side-by-side comparison where applicable, README with goal/prompt/machine checks, and provenance. Record exact row counts and any failed or skipped rows; do not claim a batch pass if any required row is incomplete.

## Frozen rows

| Row | Work | Required evidence |
|---|---|---|
| R8 | Fresh install and engine qualification | Official installer succeeds; exact rehearsal reaches the GPU boundary; pod verifies locked CUDA wheels/RECORD, environment probe, GPU sanity, and healthy H3 engine. This is the install prerequisite only. |
| H-C1 | THE CLIP repeatability run | Successful canonical generation, review packet, and comparison with the retained reference showing the repeatability checks and any differences. |
| H-C2 | C0 masked edit rerun | Successful canonical edit, review packet, and the existing C0 acceptance checks. |
| H-C3a | `t1-neo-silly` continue | Successful canonical continue job and review packet. |
| H-C3b | `t2-corner` edit | Successful canonical edit job and review packet. |
| H-C3c | `t3-neo-to-morpheus` two-reference generation | Successful canonical generation using both references and review packet. |
| H-C3d | `t4-watermark` edit | Successful canonical edit job and review packet. |
| H-C3e (optional) | `t2` then `t4` as one two-edit spec | If run, both ordered edits and cumulative checks pass and the packet records both passes. |
| H-M | M's merged-head rerun | On M's declared merged head, repeat the install/canary and all required production rows above through the canonical path; retain one review packet per generation/edit job and setup/provenance records. |

## Completion

H-C1, H-C2, and H-C3a through H-C3d are required. H-C3e is optional. Each required row must have its own accepted boundary rehearsal and pod evidence; batching may share a warm session only when the immutable row inputs and per-row evidence remain distinct. The H stream may report `DONE-S1` only when install, THE CLIP, C0, and the required Phase C packets exist. M owns H-M and the final merged-head completion signal.
