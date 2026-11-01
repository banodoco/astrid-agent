# Astrid naming: inventory (evidence tables)

Companion to [README.md](README.md). Counts are from `git grep -I` on the checkouts named in the README header. Where a cell says unverified, it was not confirmed.

## 1. Counts per repo

| Repo | Old-name lines | Files | Notes |
|---|---|---|---|
| App | 5,193 (`src/` 2,337; docs 1,690; scripts 598; supabase 137; config 68; tests 180) | 957 | 71 tracked paths carry an old name; about 70 distinct `REIGH` env names |
| Astrid | 5,597 (live 1,264; history 4,333) | about 698 | history: `.oracle` 4,103, `.megaplan` 52, `artifacts` 36, `receipts` 18, `docs/projects` 94 |
| Runtime | 24 `reigh` lines; `banodoco` 418 (`banodoco_local` 176, `banodoco_workspace_client` 61, `banodoco-local` 38, `banodoco-workspace-runtime` 15) | | 20 old-name paths including tracked `*.egg-info` |
| Distinct old tokens | 2,602 ([old-tokens.txt](old-tokens.txt)) | | 79 collide with an existing new name (section 7) |

## 2. Tracked paths carrying an old name

### astrid (8 files)

| old path | file count |
|---|---|
| `banodoco_workspace_client/` (`__init__.py`, `contract_metadata.py`, `generated.py`) | 3 |
| `.oracle/briefs/10-reigh-bridge-boundary.md` | 1 |
| `.oracle/findings/10-reigh-bridge-boundary.meta.json` | 1 |
| `.oracle/findings/10-reigh-bridge-boundary.txt` | 1 |
| `artifacts/m4/reigh-external-gate-disposition.json` | 1 |
| `tests/stage1/test_reigh_authority_absence.py` | 1 |

### runtime (20 files)

| old path | file count |
|---|---|
| `banodoco_local/` (`__init__`, `__main__`, `bootstrap`, `cli`, `custody_broker`, `io`, `operator_upgrade`, `paths`, `relocation`, `runtime_boundary`, `workspace`, `workspace_adopt`) | 12 |
| `banodoco_workspace_runtime.egg-info/` | 5 |
| `packages/python/banodoco_workspace_client/` | 3 |

### app (71 files)

| old path | file count |
|---|---|
| `src/tools/video-editor/adapters/reigh/` | 8 |
| `supabase/functions/reigh-data-fetch/` (`index.ts`, `index.test.ts`) | 2 |
| `docs/extensions/evidence/releases/extension-ship-quality-rc4/paired/raw/reigh-*.log` | 5 |
| `docs/extensions/evidence/releases/extension-ship-quality-rc5/paired/raw/reigh-*.log` | 5 |
| `scripts/quality/__fixtures__/*/extensions/test-ext/reigh-extension.json` (5 fixture dirs) | 5 |
| `src/tools/video-editor/examples/extensions/**/reigh-extension.json` | 18 |
| `src/tools/video-editor/components/ReighTimelineEditor*.tsx` (incl. 2 tests) | 3 |
| `src/tools/video-editor/components/ReighVideoEditorShell.tsx` | 1 |
| `src/tools/video-editor/runtime/processes/reighAcpBridge.test.ts`, `reighProjectChat.test.ts` | 2 |
| `src/tools/video-editor/runtime/reighAgentContext.ts`, `reighAgentContext.test.ts` | 2 |
| `scripts/reigh-*.ts` / `.test.ts` (acp-bridge, acp-local-only.yml, local-connector, loopback-gateway, project-chat) | 8 |
| `scripts/release/reigh-release-provenance.mjs`, `.test.mjs` | 2 |
| `src/shared/components/ReighLoading.tsx` | 1 |
| `supabase/functions/_shared/reighAppendService.ts` | 1 |
| `config/contracts/reigh-extension.schema.json` | 1 |
| `docs/extensions/reigh-acp-local-bridge.md` | 1 |
| `docs/local-runtime/02-reigh-plan.md`, `docs/local-runtime/04e-reigh-worker-cutover-lane.md` | 2 |
| `docs/local-runtime/reviews/grok-reigh-review.md` | 1 |
| `docs/video-editor/reigh-branch-census-and-convergence.md` | 1 |

Notes: the 71 app files are the complete list of name matches, grouped above; the grouping is exact (counts sum to 71). The `reigh-extension.json` count covers the manifest name only.

Runtime `banodoco_workspace_runtime.egg-info/` is tracked (5 files; not ignored). Remove it and ignore `*.egg-info/`.

## 3. Persisted names (App, non-test source)

### 1a. localStorage

