# Canonical receipt: UPD-LEGACY-COMBINED

Disposition: **AWAITING_EVIDENCE**.  This is a pushed provisional Agent
candidate with local focused evidence.  It is not protected approval,
matching-manifest journey proof, acceptance, an integration handoff, or
permission to land.

## Exact identity and assembly

| Item | Value |
| --- | --- |
| Repository | `banodoco/astrid-agent` |
| Candidate lane | `lane/M-R1FIX/UPD-LEGACY-COMBINED` |
| Final pushed head | `1080ef666025d5bfc317a58370ba593694c3dc45` |
| Final tree | `5a42a537df453bf3f3bac068119378f1041b42b7` |
| Published Agent base | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` (ORCHESTRATOR 16:00Z; still the Agent pinned by the unchanged install manifest in the newer entries) |
| Current-base candidate | `0bda5a433e9f1818871eae02924f1c3133b43655` |
| Foreign-root controls | `15ea8dce9695e9de6ec44346717be574e097ed1a` |
| Published Runtime product and local test checkout | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Preserved `runtime.lock.json` Git blob / SHA-256 | `96b42310cbb35b3e8ef4b684e860870558cd3691` / `859d67343dccbe71b216d114360e1f614fefd6629358038e4488c5b2a232e2a7` |

Assembly is deliberately two merge commits, followed only by the narrow
installer-test correction described below:

| Commit | Parents | Purpose |
| --- | --- | --- |
| `1770c02ee799eb16677a5b92c3df42875a961191` | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`, `0bda5a433e9f1818871eae02924f1c3133b43655` | Merge current-published-base candidate |
| `da1281873f3a061f09d0232a96e487054a331838` | `1770c02ee799eb16677a5b92c3df42875a961191`, `15ea8dce9695e9de6ec44346717be574e097ed1a` | Merge reviewer-required foreign-root controls |
| `1080ef666025d5bfc317a58370ba593694c3dc45` | `da1281873f3a061f09d0232a96e487054a331838` | Correct stale installed-schema test producer expectation to the preserved published lock |

`merge-base --is-ancestor` exited 0 for the published base and for both
requested candidate heads against the final head.  The initial WIP push at the
published base, candidate push, and final `ls-remote` verification completed
without rebasing, force-pushing, or writing an integration branch.

## Scope, lock, and current-base failure classification

The current-base candidate's imported delta contains its already-reviewed
installer, installer-test, package-data, and installed-verifier work.  The
foreign-root candidate contributes exactly
`tests/test_installer_legacy_live_update.py` relative to `224a4ed…`; the two
new controls are present in final Git blob
`6d8ae17df87b81f57141510c9ba9847b86056bde`.  The only new combined-task edit is
one line in `tests/test_installer_installed_schema.py` (final blob
`775d32b55c085c544be53df2b5a81e86fa8b9b19`), changing its stale expected
producer from `54533664…` to the verified lock source `2776ba97…`.

`git diff --check ab43bad..HEAD` and the foreign merge's diff check both exit
0.  The final base-to-head path list is confined to the imported candidate's
installer/verifier/package-data paths and installer tests; no Runtime, App,
Pinokio, workflow, generated product output, integration branch, or status
file was changed.  The final installer tree is
`dee42b7685d4535c3e1fbd92ba5dbd962d9345e9`.

The completed current-base receipt and log were inspected, not rerun as that
worker's proof.  Its 254-pass / one-fail selection failed
`test_stage_binds_installed_reader_validator_and_service_to_verified_schema`
because the published-base test literal still expected `54533664…` but the
required lock was `2776ba97…`.  The exact node has no row in the current
`KNOWN-FAILURES.md`; therefore it was neither waived nor called green.  Its
receipt establishes that the mismatch is outside the imported candidate paths.
The narrow test-only repair was run in this task as the entire affected file,
with a green terminal result.  The Runtime lock remains byte-identical to
`ab43bad…`.

