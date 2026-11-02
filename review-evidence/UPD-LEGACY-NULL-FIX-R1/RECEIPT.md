# Canonical receipt: UPD-LEGACY-NULL-FIX-R1

Disposition: **AWAITING_EVIDENCE**. This is a pushed installer candidate and local focused verification, not approval, protected exact-tuple journey proof, or a handoff.

## Identity and tuple

| Item | Exact value |
| --- | --- |
| Repository | `https://github.com/banodoco/astrid-agent` |
| Lane | `lane/M-R1FIX/UPD-LEGACY-NULL-FIX-R1` |
| Candidate head | `224a4edc8bd2cdf51f9ca4baa40b92981c3bb381` |
| Published Agent base / first parent | `999d3046321456d0ede1ceb86d564bf549ccfdda` |
| Prior candidate / second parent | `03f1aa11fd64fb3563c4a060e0679fb987c42534` |
| Candidate tree | `d656be51c2939a25f418fba6d829338bfe9b86dc` |
| Published Runtime used in local tests | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| App published in 13:45Z tuple | `b1af52e2376ac75e9ed2d6222b899220a45007c9` |
| Pinokio published in 13:45Z tuple | `3a6a98ae251ca126505245286e2d3e760f4348e9` |
| Manifest, candidate JSON, installer artifacts and their SHA-256s | Not supplied or generated for this Agent head; exact proof tuple remains incomplete |

The App and Pinokio values are context from ORCHESTRATOR, not a claim that a new manifest binds them. Runtime was cloned at the published commit solely for local verification; no integration pin was changed. `git push -u origin lane/M-R1FIX/UPD-LEGACY-NULL-FIX-R1` exited 0, once, and `git ls-remote origin refs/heads/lane/M-R1FIX/UPD-LEGACY-NULL-FIX-R1` exited 0 and returned the exact candidate head. No rebase, squash, or force-push was used.

## Scope and behavior

`git merge --no-ff --no-commit 03f1aa11fd64fb3563c4a060e0679fb987c42534` exited 0 with no conflicts; the final commit is the merge with the ordered parents above. Relative to base, the imported candidate changes are confined to `astrid/installer/**`, installer tests, and the already-authorized `pyproject.toml`, `scripts/verify_installed.py`, and verifier test. Sixteen of the 20 base-to-candidate paths retain their exact prior candidate blobs. New edits versus the prior candidate affect only `astrid/installer/backend.py`, `astrid/installer/legacy_identity.py`, `astrid/installer/update_job.py`, and `tests/test_installer_legacy_live_update.py`. No harness/workflow, bridge, Runtime, App, or discovery file was changed; published base harness and other-owner history are retained.

The classifier now requires `identity` to be absent in both health and discovery. The selected R0 still must satisfy the closed policy, installed provenance, live-owner instance binding, and other existing checks. Runtime 2776ba97 normalizes absent health identity, so installer ownership and update probes route that shape through the closed classifier instead of allowing the general Runtime validator to bypass it. Explicit valid modern identity remains on its existing path. The new tests cover health-null, discovery-null, and both-null refusal through classifier and live gates; they assert discovery bytes and health are unchanged. They also check current R1 low-level null refusal and valid current identity acceptance. Existing tests exercise absent-key R0 admission, owner mismatch, foreign/malformed provenance, and policy failure.

## Commands and inner results

Task directory: `/home/user/merge/ws/M-R1FIX/queue/running/UPD-LEGACY-NULL-FIX-R1` (`$TASK` below); commands run in `$TASK/repo` unless noted. `df -BG` before clone and tests exited 0, showing 15 GiB then 14 GiB available, above the 2 GiB floor.

- `/home/user/merge/kit/codex/task-clone.sh astrid-agent "$TASK/repo" 999d3046321456d0ede1ceb86d564bf549ccfdda` and the equivalent `astrid-runtime "$TASK/runtime" 2776ba973b6c8f3555b5629b0840d9691a98d3c2`: both exit 0.
- `git switch -c lane/M-R1FIX/UPD-LEGACY-NULL-FIX-R1`: exit 0; merge command above: exit 0.
- `UV_CACHE_DIR=/home/user/merge/cache/uv UV_SYSTEM_CERTS=1 uv venv "$TASK/venv" --python 3.13`; `uv pip install --python "$TASK/venv/bin/python" pytest==9.0.2 pytest-timeout==2.4.0 build==1.6.1 pillow==13.0.0 jsonschema==4.26.0 python-dotenv==1.2.4 pyyaml==6.0.3 setuptools==80.9.0 wheel==0.46.3 pip==26.2.2`; and `uv pip install --python "$TASK/venv/bin/python" --no-deps "$TASK/repo" "$TASK/runtime"`, both installs with the same cache/certificate environment: inner exits 0, 0, 0. Python 3.13.18.
- For all test commands, environment was `TMPDIR="$TASK/tmp" PATH="$TASK/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 ASTRID_RUNTIME_CHECKOUT="$TASK/runtime"`; each was piped through `tee`, with `${PIPESTATUS[0]}` saved separately.
- `"$TASK/venv/bin/python" -m pytest -x -vv -p no:cacheprovider --timeout=90 tests/test_installer_legacy_live_update.py tests/test_installer_predecessor_wheel.py tests/test_installer_transaction_authority.py tests/test_verify_installed.py`: inner exit **0**, 166 passed, 3 configured lane deselections. This process collected before the two additional R1 test cases were added; the next run includes them.
- `"$TASK/venv/bin/python" -m pytest -x -vv -p no:cacheprovider --timeout=90 tests/test_installer_legacy_live_update.py tests/test_installer_update_job.py::test_job_and_recovery_identity_probes_use_runtime_validator tests/test_installer_sources.py`: inner exit **0**, 120 passed, including both added R1 cases.
- `"$TASK/venv/bin/python" -m pytest -vv -p no:cacheprovider --timeout=90 -m integration tests/test_installer_transaction_authority.py::test_new_interpreter_recovers_real_completed_c1_stop`: inner exit **0**, 2 passed. The unrelated `omp` verifier case remains deselected; no full suite was run.
- `git diff --cached --check`, `git diff --check`, and `git diff --check 999d3046321456d0ede1ceb86d564bf549ccfdda..HEAD`: exits 0. Scope/blob comparison and final clean status: exit 0. An initial non-authoritative comparison script failed because it included published-base-only journey paths absent from the old candidate; the corrected base-to-index comparison succeeded, with 20 allowed paths and zero mismatch among 16 unchanged candidate blobs.

