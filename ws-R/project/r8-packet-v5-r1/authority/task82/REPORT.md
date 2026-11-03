# Task 82 — reserve stop while waiting for the heavy slot

**Readiness is WITHHELD / UNPROVEN.** The required blocking heavy wrapper was invoked once. While it waited, available disk fell below the unchanged **6,262,359,040-byte reserve**. The first retained low sample, **2026-11-02T16:23:52.706817Z**, showed **6,186,934,272 bytes** (shortfall **75,424,768 bytes**). The next sample returned to the coordinator showed **6,002,540,544 bytes** at **16:24:22.707630Z** (shortfall **259,818,496 bytes**). The coordinator interrupted the original tool session; it returned terminal **exit 130**. This is a **resource/controller guard stop, with no R8 product verdict**. [Exact failure and counts](logs/failure-stage.json), [30-second observations](logs/wait-observations.jsonl), [terminal receipt](logs/tool-session-terminal.json).

Counts: **0 installers, 0 qualifications, 0 retries, 0 direct child stages, 0 engine installs, 0 provider/GPU calls and 0 pod submissions**. There was **1 blocking heavy-wrapper invocation**, **0 heavy-slot acquisitions**, and **0 fix-3 one-scope launcher invocations**. The wrapper did **not** reach the exact `package/run.py`; there is no acquisition receipt and its ordinary log is empty. `work/82` and its install prefix were absent before and after the wait. No source preparation, download, manifest, selected matrix, install, managed environment, test state or service was created. [Scope counts](logs/scope-terminal.json), [empty heavy log](logs/heavy-scope.log), [fresh prefix](logs/fresh-prefix.json).

M-GPU's **2026-11-02T16:14Z** note was read from `W/ORCHESTRATOR.md`: “AUTHORIZED: ONE successor R8 attempt (82). Same tuple, same rules.” It records task 81's reserve stop, stale `/tmp` scratch reclaimed by the manager, and **6,592,143,360 bytes** observed at authorization. It also directs: “If the reserve gate fails again, stop as before and report. Don't lower the floor.” No successor or retry is inferred from unused installer/qualification counts or later disk recovery. [Exact authorization](logs/successor-authorization.md).

The initial terminal `df -B1` at **16:15:49Z** showed **6,589,681,664 bytes** available. The first persisted start check at **16:17:43Z** showed **6,576,877,568 bytes**; immediately before invoking the heavy wrapper it was **6,574,825,472 bytes**. All four required STOP markers (`W/STOP`, `ops/STOP-DISPATCH`, `ops/STOP-PODS`, `podq/STOP`) were absent initially, at every retained wait sample and at the post-stop check. The minimum retained wait sample was **5,995,282,432 bytes**, above the **2,000,000,000-byte hard floor** but below the reserve. There is **no locked or immediate pre-installer measurement**, because those stages were never reached. The coordinator stopped when it read the low samples; the observer records samples but does not enforce a gate itself. Later recovery to **6,662,561,792 bytes** was recorded without restarting. No gate was lowered. [Start](logs/start-check.json), [prelaunch](logs/prelaunch-guards.json), [post-stop](logs/final-guards.json).

The single executed heavy command, from this absolute task directory, was:

```sh
cd /home/user/astrid/proof-codex/queue/running/82_R8_RESTART_CANARY
bash /home/user/astrid/ops/heavy.sh /home/user/astrid/proof-codex/work/21/python-env/bin/python -B /home/user/astrid/proof-codex/queue/running/82_R8_RESTART_CANARY/package/run.py
```

