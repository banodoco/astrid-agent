# UPD-LEGACY-NULL-FIX-R1 report

Disposition: **AWAITING_EVIDENCE**. Candidate `224a4edc8bd2cdf51f9ca4baa40b92981c3bb381` is pushed to `lane/M-R1FIX/UPD-LEGACY-NULL-FIX-R1` in `banodoco/astrid-agent`; remote ref verification matched. The merge parents are published Agent `999d3046321456d0ede1ceb86d564bf549ccfdda` and prior candidate `03f1aa11fd64fb3563c4a060e0679fb987c42534`, in that order.

The classifier requires the identity key to be absent in both owner health and discovery. The installer gates keep Runtime's absent-health normalization behind the closed predecessor classifier. Explicit null in either or both observations refuses without mutating discovery; owner/provenance checks and valid current identity behavior remain covered. Published Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2` was used for local focused tests.

Named local tests passed with inner exit 0: 166 passed with 3 configured deselections; an additional gate/source focus passed 120, including current R1 null refusal and valid identity acceptance; both explicit transaction integration cases passed. `git diff --check` passed. No full suite ran. Prior package data and verifier bytes remain unchanged; the published harness is preserved. The independent subworker exited, and its [report](subworker/REPORT.md) was inspected and incorporated.

See [canonical receipt](RECEIPT.md) for exact commands, scope, tuple context, hashes, proof limits, and carry-forward. A manifest and artifact checksums binding the candidate, protected exact-tuple Linux/macOS/Windows journeys, and explicit M-AG review remain required. The prior candidate was explicitly NOT APPROVED. No handoff is authorized.
