# Process-local registry reuse

`ModelRegistry.load_default` and the default-discovery path of
`ExtensionRegistry.load` retain private, deep-copied registry data in memory for
the process lifetime. Nothing is written to disk and no state crosses a process
boundary. Each caller receives its own mutable data. Extension runtime imports,
protocol objects and runtime failures remain instance-owned.

Every lookup reuses pack discovery's **complete content fingerprint**, including
its roots, environment-selected collections, personal collection, admission
schemas and verified managed-source inventory identity. Managed-source
verification still runs on every lookup. Model keys also contain the resolved
catalog path and models.yaml bytes; extension keys contain contribution roots
(including their scope/origin and current absolute/resolved paths), SDK path and tree, and extension validator schema
paths and bytes (including symlink targets). Model assembly shares the existing discovery scope between its
taxonomy and backend loaders. The discovery implementation is unchanged.

Extension enable/disable config and malformed-config diagnostics are read and
resolved on every load, outside the cached parsed snapshot. Generation default
pins and Runtime settings are not read by model assembly or extension snapshot
parsing and are not cached here. A changed source pin changes the verified
managed inventory identity. This content-keyed scope works in long-lived hosts
and MCP servers as well as short CLI invocations; it never assumes inputs stay
fixed during one invocation. Same-size edits with restored timestamps invalidate
reuse. A miss is stored only if its before/after content identities match.
Unreadable fingerprint inputs bypass the memo and use normal admission.

Custom discovery/scanner seams have arbitrary dependencies and bypass process
reuse. Their previous instance-local extension cache behavior remains intact.
Manifest timestamps are still checked on every extension load to preserve
its existing touch-to-reset signal for instance-owned lazy imports, protocols
and runtime quarantines; timestamps never substitute for content freshness.
`refresh=True` bypasses extension snapshot reuse. Each loader module exposes
`_reset_for_tests()`, and the test suite clears both process memos around each
test. Content reads used for identity still occur on hits: the saving is parsing,
validation and pack admission work, not eliminating freshness checks.
