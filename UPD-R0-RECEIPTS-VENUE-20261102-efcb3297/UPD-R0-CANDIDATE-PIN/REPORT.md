# UPD-R0-CANDIDATE-PIN — provisional R0 interruption Agent/Runtime tuple

## Result

Pushed banodoco/astrid-agent candidate:

lane/M-R1FIX/UPD-R0-CANDIDATE-PIN @ f4f4fc99b42bcf4421f2aa27ad07ddad85fc18f9

It is based directly on the retained recovery candidate
6614cbe44bc950e239b0c7d3bd86a74c47818696, preserves its installer code and
tests byte-for-byte, and pins the authorized provisional Runtime:

255ecda21ba904520eb867936ef173cc5f7d3ae4

The root and packaged runtime.lock.json copies both now have that exact
source_commit. This is the narrow exception ordered by M-INT at 20:20:59Z for
the proof candidate.

## Scope

The candidate commit changes exactly:

- runtime.lock.json
- astrid/resources/runtime.lock.json

Both are refresh output. The Runtime client artifacts remained current, so their
recorded hashes did not change. No installer, test, or other product file
changed. The prior recovery commits remain in the ancestry:

- 3936c759801 — retain exact R0 bootout identity
- 6614cbe44bc — keep second launchd admission static

## Verification

- Runtime refresh from the clean exact producer: passed.
- Runtime generator and artifact-manifest freshness: passed.
- Producer-backed vendor lock check: passed.
- Source commit, producer head, root/package lock parity, and all 16 vendored
  artifact bytes/hashes: passed.
- Focused packaged-resource parity: 3 passed.
- Focused vendored-client lock freshness: 1 passed.
- git diff --check, base ancestry, scope audit, preserved-installer audit,
  clean worktrees, and remote-head verification: passed.

The final root/package lock SHA-256 is
480d503612cc1041b968951510523413a77d6d1504d44163f02f8c49b69f74a5.
The Runtime producer manifest SHA-256 is
561a17fedf1ab56c3cf892cf84f2df6313bb378ed3605f5757ab6fc86ed0fc5b.

## Carried proof and limits

The unchanged recovery proof is carried from
queue/pending/UPD-R0-RECOVERY-RETAINED/canonical-receipt.json; its focused
installer selections were intentionally not rerun. This candidate is not a
protected approval, published Runtime release, App proof manifest, integration
handoff, hosted/native journey, or full-suite acceptance.

The complete command outputs, inner exits, tuple provenance, per-artifact
hashes, scope audit, and carry-forward receipt hashes are in
canonical-receipt.json.