Its original tool session was **53359**. It was interrupted through that same session and returned **130**; no replacement invocation was made. The read-only observer used task-local logs and 30-second sampling. No direct flock, nonblocking acquisition/polling, priority bypass, another task's marker change/deletion or lock-inode replacement occurred. The permanent lock remains device **65024**, inode **1860067**. `heavy.sh` and `heavy-should-yield.sh` bytes exactly match task 80's reviewed versions; the priority-2 read-only probe returned **1**, before marker scanning. Current disk-guard, canary-hold and boot-recover bytes were read and retained; their canary matching includes `*R8_*`. A tool-namespace process inspection could not see the holder; no host ownership or absence claim follows. [Wrapper review](logs/heavy-wrapper-review.json), [lock identity](logs/heavy-lock-identity.json), [namespace limit](logs/blocking-lock-observations.jsonl).

The copied **unexecuted** path selects the unchanged fix-3 launcher with explicit argv arrays:

```sh
/home/user/astrid/proof-codex/work/21/python-env/bin/python -B /home/user/astrid/orch/tasks/R8-RESTART-FIX-3/run_in_one_scope.py /home/user/astrid/proof-codex/queue/running/82_R8_RESTART_CANARY/package/commands.json
```

Its install command would select `package/installer-controller.py --work-dir W/work/82 --task-dir T --uv-source /root/.local/bin/uv --install`, then the official `sh W/work/82/install/install.sh --root W/work/82/install/program --data-root W/work/82/install/data`. Its qualification wrapper would select only the public `W/harness/canary.py qualify --tuple T/frozen-tuple.json --task-dir T --work-dir W/work/82`, with the shared Python and `-B`. **None of these stage commands ran**, and neither a canonical attempt receipt nor a frozen-tuple file was fabricated. [Prepared exact argv](package/commands.json).

All full commit objects, trees, parents and donor states were independently re-read before side effects. All six shared donors were clean. Agent/Runtime/harness HEADs match the frozen selection. VibeComfy, lifecycle and runpod donors supply explicit selected objects; the prepared clone code never substitutes their donor HEADs.

| Component | Selected commit | Selected tree | Parent(s) |
| --- | --- | --- | --- |
| agent | `69f0492b7cbdd22111a9935a952273243ea2e57d` | `dba205dfe5efb1449c0d0f4acee52cff99e5f45e` | `215562c3804826a1a8b8656d1e3780e268ed617d` |
| harness | `20db767762274d83099dc1471fb6048f11084ed1` | `017552fc0a4c5a9717fe10e1197ce3bc72d324f9` | `114107d27bba64e35f1367cb699cb03ca2ca6488` |
| lifecycle | `96360a27a53c4608f780f7b370f747a2aa3c7ca8` | `24e3562181f0ad64003d5cef5403b15d16415be6` | `15fb8f85067f5b970a201a439a3e48c3e9c5a230` |
| runpod | `0786b3d51855a66176fea1e7443b92e7ef2d01b5` | `8e42fe3e9b2876b71270f2f6256bece8dc5dc651` | `59b772915ae2b8cc1ba56f538ea4acd0161c1405` |
| runtime | `67fad49ad202f18b5207fb70abcf344461b1a5cc` | `4829c0ea0275bbc4f60622c5de6e3a4690375e68` | `0ff2ecaf910b1a050914a81ebd771b59a476ffbc` |
| vibecomfy | `ac0ee1b80991f2414dae27d264af4b5e2890a7a6` | `749cd2ca1d994924b16bfcc19e0caacbf5697f3e` | `b1429c634e6c8ab44222c5e9901c318181f709f7 3e762bbc74d3d7761bced30fe4697e2954ff124a` |

Agent and Runtime donors are `/home/user/astrid/orch/tasks/R8-RESTART-FIX-3/{agent,runtime}`; others are `W/src/{VibeComfy,runpod-lifecycle,runpod-python}`; harness is `W/harness`. Full current provenance equals task 80 and the exact task 79 evidence verified by task 80. Agent's single child delta is only `runtime.lock.json.source_commit`, changing `70f15b4ceed851cba67b9e6a408d88ab9ff70e1e` to exact Runtime `67fad49ad202f18b5207fb70abcf344461b1a5cc`. All other fields are equal; **14/14** vendored members match their declared SHA256 and Runtime Git bytes. [Full provenance](logs/source-provenance.json), [archive comparison](logs/task79-80-provenance-comparison.json), [lock](logs/lock-alignment.json).

