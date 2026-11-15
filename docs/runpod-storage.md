# RunPod model storage audit

This is a read-only inventory guide for an already claimed pod. It does not
delete files or make a pod claim. Native inspection commands work immediately;
the standalone `scripts/model_storage_audit.py` from this candidate must first
be staged to `/tmp/astrid-model-storage-audit.py` through that same transport.
This leaves the installed source tree unchanged. The mounted network
volume is `/workspace` in the H3 and generation flows described here.

## Inspect the claimed pod

Run these through the existing claimed-pod transport:

```sh
findmnt -T /workspace -o TARGET,SOURCE,FSTYPE,OPTIONS
df -B1 --output=source,fstype,size,used,avail,pcent,target /workspace
du -x -B1 --max-depth=2 /workspace/h3-golden/releases /workspace/astrid/engines/comfyui/models | sort -n
du -x -B1 --max-depth=3 /workspace
find /workspace -xdev -type f -size +100M -printf '%s\t%p\n' | sort -nr
```

If `findmnt` identifies MooseFS (`fuse.mfs`, `fuse.moosefs`, or equivalent)
and its client tools are installed, inspect its subtree quota and usage:

```sh
mfsgetquota -n /workspace
mfsdirinfo -n -l -s -r /workspace
```

`mfsgetquota -n` is the read-only quota query. Its output has current, soft,
and hard values for inode count, logical length, chunk size, and estimated
physical size. It is text output; missing tools, failed queries, or output that
cannot be parsed are unknown capacity, not evidence of free space. `mfsdirinfo`
is a usage diagnostic; its default counters are quick but approximate. Add
`-p` only when a slower precise recursive scan is acceptable. On non-MooseFS
mounts, skip these commands. `df` remains useful for filesystem-reported
capacity, but does not describe per-directory MooseFS quota headroom.

## Audit model references

After staging the standalone script, locate the installed tree to read its
locks without assuming a checkout path:

```sh
AUDIT_SOURCE=$(/opt/astrid/bin/python -c 'from pathlib import Path; import astrid; print(Path(astrid.__file__).resolve().parent.parent)')
python3 /tmp/astrid-model-storage-audit.py --dry-run \
  --mount /workspace \
  --engine-root /root/astrid/engines/comfyui \
  --models-root /workspace/astrid/engines/comfyui/models \
  --lock "$AUDIT_SOURCE/astrid/packs/runpod/models.lock.json" \
  --lock "$AUDIT_SOURCE/astrid/packs/wan2gp/models.lock.json" \
  --lock "$AUDIT_SOURCE/astrid/packs/vibecomfy/models.lock.json"
```

Repeat `--lock` for each shipped lock that can reference this shared model
root. A supplied lock protects its destinations in that selected cache; an
unused locked model being absent there is ordinary inventory, not an error.
The shared RunPod lock includes H3 models used by ordinary claimed generation
in this cache. Older standalone H3 releases can hold additional copies at
their own model roots. The audit discovers their release manifests, model locks,
model inventories, and engine install receipts. It reports references by
source/version, present model paths, hard-link-aware scanned byte totals, and
files outside the scanned reference set as `not_in_scanned_references`.
The byte totals are logical file sizes, not quota or replica usage; use the
native quota report for usable free space. Metadata is limited to named files,
bounded in size, and read without following
symlinks or crossing devices. The output always has `deletable: []` and
`reference_coverage_complete: false`; another task, version, or external lock
may still reference any apparent orphan.

The audit also scans conventional directories named `models` when no recognized
metadata exists, so an interrupted version can still appear as unclassified.
Astrid currently has no sanctioned model-cache prune/GC command; releasing a
pod preserves its network volume. Do not treat unclassified files or incomplete
release directories as permission to delete them.

Code establishes that H3 release models live under
`/workspace/h3-golden/releases/<release-id>/models`, while the ordinary
ComfyUI cache lives under `/workspace/astrid/engines/comfyui/models`. Their
separate roots make duplication possible. No pod inventory has been observed
here, so this does not claim that either tree contains duplicate files on the
target volume or that either tree is safe to remove.

## Follow-up

