# Journey support gaps (COH-C12)

The hosted jobs are written, not run. A release-candidate run fails until every required scenario produces passing evidence for the frozen candidate. The weekly job also requires a pre-updater-to-candidate result and an Ubuntu 22.04 floor run. Only `fresh-install` is currently wired; later TST tasks must wire the remaining scenarios. A missing or unreplayable required release is a failure, not a skip.

The caller supplies a JSON object with full 40-character `app`, `agent`, `runtime`, and `pinokio` commit SHAs, the 64-character `manifest_sha256`, and an `artifacts` map of SHA-256 hashes for `public/install`, `public/install.sh`, and `public/install.ps1` from the same App commit. The Agent and Runtime SHAs must match that manifest. The separate TST harness checkout uses `job.workflow_repository` and `job.workflow_sha` so the reusable/manual/scheduled run executes only the reviewed Agent workflow source that defines its job. The weekly schedule uses repository variable `TST_CANDIDATE_JSON` containing the frozen product object; an absent or invalid value fails the run. Agent, App and Runtime repositories are public, so checkout uses read-only `contents` permission without a personal token, and persisted checkout credentials are disabled. The new `journey.yml` path is not on the default branch, so GitHub manual dispatch and schedule cannot trigger it until it is integrated there. The existing Windows workflow path remains dispatchable for the B3 lane.

Each is an open, named gap until linked real machine evidence closes it:

- [ ] Windows 11 client (hosted Windows is Server 2022).
- [ ] Windows 10, best effort and not claimed.
- [ ] Reboot or logon during update.
- [ ] Defender and Group Policy.
- [ ] OneDrive profile or data paths.
- [ ] Windows ARM64.
- [ ] Standard user (COH-C5; link its evidence here).
- [ ] Native Pinokio Stop/Quit, including upstream behavior.
- [ ] macOS Login Items.
- [ ] macOS 13 floor: hosted runner is macOS 14 or later.
