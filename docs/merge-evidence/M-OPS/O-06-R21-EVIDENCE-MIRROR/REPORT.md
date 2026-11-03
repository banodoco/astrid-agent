# O-06-R21 evidence mirror

This folder mirrors the accepted R-24 owner-discovery evidence packet for M-RT review.

## Source material

- Accepted packet: `queue/done/O-06-R20/SR-PROJ.md`, copied as `accepted-SR-PROJ.md`.
- Canonical R-18 report and input/output hash inventories: copied with their O-06-R18 prefixes.
- Raw Actions evidence: all regular files from `/home/user/repo/docs/merge-plan/evidence/actions/41560168557/`, copied under `raw-actions-41560168557/`.
- The branch is based on `int/r1` at `ddd5d278796641901bd11508863a8ea21e6c9697`.

## Copy verification

`COPY-HASHES.txt` records SHA-256 and byte counts for every copied file. The four packet/report/inventory files were byte-compared with their source files; the ten raw evidence files were byte-compared and checked against the O-06-R18 input inventory. All 14 comparisons passed, including a check that the staged Git blobs preserve those exact bytes. The raw evidence contains 52,246 bytes.

## Limitations

This is a diagnostic evidence mirror. It preserves supplied packet and raw artifacts for review and makes no new claim about workflow behavior, owner identity, or runtime outcome. No evidence was edited, inferred, or supplemented. Git whitespace checking reports existing trailing whitespace in copied source text/log files; those bytes were preserved to maintain identity.