| Key or prefix | Store | path:line | Value kind | Notes |
|---|---|---|---|---|
| `reigh.ext.<extensionId>.<settingKey>` | localStorage | `src/sdk/extensionSettingsService.ts:62` (prefix builder `getSettingsPrefix`); write `:671`; remove `:682`, `:446`; read `:519`; enumerate `:552-557`, `:620-625`, `:696-697` | user setting | Extension id (for example `com.reigh.*`) is embedded in the key. This is the "com.reigh ids as keys" case. Doc comment at `:166` also names the prefix. |
| `reigh.ext-state.{userId}.{timelineId}` | localStorage | `src/tools/video-editor/runtime/browserLocalPersistenceStore.ts:53` (builder); doc `:9`, `:50` | state cache | Mirror of the snapshot: meta, packs, enablement, overrides, settings, events, lock. The IndexedDB proposal DB is listed in 1b. |
| `reigh.ext.state.enablement` | provider store (localStorage when the localStorage-backed `ProviderBackedStore` is used) | `src/tools/video-editor/runtime/extensionStateRepositoryProvider.ts:58` | user setting | Constants at `:58-63`: `enablement`, `overrides`, `settings`, `events`, `lock`, `packs`. Access via `store.get/set` at `:232-247`. Doc comment `:10-15`. The file does not pin which backend production uses. The localStorage implementation is at `:94-103`. |
| `reigh.ext.state.overrides` | same as above | `extensionStateRepositoryProvider.ts:59` | state (dev override) | |
| `reigh.ext.state.settings` | same as above | `extensionStateRepositoryProvider.ts:60` | user setting | |
| `reigh.ext.state.events` | same as above | `extensionStateRepositoryProvider.ts:61` | state (append-only lifecycle log) | Doc says `append-only` at `:13`. |
| `reigh.ext.state.lock` | same as above | `extensionStateRepositoryProvider.ts:62` | state (lock) | |
| `reigh.ext.state.packs` | same as above | `extensionStateRepositoryProvider.ts:63` | state (pack records) | |
| `reigh.dev-extensions.disabled` | localStorage | `src/tools/video-editor/dev/devExtensionEnablement.ts:32` (get `:70`, set `:168`) | user setting (dev) | JSON array of disabled extension ids. Doc at `:24`. |
| `reigh-hidden-shots:{projectId}` | localStorage | `src/tools/travel-between-images/hooks/useHiddenShots.ts:3` (prefix); get `:21`; remove `:63`; set `:67` | user setting | Array of hidden shot ids. |
| `reigh:videoEditor:pendingAdds` | localStorage | `src/domains/media-lightbox/hooks/addToVideoEditorConstants.ts:6` (get `:10`, remove `:22`, set `:24`) | draft | Pending generation ids queued for the video editor. |
| `reigh:skipDeleteGenerationConfirm` | localStorage | `src/shared/components/dialogs/DeleteGenerationConfirmDialog.tsx:15` (get `:19`, set `:28`, remove `:30`) | user setting | |
| `reigh-skip-delete-shot-confirmation` | localStorage | `src/tools/travel-between-images/components/VideoGallery/VideoShotDisplay.tsx:45` (get `:199`, set `:211`) | user setting | |
| `reigh:perf-diagnostics` | localStorage | `src/tools/video-editor/lib/perf-diagnostics.ts:5` (get `:19`) | user setting (dev) | |
| `reigh:video-editor:sequence-creator` | localStorage (zustand `persist`) | `src/tools/video-editor/state/sequenceCreatorStore.ts:123`; storage `:149` | draft | Doc at `:7`. |
| `reigh:transcript-draft:{projectId}:{id}` (and suffix `:import`) | localStorage | `src/tools/video-editor/lib/project-transcript-host.ts:224` (key builder); set/remove `:227-228`; get `:235`; `:import` at `:737`, `:762` | draft | Transcript drafts and an import cache, keyed per project. |
| `reigh.runtime.media-import.v1:{projectId}:{importOperationId}` | localStorage | `src/integrations/runtime/dataProvider.ts:1268` (prefix); set `:1277-1278` | id / operation record | Stores a media-import operation for lost-ack recovery. Runtime is the stated durable authority (`:1288`). |
| `reigh.transcript` (inside the `showDataLanesByKind` preference) | preference payload; backend not confirmed in this file | `src/tools/video-editor/hooks/useEditorPreferences.ts:64`, `:141` | user setting | Data-lane kind id. Starts with `reigh`. Backend is not visible here, so it is not confirmed as localStorage. |

Keys that are not reigh-prefixed and carry old names: none found in localStorage. The `com.reigh.*` ids appear only inside `reigh.ext.<id>.` keys (row 1). Other non-storage old-name strings are listed in 1d.

### 1b. IndexedDB (database names)

| Database (object store) | Store | path:line | Value kind | Notes |
|---|---|---|---|---|
| `reigh.local-media` (store `reigh.local-media-handles`) | IndexedDB | `src/shared/lib/media/localHandleStore.ts:1` (DB), `:3` (store), open `:26` | id / handle | Stores local file handles. |
| `reigh.sync-ledger` (stores `sync-bookmarks`, `sync-keep-both-artifacts`) | IndexedDB | `src/tools/video-editor/data/syncLedgerIndexedDb.ts:1` (DB), `:3-4` (stores), open `:53`, delete `:85` | cache / ledger | Sync bookmarks and keep-both artifacts. |
| `reigh.timeline-drafts` | IndexedDB | `src/tools/video-editor/data/timelineDraftIndexedDb.ts:13`; open `:104` | draft | |
| `reigh.extension-state` | IndexedDB | `src/tools/video-editor/runtime/extensionStateRepositoryIndexedDB.ts:26`; open `:62`, delete `:86` | state | Full-snapshot store for extension state. |
| `reigh.ext-proposals` | IndexedDB | `src/tools/video-editor/runtime/browserLocalPersistenceStore.ts:60`; open `:118`, delete `:143` | state (proposals) | Legacy proposal-per-record store, still read for migration. |

### 1c. Cache Storage, cookies, sessionStorage

| Name | Store | path:line | Value kind | Notes |
|---|---|---|---|---|
| `reigh-v3` (Cache Storage) | Cache Storage | `public/sw.js:1` (`CACHE_NAME`); used at `:14`, `:63`, `:74`, `:90` (delete other names on activate). `index.html:141` deletes `reigh-v3` on load. | cache | |
| `reigh_paired` (cookie) | cookie | none in non-test app source | n/a | Only hit: `src/integrations/paired/pairedRoutePolicy.test.ts:324` (`cookie: 'reigh_paired=1'`). No `document.cookie` or cookie setter in `src/` or `public/`. |
| `x-reigh` (header) | HTTP header | none in app, runtime or astrid | n/a | |
| sessionStorage with `reigh` prefix | sessionStorage | none | n/a | Session keys seen are not reigh-named: `tasks-pane-project-scope` (`src/features/tasks/components/TasksPane/hooks/useTasksPaneViewState.ts:4`), `astrid-public-editor-reload-pending` (`src/pages/Home/PublicAstridShell.tsx:35`). |

Not persisted (for completeness) and Runtime/Astrid data:

