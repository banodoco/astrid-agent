# I-AG-04-PUBLISHED-PROOF-R1 — published tuple proof receipt

**Disposition: focused proof failed.** The exact published Agent/Runtime tuple collected all requested focused installer nodes and produced 104 passes, but 11 nodes failed. The installed-schema consumer probe proves that the isolated installed reader and validator opened the verified installed JSON schema; its test then fails on a producer SHA mismatch. Ten transaction-authority nodes also fail at Runtime replacement admission. This evidence does not satisfy the requested pass gate or replace the full I-AG-04 acceptance receipt.

## Identity and scope

| Field | Exact value |
| --- | --- |
| Repository | `banodoco/astrid-agent` |
| Proof lane identity | `lane/M-AG/I-AG-04-PUBLISHED-PROOF-R1` (proof only; no branch push or product commit) |
| Agent HEAD | `e425903f7a9fa5b82973281d15054c0716b46e2d` |
| Agent tree | `57c2f21f2f670500d800e88153ca6ee339adca53` |
| Agent merge parents | `89750ad807ecf119114e1962cc40222ae8a8b848`, `608b46c6c1aec28808ad17d806d936ebddd0f9c7` |
| Agent base | `e425903f7a9fa5b82973281d15054c0716b46e2d` (detached proof checkout) |
| Published predecessor | `ddd5d278796641901bd11508863a8ea21e6c9697` |
| Runtime producer HEAD | `8d3e3af4ede6e3579ac673ae77794464f693bf1b` |
| Runtime tree | `f97beb8148251ac79f84bc1a0c404f76f13fe13b` |
| Runtime merge parents | `1e6d899efa77a2d6ec247c38250f9748330bb587`, `5453366458f7b4ee874c94d15ae7952e3e82d3c3` |
| Final clone status | Agent and Runtime clones clean, detached at the exact heads |

Task-local clones are `agent/` and `runtime/`. `evidence/identity.txt` records the identities and final status. The Agent delta from the predecessor includes installer and schema paths: `astrid/installer/app_update.py`, `backend.py`, `cli.py`, `custody.py`, `launcher.py`, `maintenance.py`, `service.py`, `tools.py`, `update_job.py`, and `astrid/core/rendering/_local_schema.py`. No product files or shared refs were modified.

## Runtime lock check

Command, run with the task Python 3.11 environment:

```text
.venv-task/bin/python agent/scripts/vendor_lock.py check agent/runtime.lock.json --producer runtime
```

Inner exit: **1**. The lock says `source_commit=5453366458f7b4ee874c94d15ae7952e3e82d3c3`, while the required producer checkout HEAD is merge `8d3e3af4ede6e3579ac673ae77794464f693bf1b`. The exact output is in [vendor-lock-final.log](evidence/vendor-lock-final.log). No lock update or generated-file write was made.

## Focused test selection and result

Final command and inner exit: [final-command.txt](evidence/final-command.txt). Full pytest log: [focused-installer-schema-final.log](evidence/focused-installer-schema-final.log). JUnit: [focused-installer-schema-final.xml](evidence/focused-installer-schema-final.xml). Pytest was run with `-o addopts=` to preserve integration-marked transaction nodes; there was no `--maxfail` or `-x`.

| Selection | Collected | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: | ---: |
| `tests/test_installer_installed_schema.py` | 10 | 9 | 1 | 0 |
| `tests/test_installer_transaction_authority.py` | 99 | 89 | 10 | 0 |
| `tests/test_installer_release0_service_adoption.py` | 6 | 6 | 0 | 0 |
| **Total** | **115** | **104** | **11** | **0** |

The release-0 service adoption retained selection passed all six nodes. The prior `I-AG-04-R1B-R1/REPORT.md` does not enumerate R-09 node selectors; this proof therefore includes the complete transaction-authority and release-0 adoption files, in addition to its named installed-schema file. Per-file collection and exact failures are in [coverage-summary.json](evidence/coverage-summary.json).

### Failed nodes and evidence

