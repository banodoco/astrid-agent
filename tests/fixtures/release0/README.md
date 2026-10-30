# Frozen Release-0 fixture set

These are compatibility inputs, not templates.  The three `served/` files are
the exact public responses captured from `https://astrid.haus` at
`2026-10-30T02:05:12Z` (HTTP 200); their bytes have not been parsed,
pretty-printed, or reserialized.  The captured manifest names Agent
`3204bb00135bec8ee2d2d3fb21eed5abfceca649` and Runtime
`6d53a19070faf63fc15ae1f81417722edc2c8a5f`.

`services/` was rendered by the genuine Agent `3204bb00135bec8ee2d2d3fb21eed5abfceca649`,
using `astrid.installer.service.render` from a detached isolated checkout.
The external driver is `work/coord/M4-2/generate_release0_service_fixtures.py`.
It uses the normalized, non-personal values recorded in `provenance.json`, and
pins the Task Scheduler clock to `2000-01-01T00:00:00+00:00`.  The generated
bytes match that commit's reviewed `tests/fixtures/installer/` snapshots.

The launcher provenance is recorded even though this fixture set does not copy
launcher code: main is `bbba620e3d89eeff8021417f365e6ea584203105`; the full
object for the older stop-first `c3b4cd5` is
`c3b4cd5d32d8bd37e76cec292d81f18320eda9c2`.

`release2/stand-in.json` is an intentionally visible, task-authored stand-in,
not a published release.  Its marker value is `M4-2 RELEASE 2 STAND-IN —
UPDATE LANDED`.  It is frozen along with this set: change it only by creating a
new fixture set and checksums.

## Verification and regeneration

Run this from this directory to verify every byte-bearing fixture input:

```sh
shasum -a 256 -c SHA256SUMS
```

Do not download `served/` again: it is a historical public-response capture.
To reproduce the service artifacts only, from the Agent repository root make a
detached checkout of the recorded Agent commit, set `M42_OLD_CHECKOUT` to it,
and run:

```sh
PYTHONPATH="$M42_OLD_CHECKOUT" python3 work/coord/M4-2/generate_release0_service_fixtures.py "$M42_OLD_CHECKOUT" tests/fixtures/release0/services
```

Then compare the regenerated output to `SHA256SUMS`.  `provenance.json` records
the URLs, source refs, exact renderer, normalized inputs, and stand-in source.
