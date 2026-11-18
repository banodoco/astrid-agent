# Agent CLI Contract

This document defines the stable public contract between Astrid's CLI and
agentic consumers (both human operators and AI agents).  It covers stream
discipline, output modes, error signaling, and the behavioral guarantees
that agents can rely on when invoking Astrid subcommands.

The versioned producer bundle for the CLI surface and diagnostic projection
is documented in [`public-boundary`](../../schemas/public-boundary/README.md); its
manifest and fixtures define the consumer conformance inputs.

The gateway owns **seven core families** — the five product families
(`projects`, `timelines`, `media`, `tasks`, `runs`) and the two
operational families (`doctor`, `backup`) — plus the two
manifest-declared nested mounts (`timelines shots`, `media references`).
Installed external packs may additionally expose top-level commands through
the declarative pack-command registry. One verb = one SDK call. See
[the CLI census](../getting-started.md) and
[CLI journeys](../guides/cli-journeys.md).

## Stream Discipline

Every Astrid CLI invocation observes strict stdout/stderr separation:

| Stream | Content | Purpose |
|---|---|---|
| **stdout** | Command result surface | Exactly one JSON document for product commands.  Agents read stdout for the command outcome. |
| **stderr** | Diagnostics and structured errors | Error envelopes, `valid options:` / `recovery:` lines, and pure diagnostics.  Agents parse stderr for structured recovery guidance. |

### Default output and `--json`

Product and nested-mount commands emit the complete JSON SDK envelope by
default. `--json` remains accepted for compatibility and produces identical
output. Success and failure envelopes go to stdout; exit codes retain their
existing meanings. Stderr is reserved for diagnostics and argument errors.

Each response is exactly one JSON document followed by a newline.
`tasks follow` returns its observation history in the final envelope.
Operational `doctor` and `backup` retain their own output contracts;
`doctor --json` returns diagnostics and `backup --json` its runtime result.

The JSON payload is the frozen five-key envelope:

```json
{
  "ok": true,
  "data": {"id": "…", "slug": "demo"},
  "error": null,
  "receipt": {
    "receipt_id": "…",
    "command_kind": "…",
    "idempotency_key": "…",
    "request_hash": "…",
    "project_id": "…",
    "project_seq": [1, 1],
    "event_ids": ["…"],
    "result": {"…": "…"},
    "created_at": "…"
  },
  "idempotency_key": "…"
}
```

| Key               | Meaning                                                            |
| ----------------- | ------------------------------------------------------------------ |
| `ok`              | `true` on success, `false` on a typed SDK error                    |
| `data`            | the command's result payload (`null` on failure)                   |
| `error`           | a frozen error object (`{code, message, details}`) or `null`       |
| `receipt`         | the committed command receipt on mutations, `null` on reads/failure|
| `idempotency_key` | caller-supplied key, or the key the SDK generated before mutation  |

Stderr in JSON mode may carry `valid options:` / `recovery:` lines and the
structured error envelope. Agents should parse `recovery:` from stderr as
the canonical next command.

### Design Decisions (Settled)

These decisions are locked and must not be re-litigated:

- **SD1**: Default output and `--json` use the same machine-contract path.  It never includes
  preamble or prose — exactly one JSON object on stdout.
- **SD2**: Recovery guidance (`valid options:` / `recovery:`) lives on
  stderr in both modes, so the stdout contract stays parseable.
- **SD3**: Exit-code taxonomy is `0`=success (envelope `ok=true`), `1`=typed
  SDK error (envelope `ok=false`), `2`=usage/parse error (argparse).
- **SD4**: Help is side-effect-free. Top-level, family, and verb help parses
  without opening, migrating, or locking the selected project database; it
  remains printable while the workspace runtime exclusively owns that database.

## Verb Reference

The gateway families and their verbs:

- **`projects`** — `create`, `list`, `show`, `update`, `select`, `current`.
  `select` persists the runtime project-routing preference at workspace or user
  scope; `current` reads the effective selection.
- **`timelines`** — `create`, `list`, `show`, `save`, `archive`, `recover`,
  `history`, `diff`, `visualize`, `render`.  `save` is a whole-document
  compare-and-swap; a stale `--expected-version` is the typed
  `stale_version` and changes nothing. `visualize` emits a run-owned evidence
  pack, while `render` accepts a pinned canonical timeline and follows its
  task to terminal completion by default. `--detach` is the explicit
  admission-only mode and reports `state=admitted`.