1. `tests/test_installer_installed_schema.py::test_stage_binds_installed_reader_validator_and_service_to_verified_schema` — assertion at `tests/test_installer_installed_schema.py:238`: lock producer `5453366458f7b4ee874c94d15ae7952e3e82d3c3` differs from the test constant `1e6d899efa77a2d6ec247c38250f9748330bb587`. Before that final assertion, the child probe reports all inspected Astrid modules under the isolated candidate venv and schema SHA-256 `f870150ffd0e42365c048965e2f7c08efc959c36db76ed4cba59de2e58ab4e08`, matching both the Agent’s vendored file and Runtime’s `contract/schemas/timeline-config.schema.json`. The test asserts the reader and compiled validator opened that exact installed file and checked valid/invalid Timeline inputs before printing the probe record. See [schema-consumption.txt](evidence/schema-consumption.txt) and retained `tmp/pytest-of-root/pytest-2/test_stage_binds_installed_rea0/install.log`. This proves schema consumption, but the installed-schema suite is not green.

2. These ten parameterized nodes in `tests/test_installer_transaction_authority.py` fail the assertion that the exception is not a lifecycle refusal:

   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[pid-between_reads-migration]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[pid-between_reads-app]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[birth-between_reads-migration]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[birth-between_reads-app]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[instance-between_reads-migration]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[instance-between_reads-app]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[realm-between_reads-migration]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[realm-between_reads-app]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[root-between_reads-migration]`
   - `test_recovery_preserves_transaction_and_replacement_at_every_admission[root-between_reads-app]`

   At `tests/test_installer_transaction_authority.py:198`, each receives `RuntimeOperationError('Runtime interruption refused: owner lock has no matching discovery. Run astrid-runtime up --profile astrid to recover discovery.')`, which `core.is_lifecycle_refusal` classifies as a lifecycle refusal. Thus the expected replacement-admission refusal is not produced. Full node tracebacks and pytest summary are in the JUnit and log above.

## Environment and execution record

- Free space before environment setup: 9,384,600 KiB available; after proof: 7,542,628 KiB. The 2 GiB floor was maintained.
- Python: 3.11.17. Task-root venv: `.venv-task/`. Runtime was installed from the exact `runtime/` clone into that venv; task-level caches and temporary directories stayed under this task folder, except the configured shared pip cache `/home/user/merge/cache/pip`.
- The final test command used `env-safe.sh`, `ASTRID_RUNTIME_CHECKOUT=<task>/runtime`, `PYTHONPATH=<task>/runtime:<task>/agent`, and task-local `TMPDIR`/`XDG_CACHE_HOME`.
- Two initial runs are retained but are not the final result: `commands.txt`, `focused-installer-schema.log/.xml`, `rerun-command.txt`, and `focused-installer-schema-rerun.log/.xml` document the first missing Runtime setup and the second venv-in-checkout contamination. The third run used the corrected task-root environment and is the reported result.
- No full suite was run.

## Artifact hashes

| Artifact | SHA-256 |
| --- | --- |
| `agent/runtime.lock.json` | `cb17efd3bb7a626a56ec6a20e7a4fb54a74ef513e2e7d54e913f8a05d5d0f767` |
| Agent vendored Timeline schema | `f870150ffd0e42365c048965e2f7c08efc959c36db76ed4cba59de2e58ab4e08` |
| Runtime producer Timeline schema | `f870150ffd0e42365c048965e2f7c08efc959c36db76ed4cba59de2e58ab4e08` |
| Final pytest log | `cb774bff4e858bb0e6711a66d820f77388a57d5352e2b78e967cac44575093c7` |
| Final JUnit | `dc599e15ba698e33b9d42692545264a6a6009adf435a531bdfacba7fa76270b9` |
| Runtime lock check log | `6fa00af5a7e25e2adf9476ea112c6e9f659ab6f37adf1af56168a4148c25314c` |
| Coverage summary | `de806a4d34d060733b3d82aaad2b2661db21d47609d3a58af0336b9a1d6279d1` |
| Schema consumption record | `906540b9109867e7c09c64804014d72c92eef1e8af1e929fd6842e5659497cdf` |

Full task-evidence checksums, including commands, environment logs, and both corrected/initial test artifacts, are in [SHA256SUMS](evidence/SHA256SUMS).

## Limitations

The exact tuple did **not** pass the focused acceptance selections. Runtime lock provenance remains mismatched to the requested Runtime merge HEAD, one installed-schema test rejects the producer SHA, and ten Runtime transaction-authority replacement cases reject at the lifecycle fence. No lock or product update was authorized or made here. The proof does not substitute for the full I-AG-04 acceptance/Track-A receipt.
