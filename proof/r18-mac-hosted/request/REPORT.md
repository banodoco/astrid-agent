# R18 hosted macOS native owner proof

**SUBMITTED — native evidence and review pending.** The push-triggered GitHub-hosted request is [run 41561185739](https://github.com/banodoco/astrid-agent/actions/runs/41561185739), observed queued with no conclusion. It runs `.github/workflows/r18-mac-hosted-native.yml` from `venue/M-R1FIX-R18-MAC-HOSTED-NATIVE-P19-1` at `8e571fe69e758063332e3f82b44697ab14559ba6`. GitHub's run metadata identifies that exact SHA, branch, push event, and workflow. Do not dispatch a duplicate while this run exists.

ACTIONS-WATCH: astrid-agent 41561185739

The canonical request receipt is `canonical-receipt.json`, SHA-256 `3ad0ad833d770afaa5fb1fc9ff9ed0c66831af77a9acc840e6f51438d783ce1e`. The task-owned Agent branch is `lane/M-R1FIX/R18-MAC-HOSTED-NATIVE-P19`; the proof venue commit's sole parent is exact candidate Agent `098ae387521b3adb782f9b62bf60aa3ba2feb711`. Its diff adds only the task-owned workflow and `proof/r18-mac-hosted/` driver and receipt helpers; it does not change product code, protected assertions, or existing workflows.

## Exact inputs and submission

- Agent `098ae387521b3adb782f9b62bf60aa3ba2feb711`, Runtime `a95e0e836234bc6d27ba557ba1297da95ed55290`, App `2145dc85b21f3c5a22f96a842c0d257ae29051e5`.
- Locally checked out all three immutable commits with `task-clone.sh`; App's `public/install-manifest.json` hashes to `528615bf87637471ef38072e6899e745bdba7e54d5a65436b1bac5af3447b45c` and pins the specified Agent and Runtime. The hosted driver independently checks all three checkouts and the manifest.
- The push to the new venue branch exited 0 without force; `git ls-remote` independently returned `8e571fe69e758063332e3f82b44697ab14559ba6`. GitHub run metadata returned ID 41561185739, event `push`, status `queued`, and the same head and workflow. No hosted completion was awaited.
- Workflow SHA-256 `a20309da6ea2c681365e3892112f9b917616b8bc138c11c71cfc7e9bf701e578`; native driver SHA-256 `b5250d0586bb260ed2ee77f00ee5e8d06356851e19246fcfd2edc349dbfb445d`. Receipt and hash writer hashes are in the canonical receipt. This is a new proof driver and workflow, not an unchanged canonical journey.
- `python3 -m py_compile` for the three proof scripts and `git diff --cached --check` both exited 0. These are syntax/patch checks only; no focused lane tests were replayed and no macOS result is claimed.

## Proof requested

The runner is `macos-15`; the driver refuses root, mismatched checkouts/manifest, an unavailable GUI launchd domain, an existing `haus.astrid.runtime` service, or a busy IPv4/IPv6 port 17340. It creates a fresh private HOME, program root, data root and cache. It verifies the frozen R0 fixture's checksum inventory and executes its captured `served/install` with its frozen R0 manifest and explicit test roots. The R0 installer, not a fabricated fixture process, installs and launches the R0 Runtime under launchd.

Using the selected installed venv with the exact candidate Agent and Runtime source checkouts on `PYTHONPATH`, the driver reads the live R0 discovery PID and birth and invokes the candidate's full `Service.admit_launchd_bootout`. It records the real `launchctl` state, native PID/UID/birth and executable, venv `pyvenv.cfg`, interpreter hash, selected program and executable, trusted-interpreter predicate, v2 witness, and retained-identity match. All named checks must hold for that part to pass.

For the negative it copies the real bootstrap interpreter outside the trusted bootstrap tree, creates a separate venv within the selected root, and starts a separate uniquely named real launchd process. It obtains that process's native PID, UID, birth and executable, and invokes `_launchd_live_pid_is_bound` with those real observations. It requires refusal and records the reason. This is a focused live-owner-helper refusal, not a full `admit_launchd_bootout` of a foreign service and not evidence of a foreign-discovery integration path.

The driver attempts cleanup only after validating the loaded service path and selected program are under its fresh test root; it similarly checks its unique foreign job before bootout. It keeps scratch until runner disposal. It records commands and exits, bootstrap output, installed identity, native predicates and cleanup. On any failure or missing inner exit, the run must remain non-passing. In particular a timeout, cancellation, checkout failure, or publication failure may prevent complete receipts.

## Collection and limitations

On completion, inspect the Actions artifact `r18-mac-native-41561185739-<attempt>` or fetch `refs/heads/venue-results/M-R1FIX-R18-MAC-HOSTED-NATIVE-P19-1`, then extract `runs/41561185739-<attempt>/` and check `SHA256SUMS`. Review `receipt.json`, `exit-code.txt`, `command.log`, `preflight.json`, `installed.json`, `native-owner.json`, `foreign-owner.json`, both cleanup records and the bootstrap/inspection exits. Missing artifacts, native predicates, or cleanup records are not a pass. The result branch is produced by the workflow, not by this task.

OS build, non-root UID, effective installed R0 identities, native predicate values, interpreter/config hashes, inner exits and cleanup have **not yet been observed**; they are expected in the hosted receipt. The driver itself has not run on macOS and may reveal venue/setup or implementation failures. Exact-head native evidence and its review are required before acceptance. This focused request does not establish the entire R0-to-R1 lifecycle, stop-order or Runtime-discovery behavior. Earlier journey 41550566447 failed at missing discovery with lifecycle checks not run; it is not proof of success. The M-AG protected source review reported no blocker at 00:43Z, but native evidence remains required. No HANDOFF was created.
