# Render Adapter — Banodoco Publishability Decision Record

**Status**: active (SD2, 2026-06-01)  
**Scope**: How the protocol-v1 Remotion backend consumes `@banodoco/*`
packages and why they remain adapter-installed rather than published
dependencies. `rendering.render` itself is a backend-neutral facade.

## Packages

Three `@banodoco` scoped npm packages are required by the Remotion render
compositor in `remotion/`:

| Package | Purpose |
|---|---|
| `@banodoco/timeline-composition` | Remotion composition root, theme API, and codegen plugin registry |
| `@banodoco/timeline-schema` | Canonical `TimelineConfig` Zod schema shared by CLI and editor |
| `@banodoco/timeline-theme-2rp` | 2RP theme package (effect components, theme.json, per-effect schema) |

## Current Publishability Blockers

All three packages are **not publishable** to a public npm registry. The
blockers:

1. **`private: true`** — Each `package.json` declares `"private": true`,
   which blocks `npm publish` by design. The packages were authored as
   workspace-internal modules, not public distribution units.

2. **No LICENSE files** — None of the three package directories contain a
   `LICENSE` file. A license declaration (in `package.json` and/or a
   `LICENSE` file) is required before any public or even private-registry
   publication that carries redistribution rights.

3. **`@banodoco` npm org path not established** — The `@banodoco` npm
   organization is not registered or configured as a publication target.
   Even if `private` were removed, there is no destination registry, no
   CI publication workflow, and no versioning policy for these packages
   as independent artifacts.

## Adapter Path (Current State)

Until publication blockers are resolved, the render path consumes these
packages through an **adapter install** — the `remotion/package.json`
declares them as GitHub tarball dependencies:

```json
"@banodoco/timeline-composition": "https://github.com/banodoco/timeline-composition/archive/refs/tags/v0.0.6.tar.gz",
"@banodoco/timeline-theme-2rp": "https://github.com/banodoco/timeline-theme-2rp/archive/refs/tags/v0.1.1.tar.gz"
```

(`@banodoco/timeline-schema` is pulled transitively as a dependency of
`timeline-composition`.)