- **`media`** — `import`, `list`, `show`, `verify`, `relate`.
  The only supported realm is the runtime-managed `managed_local` CAS;
  reference-in-place (`external_local`), `remote`, and `relocate` are not
  product or SDK operations. `verify` requires `--realm managed_local` and
  accepts `--location-id`/`--locator` selectors;
  `relate` has the frozen five-kind
  `--kind` (`derived_from`, `variant_of`, `uses_as_input`, `mask_for`,
  `audio_for`).
- **`tasks`** — `create`, `list`, `show`, `cancel`, `retry`, `events`.
  `create` admits one immutable task (`--capability` + JSON `--spec`).
- **`runs`** — `list`, `show`, `cancel`, `retry`, `events`, `open`.
  `retry` is the batch-retry surface (all-failed-children by default,
  explicit repeatable `--task` subset otherwise). `open` verifies,
  materializes, and opens the latest successful `rendering.render` run in the
  selected current project; an optional positional run id selects an exact
  render, and `--project` overrides the current selection. It does not claim
  editor-approved/current-deliverable semantics.
- **`doctor`** — read-only health check (`schema_versions`, managed and
  runtime-managed media-byte integrity, SQLite quick-check, FK status, and
  bounded orphan-staging diagnostics).
- **`backup`** — workspace-runtime operations, with the Pack host kept stopped:
  - `create DESTINATION` (or `create --out DESTINATION`) stages and verifies a
    new backup before publishing it; an existing destination is refused.
  - `restore BACKUP DESTINATION` verifies and stages a journaled restore into a
    new inactive realm; an existing destination is refused and there is no
    overwrite/`--force` mode.
  - `export` returns structured realm data and has no destination; `--out` is
    rejected.
  - `tombstone` is a logical lifecycle transition; `--reason` and the
    compare-and-swap `--expected-version` are optional.
  - `recover` requires `--expected-realm-id`, `--expected-version`, and an
    explicit `--confirm 'RECOVER <realm_id>'`; the Astrid CLI never selects the
    Runtime's noninteractive recovery mode.
  - `purge` requires the exact `PURGE <realm_id>` confirmation and a tombstoned
    realm; the online workspace route then returns `409` at the offline-only
    boundary and never deletes a realm. Whole-realm purge is performed offline
    with `astrid-runtime-protocol purge --root <realm> --confirm 'PURGE <realm_id>'`.

Nested mounts (manifest-declared, never top-level):

- **`media references`** — `create`, `update`, `archive`, `associate`,
  `link`, `set-primary`, `list`, `show`.
- **`timelines shots`** — project-level reusable `list`, `create`, `show`, `add`, `remove`, `reorder`.

There is no `next` / `status` / `attach` / `start` / `ack` surface: the
legacy task-mode CLI was retired with the filesystem task-run store. Pack
capabilities without a declared CLI mapping still run through the SDK
(`astrid.sdk.invoke`, `astrid.sdk.client.AstridClient`). Installed pack ids
are discovered at launch; reserved launcher words are the only blocklist.

For example, the managed Hivemind pack exposes:

```text
astrid hivemind search QUERY [--limit N] [--json]
astrid hivemind item --kind {message,resource,revision} --id ID [--revision-id ID] [--json]
```

Adding another pack command requires a declaration of its pack id, public
operation, capability id, and invocation kind. It does not require another
hand-written top-level dispatch branch.

## Error Contract

All recoverable CLI failures travel through the `AstridError` envelope defined
in `astrid/core/contracts/errors.py`.  See [docs/error-model.md](error-model.md) for
the full taxonomy, envelope fields, rendering contract, and authoring rules.

Key points for agents:

- **Exit code 1** means a typed SDK error (`ok=false` in JSON mode).  Parse
  the `error` envelope for the `code`; retry only when the error contract
  says the operation is retryable.
- **Exit code 2** means a usage/parse error.  `valid options:` lists the
  accepted values and `recovery:` suggests the correct invocation.
- **Parser errors** (`AstridArgumentError`) are converted to `AstridError`
  envelopes with `valid_options` listing the accepted values and a
  `recovery_command` suggesting the correct invocation.

## Cross-References

- [Error Model](error-model.md) — canonical exit-code taxonomy, error envelope contract, recovery-command expectations.
- [Platform Contract](platform-contract.md) — cross-backend primitives and gateway-level guarantees.
- [Discovery for Agents](../guides/discovery-for-agents.md) — how agents discover available capabilities through the SDK.
