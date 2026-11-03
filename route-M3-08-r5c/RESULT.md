# M3-08-r5c RESULT (Elrond) — self-contained

Verdict: NOT ACCEPTED. Three of four BLOCKING acceptance items are met. The fourth is not: one node in a changed module still fails on head.

## Interpreter and environment
- Python 3.13.14 (same path both sides). Qualifier venv built with pip seeded, from the base checkout requirements/build.lock and proof.lock (--require-hashes); runpod-lifecycle 0.3.1.dev0 installed --no-deps; Agent and Runtime metadata absent.
- Pins: wheel 0.46.3, build 1.4.0, pytest 9.0.2, httpx 0.28.1, aiohttp 3.13.5 (environment repair exit 0).
- Preflight (canonical pinned-env): base PASS 21 / FAIL 0; head PASS 21 / FAIL 0.

## Sources
- Agent candidate 677089b882ccf789a8816482483a4685a686c9a0 (tree b3ca99926cdca323383ff62fe7a74dbe9f3ec2df). Agent base f431a8a2e97fbcb0770f6db6da64971fb03445b4 (tree a5ef624dbdab42046d8875e0b1bf3d6d3277e7af).
- Runtime 1e6d899efa77a2d6ec247c38250f9748330bb587; VibeComfy 2967033970cbb5884651c9565265778482d3b44e; runpod-lifecycle 96360a27a53c4608f780f7b370f747a2aa3c7ca8.
- Overlay: 12 changed test modules copied byte-for-byte from the candidate onto the base checkout.

## Census (real registry, pack/executor/orchestrator/element)
- Head: 30 / 107 / 1 / 44 (expected 30/107/1/44: MET). Base: 30 / 108 / 0 / 44 (old-base defect, observed).

## Base / head comparison
| Run | passed | failed | errors |
|---|---:|---:|---:|
| base | 247 | 71 | 0 |
| head | 310 | 8 | 0 |

- Node sets identical on base and head: 318 nodes each. Base-passing nodes failing on head (regressions): 0.

## Acceptance (BLOCKING items)
- Identical nonzero preflight: MET.
- Head census 30/107/1/44: MET.
- Zero base-pass to head-fail: MET (0).
- Changed-test modules pass on head: NOT MET. 1 head failure inside the changed modules (below).

## Head-failing nodes (8): node id — head status — first error line — base status
- [DEFERRED] `tests/core/generation/test_entries.py::test_import_list_and_generate_entry` — head failed; base failed — AssertionError: {
- [IN-CHANGED-MODULE] `tests/core/generation/test_moved_data_composition.py::test_explicit_renderer_inventory_never_reads_ambient_or_unselected_bytes[True]` — head failed; base failed — AssertionError: PosixPath('/home/user/astrid/route-M3-08-r5c/run/scratch/head/tmp/pytest/test_explicit_renderer_invento0/selected/render_fixture/unselected.py')
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_approve_regenerates_lock_and_keeps_quarantined_approvals` — head failed; base failed — AssertionError: assert ['badpack.snap'] == ['badpack.sna...oodpack.echo']
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_doctor_section_lists_quarantine_with_error_and_fix` — head failed; base failed — AssertionError: assert 'keywords.1' in 'Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.'
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_invalid_keyword_quarantines_only_that_pack` — head failed; base failed — AssertionError: assert [] == ['goodpack']
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_invoking_a_quarantined_capability_is_typed_unavailable` — head failed; base failed — AssertionError: assert 'keywords.1' in 'Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.'
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_quarantine_owner_matches_declared_bare_labels` — head failed; base failed — AssertionError: assert QuarantinedPack(pack_id='goodpack', pack_dir=PosixPath('/home/user/astrid/route-M3-08-r5c/run/scratch/head/tmp/pytest/..._0/packs/goodpack/pack.yaml'), error='Pack manifest needs an explicit schema
- [DEFERRED] `tests/packs/test_pack_quarantine.py::test_unapproved_capability_is_unavailable_without_stopping_host` — head failed; base failed — astrid.core.pack._common.PackValidationError: manifest not found: /home/user/astrid/route-M3-08-r5c/run/scratch/head/tmp/pytest/test_unapproved_capability_is_0/astrid/core/model_catalog/models.yaml

Labels: BLOCKING = inside the 12 changed modules (acceptance requires them to pass). DEFERRED = outside them.