Track **RunPod model-cache GC** on astrid-dev (estimate: 2–3 engineering days,
plus live qualification). Product homes are `astrid/core/engine_cli.py`, engine
custody, and the pack-owned H3 release metadata consumed by this audit.
Compute the union of current locks, retained engine/release versions, and
active job/build references; define how a version is retired. Make dry-run
the default and require explicit application, revalidated under volume
custody, with a deletion receipt. Account for MooseFS trash and delayed
reclamation. Until that reference authority exists, no orphan listing is a
safe deletion list. The audit itself remains read-only.

## Preparation admission

Managed `align` checks capacity before restoring nodes or starting the model
fetch. When a fetch is planned, needed bytes are the selected workflow's
pinned model sizes minus files whose size and SHA-256 both verify. Partial
files and same-size files with bad digests receive no credit. This uses the
workflow manifest, not all models in the shared engine lock. A run whose
files all have the expected size plans no fetch and retains the existing
post-alignment digest checks without adding another hash pass.

The manager's post-round-4 ruling retains that full-size charge. Preparation
calls native `vibecomfy.cli fetch` through `ManagedEnginePreparation._command`;
it does not read `.part` files. A stale partial therefore receives no credit.
The separate `asset_alignment.align -> _align_selected -> _fetch_model` path
never calls this preflight. Its prefix replay and mirrored partial coexist
with VibeComfy's full temporary file, needing `2 * size - retained_prefix`
additional payload bytes for a single resumed model.

Commit `f7afb3e42` introduced the preparation-timeout advice "rerun to resume
the download". The native-fetch source finding above disproves that advice
on the h3 preparation path. The manager authorizes replacing only that text
expectation: completed model files are kept and verified on rerun;
interrupted models download again from the start. The timeout-only handling
and two tests are ported from that commit here; unrelated h3 evidence changes
remain on its branch. Exception identity, code, retryability, recovery handle,
cleanup and execution-timeout test assertions remain unchanged. The one-hour
preparation limit also remains unchanged.

Preparation groups demand only by the exact resolved target directory and
compares each group's bytes with that directory's `statvfs` free bytes and
covering hard quotas. Missing intended directories remain separate groups;
their nearest existing ancestor supplies measurements without creating them.
MooseFS quotas are queried directly with `mfsgetquota -n PATH`, one short
command per queried ancestor, with a timeout capped at ten seconds and the
shared preparation deadline. There is no device or quota-alias aggregation.
This deliberately accepts missed refusals for directories sharing a budget or bind aliases in exchange for avoiding cross-directory false refusals.

The manager's round-7 ruling for the **P1 soft-quota false refusal** is that
a soft limit never refuses, regardless of grace. Soft and hard fields are
parsed separately; only the hard logical-length limit minus current usage,
clamped at zero, supplies quota headroom. The previous `without_grace` test
actually used default grace. It now declares zero grace and expects admission
below the hard limit; a 24-hour-grace regression also distinguishes soft198
from hard1000 versus hard198 at usage190. This explicitly supersedes the
earlier soft-limit expectation. The old storage probe uses only `statvfs`;
that can describe the cluster rather than a subtree quota. The declared-volume
observation below supplements these quota measurements.

The round-7 URL/revision finding remains a known missed refusal under the
manager's ruling. Native VibeComfy can redownload a digest-valid file when
its source receipt's requested URL/revision changes. Its exact decision is
inline in `fetch.download`; no read-only callable exposes the complete
decision. `read_resolution_receipt` returning `None` also covers absent
receipts where SHA verification permits reuse, so it is not a download
decision. Preparation keeps its digest credit rather than duplicating
native policy. A corrected reuse API and its native integration are separate
work owned by the manager.

Hashing checks cancellation before every 1 MiB read and before returning,
while retaining the existing deadline and file-identity checks. A single
blocked filesystem read is not preempted by this callback.