## Local focused evidence

All test commands used the task-local Python 3.13.18 venv, task-local scratch,
shared `/home/user/merge/cache/uv`, task-local Runtime checkout at the exact
published product SHA, `-vv -p no:cacheprovider --timeout=90`, and neither
`-x` nor `--maxfail`.

| Command | Inner exit | Result | SHA-256 log |
| --- | ---: | --- | --- |
| `pytest … tests/test_installer_legacy_live_update.py::test_legacy_refuses_valid_version_with_foreign_program_root tests/test_installer_legacy_live_update.py::test_legacy_refuses_selected_version_under_foreign_program_root` | 0 | 2 passed | `59c7f408a4e05887b5f1545c37a059fb89487b962d6d3b07138bd502bb0f7905` |
| `pytest … tests/test_installer_legacy_live_update.py` | 0 | 45 passed | `7153b5ca804e12ab96d7c0c3d834fc4fac648440ce887d1ddca30cd4867fdf01` |
| `pytest …` all changed/affected installer files: busy fence, launchd cache/stop, lifecycle, predecessor wheel, Release-0 rollback, service restart, sources, transaction authority, update job/reconcile, wheel, and verifier | 0 | 582 passed, 4 configured lane deselections | `da5dd82f0af260944c9d2cf1e0cf07f727fa6860639e277a5f1dbc7a7ea2d107` |
| `pytest … tests/test_installer_installed_schema.py` after the fixture repair | 0 | 10 passed | `32b60f601951217f2bc39e64ab699e453d8474a0e2de1911bd6265ebea0c636b` |

The first two rows establish both reviewer-requested foreign-root refusals and
the full affected legacy admission file.  The third row covers the imported
candidate's changed/affected installer inputs.  The fourth row is the exact
resolution check for the unlisted current-base failure.  Detailed commands and
inner exits are in `evidence/exit-codes.md`; complete logs are retained beside
it.  No prior focused execution is carried as exact-head proof because the
Agent base and Runtime lock context changed.

## Tuple and portable review evidence

The candidate has an exact Agent/Runtime pair only:
`agent=1080ef666025d5bfc317a58370ba593694c3dc45`,
`runtime=2776ba973b6c8f3555b5629b0840d9691a98d3c2`.  The latest published App
entry is harness-only and retains an install manifest for Agent `ab43bad…`;
it cannot bind this new Agent candidate.  No matching App manifest/artifact
candidate, Pinokio candidate, or protected direct-journey run exists for this
head.  These omissions are deliberate limitations, not passing claims.

The portable receipt and selected complete logs are published separately on
`lane/M-R1FIX/UPD-LEGACY-COMBINED-venue` under
`review-evidence/UPD-LEGACY-COMBINED/`; that branch is evidence-only and does
not change the product candidate.

## Exact next requests

1. M-AG: protected installer custody/admission re-review of final head
   `1080ef666025d5bfc317a58370ba593694c3dc45`, including the two named
   foreign-root controls, explicit-null boundary already present in the merged
   candidate, and the stale-fixture correction.  Do not approve from the older
   `224a4ed…` review alone.
2. M-RT: co-review the final exact Agent head against Runtime product
   `2776ba97…`, confirming the absent-key-only, owner-bound R0 boundary has not
   changed; the added foreign-root controls and schema test fix alter no Runtime
   product behavior.
3. M-APP: produce a proof-only candidate manifest/artifact tuple that pins this
   exact Agent head and Runtime product, returning App SHA plus manifest, lock,
   and install artifact hashes.
4. M-QUAL/M-VENUE: only after that matching manifest and protected reviews,
   dispatch and retain protected direct R0-to-current Linux, macOS, and Windows
   journeys against the exact tuple.  Bind their run IDs, logs, lifecycle
   results, and checksums to a successor receipt.

Until all four requests are satisfied, this remains **AWAITING_EVIDENCE** and
must not be handed off or described as accepted.
