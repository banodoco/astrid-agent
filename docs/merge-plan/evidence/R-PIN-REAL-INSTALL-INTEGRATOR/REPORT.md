# R-PIN-REAL-INSTALL-INTEGRATOR

**Result: exact implementation dependencies established; authentic provisioning and R-24 remain unproven.** Creating a fresh installation was evaluated rather than treating an absent preexisting install as an external blocker. At the exact pins there are two possible full Worker routes, and both have concrete failures that profile JSON cannot repair:

1. **Explicit `engine_launch` relay:** Runtime's real `RoleBoundCustodyBroker` rejects Linux before any ledger or child exists. Agent's engine/listener broker is also Darwin-only. This is an owner product/transport dependency, not a missing file supplied by an operator.
2. **Legacy installed Worker:** the identifiable production source, `banodoco/reigh-worker` at `efc2276d30c22ac0cbe54f735a1129cef79583ef`, is still that repository's HEAD when inspected. Its supervisor speaks `reigh.local-worker-control/v1`, whereas pinned Runtime requires `workspace.local-worker-control/v1`. A real source-only child returns the incompatible version and `private Worker control version is invalid`. Its normal module body exits 0 without entering the private control loop. No compatible Worker release/pin is supplied by the installed source manifests.

No shared installer transaction, Runtime admission, Agent custody implementation, or protected assertion was changed. No authentic installed Worker profile was generated. There was no full installer/engine run or hosted rerun. The pushed change is a four-mock test repair plus this evidence, **not a provisioning fix**.

## Exact inputs and installation boundary

Task clones, created with `/home/user/merge/kit/codex/task-clone.sh`, are under this task's absolute directory:

