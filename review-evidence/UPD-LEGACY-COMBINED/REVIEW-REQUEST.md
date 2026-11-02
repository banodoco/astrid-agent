# Exact-head protected review and tuple request

## M-AG / M-RT protected review

Please review `banodoco/astrid-agent` branch
`lane/M-R1FIX/UPD-LEGACY-COMBINED` at exact head
`1080ef666025d5bfc317a58370ba593694c3dc45`, against Runtime product
`2776ba973b6c8f3555b5629b0840d9691a98d3c2`.

The head's immediate parent is `da1281873f3a061f09d0232a96e487054a331838`.
That merge has parents current-base merge
`1770c02ee799eb16677a5b92c3df42875a961191` and foreign-root controls
`15ea8dce9695e9de6ec44346717be574e097ed1a`; the current-base merge has
parents published Agent `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` and
current-base candidate `0bda5a433e9f1818871eae02924f1c3133b43655`.

Review the closed, owner-bound absent-key R0 classifier; explicit-null,
malformed, mismatch, and foreign-root refusal; and confirm the new controls
cover (1) a valid `Version` passed with another `program_root`, and (2) a
selected version rooted beneath a foreign program root.  The only delta from
the final merge to this exact head is an installer-test fixture correction from
the superseded Runtime `54533664…` to the preserved Runtime `2776ba97…`; it
does not change installer product source.  Candidate product source changes
relative to `ab43bad…` are the imported, pre-existing installer candidate
paths documented in `RECEIPT.md`.

Local proof for this exact head: foreign controls 2/2, full legacy live-update
45/45, changed/affected installer selection 582 passed with four configured
lane deselections, and installed-schema file 10/10.  Complete logs and hashes
are alongside this request.  This is a review request only, not an approval or
a journey claim.

## M-APP matching-manifest request

The 17:00Z orchestrator update records that the earlier App provisional failed
because Agent `224a4ed…` retained both Runtime lock copies at `54533664…`.
This candidate has both `runtime.lock.json` and
`astrid/resources/runtime.lock.json` at exactly
`2776ba973b6c8f3555b5629b0840d9691a98d3c2`; their shared SHA-256 is
`859d67343dccbe71b216d114360e1f614fefd6629358038e4488c5b2a232e2a7`.

Please produce a proof-only App candidate manifest/artifact tuple that pins
Agent `1080ef666025d5bfc317a58370ba593694c3dc45` and Runtime
`2776ba973b6c8f3555b5629b0840d9691a98d3c2`, and return its App SHA,
manifest hash, both lock hashes, and install artifact hashes.

## M-QUAL / M-VENUE follow-up

Once the matching App tuple and both protected reviews exist, dispatch direct
R0-to-current Linux, macOS, and Windows journeys on that exact four-repository
tuple and retain run IDs, lifecycle outcomes, full logs, and checksums.  Do not
reuse the existing `ab43bad…` journey as proof for this candidate.
