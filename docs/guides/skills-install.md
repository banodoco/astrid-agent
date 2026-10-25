# Skills install layer

> **For developers:** this guide describes installing Astrid's prompt content
> into supported agent harnesses from a prepared Astrid environment. It does
> not install Astrid for users. For user setup, follow
> [Getting Started](../getting-started.md) and open astrid.haus.

Astrid installs its prompt content as "skills" into three agent harnesses: **Claude Code**, **Codex**, and **Hermes**. One canonical command:

```bash
python3 -m astrid.skills.cli install --all
```

`--all` only writes to harnesses whose home directory exists; missing harnesses are skipped silently.

Once an external pack is installed and selected, Astrid can compose its skill
files. Discovery and `skills sync` do not install or download pack code. If a
user removes an installed skill with `skills uninstall`, Astrid records that
choice and leaves it removed. Skills sync runs after install and upgrade. The
app's first-connect sync is being added in parallel. These syncs are best-effort;
run `skills sync` to restore a missing link for a skill that remains installed.

The external pack manifest must be strict v2. Default selection belongs to the
setup declaration, not to an installer-specific manifest field such as
`install_tier`.

The installed-wheel recovery entrypoint is `python -m astrid.skills sync` (or
`python -m astrid.skills doctor --heal`).

## SkillDescriptor contract

Each pack that wants to be installable contributes one file:

```
astrid/packs/<pack>/skill/SKILL.md
```

Discovery also understands canonical component roots declared in `pack.yaml`.
If a pack declares `content.executors: executors` or
`content.orchestrators: orchestrators`, Astrid will discover nested component
skills at:

```
astrid/packs/<pack>/executors/<executor>/skill/SKILL.md
astrid/packs/<pack>/orchestrators/<orchestrator>/skill/SKILL.md
```

Those nested descriptors use the qualified pack id
`<pack>.<component_slug>`. Legacy flat component layouts are still scanned for
packs without canonical `content` roots. Directories that contain a
`skill/SKILL.md` but lack a `pack.yaml` fall back to Strategy 2 flat-walk
discovery without error.

Discovery skips packs with `status: deprecated` or `visibility: hidden`
by default, unless the pack is explicitly requested. This keeps stale
and development-only packs out of the agent's prompt context.

The file is a Claude-Code-style skill: YAML frontmatter with at least `name` and `description`, followed by Markdown body content.

```markdown
---
name: "my-pack"
description: "Short blurb that fits on one line."
---

# My pack

Body content visible to the agent.
```

When Astrid discovers a skill it builds a `SkillDescriptor`:

| Field | Source |
| --- | --- |
| `pack_id` | directory name under `astrid/packs/` |
| `name` | frontmatter `name` |
| `description` | frontmatter `description` |
| `short_description` | reused from the discovery search index — `short_description_or_truncated(...)` |
| `skill_dir` | `astrid/packs/<pack>/skill/` |
| `skill_md` | `astrid/packs/<pack>/skill/SKILL.md` |
| `hermes_metadata` | optional `metadata.hermes.*` block (see below) |

## Per-pack `skill/SKILL.md` convention

- Source-of-truth: ONE shared SKILL.md per pack. Claude and Codex read it as-is. Hermes reads it as-is too.
- The shared file MUST NOT contain Hermes-specific dynamic tokens. The two patterns flagged by the linter are:
  - `${HERMES_*}` — environment-variable interpolation
  - `` !`shell` `` — Hermes inline-shell substitution
- If a pack genuinely needs Hermes-only dynamic content, put it in `astrid/packs/<pack>/skill/references/hermes-only.md` and reference it via `metadata.hermes.references`.

Run the lint check via `python3 -m astrid.skills.cli doctor`. Findings are non-zero exit code.

## `metadata.hermes.*` block

Optional block in the frontmatter that Claude and Codex ignore (they don't recognise the key) but Hermes can read:

```markdown
---
name: "my-pack"
description: "Short blurb."
metadata:
  hermes:
    references:
      - "references/hermes-only.md"
    enable_when: "${HERMES_FEATURE_X}"
---
```

Astrid stores the `metadata.hermes.*` mapping verbatim on the descriptor; how a Hermes runtime consumes it is up to that runtime.

## Codex AGENTS.md fenced-block format

Codex installs maintain an idempotent fenced block at `~/.codex/AGENTS.md`. The block is rewritten on every `install`, `uninstall`, and `sync`. Surrounding user content is preserved.

```markdown
<!-- astrid:begin -->
# Astrid skills

- `_core` (/Users/you/.codex/skills/astrid): Use for the Astrid repo: ...
- `examplepack` (/Users/you/.codex/skills/astrid-examplepack): Short blurb here.
<!-- astrid:end -->
```

When zero packs are installed the inner content reads `_no Astrid skills installed_`. Re-running install with the same input set produces a byte-identical file.

## Hermes mechanisms

Default (`--mechanism symlink`): per-pack symlinks at `${HERMES_HOME:-~/.hermes}/skills/astrid-<pack>` (and `astrid/` for the `_core` pack). Same shape as Claude and Codex.

Opt-in (`--mechanism external-dir`): no per-pack symlinks. Instead the install adds the absolute path of `astrid/packs/` to `~/.hermes/config.yaml` `skills.external_dirs`. Other keys in the file are preserved. The list is deduplicated, so re-running install is a no-op.

```yaml
skills:
  external_dirs:
    - /Users/you/work/Astrid/astrid/packs
```

## State file

Install state lives at `$XDG_STATE_HOME/astrid/skills.json` (default `~/.local/state/astrid/skills.json`). Tests and CI can override with `ASTRID_STATE_HOME=...`.

## Related Guides

- [discovery-for-agents.md](discovery-for-agents.md) — The agent-facing
  contract for discovering capabilities: `skills list`, search, and
  `inspect --json`. Skills install is the delivery mechanism; discovery
  describes what agents do with the installed skills.
- [creating-packs.md](../packs/creating-packs.md) — Pack authoring and source
  discovery conventions.