Task 81's report, exception, empty execution log, failure-stage and terminal counts were reviewed and retained: **5,712,674,816 bytes**, reserve shortfall **549,684,224**, zero install/qualification calls before its locked preparation guard. Its package is absent after retention and was not used. Every task 80 package member was checked against its final SHA256 record before copying. Task 80's reported task 79 REPORT/provenance/lock digests, CPU review digest and package custody were independently verified. Only task paths/labels changed in `commands.json`, `run.py`, `provenance.py` and `final-review.py`; the remaining six package members, including installer and qualification controllers, are byte-identical. The re-copy inside `prepare()` now points to the verified task 80 installer donor. No archived task, product or shared harness file was edited. [Task 81 evidence](logs/task81-reserve-review.json), [custody](logs/task79-80-custody-review.json), [package origin and hashes](logs/controller-package-origin.json), [reviewable task-path delta](logs/run.py.adaptation.diff).

The actual imported **`installed_canary.common.inventory()`** returned the same exact selected assets:

| Asset | SHA256 |
| --- | --- |
| `actions/parent.py` | `90af95bf904708bd230e48316727262da9d9463a2e070f27954218247913ef7c` |
| `pack.yaml` | `d163d0c20efc54dcfdc2f942e5cc4c53a0db42b0905e3bca2838dab5e0789ace` |

Module SHA256 **`9730d10c2b8869e5960a0a0604ebd6bb57158b592a26b1336f8fa687e048c5de`** and function-source SHA256 **`cdbe68b587f036f718de1ef7b73c3c831c5b48418a75bc327ac19441804a206d`** match tasks 79/80. Exact inventory assertion, symlink refusal and bytecode exclusion remain intact. Shared ignored bytecode was retained untouched. [Canonical inventory](logs/initial-canonical-inventory.json).

Read the playbook, structural adjudication, Agent local-source-install docs, harness README/PROVENANCE, tasks 79/80/81 reports and logs, and fix-3 one-scope launcher. Fresh controller Python compiled in memory; task launcher, heavy wrapper/policy and pinned-env shell syntax passed. Shared interpreter: `/home/user/astrid/proof-codex/work/21/python-env/bin/python`. [Prior-art digests](logs/prior-art-read.json), [syntax](logs/controller-syntax-review.json).

Reverified focused fix-3 CPU evidence: exact source identities, original command arrays, selected test bytes, artifact SHA256, JUnit counts and failure sets; the resulting review equals tasks 79/80 byte-for-byte. Runtime **516 → 519 passed**; Agent **750 passed/66 failed → 752 passed/64 failed**, zero new failures/skips; installed-wheel process regression **2 failures → 2 passes**. Agent implementation evidence belongs to parent `215562c3804826a1a8b8656d1e3780e268ed617d`; its selected child changes only the verified lock field. **No unchanged suite was rerun.** These historical results are not task 82 qualification. [Complete original commands/counts/digests](logs/fix3-cpu-evidence-review.json), [exact comparison](logs/cpu-evidence-comparison.json).

Fresh official parser/archive/source/entry resolution, harness input resolution, selected `py_compile`, bootstrap syntax, selected test collection, pinned-env execution and installer environment/mktemp rehearsal were **not reached**. Task 80's successful 65 Agent / 79 harness collections are not claimed as fresh task 82 collection. No test or integrity check was skipped or weakened to continue. The prepared path keeps clean task-local HOME/TMPDIR/XDG/basetemp/launcher and a single uv selector, `UV_PYTHON_PREFERENCE=only-managed`. [Explicit rehearsal limits](logs/rehearsal-status.json).