- `reigh:shot-pack:v1:` (`src/tools/video-editor/lib/shot-group-pack-commands.ts:257`). This is a command key built by `packKey` and used at `:277` and `:290`. It is not written to storage in that file.
- `reigh.task.cancel-*` idempotency key (`src/integrations/astrid/taskRoutes.ts:189`). It is sent to the Runtime, which stores idempotency keys (see section 2).
- `reigh-timeline:` (`src/integrations/runtime/createDefaultTimeline.ts:81`), `reigh-first-timeline-` (`:114`), `reigh-timeline-create-` (`src/tools/video-editor/pages/VideoEditorPage.tsx:1222`), `reigh.shot-popup.` (`src/tools/travel-between-images/hooks/shotTimelineDataProvider.ts:486`). All are idempotency keys.
- `reigh.cas` content key (`src/shared/lib/taskCreation/createTask.ts:178`). Not checked for storage.
- `reigh-browser` client name (`src/integrations/runtime/client.ts:349`). See section 2.
- `reigh-app:video-editor` owner string (`src/tools/video-editor/runtime/families/processAdapter.ts:42`). Not checked for storage.
- `reigh-acp` project slug (`src/tools/video-editor/hooks/useAgentSession.ts:274`; `src/tools/video-editor/components/AstridAcpSessionControls.tsx:42`).
- `__reigh_generation__:`, `__reigh_shot__:`, and related drag prefixes (`src/shared/lib/dnd/dragDrop.ts:15-19`, `:315`). Drag payloads only.
- `reigh:timeline-center-clip`, `reigh:source-navigate-to-timeline`, and similar DOM event names. Not storage.
- `reigh.internal.extension-render-surface` (`src/sdk/internalExtensionRenderSurface.ts:28`) and `Symbol('reigh.extensionContext.dispose')` (`src/sdk/context.ts:362`). Not storage.
- `reigh.adaptTranscript` (`src/tools/video-editor/data/typed/adaptTranscript.ts:8`) and `reigh.projectTranscript` adapter ids. These appear in provenance inside timeline items, which are persisted in the Runtime (see section 2).

Secret-looking values: no reigh-named key holds an API key or secret. The only secret-bearing key read in this scope is the Supabase session key `sb-${projectRef}-auth-token` (`src/integrations/supabase/support/dev/autoLogin.ts:13`; also `src/integrations/supabase/functions/invokeSupabaseEdgeFunction.ts:14`). It is not reigh-named.

### 3b. Runtime and Astrid data

### 3c. Fingerprint `reigh-fnv1a64-v1:` (app)

- Only definition: `src/sdk/video/timeline/sourceMap.ts:214-223`, function `computeHostFingerprint`. No other `reigh-fnv1a64` hits in app, runtime or astrid.
- Steps:
  - `:215` `const canonical = JSON.stringify(canonicalizeForFingerprint(value, new Set()));`
  - `:216-221` FNV-1a 64-bit over `new TextEncoder().encode(canonical)`, with offset basis `0xcbf29ce484222325n`, prime `0x100000001b3n`, mask `0xffffffffffffffffn`.
  - `:223` `return \`reigh-fnv1a64-v1:${hash.toString(16).padStart(16, '0')}\`;`
- **The prefix is not part of the hashed input.** It is prepended after hashing, at `:223`. The hash covers only the canonical JSON bytes.
- The prefix is part of the stored value. Callers store the full string in provenance (`sourceFingerprint` / `outputFingerprint`, `sourceMap.ts:273`, `:276`). Equality checks at `:315-316` compare full strings, so the prefix matters for stored equality. Changing it would break existing comparisons even though the hash is unchanged.
- Two callers strip the prefix with `.split(':')[1]`, so they use only the hex part: `src/tools/video-editor/data/typed/reconcileTranscriptReferences.ts:59` and `src/tools/video-editor/dev/transcript-lane/extension.ts:96`.
- A different hash, `fnv1a64Hex` at `src/tools/video-editor/data/typed/timelineBundle.ts:206`, has no prefix. It is not the same algorithm as the fingerprint.

### 3d. Old-name data in runtime and astrid

| Old name | Where | path:line | Stored in events/ledger or current state? |
|---|---|---|---|
| `com.reigh.astrid.liveScene` (clipType) | runtime validation `runtime/runtime_protocol/store.py:1559`; `runtime/runtime_protocol/visual_seam.py:23` | timeline config JSON | Current state. Timeline config is in `project_documents.content_json` (`canonical_schema.py:125`) and `internal_timeline_revisions.payload_json` (`:322`), which are both versioned or content-addressed rows. Events do not carry it directly. |
| `com.reigh.astrid.liveScene` (clipType) | astrid code: `astrid/packs/rendering/editor/live-scenes/identity.ts:3`; `astrid/packs/rendering/ui/live-scenes/identity.ts:3`; `astrid/packs/rendering/rendering/renderers/threejs/run.py:94`; `astrid/packs/rendering/rendering/renderers/threejs/renderer.yaml:9`; `astrid/packs/rendering/actions/render/managed_timeline.py:310`; `remotion/src/ThreeTimelineComposition.tsx:435`, `:441` | code | Not data. Reads timeline JSON. |
| `com.reigh.astrid.liveScene` (data) | `schemas/change-contract/fixtures/valid/patch.live-scene-import.json:26` | fixture | Fixture of a patch payload. |
| `com.reigh.astrid.live-scenes` (extension id) | `astrid/schemas/change-contract/patch.schema.json:17` (description); `schemas/change-contract/fixtures/valid/patch.live-scene-edit.json:5`; `fixtures/valid/patch.live-scene-import.json:5`; `fixtures/invalid/patch.no-scope.json:5`; `fixtures/invalid/patch.duplicate-cut-ids.json:5`; `fixtures/invalid/patch.unknown-op-family.json:5`; `astrid/packs/rendering/editor/live-scenes/identity.ts:2` | `source` field of patch payloads | Patch payloads (change-contract). Whether they land in an event or current state depends on the admitted path. |
| `com.reigh.transcript-lane` | `astrid/packs/rendering/ui/transcript/extension.tsx:13` | code | Code only. |
| `com.reigh.scene-phase-markers` | `runtime/tests/test_schema_contract.py:149` (tests) | test fixture | Test only. |
| `reigh.transcript`, `reigh.transcript_segment/v1` | `astrid/packs/rendering/ui/transcript/extension.tsx:17-18` | code | Code only. |
| `reigh.live-scene-request/v1` | `astrid/packs/rendering/docs/references/live-scenes-authoring.md:26`, `:36` | docs example | Docs only. |
| `reigh.element-build/v1` | `astrid/core/element/project_packs.py:152`, `:377` | code check of a build receipt | Read from stored receipt; the receipt is not created here. |
| `reigh.local-worker-control/v1` | `runtime/runtime_protocol/local_worker_composition.py:43` (definition); used as wire `version` at `:215` (compared), `:284`, `:551`, `:588`, `:641`, `:809` (sent). `:593`, `:845`, `:875` import the relay's version. | local control socket frames | **Not stored.** It is sent and compared over a control channel. Not in store.py or any table. |
| `reigh-browser` (actor / client name) | app `src/integrations/runtime/client.ts:349` (passed to `handshake`) | app code | Runtime `handshake` takes `authenticated_actor` from the request body (`runtime/runtime_protocol/service.py:780`). It is not hard-coded. Stored actor ids appear in `project_selections.actor_id` (`canonical_schema.py:240-243`). Not found as a literal in runtime or astrid. |
| `reigh-product-connector` | none in app, runtime or astrid (including `.md`) | n/a | No hits. |
| `reigh-app:local-editor` (actor id) | `astrid/tests/fixtures/timeline_visualize/desert_slice/assembly.jsonl`: 145 event lines (first at line 11). Also `astrid/.oracle/prior-runs/timeline-vlm-dirty-20260814/worktree-modified.patch:1461` (patch, not data). | event-log fixture | **Event ledger.** Each line has `event_id`, `expected_version`, `hash`, and `actor`. This is the append-only events shape. |
| `reigh-app:video-editor` (owner string) | app `processAdapter.ts:42` | app code | Not in runtime or astrid. |