The Remotion backend (`astrid/packs/rendering/backends/remotion/run.py`)
validates that `node_modules/` exists after `npm install` **and** that
each of the three `@banodoco/*` package directories is present.  It fails
with a clear message naming the missing package(s) and pointing at this
document — it does **not** pull these packages into the Python package
graph. This is a technical dependency boundary for source builds and
qualified render workers: the Python distribution does not include or
provision Node.js, npm, Remotion, or any `@banodoco` package. For normal
Astrid setup, follow the OS-specific installer instructions in
[`docs/getting-started.md`](../getting-started.md), then use
[astrid.haus](https://astrid.haus).

## Release render worker configuration

The Python wheel intentionally does not bundle the Remotion checkout or its
Node dependencies. A release worker that must render text/effect timelines
must set `ASTRID_REMOTION_PROJECT_DIR` to an absolute, server-owned Remotion
project containing the pinned `node_modules` closure and set
`ASTRID_NODE_EXECUTABLE` to the absolute, server-owned Node executable. The
worker validates the executable with a bounded `--version` probe, requires the
locked local CLI at
`node_modules/@remotion/cli/remotion-cli.js`, and validates all three required
`@banodoco/*` packages before admitting a Remotion-only timeline. Rendering
invokes `[ASTRID_NODE_EXECUTABLE, <project>/node_modules/@remotion/cli/remotion-cli.js, "render", ...]`; it never resolves `node`, `npx`, or `remotion` through
the ambient `PATH`. Set `ASTRID_TIMELINE_SCHEMA_PYTHONPATH` to the absolute
`astrid/core/timeline/schema/timeline-config.schema.json` file in the selected
Agent source. Its origin, ownership and Runtime digest in `runtime.lock.json`
are checked before admission; the schema, lock and canonical reader join the
launch-bound dependency closure propagated to renderer children. The historical
variable name remains, but the value is a JSON file rather than an import root.
The public task envelope cannot override these paths. Media-only timelines may
continue through the existing FFmpeg fallback when the optional Remotion
runtime is absent.

## Owner Actions Required Before Publishing Can Replace the Adapter Path

Before the adapter path (GitHub tarball → npm registry package) can be
replaced, the package owner must complete these actions:

1. **Add LICENSE files** — Choose an open-source license (e.g., MIT,
   Apache-2.0) and add a `LICENSE` file to each package root. Update
   `"license"` in each `package.json`.

2. **Remove `private: true`** — Set `"private": false` or delete the key
   from each `package.json`.

3. **Establish the `@banodoco` npm org** — Register or configure the
   `@banodoco` scope on the target registry (npmjs.com or a private
   registry). Add the `publishConfig` block to each `package.json` if a
   non-default registry is used.

4. **Set a versioning policy** — Decide whether these packages version
   independently or in lockstep. Tag releases (`v0.X.Y`) and ensure
   `remotion/package.json` can pin to semver ranges instead of tarball
   URLs.

5. **Wire CI publication** — Add a publication workflow (e.g., GitHub
   Actions) that runs `npm publish` on tag push, with appropriate
   credentials.

6. **Update `remotion/package.json`** — Replace the GitHub tarball URLs
   with semver-ranged npm dependencies (e.g.,
   `"@banodoco/timeline-composition": "^0.1.0"`).

7. **Update the Remotion backend guidance** — Update the error message in
   `astrid/packs/rendering/backends/remotion/run.py:_validate_project_dir`
   to point at the published package names instead of the adapter install
   instructions. Update this document to reflect the new state.

## `timeline-theme-2rp` — Optional / Adapter-Local

`@banodoco/timeline-theme-2rp` is a **theme package**, not a core render
dependency. The Remotion compositor resolves themes at runtime and applies
a fallback `banodoco-default` theme when no theme is specified (see
`astrid/packs/rendering/backends/remotion/run.py:_theme_for_props`).

This means:
- **For Python installs**: `timeline-theme-2rp` is never in the Python
  dependency graph. It lives exclusively in the adapter (Remotion) side.
- **For render users**: If a timeline references the `2rp` theme slug,
  the theme package must be installed in `remotion/node_modules`. This
  is handled by the existing adapter install path.
- **For default Python import**: `import astrid` does not touch any npm
  packages, Remotion, or theme data. The render adapter is opt-in and
  only activated when explicitly invoking `rendering.render`.

## Technical Invariant: The Python Distribution Excludes Render Packages

This decision record enforces a hard gate: **building or installing the
Python distribution must never require Node.js, npm, Remotion, or any
`@banodoco` package.** The following source-checkout and qualified render
worker procedure is for developers and operators assembling that optional
adapter; it is not an alternate setup path for normal Astrid users:

1. Installing the pinned Node.js **20.19.4** toolchain (which supplies npm
   **10.8.2**; `.node-version` records the pin)
2. Running `python3 scripts/reshape/remotion_gate.py install` from the repo
   root. This runs `npm ci` against `remotion/package-lock.json`, with a
   2-GiB free-space safety floor, and never uses `npx`.
3. Invoking `rendering.render` with `ASTRID_NODE_EXECUTABLE` set to that
   absolute Node path (the gate exports this for its test process)

The reproducible verification command is:

```sh
python3 scripts/reshape/remotion_gate.py all
```

It provisions the lockfile closure when absent, generates the renderer type
surface, runs the Remotion typecheck, and then runs the complete renderer-
parity selector. `remotion/package.json` and `remotion/.npmrc` reject other
Node/npm versions; `node_modules/` remains ignored and must never be
committed.

The selected Remotion backend fails closed — if `node_modules/` is absent or
any required `@banodoco/*` package directory is missing, it raises
`FileNotFoundError` with guidance (including a pointer to this document)
before any `@banodoco` import is attempted. This keeps the default
Python SDK importable on any machine with only Python dependencies.

## This adapter vs the rendering SDK

This document is about the **npm adapter install** for the built-in Remotion
backend. It is unrelated to the public **rendering SDK** (`astrid.support`,
`astrid.renderer_main`, `astrid.RenderContext`), which is the Python surface
for authoring protocol-v1 renderers — see
[sdk.md](sdk.md#rendering-sdk) for the worked example, and
[render-backend-v1.md](../contracts/render-backend-v1.md#renderer-author-golden-path)
for the scaffold → implement → test → validate → trusted install → smoke →
provenance authoring path.

## Related Documents

- [STAGE.md](../../astrid/packs/rendering/executors/render/STAGE.md) — Render executor stage documentation
- [render-backend-v1.md](../contracts/render-backend-v1.md) — Public pluggable renderer contract
- [sdk.md](sdk.md#rendering-sdk) — Public rendering SDK (`support`, `renderer_main`, `RenderContext`)
- `astrid/packs/rendering/backends/remotion/run.py:_validate_project_dir` — Fail-closed adapter validation
- `remotion/package.json` — Adapter dependency declarations
- SD2 gate decision — `state.json` settled decision record
