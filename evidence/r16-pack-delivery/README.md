# R16 installed default delivery evidence

This directory records actual candidate evidence, not a Release 16 source freeze,
served installer manifest, native R0/platform acceptance, or browser acceptance.
The owning change is `p93/PACK-DELIVERY`, Agent PR 701. PACK-B separately owns the
facade, independent release-source witness, readers, and generators in PR 700.

## Shipping path and scope

At Agent `44d59774abb638c61ed0aae2b32653e62a5bcf66`, the installer stages the
version-owned manifest, interpreter, dependencies, tools, and profile. The
existing release-manifest installer already supplies the canonical group
transaction, but no caller connects it to installed default delivery. The
loader's release-pin snapshot is otherwise supplied by the development
environment. The initial report was an inference; the caller audit established
these missing connections before adding them.

The bridge uses `Backend._stage -> prepare_default_packs` for private preparation,
then the existing clean install, shared update, repair, and R0 handoff fences call
`apply_default_packs` before selection. Their existing recovery paths call
`rollback_default_packs` after the existing stop fence and before restoring the
old selection. All pack acquisition, validation, consent, definitions generation,
collection exchange, and rollback remain in `install_release_manifest`,
`install_group`, and `rollback_group`.

The installed loader derives authority from its real noneditable interpreter's
version directory and selected manifest. It checks default origin, source,
commit, digest, canonical collection membership, definitions, and group
admission. The installer adapter does not select versions or create lifecycle
stop authority. Source/legacy fallback remains separate. Explicit removals,
user-origin conflicts, consent checks, and canonical rollback recovery remain
enforced.

## First actual installed candidate

The historical installed run used the real local integration commit
`40c84fc7eccfb7f8c4c927a3899d0da3368961bb`, joining DELIVERY
`175dcfa84b02fed9621849dd69d6932f8859ba47` and PACK-B
`4210932093c8c34838dbe3c7e11a14a3536c2411`. That integration commit was local at
execution time. This is not evidence that later changes ran in that prefix.

The selected producer was POMS
`23f78bfc7e3cb26e320e8d3c6bd30a8b94cd92e1`, subtree
`9668568025190d0a8b5742cda0aec32ffaf9d5d0`, pack-tree-v1 digest
`be6abd1feb9759eca56364c6a2ccded470977aaf965a8e208cdd4f1bedcf77cf`.
The source contains 64 files and 20 rendering definitions. The canonical
installation also contains `PIN` and `.build/definitions.json`; these generated
metadata files are not source files.

`Backend.stage` actually installed Python 3.11.13 and the real noneditable Agent,
Runtime, client, VibeComfy, and lifecycle packages, plus the manifest's pinned
Node, esbuild, Bun, FFmpeg, and other tools. It used the original runtime and npm
lockfiles. No `sys.prefix` assignment or product-module replacement was used.
The standalone verification program runs with the installed interpreter:

```text
<version>/py/bin/python -I scripts/prove_installed_default_delivery.py \
  --version <version> --home <isolated-home> --output <receipt.json>
```

`installed-delivery.json` records 10 passed cases: process exit before publication;
process exit after publication; canonical collection admission and installed
loader/source-reader agreement; environment override refusal; interrupted
rollback and final-receipt I/O failure recovery; definitions/source/PIN quarantine
and canonical repair; missing-default repair with byte-preserved user state;
and explicit removal/tombstone preservation. `installed-preparation.json`
records all 20 real compiler outputs and successful Node syntax checks.

The first attempt's official HTTPS pack fetch failed in the canonical isolated
Git environment. The successful retry used an explicitly declared complete,
non-shallow local Git mirror at the same exact commit/tree. That source venue is
recorded in the manifest and PIN, not disguised as an HTTPS fetch. The manifest
is an explicit local proof input derived from the real App manifest, not a
published or frozen wrapper. Remotion browser acquisition separately failed DNS;
no browser PASS is claimed.

The proof harness first requested a nonexistent rollback observability phase,
then encountered a resume-local variable error. Both failures were retained.
The corrected harness resumed verified checkpoints rather than rerunning passed
cases for metadata. The installed prefix was removed only after all receipts,
source/dependency inventories, and actual results were captured.

## Receipts

* `staged-candidate-manifest.json`: actual explicit proof manifest and source/tool
  identities.
* `installed-source-inventory.json`: actual source and installed resource hashes,
  original test/conftest/lock identities, and real Node/esbuild binary hashes.
* `installed-dependencies.json`: actual installed distributions.
* `installed-preparation.json`: private canonical preparation and compiler output
  identities.
* `installed-transaction-receipt.json`: durable publication/rollback record.
* `installed-delivery.json`: exact installed assertions and per-case outcomes.

## Source regression boundaries

The unchanged original helper, tests, conftest, and real pinned dependencies were
retained. An initial original release/default/installer-source suite passed 123
cases. A broader original suite had 381 passes, 10 failures, and its one original
default-lane deselection. All 10 failures were independently reproduced on the
untouched base: two existing R0 guard fixtures and eight readiness error-message
expectations. A loader suite had 31 passes and five legacy-schema fixture
failures; the five also reproduced on the untouched base.

The additive adapter and real canonical group suite passed 24 cases with zero
skips/deselections. This includes negative receipt/manifest/source/admission
inputs, other defaults, user-origin conflicts, previous-version rollback,
initial-generation rollback, and real process interruption.

A later optional expanded original run hit shared workspace ENOSPC. Its retry
used a relocated development venv behind a symlink, which the original managed
path checks correctly refused. The all-marker request also reached browser
fixtures without their required explicit tool inputs and an unavailable local
R0 evidence fixture. Those expanded runs are unqualified, not product PASS and
not evidence of a regression. Physical-path reruns and further installed results
must be reported separately with their actual source identity.