### 3e. Events / ledgers vs current state (runtime)

Schema: `runtime/runtime_protocol/canonical_schema.py`.

- Append-only, hash-chained ledgers:
  - `events` (`:54`). Inserts at `runtime/runtime_protocol/store.py:2957`, with `previous_hash` and `event_hash`. Scoped per run.
  - `timeline_events` (`:276`). Inserts at `store.py:2979`, with `previous_hash` and `event_hash`. Scoped per timeline.
  - `internal_timeline_revisions` (`:322`). Content-addressed by `content_digest`. Rows are not updated in place.
  - `timeline_revisions` (`:153`), with a `UNIQUE(timeline_id, version)` constraint.
- Current-state tables, updated in place with version counters:
  - `tasks`, `runs`, `attempts`, `reservations`, `execution_bindings`, `timelines` (`:112`), `timeline_shot_state` (`:159`), `timeline_reference_state` (`:163`), `project_documents` (`:125`, with `content_json` and `version`), `user_preferences` (`:131`), `project_selections` (`:240`), `project_shots`, `shot_items`, `media_references`, `reference_links`, and others.
- Idempotency: `command_idempotency` (`:194`) keeps `idempotency_key`, `request_hash`, and `result_json`. The code prunes it on a replay window (`store.py:58-92`), so it is neither append-only nor permanent.
- Rebuild: I found no function that rebuilds current-state tables from `events` or `timeline_events`. The only reads of those tables are in `store.py:2722`, `:2918-2919`, `:2949`, `:5357`, `:5786`, and `:5795-5797`. The `:5786` and `:5795-5797` reads appear to be part of an integrity check, not a projector. The current-state rows are therefore not derivable by replay in the code I read. Treat this as a finding to confirm, not a settled fact.

---


## 4. Environment variables

86 distinct names (58 `REIGH_`, 1 `VITE_REIGH_`, 21 `BANODOCO_`, 6 `ASTRID_BANODOCO_`). Ignore the bare stems `BANODOCO_RUNTIME_` and `REIGH_`.

