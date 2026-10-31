# M4-5 installable release 2 stand-in

`installable-stand-in.json` is a qualification-only stand-in, not a published
release. It uses the exact approved release-1 baseline sources: Agent
`2958bacea3e3c7961788fa9c625dd9db0be282de` and its locked Runtime
`ebd412199a9c1e53c3a984d27d335de8b40654a1`. Its Runtime schema digest is
computed from that Runtime's contract; tool pins are copied from the frozen R0
served manifest. Relative to that release-1 source-and-tool baseline, only the
release number and the qualification marker change.

Serve this manifest from the isolated test origin for M4-5. After a successful
update, assert `<version-root>/manifest.json` has `release` equal to `2` and
`qualification_marker` equal to `M4-5 RELEASE 2 INSTALLABLE STAND-IN — UPDATE LANDED`.
The original `stand-in.json` remains a frozen marker descriptor, not an installer
manifest. Verify both new files with the parent `SHA256SUMS`.
