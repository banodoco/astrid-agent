# Journey support gaps (COH-C12)

The hosted jobs are written, not run. A release-candidate run fails until every required scenario produces passing evidence for the frozen candidate. The weekly job also requires a pre-updater-to-candidate result and an Ubuntu 22.04 floor run. Only `fresh-install` is currently wired; later TST tasks must wire the remaining scenarios. A missing or unreplayable required release is a failure, not a skip.

The caller supplies a JSON object with full 40-character `app`, `agent`, `runtime`, and `pinokio` commit SHAs, the 64-character `manifest_sha256`, and an `artifacts` map of SHA-256 hashes for `public/install`, `public/install.sh`, and `public/install.ps1` from the same App commit. The Agent and Runtime SHAs must match that manifest. The weekly schedule uses repository variable `TST_CANDIDATE_JSON` containing the same frozen object; an absent or invalid value fails the run. Every run requires the `TST_READ_TOKEN` secret with read-only contents access to both `banodoco/astrid-agent` and `banodoco/reigh-app`; the reusable caller must pass it explicitly. Configuration fails before checkout when it is missing. The token is used only by checkout and is not persisted.

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
