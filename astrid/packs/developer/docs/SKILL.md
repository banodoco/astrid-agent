---
name: developing-astrid
short_description: "Develop Astrid itself: the repositories, building and testing, and the developer skills."
description: "Start here to develop Astrid itself: what the Runtime, Agent, App and Pinokio repositories own, how they pin each other, how to build and test each one, and how to install the developer skills (fast-iteration, releasing). Use when changing Astrid's own code, tests, packs, skills, CI or release machinery, not when using Astrid to make creative work."
---

# Developing Astrid

This skill is for people changing Astrid itself. To make video, images or
audio with Astrid, use the `astrid` skill instead.

## The repositories

| Repository | Owns | Pins |
| --- | --- | --- |
| `banodoco/astrid-runtime` | The workspace Runtime: projects, media, timelines, tasks, runs, receipts and events; the HTTP contract (`contract/`), `runtime_protocol`, the `POST /v1/update` bridge, and the generated Python and TypeScript clients (`packages/`). | Its own integration tests pin an Agent in `tests/astrid-agent.pin`. |
| `banodoco/astrid-agent` | The `astrid` Python package and CLI, the SDK, the packs (`astrid/packs/`), the skills layer (`astrid/skills/`), the installer (`astrid/installer/`), the MCP server and the Remotion renderer. | The Runtime commit in `runtime.lock.json`, with the client vendored in `astrid_runtime_client/`. |
| `banodoco/astrid-app` | The editor App and the install manifest users update from (`public/install-manifest.json`). | The Runtime in `runtime.lock.json` (vendored TypeScript client) and the Agent and Runtime SHAs in `public/install-manifest.json`. |
| `banodoco/astrid-pinokio` | The Pinokio launcher for the Pinokio channels. | Follows the published tuple. |

The pin chain is **Runtime → Agent → App**. A Runtime change lands first, the
Agent then pins the published Runtime SHA and regenerates, and the App pins
both and regenerates its manifest. Each step invalidates the generated files
and the evidence of the steps after it. App scripts that enforce the chain:
`scripts/installer/pin.mjs`, `scripts/vendor-lock.mjs` and
`scripts/sync-runtime-client.mjs`.

## Build and test

Keep the virtual environment and `node_modules` inside the checkout, and
reuse the host's package caches (`UV_CACHE_DIR`, `PIP_CACHE_DIR`,
`npm_config_cache`).

**Agent** (Python 3.11+):

```sh
uv venv .venv && uv pip install -p .venv/bin/python -e '.[dev]'
.venv/bin/python scripts/ci_install_runtime.py   # pinned Runtime and client, as CI does
make check                                        # structure, doc references, doctor, ruff, mypy, cycles, renderer parity
.venv/bin/python -m pytest -o addopts= -q tests/test_skills.py   # a focused proof
make test                                         # the whole suite
ASTRID_RUNTIME_CHECKOUT=../astrid-runtime .venv/bin/python -m pytest -q -m integration
```

Without pip in the venv, install the pinned Runtime from a checkout at the
`source_commit` in `runtime.lock.json`:
`uv pip install -p .venv/bin/python --no-deps <runtime> <runtime>/packages/python`.
Generated files are tested for freshness: after changing a CLI parser or a
shipped artifact, run `.venv/bin/python scripts/gen_cli_surface.py`,
`.venv/bin/python scripts/artifact_manifest.py` and
`.venv/bin/python scripts/gen_tool_catalog.py`.

**Runtime**:

```sh
.venv/bin/python -m pip install -e '.[test]' -e packages/python
.venv/bin/python -m pytest -q -n 2
.venv/bin/python generators/generate.py --check      # contract and clients are current
ASTRID_CHECKOUT=../astrid-agent .venv/bin/python -m pytest -q -m integration
(cd packages/typescript && npm ci && npm run build && npm test)
```

**App** (the Node version in `.node-version`):

```sh
npx -y npm@11 ci
npm run dev:local            # the editor against a local Runtime
npm test && npm run lint && npm run quality:check
make release-check           # the full App release gate
```

## Working on Astrid's skills

Skills ship in packs. A pack's main skill is the `docs/SKILL.md` its
`pack.yaml` declares under `documentation`; extra skills are
`docs/<name>/SKILL.md` files declared under `resources` with `kind: skill`.
`astrid/packs/_core/docs/` is the gateway. `astrid skills sync` composes a
writable view per harness under Astrid's state directory and links `astrid`
(and `astrid-<pack>[-<skill>]` for selected skills) into Claude, Codex and
Hermes. A skill whose frontmatter declares

```yaml
metadata:
  astrid:
    audience: developer
```

is developer-only: it stays out of the view until a developer sync selects it.
The tests are `tests/test_skills*.py`; the user guide is
`docs/guides/skills-install.md`.

## The developer skills

| Skill | Use it for |
| --- | --- |
| [Fast iteration](fast-iteration/SKILL.md) (`fast-iteration`) | The edit → test → result loop: the venue ladder, focused proofs, verdicts, retries, receipts, the tuple train and landing pre-checks. |
| [Releasing](releasing/SKILL.md) (`releasing`) | Release gates and qualification: protected journeys, the frozen `POST /v1/update`, the App update guard, coverage policy, RC evidence, freeze and go/no-go. |

They are not installed by a normal sync. If the links above are plain text,
install them:

```sh
astrid skills sync --developer                   # every detected harness
astrid skills sync --developer --harness claude  # one harness
```

From a source checkout, run it with the checkout's Python
(`.venv/bin/python -m astrid skills sync --developer`) so the view is built
from your working tree. Later plain `astrid skills sync` runs keep them;
`astrid skills uninstall --developer` removes them again and leaves this skill
in place. For MCP clients, start the server with
`python -m astrid.mcp_server --developer`.