| name | repos | lines | first path:line |
|---|---|---|---|
| `ASTRID_BANODOCO_AGENT_EXECUTORS` | astrid | 6 | `astrid:.env.example:20` |
| `ASTRID_BANODOCO_CATALOG_URL` | astrid | 11 | `astrid:.env.example:21` |
| `ASTRID_BANODOCO_DEFAULT_EXECUTORS` | astrid | 1 | `astrid:astrid/core/execution/executor/banodoco_catalog.py:40` |
| `ASTRID_BANODOCO_EXECUTOR_CACHE` | astrid | 1 | `astrid:astrid/core/execution/executor/banodoco_catalog.py:36` |
| `ASTRID_BANODOCO_MANDATORY_EXECUTORS` | astrid | 1 | `astrid:astrid/core/execution/executor/banodoco_catalog.py:41` |
| `ASTRID_BANODOCO_REFRESH` | astrid | 1 | `astrid:astrid/core/execution/executor/banodoco_catalog.py:45` |
| `BANODOCO_ASTRID_SOURCE_CHECKOUT` | astrid | 1 | `astrid:tests/stage1/test_cold_launch_autobootstrap.py:491` |
| `BANODOCO_LOCAL_DATA_ROOT` | app,astrid,runtime | 97 | `astrid:astrid/core/dev/journey.py:599` |
| `BANODOCO_LOCAL_ENABLE_REAL_REBOOT` | runtime | 2 | `runtime:banodoco_local/cli.py:417` |
| `BANODOCO_LOCAL_HOME` | astrid,runtime | 8 | `astrid:tests/helpers/h3_cpu_runtime.py:381` |
| `BANODOCO_LOCAL_LAUNCHER` | astrid | 22 | `astrid:astrid/sdk/autobootstrap.py:127` |
| `BANODOCO_LOCAL_OWNER_TOKEN` | astrid,runtime | 2 | `astrid:tests/stage1/test_final_cold_launch_matrix_luna.py:1377` |
| `BANODOCO_LOCAL_RUNTIME_CHECKOUT` | astrid | 1 | `astrid:tests/stage1/test_cold_launch_autobootstrap.py:490` |
| `BANODOCO_LOCAL_SOURCE_MANIFEST` | astrid,runtime | 33 | `astrid:astrid/core/dev/journey.py:617` |
| `BANODOCO_RELEASE_RECEIPT_ROOT` | astrid,runtime | 2 | `astrid:astrid/core/release_identity.py:36` |
| `BANODOCO_RUNTIME_` | astrid | 1 | `astrid:docs/guides/build-your-first-agentic-ux.md:199` |
| `BANODOCO_RUNTIME_ADMISSION_TIMEOUT_SECONDS` | astrid,runtime | 5 | `astrid:astrid/sdk/autobootstrap.py:143` |
| `BANODOCO_RUNTIME_CHECKOUT` | astrid | 22 | `astrid:.github/workflows/ci.yml:51` |
| `BANODOCO_RUNTIME_CREDENTIAL` | astrid | 53 | `astrid:astrid/core/execution/generic_host.py:7378` |
| `BANODOCO_RUNTIME_DISCOVERY` | astrid | 1 | `astrid:tests/timeline/test_timeline_kernel_kinds.py:403` |
| `BANODOCO_RUNTIME_ENDPOINT` | astrid | 39 | `astrid:astrid/core/execution/generic_host.py:7377` |
| `BANODOCO_RUNTIME_FREE_DISK_FLOOR_BYTES` | runtime | 3 | `runtime:runtime_protocol/store.py:3430` |
| `BANODOCO_RUNTIME_OWNER_TOKEN` | astrid,runtime | 5 | `astrid:tests/stage1/test_final_cold_launch_matrix_luna.py:1377` |
| `BANODOCO_RUNTIME_ROOT` | runtime | 5 | `runtime:runtime_protocol/cli.py:45` |
| `BANODOCO_RUNTIME_SPEC_RETENTION_DAYS` | runtime | 4 | `runtime:runtime_protocol/store.py:49` |
| `BANODOCO_TIMELINE_SCHEMA_ROOT` | runtime | 1 | `runtime:generators/materialize_timeline_schema.py:29` |
| `BANODOCO_URL` | app | 2 | `app:src/pages/Home/components/hero/HeroSection.tsx:15` |
| `REIGH_` | astrid | 1 | `astrid:scripts/reshape/installed_artifact.py:257` |
| `REIGH_ACCEPTANCE_EVIDENCE` | app | 2 | `app:docs/acceptance/local-project-gallery.md:34` |
| `REIGH_ACCEPTANCE_MATRIX` | app | 2 | `app:package.json:129` |
| `REIGH_AGENT_CONTEXT_SCHEMA` | app | 3 | `app:src/tools/video-editor/runtime/reighAgentContext.ts:6` |
| `REIGH_APPEND_SERVICE_INTERNAL_TOKEN` | app | 4 | `app:supabase/functions/_shared/reighAppendService.ts:8` |
| `REIGH_APPEND_SERVICE_URL` | app | 4 | `app:supabase/functions/_shared/reighAppendService.ts:7` |
| `REIGH_APP_CHECKOUT` | astrid | 1 | `astrid:tests/integrations/test_generation_runtime_composition.py:33` |
| `REIGH_BACKEND` | app | 5 | `app:docs/local-runtime/04-gpu-pack-execution-plan.md:202` |
| `REIGH_BOUNDED_BROKER_SESSION` | app | 5 | `app:scripts/release/bounded-command.mjs:28` |
| `REIGH_BOUNDED_PROCESS_SCOPE` | app | 1 | `app:scripts/release/bounded-command.mjs:27` |
| `REIGH_CANDIDATE` | app | 3 | `app:scripts/quality/check-extension-release-preflight.mjs:125` |
| `REIGH_CAS_ROOT` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:180` |
| `REIGH_CUSTOM_NODE_MANIFEST_PATH` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:178` |
| `REIGH_CUSTOM_NODE_ROOT` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:177` |
| `REIGH_ELEMENT_KINDS` | app | 3 | `app:src/tools/video-editor/runtime/element-contract.ts:4` |
| `REIGH_ELEMENT_OPERATIONS` | app | 2 | `app:src/tools/video-editor/runtime/element-contract.ts:111` |
| `REIGH_ELEMENT_OPERATION_NAMES` | app | 3 | `app:src/tools/video-editor/runtime/element-contract.ts:95` |
| `REIGH_ELEMENT_PLACEMENTS` | app | 3 | `app:src/tools/video-editor/runtime/element-contract.ts:7` |
| `REIGH_ELEMENT_PUBLICATION_STATES` | app | 2 | `app:src/tools/video-editor/runtime/element-contract.ts:10` |
| `REIGH_ENGINE_INTERPRETER` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:172` |
| `REIGH_ENGINE_LOCK_PATH` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:174` |
| `REIGH_EXPECTED_GENERATIONS` | app | 2 | `app:docs/acceptance/local-project-gallery.md:5` |
| `REIGH_EXPECTED_SHOTS` | app | 2 | `app:docs/acceptance/local-project-gallery.md:4` |
| `REIGH_EXTENSION_PORTS_ALLOCATED` | app | 6 | `app:playwright.extension-accessibility.config.ts:11` |
| `REIGH_GATE_PROFILE` | app | 17 | `app:scripts/release/verify-extension-ship.mjs:56` |
| `REIGH_HEAD` | app | 1 | `app:docs/phase-c/b8-real-bridge-acceptance-blocked.md:6` |
| `REIGH_INTERPRETER` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:171` |
| `REIGH_LIVE_ORIGIN` | app | 1 | `app:scripts/acceptance/verify-local-project-gallery.mjs:442` |
| `REIGH_LOCAL_DEV_COMMAND` | app | 4 | `app:src/shared/lib/localAstridRuntime.ts:5` |
| `REIGH_LOCAL_EDITOR_ACTOR` | app,astrid | 12 | `astrid:.oracle/findings/10-reigh-bridge-boundary.txt:5` |
| `REIGH_LOCAL_PROJECT` | app | 2 | `app:docs/acceptance/local-project-gallery.md:4` |
| `REIGH_LOCAL_PROJECT_MATRIX` | app | 6 | `app:docs/acceptance/local-project-gallery.md:11` |
| `REIGH_LOCAL_TIMELINE` | app | 2 | `app:docs/acceptance/local-project-gallery.md:4` |
| `REIGH_LOOPBACK_AUDIT_LOG` | app | 1 | `app:scripts/dev-local-workspace.mjs:377` |
| `REIGH_LOOPBACK_ORIGIN` | app | 2 | `app:scripts/dev-local-workspace.mjs:69` |
| `REIGH_LOOPBACK_PORT` | app | 1 | `app:scripts/dev-local-workspace.mjs:87` |
| `REIGH_MODEL_MANIFEST_PATH` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:176` |
| `REIGH_MODEL_ROOT` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:175` |
| `REIGH_OUTPUT_ROOT` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:181` |
| `REIGH_PAIRED_CONNECTOR_STATE` | app | 3 | `app:.env.example:52` |
| `REIGH_PAIRED_PRODUCT_ACTOR` | app | 6 | `app:.env.example:51` |
| `REIGH_PAIRED_REALM_ID` | app | 3 | `app:.env.example:50` |
| `REIGH_PAIRED_RELAY_ENABLED` | app | 3 | `app:.env.example:45` |
| `REIGH_PAIRED_RELAY_LOOPBACK_TEST` | app | 2 | `app:.env.example:46` |
| `REIGH_PAIRED_RELAY_ORIGIN` | app | 9 | `app:.env.example:44` |
| `REIGH_PERSONAL_ACCESS_TOKEN` | astrid | 1 | `astrid:.oracle/legacy-epic/batch-1-r2.md:12581` |
| `REIGH_PROCESS_FIXTURE_HEALTH_SEQUENCE` | app | 2 | `app:tests/fixtures/video-editor/process-fixture-descriptor.ts:57` |
| `REIGH_PROCESS_FIXTURE_OPERATION_IDS` | app | 2 | `app:tests/fixtures/video-editor/process-fixture-descriptor.ts:55` |
| `REIGH_PROCESS_FIXTURE_PROCESS_ID` | app | 2 | `app:tests/fixtures/video-editor/process-fixture-descriptor.ts:54` |
| `REIGH_PROCESS_FIXTURE_VERSION_SEMVER` | app | 2 | `app:tests/fixtures/video-editor/process-fixture-descriptor.ts:56` |
| `REIGH_REF` | app | 27 | `app:docs/extensions/creative-extension-lab-frictions.md:566` |
| `REIGH_ROOT` | astrid | 5 | `astrid:tests/integrations/test_generation_runtime_composition.py:32` |
| `REIGH_RUNTIME_LOCK_PATH` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:173` |
| `REIGH_SCRATCH_ROOT` | astrid | 1 | `astrid:tests/integrations/test_c12_cpu_runtime_harness.py:179` |
| `REIGH_TIMELINE_PORTS_ALLOCATED` | app | 2 | `app:playwright.config.ts:16` |
| `REIGH_VIBECOMFY_ATTENTION_PROFILE` | astrid | 4 | `astrid:astrid/packs/vibecomfy/production_engine.py:756` |
| `REIGH_WGP_HOME` | astrid | 1 | `astrid:.oracle/checkins/batch-B3-exec.md:16` |
| `REIGH_WORKER_CHECKOUT` | astrid | 9 | `astrid:.github/workflows/ci.yml:52` |
| `VITE_REIGH_APPEND_SERVICE_URL` | app | 6 | `app:docs/video-editor/extensions-debugging.md:196` |