The optional native query supports logical-length quotas. Missing tools,
failed or malformed reports, no visible byte quota, other network backends,
and unaccounted quota dimensions do not refuse preparation. Valid measurements
remain usable independently: a valid quota can establish capacity when
`statvfs` fails, and `statvfs` is the fallback when quota measurement fails.
Only a measured available-byte count below needed bytes refuses. Unknown
capacity is never treated as zero. See the
[MooseFS quota contract](https://moosefs.com/manpages/mfsquota) and its
[table implementation](https://github.com/moosefs/moosefs/blob/master/mfsclient/tools_quota.c).

When quota measurement is unavailable and fallback permits preparation, or
neither source can measure capacity, preparation proceeds and writes exactly
one warning line to the declared `preparation.storage.stderr` log:

```text
storage capacity could not be measured; proceeding
```

### Manager ruling, 14 November 2026

The manager changed the earlier refusal policy: **never refuse on an unknown
measurement**. The previous `storage_capacity_unavailable` refusal is removed.
The 13 original preflight cases are retained; only their unknown-measurement
expectations change. New cases prove missing-tool fallback with ample and low
`statvfs`, both sources unavailable, another network filesystem, and valid quota
with unavailable `statvfs`. The eight original measured-capacity cases retain
their assertions. Warning retrieval is checked through declared-log collection.

### Evidence boundary

The official RunPod [golden-path table, row 25](https://github.com/runpod/runpod-plugins-official/blob/main/plugins/runpod/skills/runpod/golden-paths/README.md)
mentions a live-verified `df -T` comparison of overlay and MooseFS. This is
evidence of a documented deployment, not proof that every RunPod network
volume, or this particular pod, uses MooseFS. The repository's existing
`astrid/packs/runpod/storage_probe.py` reads mount information and uses
`os.statvfs`; no existing quota-client installation or pod-image binary path
was established. In particular, availability of `mfsgetquota` on the target
image is **unproven**. Its new invocation through
`/usr/local/bin:/usr/bin:/bin` is opportunistic, not an image prerequisite.
No pod or provider call was made to investigate this.

An insufficient-capacity refusal is one fixed line, preserved across the
RunPod failure protocol using only bounded integer facts:

```text
Model download refused (reason=insufficient_storage): <needed> bytes needed, <free> bytes free; use a larger volume; see `astrid tasks logs <task>` (storage.inventory).
```

Capacity is an observation, not a reservation against concurrent writers.
Preparation does not delete files, install quota tools, or extend paid time.

## Product diagnostics on refusal (manager follow-up 2)

An `insufficient_storage` refusal now gathers inventory inside preparation and
declares it through `run_logs.declare(out, LogDecl("storage.inventory", ...))`.
The owner can retrieve it with `astrid tasks logs <task>` without a pod shell.
It contains covering `/proc/self/mountinfo` lines, `statvfs` free/total bytes,
the largest 40 directory totals through depth three, and the largest 60 regular
files over 1 GiB. A metadata-only `os.scandir` walk stays on the models root's
device and skips symlinks and `.codex`. No inventory subprocesses are started;
the separate quota read is one direct `mfsgetquota -n <path>` command with a timeout.

The walk checks cancellation and a shared 55-second deadline capped by
preparation's deadline, and bounds entries, directory bookkeeping and output
(128 KiB). An incomplete note accompanies partial results or unavailable
metadata; diagnostics never replace the measured refusal's numbers. Only the
declared log and normal log-registration metadata are written.

CPU cases prove partial deadline/cancellation results, skipped symlinks and
other devices, capped sorted lists, and no volume deletion.
The prior low-statvfs test now requires the inventory declaration while still
forbidding a misleading proceeding warning; this assertion changes because the
manager explicitly requires a log on every refusal.

## Option (c): storage choices in setup

`astrid machines setup` has no network-volume creation or size flag, and no
container-disk size flag. It can select an existing volume:

```sh
astrid machines setup --volume EXISTING_VOLUME_ID_OR_NAME --json
```

For container/ephemeral disk, run:

```sh
astrid machines setup
```

Press Enter at `Volume [ephemeral]:`. With `--json`, omitting `--volume`
preserves an already saved volume, so it does not clear that selection.
Setup changes preferences for future claims; it does not resize, migrate or
replace the held pod. The provider configuration currently defaults container
disk to 200 GB, but setup exposes no size override. Container-disk caches are
lost when that pod is destroyed; a new network volume starts with an empty
cache and can retain its subsequent downloads.

Cold per-cell locked model bytes, confirmed against declared workflow assets:

| Cell | Bytes |
| --- | ---: |
| Qwen TTS custom voice | 2,498,383,610 |
| Hunyuan Foley SFX | 10,571,592,990 |
| Qwen image T2I | 17,283,095,877 |
| Qwen image edit | 17,283,095,877 |
| AceStep music (live-cell declaration) | 14,695,999,816 |
| H3 default workflow, turbo enabled | 53,484,234,760 |
| Music 3 (built-in music default) | 11,915,469,696 |

The five cells in `scripts/live_cells.json` use TTS, Foley, Qwen T2I/edit and
AceStep. T2I/edit share the same three files, so their distinct model union is
45,049,072,293 bytes. Including H3 gives 98,533,307,053 bytes. Including the
separate Music 3 model set gives the full 110,448,776,749-byte RunPod lock.
The full-lock H3 release publisher differs from an individual H3 workflow:
it serializes and downloads all 36 lock rows. These figures exclude runtime,
outputs and optional unlocked adapters; actual admission still subtracts only
models verified at the selected destination. Recreated ephemeral pods must
re-download their selected model sets.

## Declared network-volume capacity (Release 13)

The RunPod claim journal already records `network_volume_id`,
`network_volume_size_gb` and `volume_mount_path`. Machine submission copies
these facts into the existing `astrid.machine-job.v1` document as
`storage_volume` (`id`, `size_gb`, `mount_path`). The pod scopes that declaration
to this job's managed preparation; it makes no provider lookup. Ephemeral pods
and older claims without a declared network-volume size keep the existing
statvfs/quota behavior.

The repository labels RunPod's size field as GB, but contains no provider API
statement defining its byte multiplier. Admission deliberately uses
`size_gb * 2**30`, the larger reading: ambiguity can under-refuse, never cause a
false refusal through choosing decimal GB.

Before fetch, a metadata-only walk measures allocated bytes (`st_blocks * 512`)
across the **whole declared mount**, including old engines, other caches and
outputs beside the current models directory. Each `(device, inode)` contributes
once, so hardlinks do not inflate usage. Symlinks contribute only their own
allocated metadata; their targets are never followed. Directory descriptors
opened with `O_NOFOLLOW` also guard against replaced directory links. Other
devices are excluded. A target must be inside the declared mount and on its
device for the observation to apply.

The walk checks preparation cancellation and its shared deadline between
entries and directories. Its own budget is at most 60 seconds or one quarter
of preparation's remaining time, whichever is smaller; it also caps entries
at 100,000, directories at 10,000 and depth at 128. A timeout, unreadable
metadata, changed directory identity or exhausted bound makes the observation
unknown. Partial usage never becomes a free-space estimate. Cancellation and
the overall preparation deadline still stop preparation normally. As with the
existing inventory, one blocked filesystem syscall cannot be preempted by a
Python callback.

A completed observation is `max(0, declared_capacity - allocated_usage)`. It is
computed once per preflight and joins statvfs and hard logical-byte quota
measurements for each covered target directory: the tightest known budget
wins. Soft quotas do not refuse; an unknown observation cannot refuse by
itself. Existing quota queries and their warning policy remain in place.
This remains a capacity observation, not a reservation against other writers.

## Inventory after fetch runs out of space

The native managed `fetch` step also declares `storage.inventory` when it
raises `OSError` with EDQUOT (122) or ENOSPC (28), or when its failed command
reports those errno values, symbolic names, or the corresponding standard
quota/space diagnostic. Both stdout and stderr are checked locally; raw
output still stays in the declared preparation logs rather than the public
remote failure diagnostic.

Inventory uses the same bounded declaration and collection as a preflight
refusal. The original error object is re-raised with its type, code and message
unchanged; a Python exception note points to
`astrid tasks logs <task>` (`storage.inventory`) when inventory succeeds. The
ordinary failed-job log transfer also carries this named log into the failure
receipt. A failed inventory cannot replace the original fetch failure.
Other fetch failures and storage errors in other native steps do not trigger
this inventory path.

CPU regressions cover claim-to-job transport, full-mount allocation accounting,
exact admission boundaries, binary GB, hardlinks, symlinks, other devices,
deadline/cancellation, unknown and absent declarations, EDQUOT/ENOSPC exceptions
and command diagnostics, preserved errors, inventory failure, and receipt logs.
