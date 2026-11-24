# transcript-refs envelope fix (release/15/transcript-refs-envelope)

## Diff summary
- astrid/sdk/timeline_transcript_refresh.py: `resolve_asset_id` (registry key whose media_id/content_sha256 == media; parent first, then shots), `with_reference(..., asset_id=None)` writes {assetId, documentId, mediaId, sourceId}, `backfill_asset_ids` (older rows lacking assetId get it when resolvable), `complete_envelope` (setdefault schema_version=1, itemsBySchemaRef={}).
- astrid/packs/timeline/cli_working.py `_cmd_transcripts_refresh`: resolves all assetIds first (unresolvable -> exit 2 with clear message, working copy never created/saved), completes the envelope, calls `validate_timeline_bundle` on `parent.config.bundle` before `save` (non-conforming -> refused, nothing saved).
- astrid/core/timeline/bundle.py: `validate_timeline_bundle` now checks `transcriptReferences` if present (schema literal `media.transcript.references/v1`; rows require non-empty assetId + documentId; optional mediaId/sourceId; any other key rejected). Nothing loosened; previously it did not look at transcriptReferences at all.
- Tests: new tests/sdk/test_timeline_transcript_refresh_envelope.py (4 tests); one existing assertion changed (see risks).
- Matches App origin/release/15/main timelineBundle.ts: envelope = parent.config.bundle with `schema_version`, `itemsBySchemaRef`, `transcriptReferences{schema, associations[{assetId, documentId}]}`. The App branch release/15/transcript-refs-schema was not fetchable (not on origin), so the optional mediaId/sourceId shape is taken from the brief.

## Release 14: YES
origin/release/14/main = c0ad6b54aadd21630d98bb5039613e37efe92a8f. Its cli_working.py:595-597 has the identical writer (`transcriptReferences` setdefault + `with_reference(...)`), and its with_reference ends `kept.append({"documentId", "mediaId", "sourceId"})`. Introduced by 19db88c2f (2026-11-17, "Add transcripts refresh: status, missing runs, recorded choices") per `git log -S'with_reference(working.bundle'`; R14 head is a descendant-ancestor of R15 (R14 head is in R15 history).
Legacy shape written by R14 under `parent.config.bundle`:
`transcriptReferences: {"schema": "media.transcript.references/v1", "associations": [{"documentId": "...", "mediaId": "sha256:...", "sourceId": "..."}]}` - no `assetId` in any row; and when the project had no prior bundle, the `config.bundle` object was just `{transcriptReferences: {...}}` with no `schema_version` and no `itemsBySchemaRef`. (App-side repair: mediaId -> registry asset key, add header.)

## Tests (agent-pytest.sh -q)
Selected files: tests/sdk/test_media_transcript.py, test_timeline_transcript_refresh.py, test_timeline_words_projection.py, tests/stage1/test_legacy_authority_absence.py, tests/test_generation_dry_run_preflight.py (grep of timeline_transcript_refresh / _cmd_transcripts / validate_timeline_bundle / core.timeline.bundle).
- Base (origin/release/15/main): 81 passed.
- Branch: 85 passed (81 + 4 new in test_timeline_transcript_refresh_envelope.py).

## Risks
- Existing assertion changed (unavoidable): test_explicit_choice_is_recorded_and_saved compared associations to an exact row without assetId; it now includes `"assetId": "a"`. No other assertion changed.
- The Agent validator now rejects `transcriptReferences` rows with unknown keys; the App parallel change must accept mediaId/sourceId or published projects will still fail to open.
- Validation covers the whole `config.bundle`: a pre-existing malformed `itemsBySchemaRef` item will now block this verb until fixed.
- assetId = registry key; if the App means a different id for registry-less/selection-based projects (film fixture), resolution fails closed (refusal, nothing saved).
