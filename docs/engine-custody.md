# Linux installation/session owner

The shared engine runner remains the installer. `LinuxEngineServiceClient` implements the local v2 service handoff in `engine_custody.py`:

```python
client = LinuxEngineServiceClient(
    root=root, engine=engine, platform=platform,
    recipe_sha256=recipe_sha256, helper_sha256=helper_sha256,
)
facts = client.call(operation, runner_payload, deadline=deadline)  # probe/start/health
result = client.run(command_request)  # CommandResult
client.stop_owned(deadline=deadline)
```

Construction has no process, network or filesystem effects. The first operation establishes one supervisor under `root/.astrid-engine/session`. Its directory is owner-only (0700), its Unix socket and small session marker are 0600, and it holds `owner.lock` for its lifetime. Long roots work through descriptor-relative Unix socket paths. The installation is for one trusted owner and one active workflow. The socket is not a general multi-user execution service.

The supervisor validates the existing runner service payload against its engine, platform, root, helper/recipe hashes and recipe-derived argv/cwd/env. It returns the existing service facts without a second receipt schema. `probe` never adopts a port owner. `start` launches that exact recipe command, waits for JSON health and required nodes, verifies that its retained direct child owns the loopback listener, then publishes readiness. Repeated clients reuse that child. No signal authority comes from a PID, registry file, port, timestamp or free flock.

Finite commands retain Astrid's existing `ProcessCustody` native handles, observe descendants, drain both output streams continuously, and verify owned cleanup on exit, deadline or client disconnect. The caller's monotonic deadline is transmitted as a remaining duration. Output is bounded per stream (default 256 KiB, maximum 1 MiB; discarded bytes are still drained). Timeout results have `timed_out=True` and `exit_code=None` only after cleanup. A bounded cleanup reserve may extend beyond the execution deadline. Native census and filesystem operations retain their existing finite waits; this does not promise a hard wall-clock bound for a stalled kernel/filesystem. Unknown responses fail with `CustodyUnresolved` and must not be replayed automatically. Failed native cleanup fences further work and retains the live owner's handles for explicit stop/recovery.

HTTP readiness accepts fixed loopback HTTP/1.0 or HTTP/1.1 200 JSON objects with Content-Length. It rejects redirects, compression, transfer encoding, nonfinite JSON, oversized/truncated bodies and missing nodes. Headers are bounded to 64 KiB, health to 1 MiB and object_info to 32 MiB. The total exchange, including slow reads, shares the caller/recipe deadline. ComfyUI's normal aiohttp JSON responses use this framing.

## Existing VibeComfy session identity

The supervisor is the daemon and the canonical recipe's ComfyUI process is its direct child. The existing session reference is **the engine ID**, e.g. `vibecomfy-h3`. Its registry is:

```
root/runtime/out/sessions/vibecomfy-h3/
    pid, comfy_pid, comfy_process_start_identity, url
    config.json, launch.json
    source_revision, source_content_digest
    daemon.log, comfy.log
```

`launch.json` retains VibeComfy's launch token, daemon and child Linux start ticks, and URL. The supervisor's argv carries the existing `--launch-token` pair, for compatibility with VibeComfy's ownership verifier; this is not a new authorization protocol. `config.json` binds the actual runtime/Comfy/model/output roots and port. VibeComfy's effective tracked/non-ignored source hash convention is preserved, excluding bytecode. The source revision must equal the pinned recipe revision. `comfy.log` is a bounded startup snapshot, not an unbounded log file; `daemon.log` is an empty compatibility marker. The marker is published only after owned health succeeds. Failed starts and normal stops remove only the registry files this owner created.

The prepared worker/readiness producer must select this same session reference and copy the existing registry fields and exact config-bytes SHA into its existing `vibecomfy_session` readiness binding. Model/runtime/source checks and Runtime admission/publication remain with their existing owners. The worker must attach to this session; it must not call VibeComfy `session start` to launch a second daemon. An independently prepared reference or mismatched output/config identity must fail admission. The consumer seam below implements exact selection and target-local verification; remote live attachment remains refused. The CPU fixture tests call VibeComfy's actual `_session_ready` and composite ownership verifier against this registry.

The registry is observable compatibility evidence; only the live supervisor's retained native handles authorize engine cleanup. Read it directly for handoff. Do not use VibeComfy's `active_session_metadata` as a recovery operation: that legacy helper can stop/clear a session it considers unhealthy.

The checkout adapter rejects `release(reason="capacity_replacement")` for the reserved `vibecomfy-h3` registry before control preflight, state mutation or legacy stop/start. Its private restart method also guards that registry. Ordinary cancel/free behavior remains in the existing engine controls. This guard must be retained when merging the authenticated endpoint backend changes.

## Prepared consumer selection and current remote limit

`packs/runpod/worker_session.py` reserves `session_ref="vibecomfy-h3"` for installed-engine attachment. `create_managed_session_owner(pod, *, session_ref, preparation_transport=None)` is the internal original-owner composition used by `prepared_task.py`. Other session references retain the legacy transport. The reserved reference requires installed observations even if a caller supplies the old transport; absence never authorizes legacy start.

`packs/runpod/installed_session.py` supplies these internal observation classes:

```python
LocalInstalledVibeComfySessionTransport(*, platform="linux-x86_64-cu130")
RunPodInstalledVibeComfySessionTransport(pod, *, platform="linux-x86_64-cu130")
transport.read_installed(runtime_root)  # dictionary; file evidence, not live readiness
transport.status(selection)           # live verified status, or refusal
```

