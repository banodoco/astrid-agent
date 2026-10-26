# Shot-local generation timelines and media roles

Status: proposed implementation plan, not an implemented contract. 15 September 2026.

## Outcome

A shot owns a bounded local timeline of generation intent, untimed image/video references, versioned prompts, and selected playback takes. The parent timeline owns editorial placement. Agents can inspect, validate, generate, review, select, and resume without inventing roles from filenames or metadata conventions.

Reuse the existing child timeline rather than creating a second timeline store. Timed generation intent must be distinguishable from renderable picture clips: an anchor should not accidentally appear as an overlay in the final video.

## Existing foundations and documentation

| Area | Existing support / evidence | Missing piece |
| --- | --- | --- |
| Shot composition | Registered shots, ordered media items, child timeline linkage; `astrid/sdk/shot_grouping.py`, `astrid/packs/shots/cli.py`, rendering skill | Explicit bounded generation-intent layer and its invariants |
| Prompts | Runtime-owned immutable text bindings with named prompt slots and optimistic head checks; [SDK reference](../reference/sdk.md#shot-text-bindings) | General-versus-segment prompt semantics and frozen compilation into a generation request |
| References | Reusable subjects and canonical/used-as-input/depicts/inspired-by associations; [reference guide](../../astrid/packs/media/docs/references.md) | Shot input bindings with explicit generation roles; association alone does not wire model inputs |
| Video generation | `image_ref` and `image_end_ref`; [video contract](../generation/31-video-contract.md) | Multiple timed anchors/reference inputs, video spans, and capability-specific lowering |
| Execution | Runtime tasks/runs, managed media, VibeComfy custom graphs | Durable segment dependencies, approval gates, provenance-complete resume and selection |
| Playback | Versioned child/parent timeline saves and managed renders; [rendering skill](../../astrid/packs/rendering/skill/SKILL.md) | Explicit, reversible selection of a generated take versus a reference still |

The existing video contract documents `video_ref` conceptually but marks generic `v2v`/`video-edit` as not wired. Do not infer backend support from a feature name. Audit executable capability declarations during implementation and correct stale status tables with tests. H3's custom path remains VibeComfy.

## Proposed model

### Reuse existing image-generation recipes and promotion

Important audit correction: this is not greenfield role/selection functionality. `astrid/packs/generation/executors/generate_image/task_adapter.py` already validates `astrid.shot-generation-recipe/v1`, including a pinned prompt binding and ordered reference inputs carrying `role`, `reference_id`, `media_id`, and content hash. The target is currently constrained to `primary_visual` and the adapter is image-generation-specific.

The neutral runtime also supports primary-visual candidate/promotion semantics (`candidate`, `primary`, `superseded`) with atomic provenance validation and invalidation, covered by `tests/stage1/test_runtime_client_cutover.py::test_promote_candidate_receipt_replay_and_foreign_provenance_rejection`. Extend this existing recipe/promotion design for segment-scoped video takes; do not introduce another independent canonical switch. The gap is typed temporal roles/spans and video generation integration, not the complete absence of role-bearing inputs or promotion.

Existing child expansion in `astrid/core/timeline/expand_shots.py` already offsets child clips into the parent window and clamps/drops content at its boundaries. New anchor validation must account for that behavior explicitly rather than claiming bounds are entirely new.

### 1. Local timing and bounds

- Define one authoritative shot-local duration and rational frame rate; parent placements may reuse that shot without changing its intent.
- Represent frame positions exactly. Define clip intervals as half-open and the final image anchor as the last valid frame, not the exclusive end boundary.
- Distinguish source-media time, shot-local time, parent placement, and model-native time. Store explicit mappings for trim, conform, or speed changes.
- Validate every timed anchor/span against bounds. A parent duration override must produce an explicit conform decision or validation failure, not silently retime anchors.
- No arbitrary recursive shot nesting in this feature. Preserve the current nesting constraint.

### 2. Timed anchors versus untimed references

Timed anchors express a desired composition at a local frame. Start/end roles are derived relative to a generation segment; a shared anchor may end one segment and start another. Intermediate anchors use the same representation.

Untimed references express appearance, identity, setting, style, or motion guidance without asserting visibility at a particular moment. Keep reusable subject identity separate from the exact media revision chosen for one run.

A binding should identify exact managed media, owning shot, intended role, optional segment scope, crop/transform, and provenance. The same media may have separate timed and untimed bindings. Ordered item position is not a substitute for role or timestamp.

Use a constrained vocabulary and reject incompatible combinations. Keep provider-specific knobs in a validated adapter payload, not arbitrary metadata interpreted differently by each agent. Do not overload existing lineage roles such as `canonical` to mean `end_frame`.

### 3. Video references are first-class

Support video media as references, with explicit purpose:

- **Motion/performance guide:** source camera or subject movement; not necessarily copied pixels.
- **Continuation context:** a chosen preceding interval used to continue motion.
- **Preserved interval:** footage intended to remain within a generated result.
- **Appearance reference:** visual identity/style evidence, subject to backend support.

Each video binding needs exact source identity, source in/out bounds, frame-rate/timebase and VFR information, video/audio stream selection, crop/geometry, and explicit audio policy. A timed preserved interval additionally needs destination placement and fit policy; an untimed motion reference does not.

Distinguish one extracted frame from a video span. Record frame extraction identity/time, rather than silently treating a video reference as its thumbnail. Reject missing streams, invalid spans, and unsupported temporal roles before GPU admission. A silent video must have an explicit ignore/silence policy where an AV workflow requires audio.

Do not make native latents generic video references. If latent checkpoint reuse is supported, give it a separate typed checkpoint contract tied to model, VAE, dimensions, time grid, workflow/runtime versions, and predecessor run.

### 4. Prompt ownership

Reuse shot text bindings. Provide a general shot-direction prompt and stable segment-specific prompt slots. Define deterministic composition/override rules: global style + shot direction + segment action, with explicit exclusions and no silent duplication.

The saved final prompt must be inspectable. Each request pins all contributing binding heads and the exact compiled text/digest. A later edit creates a new revision and invalidates dependent request identity; it does not rewrite a prior take's history. Keep voiceover text separate.

### 5. Segments, attempts, and canonical playback

A generation segment references its local bounds, anchors/references, prompt revisions, workflow adapter, and optional predecessor. References and anchors are intent; generated takes are results.

Keep attempts immutable. Selection points to a specific accepted take and its conform recipe, or explicitly to the original still. Save selection through runtime compare-and-swap, preserving source anchors and overlays. Avoid a second independent selection flag that can disagree with actual timeline playback.

Candidate previews must not promote themselves. Support both per-segment human approval and explicitly authorized automatic progression. Editing upstream context marks descendants stale; resuming verifies immutable identities, not filenames or cache presence. Rejected takes never become implicit context.

## Generation adapter contract

Compile provider-independent shot intent into a frozen, validated request. Advertise support for multiple images, timed anchors, video roles, audio policies, duration/frame grids, and endpoint fidelity. Reject unsupported essential intent; never quietly drop an end image or preserve constraint.

For the initial H3 adapter, derive from the corrected Seitanism AV Extension bundle in the [general anchor-to-anchor guide](../../guides/anchor-to-anchor-video-generation.md), with the [Astrid Intro worked example](../../guides/astrid-intro-anchor-video-generation.md) as a project-specific reference. Wire endpoint controls into every applicable extension, retain native 24-fps context, validate H3's frame grids, and record overlap removal separately from the target timeline duration. Soft keyframes are guidance, not exact-pixel guarantees.

Input materialization must use the runtime's authorized managed-media boundary. Resolve media to attempt-local files there; do not make local source paths the durable identity or assume a public export API exists.

Finishing/upscaling is a derived take with provenance. It must not silently replace the native continuation source. Render overlays/text at final resolution after background finishing.

## Delivery sequence

1. **Contract audit and schema proposal.** Trace neutral runtime shot/media/text/timeline APIs, document current bounds and reuse behavior, decide owning entities and migration rules. Review examples before adding fields.
2. **Runtime primitives.** Add typed role/timing bindings and validation, CAS updates, archive/recovery semantics, request snapshots, and client API schemas. Runtime remains the sole authority; no pack-local database.
3. **SDK and CLI.** Add inspectable anchor/reference/segment operations using existing families. Show local times, media roles, prompt heads, chosen takes, and unsupported inputs together. Derive concrete command names from the approved API rather than treating examples in this plan as existing commands.
4. **Preview/editor support.** Visualize anchors separately from playback, show video source spans and reference thumbnails, provide reversible take selection, and expose stale dependencies. Preserve Remotion/audio composition.
5. **H3 pilot.** Compile two connected segments with a shared anchor, previous-video context, a distinct appearance reference, and endpoint control. Generate only with user-approved runtime/budget. Review both individual outputs and the join.
6. **Durable queue and finishing.** Implement runtime-owned dependency/approval transitions, retries and restart recovery. Add derived upscaling takes, then assemble the complete intro while skipping its Remotion-only interval.
7. **Migration and documentation.** Existing shots render unchanged. Do not auto-classify every image as an anchor or infer timing from ordered media items. Offer a reviewed conversion of actual image-backed picture intervals into intent bindings.

## Verification / acceptance

- Multiple images with different roles, one media object in two roles, and several anchors inside one shot.
- Start/end/off-by-one and rational-rate tests; rejects out-of-range/conflicting anchors and ambiguous duration overrides.
- Multiple video references, trim bounds, VFR normalization, missing audio, incompatible geometry, and preserved-region overlap tests.
- Prompt inheritance/head pinning and deterministic request digests; concurrency conflicts do not lose edits.
- Unsupported features fail preflight before cost; masked/soft endpoint fidelity is reported honestly.
- Candidate does not affect canonical playback; selection and rollback retain overlays, audio, timing, and original anchors.
- Retry/resume after process restart, approval blocking, upstream revision invalidation, and no accidental duplicate admissions.
- Fake/no-GPU end-to-end suite first; real two-segment H3 pilot proves motion/endpoint behavior separately.
- Whole-intro review retains the 38.280–64.592 s Remotion-only section (reinspect current timing), final duration, and soundtrack.

## Documentation ownership

Do not leave the final behavior documented only in this plan or the reusable guide; keep project-specific timing evidence in a worked-example page when useful.

1. **Neutral runtime repository:** authoritative schema/API contract for shot bindings, time bounds, selection transactions, generation snapshots, approval/dependency lifecycle, and migrations. Locate and extend its existing owner documents before creating a parallel specification.
2. **`docs/contracts/shot-generation.md` (proposed):** Astrid-facing normative mapping and invariants, linked to the runtime contract. Explicitly distinguish intent, references, generated takes, and playback.
3. **`docs/reference/sdk.md` (existing):** typed usage and canonical shot prompt APIs; extend with anchors/video spans/selection once implemented.
4. **`docs/generation/31-video-contract.md` and `00-features.md` (existing):** supported generation roles and backend capability matrix, with honest unavailable states.
5. **Rendering and references pack skills (existing):** agent entry points and division of responsibilities. Link the shared contract; avoid copying competing schemas into skills.
6. **`guides/shot-generation.md` (proposed):** reusable human workflow: attach inputs → author intent/prompts → generate → approve → select → render.
7. **`guides/anchor-to-anchor-video-generation.md` (existing):** reusable Seitanism recipe, interval/gap timing, approval, provenance, and assembly procedure for any timeline. **`guides/astrid-intro-anchor-video-generation.md`** is the worked example with exact anchors/timings and overlay exceptions.

Decisions to settle in phase 1: whether shot duration is already independently authoritative; exact permitted role vocabulary; shared-shot placement overrides; initial human-review UI; and backend-specific preservation guarantees. These must be explicit contract choices, not inferred metadata conventions.