### 1a. Same suffix in BANODOCO_LOCAL_* and BANODOCO_RUNTIME_*

| suffix | BANODOCO_LOCAL_ name | BANODOCO_RUNTIME_ name | target ASTRID_RUNTIME_<suffix> present? |
|---|---|---|---|
| `OWNER_TOKEN` (exact) | `BANODOCO_LOCAL_OWNER_TOKEN` (astrid, runtime; 2 lines) | `BANODOCO_RUNTIME_OWNER_TOKEN` (astrid; 5 lines) | No. `ASTRID_RUNTIME_OWNER_TOKEN` appears in no repo |
| `RUNTIME_CHECKOUT` vs `CHECKOUT` (LOCAL suffix equals RUNTIME_ + CHECKOUT) | `BANODOCO_LOCAL_RUNTIME_CHECKOUT` (astrid; 1 line) | `BANODOCO_RUNTIME_CHECKOUT` (astrid; 22 lines) | No. `ASTRID_RUNTIME_CHECKOUT` appears in no repo |
| `ROOT` (near match: LOCAL is `DATA_ROOT`, RUNTIME is `ROOT`) | `BANODOCO_LOCAL_DATA_ROOT` (app, astrid, runtime; 97 lines) | `BANODOCO_RUNTIME_ROOT` (runtime; 5 lines) | No. `ASTRID_RUNTIME_ROOT` appears in no repo |

Also relevant: `ASTRID_RUNTIME_ENDPOINT` already exists (astrid `astrid/packs/runpod/worker_process.py:1049`, runtime `runtime_protocol/remote_worker_deployment.py:873`), which is the same suffix as `BANODOCO_RUNTIME_ENDPOINT` (astrid `astrid/core/execution/generic_host.py:7377`, 39 lines). That is the one live collision with an existing ASTRID_RUNTIME_ name.

Other `ASTRID_RUNTIME_*` names already in use (for collision checks): `ASTRID_RUNTIME_CUSTODY_*` (custody_broker in astrid and runtime; env contract), `ASTRID_RUNTIME_INSTANCE_ID`, `ASTRID_RUNTIME_EPOCH`, `ASTRID_RUNTIME_SCHEMA_DIGEST` (runpod), `ASTRID_RUNTIME_CLI` (astrid `astrid/runtime_cli.py:24`), `ASTRID_RUNTIME_PROXY_PORT` (app `scripts/dev-local-workspace.mjs:356`).


## 5. CI jobs

Old-name tokens: `reigh`, `banodoco_workspace`, `banodoco_local`, `banodoco-local` (case-insensitive).

### astrid/.github/workflows/ci.yml (9 jobs)