The optional target-local seam uses the existing `session_owner=` injection on `RunPodRemoteWorkerPreparer`: construct `RunPodManagedVibeComfySessionOwner(LocalInstalledVibeComfySessionTransport(...))` only where the selected installation and processes are actually local. Constructors have no effects. Neither class owns or starts a supervisor. `read_installed` derives the installation root from P2's existing candidate/runtime paths, validates the canonical **local-placement** install receipt through the runner's existing strict parser, checks its start owner/launch hash against the registry, and compares all config fields against the installed recipe. It returns the exact config-byte digest, source revision/content digest, process/birth observations, URL and launch-token digest. It does not return a full launch token or manufacture a ready profile. Each file is limited to 1 MiB and each file-observation pass shares a 20-second deadline. Repeated file reads detect ordinary concurrent changes; they are not an atomic filesystem transaction.

`select` uses those actual config values, validates them with existing `SessionConfig`, and requires equality with the already selected reference digest, interpreter, model/output roots, port and canonical VibeComfy source path. It does not rewrite an immutable deployment reference. A prefreeze producer can call `read_installed` to obtain the correct digest before constructing that reference. Its roots and receipt must already agree with P2; a staged source elsewhere cannot silently substitute for the installed source. No public selection or readiness schema changed.

The local status implementation additionally runs VibeComfy's current source checks and its existing live birth/composite/listener/readiness verifier, then re-observes the files. It performs no process adoption or stop. The remote implementation uses the original pod's `https_transport()` authenticated client and existing `POST /download-chunk` JSON envelopes, validating offset/total/etag/chunk SHA. It sends no Range, Upgrade, job or exec request and cannot expand the startup observation allowlist. **The current read-only agent exposes no target-side live composite-verification operation. Remote `status` therefore refuses after file validation. Downloads alone are insufficient evidence of live custody.** No callback pretends to fill that gap.

Attachment `stop` refuses: it has no retained native handles. Consequently the original prepared-owner release path cannot synthesize session quiescence or delete the pod through this attachment. It still requires a real original-supervisor stop and its existing release checks. That remote stop composition and remote live verification remain unfinished; this seam does not claim a complete remote prepared workflow or provider release. Original P1 claim/release ownership, P2 source/model validation, Runtime admission and publication are unchanged.

## Explicit local stop

Use the same retained artifact/interpreter that launched the installation:

```sh
python -m astrid.core.generation.engine_custody stop /absolute/install/root \
  --engine vibecomfy-h3 --platform linux-x86_64-cu130 --timeout 30
```

Or call `client.stop_owned(deadline=...)`. Stop cancels the active command/start, drains the exact engine and finite-command handles, removes the owned registry files and then retires `control.sock` and `session.json`. It leaves models, sources, dependency locks, install receipts and the stable lock inode intact. A second stop after successful retirement is harmless and does not start a supervisor. SIGTERM/SIGINT delivered by an already-authorized service manager to the *live retained supervisor* also request this cleanup. Do not discover a process by name or signal a PID read from old state.

If the original local owner is lost, a new client refuses replacement while old session state remains. Stop the exact owning service/container/environment through its existing lifecycle owner and verify its containment is stopped before an operator discards that environment's ephemeral state. If exact local containment cannot be established, retain the state and report `CustodyUnresolved`; neither age, PID absence nor a free local flock permits takeover. There is no automatic crash adoption or sweep of other processes.

## Recovery on a replacement pod

1. The existing provider/claim owner must verify destruction of the **original exact pod** with provider GET404 tied to that claim and retained volume. Unknown/live original pods block recovery. A local free flock is insufficient.
2. Preserve model bytes/cache, pinned sources, the complete immutable artifact and all resolved dependency locks, including `root/.astrid-engine/environments`. Preserve install receipts for the runner's normal reconciliation.
3. Only after that verification, discard the old incarnation's `root/.astrid-engine/session/session.json` and `control.sock`, and the old engine registry files listed above. These are ephemeral session evidence, not process authority. Keep `owner.lock` and `install.lock` inodes. Do not recursively delete `.astrid-engine`, `runtime`, models or source trees. The claim owner must correlate the old session owner ID in the existing install receipt with the old pod/volume before selecting this state.
4. Start the canonical installer immediately from the same retained artifact on the new authorized pod. It rebuilds native ownership/readiness/receipt facts; the shared runner/downloader verifies existing model size and full SHA before reuse and reuses the retained resolved locks. HTTPS/Runtime admission must be proved by the existing endpoint owners before generation.

This is a documented operator/provider lifecycle boundary, not a new provider mutation API. This module does not query or mutate providers and never infers original-pod destruction.

## Artifact/source lifetime

The client spawns the same selected interpreter, importing the exact installed Astrid and Runtime source roots. Those roots and the environment must remain at their immutable paths through explicit stop; do not launch from a temporary extraction that a finite CLI deletes. `engine_runner.helper_manifest` includes the custody source and direct native-consumer files. Artifact packaging must also retain the existing `astrid.core.execution` initializer's transitive imports and the complete selected Runtime package (`platform`, cancellation, dirfd/errors and their dependencies). The helper hash is the existing runner identity, not a substitute for complete release-manifest integrity. Full artifact/installed-CLI qualification and paid/GPU execution are separate integration gates.
