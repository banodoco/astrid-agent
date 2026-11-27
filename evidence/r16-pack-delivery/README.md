# R16 installed default delivery evidence

## Independent HOLD and current correction evidence

The earlier READY at PR701 head `78480533d25dd8731a7b1f04c737719dea2fc880`
was withdrawn after independent review reproduced two P1 defects. The corrected
product, exact current source identities, complete new proof, retained failures,
and HOLD disposition are recorded in [the correction report](hold-fix/README.md).
The original records below are historical candidate evidence, not the corrected
current proof. PR701 remains draft under the explicit manager landing/READY HOLD.

This directory records actual candidate evidence, not a Release 16 source freeze,
served installer manifest, native R0/platform acceptance, or browser acceptance.
The owning change is `p93/PACK-DELIVERY`, Agent PR 701. PACK-B separately owns the
facade, independent release-source witness, readers, and generators in PR 700.

## Completed current implementation proof

The retrievable integration commit
`baf6b73f297865f681bfe07ca2035a53168e79d5` (tree
`f2749ccd7dcfcd8526e4d6bacfdc53d54b114999`) joins DELIVERY
`d91d49deed96d756224484ecae423ba2515d9048` and PACK-B
`4210932093c8c34838dbe3c7e11a14a3536c2411`. It is available on
`p93/PACK-DELIVERY-proof`. The actual source pair is the K23f78bf candidate
described below, matching PACK-B's committed independent witness. It is not the
later corrected PACK-A commit, an accepted final tuple, or a new release pin.

All **13 installed cases** in `native/` passed against that actual integration:

| Actual proof | Cases | Result |
| --- | ---: | --- |
| Public CLI clean install and quarantine/repair | 2 | Exit 0, actual Runtime readiness, selected default admitted, exact reader K/D match |
| Two real installed versions; public update SIGKILL and journal rollback | 2 | Real exit -9 after canonical publication/selection; normal CLI recovery restores the prior selection and pack identity |
| Selected authority, receipt, admission, and user-origin negatives | 7 | Refusal/quarantine with byte-preserved selection and collection |
| Unselected installed candidate: undeclared resource and real compiler failure | 2 | Actual child exit 1 for each intended refusal; old selected version and live collection unchanged |

The two-version test uses an explicitly empty-default negative manifest for the
second version, with the same real Agent/Runtime/tool inputs. This deliberately
exercises canonical removal and rollback. Both prefixes were produced by the
actual `Backend.stage`, with real noneditable packages. After SIGKILL at the
durable `switched` journal, the public CLI restores all 64 source hashes, valid
canonical admission, the exact old pointer, a user folder pack's four files, its
separate removal tombstone, and the protected user document. Runtime reaches
readiness again. This is a two-version R16 candidate fixture; it does not claim
the separate H1 release-15-to-16 App/editor acceptance journey.

The candidate refusal tests reuse the genuinely installed, unselected candidate
as a private negative-input venue after rollback. Each input is a real local Git
descendant of K23 with one explicit component mutation and an independently
computed tree/digest. The candidate's version-owned manifest selects that
negative input for `Backend.prepare_default_packs`; it is restored afterward.
The selected old manifest is never changed. These are rejected test inputs, not
proposed producer commits or substitutes for the repaired source.

Independent Git-object comparison matched **1,038 actual staged and installed
Python/witness files**, including all eight owned product files and all four
PACK-B package interfaces. The original tests/conftest/configuration/lock blobs
match untouched base44. The installer excludes `.git` from its staged source;
identity was checked against independent real Git blobs, not inferred from a
fabricated checkout marker. The full inventory is in the raw proof bundle;
`native/source-inventory-summary.json` binds its SHA256 and mandatory entries.

The original handoff file completed 79 passes and one CLI refusal-wording
failure. That exact node also fails on an independently set-up untouched44.
The five added stop/apply/select/recovery-order cases pass with the original R0
fixture, including stop refusal. The original transaction files collected and
executed exactly the same 203 unique node IDs, with zero skips: 202 passed and
one lacked its required `ASTRID_RUNTIME_ADMISSION_CHECKOUT` environment input.
That unchanged node passed with the actual pinned Runtime686ca695 checkout.
Passing cases were not rerun for metadata. `source-regressions.json` preserves
all counts, failed nodes, messages, and JUnit hashes; it does not label the
broader inherited-failure suites green.

The venue is Linux under observed seccomp, using the installer's existing
explicit `--sandbox` process-control mode. All four optional setup choices were
off. Real Runtime stop/start/readiness ran; no OS manager was simulated in this
public CLI proof. The task's Runtime was stopped through its existing fence
afterward. The original R0 source tests are explicitly manager simulations,
not native R0 qualification.

Actual Node 20.19.4, esbuild 0.27.7 API/native hashes, Bun 1.3.14, and the original
Remotion lock are recorded. The unchanged manifest specifies FFmpeg 7.0.2, but
the existing media-tool policy selected PATH FFmpeg/ffprobe 7.1.5-0+deb13u1 in
this venue. No FFmpeg 7.0.2 asset qualification is claimed. Browser acquisition
failed DNS; the public CLI correctly reported rendering/visualization
unavailable. No browser, App/editor, macOS/Windows, served-wrapper, or final H1
qualification follows from this delivery proof.

The first full CLI attempt was killed with exit 137 during OMP staging, before
selection or live collection creation. Its exact source/log receipt remains in
the raw bundle. A fresh physical disk-backed venue completed the retry. A
negative-proof harness initially inspected stdout while Backend correctly
logged separate stderr; its failed assertion is retained, and the successful
retry checks the exact new diagnostic log slice. No product checks were relaxed.

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
Runtime, client, VibeComfy, and lifecycle packages, plus real Node, esbuild, Bun,
and the existing media-tool selection. The manifest names FFmpeg 7.0.2; that
alone is not evidence that its archive was downloaded. It used the original
runtime and npm lockfiles. No `sys.prefix` assignment or product-module replacement was used.
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
not evidence of a regression. The completed physical-path and installed results
are recorded separately above with their actual source identity.
