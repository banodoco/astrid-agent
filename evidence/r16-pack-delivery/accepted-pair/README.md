# R16 PACK-DELIVERY — accepted K8/B0a3 proof

## Disposition and exact scope

**READY for bounded DELIVERY review on the accepted source pair.** The fresh
producer suite is 35/35, PACK-B is 83/83, the focused DELIVERY/definitions suite
is 63/63, the original transaction/default/release suite is 311/311, and all
50 installed case records pass. The broader Agent suite remains **929 passed /
16 failed**, with zero skips and one original integration-lane deselection.
Those failures are retained, not converted to passes.

This is a new execution against the accepted pair, not a relabelling of Kf40
results. The two P1 corrections were independently closed in
[Dev2032/7229296423](https://github.com/banodoco/astrid-dev/issues/2032#issuecomment-7229296423).
[Lead acceptance 7230072141](https://github.com/banodoco/astrid-dev/issues/2032#issuecomment-7230072141)
accepts K8/T/D and B0a3 for source integration; it does not grant final release
qualification. No merge, dispatch, served manifest, or release publication is
part of this work.

At the recorded refresh, Agent plan was still `44d59774abb638c61ed0aae2b32653e62a5bcf66`;
PR700 was unmerged. D run `46082059963`, attempt 1, completed with `failure` at
wrapper `ed3fa2d019458774a3f3b442eb99b487339e2840`. No actual joined Agent plan
commit had been posted or observed. This proof consumes actual B directly on
the DELIVERY branch. A later joined plan must be reconciled against its real
ancestry and hunks; no joined or final Agent pin is invented here, and no Lead
tuple acknowledgment is added as a gate.

## Source identities and preservation

| Role | Exact identity |
| --- | --- |
| Actually executed and installed Agent | `a82d483def612fb7aeb193aa9afa147976a64d64` |
| Agent tree | `88035891b099eece84737d026c855c170cdbdd43` |
| Reviewed DELIVERY parent | `6ab3bd0741f48bba3c638ff269b9606e9d12129e` |
| Integrated accepted PACK-B parent | `0a3d4019db73f1dc55881af4b4637d9795e21d4c` |
| Refreshed Agent plan base | `44d59774abb638c61ed0aae2b32653e62a5bcf66` |
| Selected producer K | `8f392101717c18b2e84de425828de3985913a6f9` |
| Selected `astrid_motion` tree T | `9668568025190d0a8b5742cda0aec32ffaf9d5d0` |
| `pack-tree-v1` digest D | `be6abd1feb9759eca56364c6a2ccded470977aaf965a8e208cdd4f1bedcf77cf` |
| Actual poms plan merge, containing K | `6a1d7d2191278a382df143b5a3fec4f2f633d57f` |
| Runtime | `686ca695469bda48cd40a70824c4ccef2a137284` |
| VibeComfy | `55c3948212d23cf037fae8a4c6faad77d33e7279` |
| Lifecycle | `9f88ef7640bc664f697112d966ecefa123d44340` |

The poms merge SHA is not substituted for selected K. All nine PACK-B-owned
files are byte-identical to accepted B, including its independent witness.
Relative to the previously tested `b1a3a6a9e64917c2847ff91c293ff210c8aa10ad`
integration, the only non-evidence change is that witness's Kf40-to-K8 identity.
The producer's accepted K8 change is the strict PIN-source assertion:
`repository + "#astrid_motion"`; T/D and all 64 pack files are unchanged.

All 2,529 original base44 test/helper entries outside the three accepted
PACK-B-owned test files are unchanged. Those three files bind to B. All 2,533
reviewed DELIVERY test/helper entries outside B are unchanged, including the
P1 regressions and all installed proof programs. Original conftest, lane policy,
helper, dependency/configuration and lock files were retained. An initial
blanket audit incorrectly required the accepted B test changes to equal base;
the corrected ownership accounting is recorded without changing source or
assertions. The real pinned dependency checkouts have no tracked changes.

The commit adding this report changes evidence only. The raw final-head
binding compares every non-evidence Git tree entry with executed a82d. No
passing test or installation is rerun solely for that metadata commit.

## Existing delivery path and P1 protection

No additional bridge or installer was added in this refresh. The reviewed path
is exercised through the real public installer and installed interpreter:

1. `Backend.stage` creates the actual version and calls
   `prepare_default_packs`; `_default_packs` invokes that version's Python with
   `-I -m astrid.installer.default_packs` and its actual home/environment.
2. The adapter reads the version-owned manifest, prepares real rendering
   outputs, and uses `install_release_manifest` and the existing canonical
   `install_group` transaction for publication and explicit removals.
3. The loader reads selected installed authority through
   `installed_release_manifest`, then independently bound canonical admission
   through `admitted_pack_pin` and `verify_release_default`. A missing or
   relabelled local PIN cannot change the origin committed in `.group.json`.
4. Existing installer recovery calls the same adapter's rollback. A receipt
   PIN of null requires genuine `lstat` absence before preparation, before
   exchange, and in the exchanged generation. A late conflict reverses the
   exchange; a separately written published generation is retained.

The new installed run quarantines both matching and changed source when a
canonical default's PIN is missing or changed to user/folder. Genuine modern
and legacy user/folder admissions and intentional source edits remain admitted.
Legacy controls are written by the actual older installed executable. Source
regressions additionally cover explicit replacement, unrelated group
publication, local metadata tampering, same-ID freely authored folders and
tombstones.

Rollback refuses a recreated public `pack new` pack, unpinned directory,
ordinary file, empty directory or dangling link and preserves their recorded
entries and group bytes. Interruption/retry and a prepared-callback recreation
are refused correctly. Genuine absence restores the default; explicit
tombstones remain removed. The two unchanged independent late-exchange checks
schedule a write with a test-process wrapper; publication and restoration both
call the original exchange primitive. Installed files are not substituted, and
both written user generations survive the two-generation case.

## Fresh source results at a82d

| Complete boundary | Executed | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: |
| Focused loader/rollback/adapter/order and original definitions | 63 | 63 | 0 | 0 |
| Seven original transaction/default/release files | 311 | 311 | 0 | 0 |
| PACK-B reader/projection/revision files | 83 | 83 | 0 | 0 |
| Four original loader/quarantine files | 36 | 31 | 5 | 0 |
| Original installer/source/update/lifecycle/handoff files | 452 | 441 | 11 | 0 |
| **Agent total** | **945** | **929** | **16** | **0** |
| Actual K8 producer originals/additions | 35 | 35 | 0 | 0 |

The Agent collection and each JUnit file have exactly the same node multiset,
with no duplicate executions. The original lane policy deselects
`tests/test_installer_lifecycle.py::test_real_backend_upgrade_binding_survives_fresh_controller_recovery`;
it is not a pass. The producer's 35 selected and executed nodes also match
exactly; all 20 real compiler/Node records and the strict canonical
PIN/definitions/admission assertions pass. Assertions after the old Kf40
failure now actually execute.

The 16 Agent failure identities equal the retained independently executed
untouched44 controls: five legacy-schema fixture failures, two R0 guards,
eight readiness-message expectations and one handoff refusal-wording
expectation. Thirteen messages match exactly. The other three preserve the
same observed cause with temporary-path/truncation or unordered-set display
differences. The raw comparison retains both messages and traces. No fresh
base execution or waiver is claimed.

The source environment uses real Python 3.13.5 and the original helper. The
original transaction fixtures receive their required actual pinned
Node/esbuild and Runtime-reader inputs. Producer tests use the verified new
staged Agent source and its real pinned Remotion compiler; that is explicitly
a source test venue, separate from the installed executions below.

## Genuine installed execution

| Installed proof | Case records | Actual outcome |
| --- | ---: | --- |
| Public clean install and quarantine/repair | 2 | Exit 0, Runtime ready, selected K8/D admitted, B witness matches |
| Authority and absence controls | 24 | Correct quarantine/refusal; genuine user/folder controls and tombstones preserved |
| Unchanged original installed transaction program | 10 | Real interruption/receipt I/O/retry/repair/removal assertions pass |
| Manifest, receipt, admission and user-origin negatives | 7 | Refusal/quarantine without selection or user-state mutation |
| Public two-version SIGKILL and journal recovery | 2 | Actual exit −9 at switched journal; ordinary CLI recovery exits 0 |
| Resource and compiler candidate refusals | 2 | Actual child exit 1 for each intended error; selected state unchanged |
| Independent late-exchange program | 2 | Original exchange and reverse exchange; all written user generations retained |
| Genuine older writer/reader rollback | 1 | Old reader exits 0; all 66 old pack files and user bytes restored |
| **Total** | **50** | **All case records pass** |

These case records intentionally include overlapping positive/protection
controls; they are not 50 independent release qualifications. Every outer
proof program exited 0. Intended refusal and interruption exits remain in the
receipts and raw logs.

The public selected prefix is `accepted-pair-native/program/versions/090bc6ca346e`.
The genuinely staged empty-default update candidate is `b8167037f0ab`; the
separate original-ten-case installation is `accepted-pair-stage/.../e51fd817037d`.
All three install actual a82d packages. The real legacy control is
`native/.../2be5c5132c6c`, Agent `baf6b73f297865f681bfe07ca2035a53168e79d5`.
Each prefix has **1,038 staged and installed Python/witness files** matched
against independent Git objects. Actual imports are under their own Python
prefixes, with noneditable distribution metadata. No prefix is fabricated.

The public update uses the same Agent/Runtime/tools and an explicit empty
default list to exercise removal. External SIGKILL occurs after canonical
publication and selection. The candidate's normal CLI recovers the exact
prior pointer, all 64 source files, the user pack's four files, the separate
`user_removed` tombstone and the protected document. Runtime becomes ready
again and is stopped afterward. The candidate negative inputs are real local
Git descendants of K8; they are rejected fixtures, not replacement producer
identities. Their manifest is restored afterward.

All 64 selected pack files match actual K/T and independently recomputed D.
The canonical PIN names `file:///tmp/r16-pack-delivery-poms#astrid_motion`;
definitions retain the bare repository URI. Acquisition honestly uses the
complete, non-shallow local Git mirror through the existing installer. This
is not a claimed HTTPS distribution or published-release download.

## Limits, evidence and next join

Actual installed tools are Python 3.11.13, Node 20.19.4, esbuild 0.27.7 and
Bun 1.3.14. The existing media policy chooses PATH FFmpeg/ffprobe
7.1.5-0+deb13u1; the manifest's 7.0.2 asset remains unqualified. Remotion browser
acquisition failed and the public CLI reports visualize/renders unavailable.
The venue is Linux seccomp with the existing public `--sandbox` mode and
optional setup choices off. All task-owned Runtimes are stopped at handoff.

No App/editor/browser acceptance, native Windows/macOS/R0 result, R15→R16 H1
journey, served wrapper, final shared tuple or release PASS follows. Actual
joined-plan integration is still pending the manager's postflight/landing;
when a real join is available, inspect its ancestry, changed hunks and pins
before attributing these results to it. Evidence-only changes do not justify
rerunning passing proof.

`proof-summary.json`, `source-regressions.json` and
`originals-and-dependencies.json` bind the concise report to raw receipts,
JUnit, command/environment records, source inventories and source-preservation
audits. The full archive is linked from Dev2032 and PR701 with its SHA256.
It contains the unchanged independent closure archive and the historical Kf40
archive, including prior failed attempts. Historical results retain their
original identities. Cleanup removed only obsolete archived task-generated
code environments; every prior HOME/data entry was checked unchanged.