- Agent inspection/test base: `bc735b9e212cd004348f7494fc4c5b6f4dc512bb`.
- Runtime: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`.
- Pinokio: `3a6a98ae251ca126505245286e2d3e760f4348e9`.
- Required predecessor: `queue/done/R-PIN-REAL-WORKER-PROVISION/{REPORT.md,canonical-receipt.json}`. Both were read and are hashed in the canonical receipt.
- Raw Actions: `/home/user/repo/docs/merge-plan/evidence/actions/{41566976964,41566955365}/`. The runner head is distinct from the installed Agent identity. Backend installed Agent `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`; fully-local installed Agent `69dda026212ca16c37cc8239d06ccd159603aa24`, with Pinokio `7955800419fc92b11e553d6f7acb7613090617e6`. Both installed Runtime pins are the exact Runtime above. Complete tuples, raw paths, hashes, argv and inner exits are retained in the canonical receipt.

The two raw receipts independently record Pinokio install=0, Runtime start=0, Worker-start=1, cleanup=0. Their worker logs both hash to `7773dd8b2e2cd5e929989a3652d0dd72a7a0d8821eb47b8396d92a5b391bf2b9` and report missing `worker_profile`. Neither artifact retains the generated installed source profile separately: omission is source-grounded and corroborated by the Runtime error, not a claimed direct read of an uploaded profile.

## Source-grounded route evaluation

| Route/input | Source at pinned checkout | Finding |
|---|---|---|
| Installed source manifest | Agent `astrid/installer/backend.py`, `Backend.stage`; Pinokio `pinokio/session.mjs` | Installer stages version-specific source/environments and writes the source manifest without `worker_profile`; Pinokio selects that manifest through `ASTRID_RUNTIME_SOURCE_MANIFEST`. Session options expose only `prepareEditor`. |
| Profile loading | Runtime `astrid_runtime/bootstrap.py:398`; `runtime_protocol/local_worker_composition.py:1253` | Strict path and mode-aware declaration validation. Loading is not live process admission. Worker engine, explicit relay, and host-only CPU are distinct selections. |
| Explicit relay | Runtime `local_worker_composition.py:492,520`; `astrid_runtime/custody_broker.py:949,967` | Uses the installed Runtime supervisor rather than importing an external Worker. Its real broker constructor accepts Darwin/Win32 and rejects Linux with `Darwin audit-token custody is unavailable`. |
| Agent delegated engine | Agent `astrid/core/execution/generic_host.py:15753`; `custody_broker.py:1753`; `vibecomfy_engine_adapter.py:645` | Generic host owns the engine/listener, validates executable and source/seam pins, and needs a broker that rejects every non-Darwin platform at this pin. A Linux profile cannot implement its transport. |
| Host-only CPU | Runtime `local_worker_composition.py:324,403,1504` | Avoids engine declarations but still calls the same unsupported Runtime broker. It is also not evidence of the requested engine journey. It is not a workaround here. |
| Linux portability documentation | Runtime `docs/runtime-portability-testing.md:131-139` | Explicitly documents the delegated host/relay broker as macOS-specific and requiring transport work on Linux. This independently agrees with the constructor canary. |
| Legacy Worker | Runtime `local_worker_composition.py:265,292,223`; fetched Worker `source/runtime/supervisor.py:33,1661,1914` | Runtime launches `python -m source.runtime.supervisor --prepared-control-fd ...` and checks `workspace...`. Known Worker pin has the wrong control version and no module entrypoint invocation. Probe executes the unchanged source body and, separately, its real `main` function. No profile or host/engine is supplied. |
| Boot manifest | Agent `astrid/core/gateway/dispatch.py:704`; `astrid/sdk/host_bootstrap.py:1970` | `compose_profile_handoff` already stamps a production manifest from the real registry and checked-in accepted profile specifications. Its absence on this host is constructible, not an external dependency or justification for fabricating conformance evidence. |
| Engine provisioning | Agent `astrid/core/engine_cli.py`; `packs/comfyui/recipe.linux-cu130.json`; `packs/runpod/installed_session.py:68` | Linux ComfyUI and H3 have real local provisioning paths. H3 supports `placement=local`; a Windows-only recipe named `vibecomfy` does not establish that Linux provisioning is impossible. |

The genuine relay profile could be constructed from installed interpreter/kernel bytes, exact Agent/Runtime/Vibe source and release inventories, a production boot stamp, real machine identity, fresh session selection, and measured adapter function/file hashes. Its configuration digest is a **prospective exact serialization** (`json.dumps(config, indent=2, sort_keys=True)`); the actual session writes and live inspector checks it later. Requiring an already-running session as an external input was too strong: `generic_host._NativeEngineGraph.prepare` explicitly refuses a preexisting session registry. Those constructible values do not remove the platform gate. Release/profile identity must be bound to the selected real installation, not invented as an acceptance receipt.

## Creating an installation and resource feasibility

Ran the real production planner, without substituting models, a fake listener, or a fixture engine:

```sh
python3 -c 'import sys; sys.path.insert(0,"agent"); from astrid.core.engine_cli import main; sys.exit(main(["install","comfyui","--platform","linux-x86_64-cu130","--placement","local","--root","/home/user/merge/ws/M-PIN/queue/running/R-PIN-REAL-INSTALL-INTEGRATOR/real-engine","--dry-run"]))'
```

Exit 0 means **plan emitted**, not installation succeeded. The plan's `disk_check.status` is `blocked`: locked model bytes alone are **17,283,095,877**, versus **10,741,649,408** free at that observation. The source/environment/temporary bytes are additional. Preserving 10 GB free requires more than **27,283,095,877 bytes** free before even the unquantified environment/source overhead (or more than 28,020,514,117 with a 10 GiB reserve). The stock recipe has no skip-model switch and requires models before start. Its pins are ComfyUI `423cbd20511becb1f16122ab518e92e58bfa03d9` and VibeComfy `e2905d068d5851b9c32856d32b8251852a01d6dd`, Python 3.12.10 and CUDA 13 wheels. A smaller reviewed engine composition could avoid those stock workload models, but is not proved here; capacity alone is not used to claim every alternative impossible.

Disk was checked before source clones/planning and again before finalization. Some observations were below 10 GiB because this is a shared filesystem; no large dependency/model download or heavy installation was started. Shared caches were preserved. Existing cached Python/pytest was used for focused tests, with bytecode/cache writes disabled. Final recorded free bytes are in `evidence/filesystem-free-bytes.log`.

## Validation, commands, and inner exits

Exact local argv, cwd, exits, timing, and log paths are in `commands.jsonl`. Raw Actions argv and inner exits are copied into the canonical receipt. Source inspection commands and task-clone/bootstrap outcomes are separately recorded there; the session transcript is `events.jsonl`.

| Check | Exit/result | Interpretation |
|---|---|---|
| Three exact-input `task-clone.sh` commands | 0 each | Separate shared-object task clones, not shared installer edits. |
| WIP commit `9cbc7475ca9b0362c63b5cf31221b2f5431df396` and push | 0 | Pushed before remaining probes; no long installer run. |
| Real engine CLI dry-run | 0, disk blocked | Installed-product planner, not an installed engine. |
| Real Runtime and Agent broker constructors, Linux | 2 | Both real implementations reject platform; neither ledger exists. Root UID 0; no monkeypatched platform or identity. |
| Attempt to launch identical canary as UID/GID 65534 | 1 | Kernel `PermissionError: EPERM` during credential change, before the canary starts. **No non-root proof.** Approval was not requested or bypassed. |
| Fetch exact legacy source and `git ls-remote ... HEAD` | 0 each | Raw source SHA-256 `115e7db0b3e9386ad23cb41180d88ddbd71d50a3743406ccd7d7472043c1fc9c`; HEAD matches documented old pin. No full Worker installation claimed. |
| Legacy source compatibility canary | 2 | Normal source-body child exit=0 with no response; explicit `main` child rejects control version and exits=78 after control closes. No profile generated. |
| Four named Runtime profile tests | 0, **4 passed** | Legacy/relay/host-only schema behavior only; fixtures are tests, not installed provenance. |
| Initial Agent selection | 1, 1 passed / 3 failed | Existing `_client` mocks accept 2 arguments while current helper passes diagnostic argument 3. |
| Corrected named Agent selection | 0, **6 passed** | Four mock signatures now accept optional diagnostic input. Current-session, terminal refusal, exact not-found, get/start/get and pin checks pass. All 90 AST assertion statements unchanged. |
| `git diff --check` in Agent, Runtime, Pinokio | 0 | No whitespace errors; Runtime/Pinokio have no changes. |

Pytest reports one configuration warning in each invocation because the cached environment lacks the optional timeout plugin. No `--maxfail`, `-x`, test skips, or assertions were changed. This warning is not an installed proof. Earlier reads of prerequisite clones failed when the manager reclaimed them; exact clones were then created within this running task. The command records preserve the initial non-root refusal and initial test failures.

## Subworker comparison and spot checks

Both staged Luna investigations ran concurrently: sub/1 through the host watcher and sub/2 through the collaboration service because the watcher was capped at one. Their final reports and hashes are in the receipt.

- **sub/1:** correctly found actual Linux engine commands, source pins, Pinokio install/start boundary, and no end-to-end producer. Its read-only conclusion about unavailable installed inputs is insufficient by itself; parent added planner execution, live constructor refusal, legacy source/ABI probing, and boot-generation timing. It did not execute an install or prove live custody.
- **sub/2:** traced the explicit relay, live observer, and boot composition. Parent source spot-check rejected its initial suggestion that custody was Linux-capable and its intermediate standard-recipe-name blocker. Final report corrects both: Runtime/Agent brokers explicitly reject Linux; H3 supports local placement. Parent verified the corrected exact source lines and executed the real constructors. Its no-Worker-generator search is not treated as proof that one cannot be written.

## Executable next action and owner handoff

The exact missing dependency is **a reviewed Linux-capable execution implementation**, through either of these routes:

1. **Preferred existing architecture: M-RT plus Agent execution/custody owner** supply compatible Linux implementations of the Runtime relay/host broker and Agent engine/listener broker and their authenticated private channels, retained process handles, birth/UID checks, descriptor transfer, signal/exit/drain and recovery semantics. Preserve existing admission. A JSON generator or enabling a platform string cannot substitute for this work.
2. **Alternative: M-RT plus legacy Worker release owner** supply a reviewed immutable Worker release with a functioning `source.runtime.supervisor` module entrypoint and the exact `workspace.local-worker-control/v1`, preparation/activation/receipt behavior expected by Runtime 2776ba9. The known `efc2276...` release does not supply it. Do not silently change protocol constants or fetch an unpinned tip in Pinokio.

Re-run the canaries on the disposable **non-root** Linux runner before an expensive install, using the actual reviewed refs:

```sh
python3 probe_linux_custody.py --runtime-checkout /absolute/runtime --agent-checkout /absolute/agent --scratch /absolute/task/probe-runs
python3 fetch_legacy_source.py
python3 probe_legacy_worker.py --runtime-checkout /absolute/runtime --worker-source /absolute/task/evidence/legacy-supervisor.py
```

For the relay route the first command must successfully exercise the real Linux implementation; it currently exits 2. The legacy commands instead reproduce why the documented old release cannot serve as the alternative (exit 2). A new reviewed legacy release must be explicitly selected and hashed before that alternative can be enabled. These are executable dependency probes, **not Worker-ready or R-24 receipts**.

After one implementation route exists, M-PIN owns creating the real target installation and producer/materializer, with enough disk for the selected engine. Use `compose_profile_handoff` for the boot stamp; derive all executable/source/seam/release pins from installed bytes, select a fresh session, then call the pinned loader followed by real Worker generation/admission. M-PIN's producer must accept the actual staged install paths, not guess their random/version-specific names.

**M-R1FIX installer contract/lifecycle:** no validated input was delivered by that owner; its `R18-PIN-WORKER-PROFILE/REPORT.md` explicitly requests producer and fresh/update/recovery timing. Coordinate a callback or supported staging-phase interface **after** candidate version paths/environments exist, **before** source-profile validation, activation, and Runtime start. Publish only a validated, version-bound `worker_profile` path through the owner's transaction. Fresh install produces new measured inputs; update regenerates for the candidate version rather than carrying stale executable/source pins; rollback selects its matching preserved version/profile; crash recovery retains/refuses uncertain live custody and cannot adopt/recreate an existing session registry. These are required handoff semantics, not implemented behavior. Installer validation/transactions remain M-R1FIX's scope, Runtime/Agent execution remains their owners' scope. Pinokio will transport the resulting supported input in both modes without patching an installed manifest after the fact.

Requests and results were routed through `/home/user/merge/ws/M-PIN/STATUS.md` to M-R1FIX and M-RT. No handoff for release acceptance was emitted.

## Pushed lane and limitations

Repository `banodoco/astrid-agent`, lane `lane/M-PIN/R-PIN-REAL-INSTALL-INTEGRATOR`, exact base `bc735b9e212cd004348f7494fc4c5b6f4dc512bb`. Test candidate `643cd5c27a77589a37a9a857fbfb55c944300813` is pushed. It changes only four test callback signatures. The evidence publication commit, parent chain, hashes, and remote confirmation are recorded in the task's **single canonical receipt**. Evidence-only publication carries forward the focused tests because product and test bytes do not change afterward. Runtime and Pinokio remain clean at their input pins.

No installed-profile loader proof, authentic non-root process validation, real-engine readiness, running task, protected busy/Stop/retry journey, M4-6 qualification, or R-24 acceptance is claimed. The concrete alternative completion condition is satisfied by source-backed missing implementations, executed refusal/ABI probes, and a routed executable next action; the original product stop remains open.
