# UPD-LEGACY-COMBINED — provisional combined candidate

Pushed candidate: `lane/M-R1FIX/UPD-LEGACY-COMBINED` at
`1080ef666025d5bfc317a58370ba593694c3dc45`.

It preserves the requested two merge commits for current-base candidate
`0bda5a433e9f1818871eae02924f1c3133b43655` and foreign-root controls
`15ea8dce9695e9de6ec44346717be574e097ed1a`, on published Agent base
`ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`.  The Runtime lock remains
`2776ba973b6c8f3555b5629b0840d9691a98d3c2`.

The reviewer-named foreign-root tests pass (2/2); the full legacy admission
file passes (45); the affected installer selection passes (582, with four
configured lane deselections); and the installed-schema file passes (10) after
correcting its stale `54533664…` fixture to the preserved `2776ba97…` lock.
The prior current-base worker's 254/1 result was inspected and classified as
that unlisted stale expectation; it was not carried as green.

Portable review evidence is on the separately pushed
`lane/M-R1FIX/UPD-LEGACY-COMBINED-venue` branch under
`review-evidence/UPD-LEGACY-COMBINED/`.  This is **AWAITING_EVIDENCE** only.
Required next actions are protected M-AG/M-RT exact-head review, a matching
M-APP candidate manifest/artifact tuple, and then protected exact-tuple
Linux/macOS/Windows journeys.  See `RECEIPT.md` for commands, hashes,
limitations, and the exact requests.
