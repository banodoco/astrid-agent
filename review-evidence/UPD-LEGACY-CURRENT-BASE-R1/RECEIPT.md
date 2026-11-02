# Portable receipt: UPD-LEGACY-CURRENT-BASE-R1

Status: **PROVISIONAL / NOT ACCEPTED**. This evidence commit is not part of the candidate tree. Review the candidate parent `0bda5a433e9f1818871eae02924f1c3133b43655`, not this venue commit, for product scope.

| Item | Exact value |
| --- | --- |
| Candidate lane/head | `lane/M-R1FIX/UPD-LEGACY-CURRENT-BASE-R1-wip` / `0bda5a433e9f1818871eae02924f1c3133b43655` |
| Candidate parents | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`, `224a4edc8bd2cdf51f9ca4baa40b92981c3bb381` |
| Candidate tree | `e760495ed054ad64323c93a7a8c9abd6f2f9ce1c` |
| Published Runtime lock source | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Runtime-lock SHA-256 | `859d67343dccbe71b216d114360e1f614fefd6629358038e4488c5b2a232e2a7` |
| Installer tree | `dee42b7685d4535c3e1fbd92ba5dbd962d9345e9` |
| Focused log SHA-256 | `98dc39b89ddaf48f7aac1d03fb3bf77de8897d7260b4b783086349185065fe15` |

Assembly used a fresh task clone at published Agent `ab43bad…`, then `git merge --no-ff` of corrected candidate `224a4ed…`; `git diff --check` and final clean status passed. The old interrupted object `87cc3d89f9bf582a5f2460591fcb54191882b85e` is a merge tree rather than a reusable commit, and no pushed current-base WIP ref existed. The WIP branch was pushed once and remote-ref verified.

The imported classifier remains absent-key-only: a present `identity` key in health or discovery, including explicit `null`, refuses admission; the bound identityless R0 shape remains the sole legacy exception. The focused selection ran without `-x` or `--maxfail` against Python 3.13.18 and published Runtime `2776ba97…`. It yielded **254 passed, 1 failed, 3 configured lane deselections**. Explicit-null health, discovery, and both-null refusal controls passed.

The sole failure is current-base `tests/test_installer_installed_schema.py::test_stage_binds_installed_reader_validator_and_service_to_verified_schema`: the published test literal expects producer `5453366458f7b4ee874c94d15ae7952e3e82d3c3`, whereas the preserved published Runtime lock is `2776ba973b6c8f3555b5629b0840d9691a98d3c2`. It is outside the imported candidate's paths and was not changed or suppressed.

No prior focused execution was carried forward because the first parent and Runtime-lock files changed. Exact-head M-AG protected review, the independent direct foreign-root controls, a bound App artifact tuple, and protected Linux/macOS/Windows journey proof remain outstanding. No acceptance or handoff is claimed.
