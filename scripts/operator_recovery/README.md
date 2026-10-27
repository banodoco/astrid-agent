# Private operator recovery launcher

These launchers invoke the canonical pinned App bootstrap with only
`--manifest`, `--root` and `--recover status|prepare|resume`. They contain no host
retirement, ACL repair, database restore or reboot implementation.

For this reviewed private package, copy the launchers into a new package folder.
Add byte-exact `public/install.ps1` from App commit
`d174f667aa04a7c43e6b0f767abb168b6022a6ab` as `bootstrap/install.ps1`.
Copy that App commit's `public/install-manifest.json` to `recovery-manifest.json`,
changing only `agent.sha` to `44d965542f80d8b1c7a2b4680eb0c2d419bad665` and
`runtime.sha` to `9fed6cfc02a1a130011f97af701f5edf9eb374b0`. The reviewed manifest
serialization has SHA256 `903d7703101a0cdbfbce7d4d5dea29f715b615a247499bd309483aa69c31d38c`;
the wrapper refuses another byte representation or source selection.

Preserve CMD CRLF line endings. Include the operator instructions, source/test
refs and package file hashes. Run `tests/test_installer_recovery_package.py` on
native Windows before delivery. Its bootstrap stub checks argument forwarding,
refusals before launch and exit propagation without touching managed Astrid.
The real Windows Restart and installed creative-task acceptance remain separate
gates; see `docs/guides/windows-host-acp-recovery.md`.
