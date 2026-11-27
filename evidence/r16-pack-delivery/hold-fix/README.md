# R16 PACK-DELIVERY: correction of the independent PR701 HOLD

The two P1 counterexamples in [review 7227239177](https://github.com/banodoco/astrid-dev/issues/2032#issuecomment-7227239177)
are corrected and reproved. **PR701 remains draft under the manager's explicit
landing/READY HOLD.** This report does not clear that review, waive inherited
failures, qualify the producer suite, or freeze a release tuple. The original
READY at head `78480533d25dd8731a7b1f04c737719dea2fc880` is withdrawn.

## Exact source used

| Role | Actual identity |
| --- | --- |
| Refreshed release base | `44d59774abb638c61ed0aae2b32653e62a5bcf66` |
| Corrected DELIVERY product/tests/proof programs | `31fe4ea07dd44bb97c708a2f4f2d1a54a4362ecc` |
| DELIVERY tree | `2d99e2cfb324b888c8cf04007ae42ed3fdb02f34` |
| Actual PACK-B | `f02ceffa39d221756999cebd1a38b97f5d42519b` |
| Executed integration, retrievable on `p93/PACK-DELIVERY-hold-proof` | `b1a3a6a9e64917c2847ff91c293ff210c8aa10ad` |
| Executed integration tree | `2143d91da0cf80cdc9ff70d33e804c9908e972ea` |
| Actual producer K | `f40b1f44bbe513cb871387a07e14332ffbed8a53` |
| Actual `astrid_motion` subtree T | `9668568025190d0a8b5742cda0aec32ffaf9d5d0` |
| Actual pack-tree-v1 D | `be6abd1feb9759eca56364c6a2ccded470977aaf965a8e208cdd4f1bedcf77cf` |
| Runtime | `686ca695469bda48cd40a70824c4ccef2a137284` |
| VibeComfy | `55c3948212d23cf037fae8a4c6faad77d33e7279` |
| Lifecycle | `9f88ef7640bc664f697112d966ecefa123d44340` |

The final evidence commit changes only this evidence directory and its index.
The bundle includes the exact final-head comparison; passing product proof was
not rerun for that metadata. PACK-B's nine integrated files exactly equal f02.
Its facade, independent witness, readers, and generators remain PACK-B-owned.
No source freeze, published installer, merge, or native subagent was used.

## Corrections and owned functions

Only three product files change relative to held head 78480533.

* `loader._isolate_incompatible_packs` consults independent canonical admission
  before the mutable pack-local PIN can determine whether release authority
  applies. The real `scan_configured_packs` path is exercised by the regressions.
* `release_manifest.admitted_pack_pin` reads the full admitted PIN from the
  canonical collection record, checks admission identity and held artifact
  bytes, and verifies the actual PIN against that binding.
  `verify_release_default` requires this independent binding as well as the
  selected version-owned manifest, actual source digest, canonical membership,
  and exact definitions provenance.
* `transaction.install_group` atomically publishes and carries validated PIN
  bindings with existing admissions. `_state`, `read_group`, and
  `read_retired_group` accept legacy records without manufacturing provenance.
  `_verify_admitted_pins` guards retained, prepared, and restored generations.
  `rollback_group`, `_verify_absent_paths`, and
  `_publish_and_verify_external_roots` enforce genuine absence at the removed
  name before preparation, immediately before exchange, and in the exchanged
  old generation. A file, empty directory, unpinned public pack, or dangling
  symlink is present, regardless of whether inventory can parse it or find PIN.

The independent binding is an additive `pins` map in the same atomic
`.group.json` publication, with each full PIN checked against its admission.
The exact definitions JSON and existing admission/browser metadata shapes
remain unchanged. This preserves the actual producer contract: the canonical
PIN source is `repository#pack_id`; definitions retain the bare repository URI.

On a late conflicting write, the existing exchange recovery restores the live
user generation. If the newly published generation was also written, its
bytes remain in a named `refused-<transaction>` generation. Tests execute the
real exchange primitive and verify both sets of user bytes. The original
transaction, consent, quarantine, explicit removals, tombstones, other defaults,
user/folder packs, prior selection, and rollback recovery remain authoritative.

Genuine independently admitted user/folder packs—including older unbound
admissions—retain their inventory semantics and intentional source edits.
An explicit canonical user replacement changes the binding. A same-ID freely
authored folder and a removal tombstone do not manufacture a default admission.

## Additional compatibility defect found and corrected

The first correction, integrated as `b59ce84f2c2b1ab1d3f8f215839c2b01405acb14`,
passed the new P1 regressions but wrote an empty additive `pins` field when
rolling back a legacy collection. A genuine earlier installed Agent at
`baf6b73f297865f681bfe07ca2035a53168e79d5` rejected that record with
`invalid_state: Group state is malformed`. All 66 pack files and protected user
bytes survived, but the old reader failed; that is retained as a counterexample.

Rollback now preserves the retired generation's legacy wire format when no
independent bindings remain. A partial rollback retains every untouched binding;
a modern prior generation keeps its binding format. Three additional regressions
cover these cases. The genuine old executable now accepts the restored group,
strictly verifies the old release default, and discovers it through the actual
loader. Every original pack byte, including PIN/definitions, and the protected
user bytes match their pre-update hashes.

## Executed source boundaries

| Exact b1a3a6a9 source run | Passed | Failed | Skipped | Scope |
| --- | ---: | ---: | ---: | --- |
| Owned loader/rollback/adapter/order plus original definitions | 63 | 0 | 0 | Includes all 28 new regressions and all 29 earlier owned cases |
| Original transaction/default/release files | 311 | 0 | 0 | Entire seven-file boundary, real pinned Node/esbuild and Runtime reader supplied |
| PACK-B readers/projections/revisions | 83 | 0 | 0 | All four complete files at actual Bf02 |
| Original loader/quarantine files | 31 | 5 | 0 | Same five failure identities as retained independently executed untouched44 |
| Original installer/source/update/lifecycle/handoff files | 441 | 11 | 0 | Same eleven failure identities as retained independently executed untouched44 |
| Actual Kf40 producer originals/additions against verified staged b1 source | 34 | 1 | 0 | Complete 35-node producer collection; original tests and assertions unchanged |

The Agent default-lane collection and execution match the same **945 unique
node IDs**, with no missing or duplicate selected tests: 929 passed, 16 failed,
zero skips. Its one original integration-lane deselection is
`tests/test_installer_lifecycle.py::test_real_backend_upgrade_binding_survives_fresh_controller_recovery`.
The unchanged project policy assigns that node to the integration lane; this
report does not count it as executed or passed. The separate producer collection
and JUnit match all 35 unique nodes, with zero deselections. Full commands,
environments, node sets, JUnit and failure details are in the bundle.

The five loader failures use existing legacy-schema fixtures. The eleven
installer/handoff failures consist of two existing R0 guards, eight readiness
error-message expectations, and one refusal-wording assertion. Their exact
identities match retained untouched44 controls; no fresh base run is implied.
Original Windows-branch tests and R0 source fixtures are simulations on Linux,
not native Windows or R0 qualification.

The producer failure is still at `test_motion_producer_contract.py:262`:
`test_real_pack_release_group_admission` expects the bare repository URI in PIN,
where the real canonical transaction writes the pack-qualified source.
All four original producer tests and all 20 real compiler/Node syntax cases
pass. Assertions after that failing PIN assertion were **not executed**.
PACK-A/PACK-B were given the exact actual source/identity contract; no assertion
was altered or waived, and no final K/D/A was invented.

## Genuine installed proof: 48 passed cases

| Proof | Cases | Actual result |
| --- | ---: | --- |
| Fresh public CLI install and quarantine/repair | 2 | Real Runtime readiness, selected default, canonical admission and PACK-B K/D agreement; repair preserves selection and user document |
| New authority/absence controls | 24 | Matching/damaged source with deleted/user/folder PIN; real modern/legacy user/folder controls; public recreation, present file/empty directory/dangling link; interrupted rollback/retry; callback recreation; legitimate absence and tombstones |
| Real earlier executable after canonical rollback | 1 | Old reader/strict default verification/actual loader accept restored legacy generation; all 66 pack files and user bytes preserved |
| Original installed interruption/receipt-I/O/repair assertions | 10 | Separate genuinely staged unselected prefix; original program and all assertions preserved |
| Original selected-manifest/receipt/admission/user-origin negatives | 7 | Expected refusal/quarantine, selected pointer and collection bytes unchanged |
| Public two-version SIGKILL and journal recovery | 2 | Actual exit -9 after canonical removal and selection; next public command restores old selection, all 64 source hashes, user folder pack, tombstone and Runtime readiness |
| Real candidate resource/compiler refusals | 2 | Real child exit 1 for undeclared import and syntax failure; selected old version/collection/receipt remain unchanged |

The selected clean install is version `0a53c15e3bac`; the genuine second version
is `32b38d2dfa2a`; the original-ten-case stage is `2e012063d18a`. All were produced
by existing `Backend.stage` and noneditable installation at integration b1.
No fake `sys.prefix`, installed-module replacement, direct pack delivery copy,
alternate runtime, or second installer was used. The older executable used for
legacy controls is actual version `2be5c5132c6c` from the retained earlier proof.

The existing public `--sandbox` mode was used in the observed Linux seccomp
venue, with optional setup choices off. Runtime was stopped through the
existing Backend fence after each public proof. The two-version negative
candidate has an explicitly empty default list and the same real product/tool
inputs; it is a rollback fixture, not a proposed release manifest. Negative
resource/compiler inputs are real local Git descendants of actual Kf40.

Independent Git-object comparison matched **1,038 staged and installed
Python/witness files** in the clean public install and again in the separate
original-ten-case stage. All **64 installed producer source files** match the
actual K/T subtree and D. Source staging legitimately excludes `.git`; the
identity check uses independent Git objects. Installed distribution metadata
confirms noneditable packages. Original config/conftest/lock blobs match base44.
Across DELIVERY's pre-HOLD test/helper tree, 2,534 files are byte-identical, and
the one existing transaction regression file preserves its entire original byte
prefix with appended tests only. All original assertions remain.

Actual tools are Node 20.19.4, esbuild 0.27.7, and Bun 1.3.14. Existing media
policy selected PATH FFmpeg/ffprobe 7.1.5-0+deb13u1; the manifest's 7.0.2 archive
was not qualified. Browser acquisition still failed, so no browser, App/editor,
native macOS/Windows, served-wrapper, final H1 journey, or release-freeze PASS
is claimed.

## Failed attempts retained

The original review bundle and exact reproductions are included unchanged.
Before the correction, eight new absence/rollback assertions failed. The first
loader run had 14 fixture setup errors from a redundant HOME mkdir; it did not
execute those loader assertions. After correcting only the new fixture to use
the original conftest's HOME, its 14 cases produced six passes and eight failures:
six real mutable-PIN bypasses, a retained-default bypass, and the new binding
expectation absent in old code. A provisional correction produced 57 passes and
one failure of the original missing-admission repair assertion; the assertion
was preserved and the per-pack diagnosis was corrected. The real old-reader
counterexample described above was also fixed before final handoff.

The first final candidate-negative attempt cloned the private mirror's advertised
branches without Kf40, which existed only in its remote-tracking refs. Checkout
failed before any negative product case ran. The actual existing K was advertised
under a private proof branch, then the **unchanged** original program succeeded
from a fresh clone. Both the failed log and clone are retained; no transport
failure was treated as a product PASS. The manifest honestly identifies
`file:///tmp/r16-pack-delivery-poms`, a complete non-shallow mirror.

## Evidence and disposition

`proof-summary.json`, `source-regressions.json`, and
`originals-and-dependencies.json` bind the results and raw-artifact SHA256 values.
The complete `r16-pack-delivery-hold-correction-proof.zip` linked from PR701 and
the final #2032 comment contains programs, commands, original assertions,
source inventories, all failed attempts, raw JUnit, node reconciliation, and
installed receipts. Historical evidence above this directory remains historical.

Owned correction work is complete. The manager's explicit landing/READY HOLD
and the actual producer-suite failure are reported without waiver. PR701 is
unmerged and draft; no installer or release was published.