| job | key line | range | old-name token lines |
|---|---|---|---|
| m7-admission | 15 | 15-44 | none |
| m7-release-candidate | 45 | 45-203 | **yes**: 52, 63, 64, 65, 110 |
| python | 204 | 204-322 | **yes**: 211, 222, 223, 224 |
| m4-gate | 323 | 323-376 | none |
| m4-editor-reporting | 377 | 377-407 | none |
| pack-recoverability-exhaustive | 408 | 408-440 | none |
| m8-artifact | 441 | 441-493 | none |
| m8-installed | 494 | 494-736 | none |
| m8-publication | 737 | 737-807 | none |

### app/.github/workflows/*.yml

| file | job | key line | old-name token lines |
|---|---|---|---|
| bridge-latency.yml (historical: deleted with the bridge-latency gate; nothing to rename) | bridge-latency | 28 | **yes**: 34, 35, 47, 53 |
| dockerfile-check.yml | dockerfile-check | 12 | none |
| slot-first-quality.yml | slot-first-audit | 19 | none |

Note: the `BANODOCO_RUNTIME_CHECKOUT` / `REIGH_WORKER_CHECKOUT` env lines in ci.yml (51-52) are not old-name tokens under the list above, so they are not flagged; they are counted in section 1.

### Generated, lock and census files
- Astrid: `config/astrid-capability-census.lock.json` (27 entries; generator `capability_ledger.py:636-665`); `banodoco_workspace_client/{generated,contract_metadata}.py` (vendored, identical to Runtime `packages/python/banodoco_workspace_client`).
- Runtime: `runtime_protocol/contract_metadata.py`, `packages/python/banodoco_workspace_client/`, `packages/typescript/src/{generated,contract-metadata}.ts`, `clients/typescript/{generated,contract-metadata}.ts` (all from `generators/generate.py`).
- App: `src/integrations/runtime/generated.ts`, `generated-contract-metadata.ts`; `config/governance/sdk-public-api-manifest.json`.

## 6. History folders and their live readers
### astrid

| folder | tracked files | old-name lines | live readers outside folder (code, tests, scripts, CI) |
|---|---|---|---|
| `.oracle` | 534 | 4097 | `scripts/reshape/check_repo_hygiene.py:39` (lists `.oracle` as an allowed top-level name, not a read). No verified live reader of files under `.oracle/`. |
| `.megaplan` | 27 | 52 | `.gitignore:102`, `.gitignore:111` (ignore rules, not readers); `scripts/reshape/check_repo_hygiene.py` treats `docs/megaplan/` only. No verified live reader. |
| `artifacts` | 20 | 36 | `.github/workflows/ci.yml:34,35,42,91,97` reads/uploads `artifacts/m7/` (live CI reader of `artifacts/m7/`). |
| `receipts` | 8 | 18 | None found. Matches were `astrid/core/receipts/` Python package paths (different folder), so no verified live reader of the root `receipts/` folder. |
| `findings` | 5 | 7 | None found (matches were prose or `findings` identifiers, not the folder). |
| `docs/projects` | 58 | 93 | `guides/runpod-lifecycle.md:256` and `guides/spinning-up-and-executing-tasks-on-runpod.md:841,848` (markdown links, docs-only). No code reader found. |
| `docs/plans` | 26 | 25 | `docs/projects/runpod-managed-generation-20260921/control-provenance.json:2082,2088` and `SHA256SUMS:138` (provenance records, not readers). No live reader found. |
| `docs/slot-first-audits` | 0 | 0 | Not tracked in astrid. |
| `docs/local-runtime/reviews` | 0 | 0 | Not tracked in astrid. |
| `docs/extensions/evidence` | 0 | 0 | Not tracked in astrid. |
| `docs/megaplan` | 3 | 1 | `scripts/reshape/check_repo_hygiene.py:200` (comment, says it is a source-of-truth dir); `tests/reshape/test_repo_hygiene.py:139,150` (test fixture path). |

### app

| folder | tracked files | old-name lines | live readers outside folder (code, tests, scripts, CI) |
|---|---|---|---|
| `docs/slot-first-audits` | 8 | 256 | `.github/workflows/slot-first-quality.yml:11` (CI path filter `docs/slot-first-audits/**`). |
| `docs/local-runtime/reviews` | 2 | 34 | None found. |
| `docs/extensions/evidence` | 137 | 206 | `scripts/quality/capture-extension-external-evidence.mjs:287` (builds a path under `docs/extensions/evidence/releases/`; runtime read/write behaviour unverified); `scripts/quality/check-extension-release-preflight.mjs:161` (command string in a message); `.gitattributes:6`, `.gitignore:69,70` (attributes and ignore rules). |
| `.oracle`, `.megaplan`, `artifacts`, `receipts`, `findings`, `docs/projects`, `docs/megaplan` | 0 each | 0 each | Not tracked in app. Path strings found elsewhere (e.g. `.gitignore`, `.dockerignore`, `.cursorrules`, `docs/cutover-inventory.md`) are ignore rules or docs, not live readers. |
| `docs/plans` | 0 | 0 | Not tracked in app. `config/contracts/astrid-plan-a-c1.json:1222` references `docs/plans/astrid-default-packs-and-skills/PLAN.md`; whether that path resolves is unverified. |


## 7. Collision table

Blanket `reigh` to `astrid` gives a name that already exists. N-MAP resolves each using rule 2 (App becomes `app` / `App` / `ASTRID_APP_` / `astrid-app`).

