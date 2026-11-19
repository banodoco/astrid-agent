# LS-12.1 deferred verification evidence

This archive preserves the verification optimization and its five topic-only
requirement probes under Lead decisions
[7010819708](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7010819708)
and
[7012365894](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7012365894).
The latter assigns these probes to deferred LS-12.1 while requiring independent
correctness coverage in both landing test files. The Lead owns the task row.

## Exact sources

- Original implementation and hosted proof:
  [b2a993f72eafb936ba9e3db77e43ad49da82293c](https://github.com/banodoco/astrid-agent/commit/b2a993f72eafb936ba9e3db77e43ad49da82293c).
- Immutable intermediate split:
  [b369213a1b517a23a86d1127e8a21b02ea2beaac](https://github.com/banodoco/astrid-agent/commit/b369213a1b517a23a86d1127e8a21b02ea2beaac),
  tree `c7d075c3d6cc548ae1b7ee5d88a4c5ee4b95ab09`.
- Destination/reference: `e379565641414bc183cd02edebab1cbf77617931`.

`host_admission.b2.py.txt` and `snapshot_admission.b2.py.txt` are complete,
byte-for-byte copies of the two original b2 test files. Their exact bodies,
decorators, assertions and provenance remain available for LS-12.1.
`provenance.json` records their Git blobs, SHA256 digests and the five test IDs.

`verification-optimization.patch` is the exact product-only diff from the
intermediate split back to b2 in three files, using zero context so the
archive has no whitespace-only context lines. Its replay check uses
`git apply --check --unidiff-zero`. It preserves:

1. The full-content discovery walk using `scandir`.
2. Host admission's reuse of selected-root digests for source-identity projection.
3. Reuse of the private nested workflow snapshot after its held-byte check.

The patch contains the deferred implementation, not a qualified improvement.
The correctness subset retains exact admitted Python bytes, bounded
environment/namespace admission, fresh public validation and later installs,
the changed-digest fresh-worker rule, nested edited-before-use protection and
single emission.

## Scoped test disposition

The entire landing test files continue to run with the original pytest
commands, without skip, xfail, selector or threshold changes. The five
optimization-specific cases above move with their implementation to LS-12.1.
Independent landing cases preserve the following correctness coverage:

| Deferred requirement | Independent landing coverage |
| --- | --- |
| Two host once-read parameters | `test_host_admission_binds_all_roots_and_rechecks_later_edits[False/True]`; fresh selected and additional root identity, full later validation, same-size restored-time edit refusal |
| One nested load/publication pair | `test_nested_preparation_preserves_admitted_pair_contents_and_cleanup`; exact Python/companion/source content, private paths, later fresh scope, cleanup |
| Same nested JSON snapshot identity | `test_nested_json_snapshots_preserve_contents_and_expire_with_scope`; nested bytes, cleanup and refusal of expired paths |
| Same snapshot under a temporary-directory alias | `test_snapshot_paths_are_normalized_and_keep_nested_contents`; canonical outer/inner paths, exact bytes and cleanup |

The existing wrong-root refusal and all four edited-first-use member cases are
unchanged. The intermediate split's two independent host Python/resource edit
probes also remain unchanged. Original import-admission, named-entry, source
identity, canonical-pair emission and attempt-custody probes stay in their
original files.

## Retained proof and limits

- [D1/counts44515956267](https://github.com/banodoco/astrid-agent/actions/runs/44515956267)
  and [owned phases44515956210](https://github.com/banodoco/astrid-agent/actions/runs/44515956210)
  record the b2 observation only.
- [Terminal evidence receipt](https://github.com/banodoco/astrid-agent/pull/361#issuecomment-7012338264)
  preserves all 12 D1 and six phase archives, failures, raw exits and source
  identities. Linux first-job timing and Windows phase collection are
  NO-RESULT. Pack verification regressed on Linux and macOS.
- The absolute D1 target remains **NOT MET**. Its median and slowest thresholds
  are unchanged, as are the provisional 15% same-OS tolerance, owned-phase
  criterion, dependency guards and LS-2.1 controls.
- Reinstating any deferred optimization requires the approved proof for that
  task. The b2 results and archived probes are not passing landing evidence.

No destination merge, task-board change or second implementation packet is
part of this archive.