Installed identity/RECORD, 32 API bindings, four real composition contracts, restart, ready/profile/matrix/capability captures, H3 custody/admission/claim, project/input/lineage/placement, terminal acceptance and the integrated journey are **UNPROVEN**. No installed version or Runtime data root exists; canonical down was not applicable and was invoked **0 times**. Installed stopped/idle-safe/zero-task acceptance remains **UNPROVEN**. **Host-process absence is manager-owned and unproven**; no manager evidence was received. No GPU behavior, warm reuse or actual lease execution was established. [Service/cleanup observation](logs/cleanup-service-observation.json).

All nine adjudicated invariants remain unchanged: owner-only stop/unload; failed start retires only its own publication; custody before side effects; one terminal-transition owner per attempt; digests identify bytes and associations carry meaning; one output inventory/materializer; advertised routes compose and unsupported routes refuse before acquisition; qualification uses the exact installed tuple with test-owned state; one journey validator on the integrated candidate. The one warm lease retains its **1800-second default**, single precedence, holder-specific expiry and verified warm-snapshot reuse inside the lease. No disconnected validator or duplicate inventory was added. The reviewed task-path delta found no new R1–R5 violation; historical R3 inventory ownership is recorded in [BUGS.md](BUGS.md). This is not a complete structural or installed-success audit.

Results are recorded here **before retention cleanup/final verification**. No scratch clone, basetemp, TMPDIR, rehearsal sandbox, review/patch-check copy, ad hoc venv or full clone was created. `work/82` remains absent. Runner-owned control files remain untouched. Final verification **passed**: all **7857 shared tracked files**, all six donor Git HEAD/status records and the ignored harness bytecode hash/size/mtime/inode are unchanged. Task 79/80 archived package and REPORT hashes and task 81's copied evidence remain unchanged. Reference review checked **33 live roots**, **229 declaration/script files** and **16 Git alternate files**, with **zero live references and zero external borrowers**. Nothing required deletion; no unrelated file was deleted. [Shared bytes](logs/final-source-and-harness-identities.json), [archive review](logs/archived-inputs-final-review.json), [references](logs/cleanup-precheck.json), [cleanup](logs/cleanup.json). No local commit or incremental bundle is necessary.

Task session started **2026-11-02T16:15:37Z**. **Pod/GPU sessions 0, GPU seconds 0, pod/GPU cost USD 0**. Host/model billing is unavailable. No credential was read/exposed, upstream push made, provider called, or heavy output written in `/tmp`. One local read-only observer was used; no sub-worker, Codex or launch-worker process was spawned.

Final patch: [patches/82_R8_RESTART_CANARY.patch](patches/82_R8_RESTART_CANARY.patch), **0 bytes**, SHA256 **`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`**. Validation passed, **exit 0**, without applying any patch:

```sh
git -C /home/user/astrid/proof-codex/harness apply --check --allow-empty /home/user/astrid/proof-codex/queue/running/82_R8_RESTART_CANARY/patches/82_R8_RESTART_CANARY.patch
```

[Patch receipt](logs/patch-check.json), [patch identity](logs/patch-identity.json), [final package hashes](logs/final-package-sha256.json). No local commits were necessary, so no incremental bundle is required. Final artifact digests and their verification are retained in [checksums.sha256](logs/checksums.sha256) and [checksum-verification.log](logs/checksum-verification.log).

Final review at **2026-11-02T16:28:54.220902+00:00** records **797.2 seconds (13.29 minutes)** elapsed from task start and **6,304,907,264 bytes** available. The observer's last retained sample remains 16:24:52Z after the original tool session was interrupted; its stop marker is present. Host-process absence remains manager-owned. Readiness stays **WITHHELD / UNPROVEN** after the reserve stop; no retry or new attempt was performed after free space recovered. [Elapsed/cost record](logs/final-review.json).

Delegation: used 0 sub-workers