```tsv
old	blanket_new	repos_where_new_already_exists
--reigh-commit	--astrid-commit	app
...REIGH_GATE_PROFILE.map	...ASTRID_GATE_PROFILE.map	app
.reigh	.astrid	app,astrid,runtime
.reighCommit	.astridCommit	app
/reigh	/astrid	app
/tmp/reigh	/tmp/astrid	app,runtime
CLI/Reigh	CLI/Astrid	astrid
REIGH	ASTRID	app,astrid,runtime
REIGH_	ASTRID_	astrid,runtime
REIGH_GATE_PROFILE	ASTRID_GATE_PROFILE	app
REIGH_GATE_PROFILE.length	ASTRID_GATE_PROFILE.length	app
REIGH_OUTPUT_ROOT	ASTRID_OUTPUT_ROOT	astrid,runtime
REIGH_REF	ASTRID_REF	app
REIGH_REF:	ASTRID_REF:	app
REIGH_ROOT	ASTRID_ROOT	astrid,runtime
Reigh	Astrid	app,astrid,runtime
Reigh-	Astrid-	app
Reigh-compatible	Astrid-compatible	astrid
Reigh-first	Astrid-first	app,astrid
Reigh-host	Astrid-host	app
Reigh-local	Astrid-local	astrid
Reigh-native	Astrid-native	astrid
Reigh-only	Astrid-only	app,astrid
Reigh-owned	Astrid-owned	app,astrid
Reigh-side	Astrid-side	app,astrid
Reigh-specific	Astrid-specific	astrid
Reigh.	Astrid.	app,astrid,runtime
Reigh:	Astrid:	app,astrid,runtime
ReighElementKind	AstridElementKind	app
candidate.reighCommit	candidate.astridCommit	app
checkReigh	checkAstrid	app
checkedInLedger.candidate.reighCommit	checkedInLedger.candidate.astridCommit	app
codex/reigh-main-extension-merge	codex/astrid-main-extension-merge	astrid
context.reighSnapshot	context.astridSnapshot	app
document.candidate.reighCommit	document.candidate.astridCommit	app
env.REIGH_REF	env.ASTRID_REF	app
expected.reighCommit	expected.astridCommit	app
finalReigh	finalAstrid	app
https://github.com/banodoco/Reigh	https://github.com/banodoco/Astrid	astrid
integrations/reigh	integrations/astrid	astrid
local:reigh	local:astrid	app
manifest.reigh	manifest.astrid	app
manifest.reigh.branch	manifest.astrid.branch	app
non-Reigh	non-Astrid	app,runtime
non-reigh	non-astrid	astrid
pins.reighCommit	pins.astridCommit	app
reigh	astrid	app,astrid,runtime
reigh-	astrid-	app,astrid,runtime
reigh-beta	astrid-beta	app
reigh-branch-commit	astrid-branch-commit	app
reigh-browser	astrid-browser	app,astrid
reigh-browser-first.log	astrid-browser-first.log	app
reigh-commit	astrid-commit	app
reigh-on-call	astrid-on-call	app
reigh-preview.log	astrid-preview.log	app
reigh-v3	astrid-v3	app
reigh-workspace	astrid-workspace	astrid
reigh-worktree-clean	astrid-worktree-clean	app
reigh.	astrid.	astrid,runtime
reigh.boot_manifest	astrid.boot_manifest	astrid
reigh.branch	astrid.branch	app
reigh.branch.trim	astrid.branch.trim	app
reigh.tar	astrid.tar	app
reigh/	astrid/	app,astrid
reigh:	astrid:	app,astrid
reighCommit	astridCommit	app
reighCommit:	astridCommit:	app
reighHandle	astridHandle	app
reighHead	astridHead	app
reighRef	astridRef	app
reighRef:	astridRef:	app
reighSnapshot	astridSnapshot	app
reighSnapshot:	astridSnapshot:	app
reighTreeBytes	astridTreeBytes	app
reighTreeBytes:	astridTreeBytes:	app
reigh_	astrid_	astrid
reigh_root	astrid_root	astrid,runtime
runtime/Reigh	runtime/Astrid	app
startReigh	startAstrid	app
```

## 8. Satellites
Per-repo table (7 satellites):

| Repo | What | Last commit | reigh hits | bw-runtime / banodoco_workspace | `banodoco-workspace` | BANODOCO_ | Recommendation |
|---|---|---|---|---|---|---|---|
| reigh-worker | GPU worker (Wan2GP), pyproject `name = "reigh-worker"` | 2026-09-30 | 1093 (uv.lock 366, scripts/live_test + canary reports, history) | 0 | 0 | 0 | **rename** to `astrid-worker` |
| reigh-worker-orchestrator | "Headless WGP Orchestrator": RunPod GPU spawn + API tasks; Railway deploy | 2026-08-30 | 703 | 0 | 1 | 28 | **rename** to `astrid-worker-orchestrator` |
| reigh-workspace | Reigh dev workspace, docs/structure | 2026-09-22 | 540 | 2 (plan doc) | 2 | 0 | **archive** (superseded) |
| banodoco-workspace | Banodoco community workspace + Supabase `bndc` | 2026-09-29 | 108 | 0 | 15 | 31 | **keep name**; scrub Reigh docs; decide on `banodoco-worker/` |
| timeline-schema | `@banodoco/timeline-schema` | 2026-05-02 | 10 (tests/schemas comments, `provider:"reigh"`) | 0 | 0 | 0 | keep; scrub comments |
| timeline-composition | `@banodoco/timeline-composition` | 2026-05-07 | 9 (gen-registry comments) | 0 | 4 (comments) | 0 | keep; scrub comments |
| timeline-theme-2rp | `@banodoco/timeline-theme-2rp` | 2026-05-02 | 0 | 0 | 1 (README) | 0 | keep |

### Summary of verdicts
| Repo | Verdict |
|---|---|
| reigh-worker | live: Astrid CI C12 harness imports `source.runtime`; rename, do not archive |
| reigh-worker-orchestrator | deprecated for Astrid; archive after removing App README:31 and banodoco-worker README refs, unless banodoco-worker is deployed |
| reigh-workspace | deprecated: archive after repointing 2 planning scripts |
| banodoco-workspace | live: `banodoco_social` import + App `@workspace-*` aliases; keep name |

## 9. R4 planning docs with old names (about 90 lines)
reviews/astra-high-security.md 36, astra-high-runtime.md 11, astra-xhigh-app-deep-dive.md 10, astra-high-union-delta.md 8, astra-high-agent-ux.md 7, astra-high-parity.md 5, astra-high-migration.md 4, astra-high-contract.md 4, astra-high-timing.md 2, astra-high-extensions.md 2, DECISIONS-deep-dives-1.md 1; cards.md 6 (lines 145, 299, 480, 1065, 1078, 1091), spec-rendering.md 4, briefs/WS-SAVE.md 3, agent_goal.md 3, status.md 1, open-questions.md 1, northstar.md 1, manager-brief.md 1. Under `~/Developer/astrid-video/.otto/runs/timeline-platform-r4/`.
