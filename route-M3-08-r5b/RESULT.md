# M3-08-r5b RESULT (Elrond) — self-contained

Verdict: NOT ACCEPTED. Acceptance requires the 12 changed test modules to pass on head; 14 of 21 head-failing nodes are inside them.

## Interpreter and environment
- Python 3.13.14 (M-GPU amendment 23:15Z; route named 3.13.16, not available on Elrond), same path on base and head.
- Venv built from base checkout requirements/build.lock and proof.lock (--require-hashes); runpod-lifecycle 0.3.1.dev0 installed --no-deps; Agent and Runtime metadata ABSENT.
- Pins: wheel 0.46.3, build 1.4.0, pytest 9.0.2, httpx 0.28.1.
- Preflight (canonical pinned-env): base PASS 20 / FAIL 0; head PASS 20 / FAIL 0.

## Sources
- Agent candidate 395a09f538e8cf5e58c1bd5c06e4945af4f5239f (tree a2d1e70701d4); Agent base f431a8a2e97fbcb0770f6db6da64971fb03445b4 (tree a5ef624dbdab).
- Runtime 1e6d899efa77a2d6ec247c38250f9748330bb587; VibeComfy 2967033970cbb5884651c9565265778482d3b44e; runpod-lifecycle 96360a27a53c4608f780f7b370f747a2aa3c7ca8.
- Overlay: 12 changed test modules copied byte-for-byte from the candidate onto the base checkout.

## Census (real registry, pack/executor/orchestrator/element)
- Head: 30 / 107 / 1 / 44 (expected 30/107/1/44: MET).
- Base: 30 / 108 / 0 / 44 (the known old-base defect, observed).

## Base / head comparison
| Run | passed | failed | errors |
|---|---:|---:|---:|
| base | 235 | 71 | 12 |
| head | 297 | 9 | 12 |

- Base-passing nodes that fail on head (regressions): 0.
- Node sets identical on base and head (comparator identical_nonzero: true).

## Head-failing nodes (21): node id — status — first error line (base status shown)
- [DEFERRED] `tests/core/generation/test_entries.py::test_import_list_and_generate_entry` — head failed; base failed — AssertionError: {
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_base_missing_and_ambiguous_models_fail_closed[unets0-vaes0-missing0]` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_base_missing_and_ambiguous_models_fail_closed[unets1-vaes1-missing1]` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_base_missing_and_ambiguous_models_fail_closed[unets2-vaes2-missing2]` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_base_missing_and_ambiguous_models_fail_closed[unets3-vaes3-missing3]` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_discovered_windows_target_resolves_real_turbo_roles[i2i]` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_discovered_windows_target_resolves_real_turbo_roles[t2i]` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_missing_vae_loader_and_none_object_info_fail_closed_with_fix` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_no_target_and_malformed_probe_preserve_unready_rows` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_probe_failure_keeps_target_without_echoing_endpoint_error` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_stale_direct_image_aliases_are_not_represented_as_resolved_turbo` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_target_probe_is_single_snapshot_and_never_starts_or_submits` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_listing.py::test_unknown_model_roles_fail_closed_without_claiming_readiness` — head error; base error — failed on setup with "ModuleNotFoundError: No module named 'aiohttp'"
- [IN-CHANGED-MODULE] `tests/core/generation/test_moved_data_composition.py::test_default_parent_inventory_skips_legacy_folder_orchestrator_policy` — head failed; base failed — AssertionError: assert {'video_editing.hype'} >= {'inventory_f...xture.parent'}
- [IN-CHANGED-MODULE] `tests/core/generation/test_moved_data_composition.py::test_explicit_renderer_inventory_never_reads_ambient_or_unselected_bytes[True]` — head failed; base failed — AssertionError: PosixPath('/home/user/astrid/route-M3-08-r5b/run/scratch/head/tmp/pytest/test_explicit_renderer_invento0/selected/render_fixture/unselected.py')
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_approve_regenerates_lock_and_keeps_quarantined_approvals` — head failed; base failed — AssertionError: assert ['badpack.snap'] == ['badpack.sna...oodpack.echo']
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_doctor_section_lists_quarantine_with_error_and_fix` — head failed; base failed — AssertionError: assert 'keywords.1' in 'Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.'
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_invalid_keyword_quarantines_only_that_pack` — head failed; base failed — AssertionError: assert [] == ['goodpack']
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_invoking_a_quarantined_capability_is_typed_unavailable` — head failed; base failed — AssertionError: assert 'keywords.1' in 'Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.'
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_quarantine_owner_matches_declared_bare_labels` — head failed; base failed — AssertionError: assert QuarantinedPack(pack_id='goodpack', pack_dir=PosixPath('/home/user/astrid/route-M3-08-r5b/run/scratch/head/tmp/pytest/..._0/packs/goodpack/pack.yaml'), error='Pack manifest needs an explicit schema
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_unapproved_capability_is_unavailable_without_stopping_host` — head failed; base failed — astrid.core.pack._common.PackValidationError: manifest not found: /home/user/astrid/route-M3-08-r5b/run/scratch/head/tmp/pytest/test_unapproved_capability_is_0/astrid/core/model_catalog/models.yaml

Labels: BLOCKING = inside the 12 changed modules (acceptance requires them to pass). DEFERRED = outside them.
