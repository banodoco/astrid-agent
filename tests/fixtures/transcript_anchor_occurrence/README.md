# Transcript anchor source and occurrence fixtures

This synthetic corpus follows `media.transcript/v1` and the `clip.app.transcript.binding`
fields in the Astra101 producer contract. All fixture IDs are literal test data.
The three `*-source-*.json` files are the exact UTF-8 snapshot content bytes in
compact, key-sorted JSON form; `cases.json` records the corresponding immutable
snapshot object IDs and byte digests. Nothing resolves a mutable latest document
or external sidecar.

The cases cover:

- A media-seconds source with opaque word IDs and explicit segment membership.
- A same-lineage correction with changed segmentation and one removed word;
  retained words keep their original IDs. The removed word's existing anchor is
  kept as an `ORPHAN` with its last accepted position and length.
- Two clips selecting different words from one pinned snapshot. Their target
  occurrence IDs, trims, speeds, and placements are distinct and remain clip
  inputs.
- A new take with identical visible text and distinct source, lineage, revision,
  and word identities. Its explicit take correspondence is one-to-one and points
  at a fixture binding that carries the immutable source provenance.
- A duplicate-target correspondence negative and an ambiguous repeated-text
  case with no automatic map.

Run the fixture-local integrity selector with:

```sh
python3 -m pytest -q tests/sdk/test_timeline_transcript_anchor_fixtures.py
```

This validates the fixture data and documented relationships. It does not invoke
or prove the Astrid timeline compiler, publisher, persistence command, or runtime.
