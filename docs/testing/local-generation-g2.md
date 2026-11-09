# G2: local generation on stock CPU ComfyUI

Verified 2026-10-24 in the `agent-g2` worktree. All requested CLI/task paths
passed after four fixes. No model files, API keys, cloud sessions, or timeline
edits were used. Scratch fixtures, environments, logs, and Runtime data remain
under `/home/user/e2e`; none are committed.

## Environment and method

- Copied stock ComfyUI revision `fb3befd168731438f88bc96e0920a538e8649878`
  to `/home/user/e2e/ComfyUI`; created its own `.venv` with uv. CPU-index torch
  installation and ComfyUI requirements succeeded without a PyPI fallback.
- `/system_stats`: ComfyUI 0.42.0, Python 3.13.17, torch 2.14.1+cpu, device CPU.
  Started `main.py --cpu --listen 127.0.0.1 --port 8188`. A middleware added
  **only to the scratch copy** records HTTP methods, paths, and prompt/queue/
  interrupt bodies in `comfyui.log`; execution nodes were stock.
- Created a fresh Runtime workspace at `/home/user/e2e/g2-runtime/data` with
  `astrid.services declare`, `astrid_runtime workspace create`, and
  `python -m astrid_runtime up --profile astrid --hosted-origin off`.
  The profile selected this worktree and the supplied agent environment;
  host identity attestation confirmed `/home/user/wt/agent-g2`.
- Saved all four UI workflows through `POST /userdata/workflows%2F<name>.json`.
  Imported workflow JSON and the image ZIP with `astrid media import`, then
  admitted tasks through `python -m astrid.omp_invoke`. Inspected results with
  `astrid tasks show`, `tasks follow`, and `media list`.

## Results

| Check | Result and evidence |
| --- | --- |
| Discovery | Found port 8188, absolute folders, output directory, Python, all four saved workflows, and Manager absent. Port 8000 correctly reported connection refused. |
| EmptyImage → SaveImage | Succeeded; image and result manifest settled into project `g2`. Initial task `135be3d805164bcbaee10dcfbdf1478a`. |
| LoadImage → ImageInvert → SaveImage | Succeeded using `local pack-images` → imported `managed_assets`. Upload reference stayed under `astrid/<task-id>/`. Task `8eb9190c0f134f9bb54dec9580a85a7a`; returned pixels exactly match ComfyUI's float32 inversion and uint8 conversion. |
| Missing node | Task `6553bb532a1746d2a4ae5959864f68e6` failed before `/prompt`: `missing nodes: AstridDefinitelyMissingNode`. |
| Missing model | Task `a2fce7ecf12a4a338519ef7ed37bee0b` failed before `/prompt`: `missing models: astrid_nonexistent_checkpoint.safetensors (CheckpointLoaderSimple.ckpt_name, node 1)`. |
| Cancellation | Cancelled running task `42eed41c4d9c484580ee12cabdaecc77` (EmptyImage → five ImageBlur operations → SaveImage). Only prompt `e7cd1984-c245-4859-a145-634c79a12cd0` was deleted/interrupted. Foreign prompt `d0cb15ae-3613-4849-ad10-5e4c7861109a`, observed pending before cancellation, completed successfully. ComfyUI stayed healthy. |
| Rerun | Tasks `b3d4a61f07c748209b9de247692c9539` and `a044e666677943199540394b41db3f4e` wrote separate files, with image digests starting `936cdcfceb2e` and `fb1f72790ecf`. EmptyImage remains deterministic: pixels are identical; distinct save prefixes change PNG prompt metadata and prevent SaveImage cache reuse. |
| Custody | All generated image bytes read from Runtime matched the original ComfyUI output file hashes after collection. All four saved workflow documents remained unchanged. Generated images appeared in project media. |
| No unload/foreign cancellation | HTTP audit, including Runtime shutdown: zero `/api/free` or `/free` requests, no queue clear, only prompt-specific deletes, and four interrupts all naming our cancellation prompt. |
| Readiness | `vibecomfy.run` ready on CPU; no API-key environment variables in the launcher, Runtime, or pack host, and none configured in the fresh workspace. |
| Preferences | Checkout/check-in saved `## Local generation`; a fresh `python -m astrid --agent-context` invocation printed it, including after the container restart. |

Both missing-dependency errors included the server URL and
`See 'Missing nodes and models' in the VibeComfy skill (packs/vibecomfy/docs/SKILL.md).`

## Bugs found and fixed

1. The public package gateway rejected `--agent-context` as an unknown command.
   It now routes to the existing-runtime-only preference helper.
2. Preference check-in generated `document-checkin:<digest>`, rejected with
   `Idempotency-Key must start with an alphanumeric character and contain at most 256 ASCII characters`.
   It now uses a hyphen and recognizes persisted legacy retry metadata.
3. `pack-images` bound its first asset as `image_1`, causing
   `workflow run input 'image_1' is not a declared public input; valid inputs: image`.
   It now binds the first asset as `image`; the integration test uses this
   actual helper instead of a hand-built ZIP.
4. A seedless rerun returned the same ComfyUI file and Runtime digest
   (`8dd5abb3d578…`). A private canonical workflow revision now appends the
   attempt identity to existing nonempty save prefixes. Saved user workflows
   remain unchanged; other execution profiles retain their existing path.
   The regression failed before the fix with `assert 'astrid' != 'astrid'`.

## Automated verification

From the worktree root, using
`P=/home/user/banodoco/astrid-agent/.venv/bin/python`:

| Command | Result |
| --- | --- |
| `$P -m pytest -q tests/test_agent_context.py tests/test_preferences_documents.py tests/test_omp_agent.py` | 81 passed |
| `$P -m pytest -q tests/test_vibecomfy_production_engine.py tests/packs/test_vibecomfy_metadata.py tests/packs/vibecomfy/test_local_server_helpers.py tests/packs/test_vibecomfy_local.py tests/test_generic_host_vibecomfy_lifecycle.py` | 67 passed, 2 subtests passed; 1 integration test deselected and run below |
| `ASTRID_RUNTIME_CHECKOUT=/home/user/banodoco/astrid-runtime $P -m pytest -q -m integration tests/packs/vibecomfy/test_local_server_run.py` | 15 passed |

Total: **164 passed, plus 2 subtests**. No unresolved test failures.
The pinned VibeComfy emits one existing generated-template deprecation warning.

## Limits and cleanup

No full provider-backed cold chat, browser app interaction, Windows/GPU run,
model/node installation, or absent-to-installed VibeComfy readiness transition
was exercised. VibeComfy was already installed in the supplied environment.
E1/E2 skill routing/recipes were absent from this supplied merge; missing-node/
model errors correctly reference that planned section, but the section itself
is not yet in this checkout.

The container restart killed the first services; they were restarted from the
surviving scratch data. An intermediate task failed with `host shutdown before
execution` while source edits changed host identity; the missing-node check was
repeated successfully. Startup used `python -m astrid_runtime` because the
standalone entrypoint could not find the worktree's vendored generated client.

Final `astrid_runtime down` reported `stopped`, with no active tasks,
unreconciled attempts, or unreleased reservations. ComfyUI was terminated;
both listening ports were confirmed closed. No push or PR was created.