## Hashes and carry-forward

| Object | Hash |
| --- | --- |
| Installer tree (Git) | `dee42b7685d4535c3e1fbd92ba5dbd962d9345e9` |
| Tests tree (Git) | `121335e8937773df0e74a8570566ad5d3d3dbe08` |
| `backend.py` (Git blob) | `01a6f75705e3e6e4631e293a7d455cf831aed8eb` |
| `legacy_identity.py` (Git blob) | `a3a7884ca2c6f45102661892f186a53d041ac8eb` |
| `update_job.py` (Git blob) | `51cbab2eca54fb389c1e41afcdda6fc36f6b5829` |
| `test_installer_legacy_live_update.py` (Git blob) | `6e846742a9ee8aa1143fa7662099f9e096911775` |
| `predecessors.json` (Git blob / SHA-256) | `47f42daa5edaddb2591a315e0c06a76a38290822` / `48cf8dea385ce30810ccf7e5a5091c8554097ef157ed3c4b35b57362f9956d50` |
| `pyproject.toml` (Git blob / SHA-256) | `826a59da26740da3cb2d66bc4af9955e21f0a85c` / `8c5467b51ef9c34251a7d670f0569d35a8b12075312b78ce09f76002778e3b60` |
| `scripts/verify_installed.py` (Git blob / SHA-256) | `2058ecd8cfdc048545e9f63455fe0e0034567b83` / `87f6be3cf2848cc7a8ac77c0a1d697c7aceb0ce100f73c4b22786fadb9c9b808` |
| `evidence/focused.log` SHA-256 | `e455ef28d95009908982f4035f003ab38e74cb8c9888720634f260678949085c` |
| `evidence/gates.log` SHA-256 | `c59c7a0d31146ff2e53497201244773d7ed18016a88851fc45124db54e4c165d` |
| `evidence/integration.log` SHA-256 | `7d6800582acbe330821b52049b301ad2340728c9451cfd40be7a7508e6c5300a` |
| Each three test exit files SHA-256 | `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa` |
| `evidence/setup-exits.txt` SHA-256 | `ee852a59a5cbf8b57d2df3481d37aa64abf84fc33661163e7e71fd2a8b1898f7` |
| `subworker/REPORT.md` SHA-256 | `895635c1bd42406b39765cfbc52c73cffd3abf7a90a7192855a9c09b3479c1d3` |

The predecessor policy is the shipped generated/package-data input; its bytes and the previously authorized package/verifier blobs remain identical to prior candidate. The temporary test-built wheel/sdist were not retained as release artifacts; no installer release artifact hashes are claimed.

Prior canonical receipt `queue/pending/UPD-LEGACY-AGENT-HEAD-R2/RECEIPT.md`, SHA-256 `7a8a6d06a94e96f03dec06570b704e9e36b9b3117c2b964d6f17f35d1fe487d5`, is carried as historical lineage and unchanged-file byte evidence only. Its classifier-dependent execution result is not carried as proof of this changed code. Its historic two transaction cases were rerun above against the published Runtime; no old execution is represented as exact-tuple evidence.

Historic direct Linux and macOS install logs at `/home/user/merge-docs/docs/merge-plan/evidence/actions/41446010147/journey-receipt-direct-{linux,macos}/before-cleanup/install.log` have respective SHA-256 `528746bae432f0dd6e7d153d8886b9320515bbd98a67612568620cf199fba789` and `92af357570cb4cb9e6ecf2ad3aaf3719412e72e00cc1e8a8bb5b44fd2c55e81d`. They diagnose the older failure and are not evidence for this candidate.

## Outstanding

A published manifest/candidate JSON and installer-artifact hashes binding the final tuple are required, followed by protected exact-tuple direct R0-to-current Linux, macOS, and Windows journeys and retained logs, lifecycle results and checksums. Explicit M-AG installer custody/recovery review is still required: confirm the absent-key-only, owner-bound predecessor admission and fail-closed null/malformed/mismatched/foreign/current cases without discovery translation or custody weakening. The earlier M-AG review of `03f1aa11` was **NOT APPROVED**; this receipt does not claim a new approval. Do not hand off.
