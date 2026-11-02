# Portable receipt: UPD-LEGACY-PUBLISHED-BASE-R1

| Item | Value |
| --- | --- |
| Product candidate | `lane/M-R1FIX/UPD-LEGACY-PUBLISHED-BASE-R1@f6e0a5dfca6bf6db7496ada9663aee9c8fc15838` |
| Parents | `2d98ce74db618b6bf99c2b6dfed8ab280ed2c507`, `b54117c9312165e2799554ec27480665d5cad2b1` |
| Required combined ancestor | `1080ef666025d5bfc317a58370ba593694c3dc45` |
| Runtime tuple member | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Final tree | `09b49503b5f316751bb9ea1398c77614b9e31609` |
| Both Runtime lock blobs | `96b42310cbb35b3e8ef4b684e860870558cd3691` |
| Both Runtime lock SHA-256 | `859d67343dccbe71b216d114360e1f614fefd6629358038e4488c5b2a232e2a7` |

Exact-head inner exits were 0 for all three ancestry checks (intermediate,
published Agent, combined candidate), merge-tree byte comparison against the
intermediate, installer/test/setup byte comparison against the combined
candidate, both `git diff --check` runs, task-local Runtime checkout, product
lane push, and remote-ref verification. The merge's first-parent diff is
empty: it adds the published history without changing product bytes.

Foreign-root controls remain in final blob
`6d8ae17df87b81f57141510c9ba9847b86056bde`; the two named refusals are still
defined. The precursor focused evidence is retained without replay because all
relevant inputs are byte-identical: 2 named controls passed, full legacy file
passed 45, affected installer selection passed 582 with 4 deselected, and
installed-schema passed 10. Full logs and predecessor receipt are retained in
the sibling `review-evidence/UPD-LEGACY-COMBINED/` directory, originating at
venue commit `76dc9fa2c056673c014d584c02db31b010eb8c0a`.

No protected review, matching App manifest/artifact, Pinokio candidate, or
protected direct journey is claimed here. This venue branch is evidence-only
and does not alter the product candidate.
