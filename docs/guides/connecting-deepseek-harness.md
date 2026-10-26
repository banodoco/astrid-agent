# Connecting DeepSeek Harness to Astrid's chat

> **For developers:** this guide describes how DeepSeek Harness (DSH) is wired
> into Astrid's local-agent chat, and why each piece is needed. It doubles as a
> checklist for adding another ACP-speaking harness. Integration code lives in
> [banodoco/astrid-agent#110](https://github.com/banodoco/astrid-agent/pull/110)
> with the editor half in
> [banodoco/astrid-app#126](https://github.com/banodoco/astrid-app/pull/126).

Astrid's chat can drive a local agent over the Agent Client Protocol (ACP). Claude
Code and Codex reach ACP through adapter packages that run under Bun; Hermes serves it
natively. DSH is closest to Hermes, with two differences that matter: it needs a real
Node runtime, and its ACP surface is narrower than the adapters Astrid already knew.

## What DSH provides

DSH serves ACP from a named profile:

```bash
dsh --profile acp      # JSON-RPC over stdio
```

- The `acp` profile auto-initializes from a template the launcher ships, and the
  `dsh-acp-app` bundle resolves from inside that launcher, so launcher and bundle cannot
  drift apart. **The template only exists from the 0.2.x line onward.** An older CLI
  answers `profile "acp" does not exist`, which is why Astrid pins a launcher instead of
  trusting whatever `dsh` happens to be installed.
- Advertised session capabilities are `close`, `list`, and `resume`. There is **no
  `load`**; `session/load` returns `-32601 Method not found`.
- `authMethods` is empty. DSH has no sign-in subcommand: keys live in
  `${DSH_HOME:-~/.dsh}/.credentials.yaml`, written by its own Settings surface
  (`dsh web`), or in `DEEPSEEK_API_KEY`.

## Constraints that shaped the integration

**It must run on Node, not Bun.** The harness loads V8-only native bindings. Under
`bun x --bun` it fails with `Unsupported/no-context`; on a Node older than 22 it exits
**silently with code 0 and no output**. Both failures look alike from the outside — the
child just disappears — so the launch resolves a Node that meets the floor and refuses
before spawning when there is none. Bun stays a requirement only for the Bun-backed
adapter packages.

**It must not boot in the person's own home.** The pinned 0.2.x launcher deletes or
renames `settings.yaml` in the `DSH_HOME` it boots in. Booting it at `~/.dsh` therefore
takes the provider configuration away from the `dsh` the person is actually running.
Two consequences:

1. Astrid boots the harness in its own home under the state directory, seeded with
   **copies** — never symlinks, which would let the harness rewrite the originals through
   the link — of `.credentials.yaml`, `settings.yaml` and the home-level
   `cordis.patch.yml`.
2. Providers worth keeping should live in the **home-level patch layer**
   `cordis.patch.yml`. DSH applies that file over every profile (`web`, `acp`,
   `headless`) across the 0.1.x and 0.2.x lines, so it survives the launcher rewriting or
   deleting `settings.yaml`.

**Sessions belong to a home.** Because the harness runs in its own home, a chat started
elsewhere is not resumable there. The host treats a refusal of that kind as "this session
is gone, continue in a new one": the chat keeps its own id and its traffic — including
streamed notifications — follows the replacement, so reopening an old chat works instead
of dead-ending.

## What the integration adds

In `astrid-agent`:

| Piece | Purpose |
|---|---|
| `localAgents.ts` | `dsh` id, name, install and sign-in copy for POSIX and Windows, and the `dsh:` session namespace |
| `astrid-local-agents.ts` | PATH discovery, a sign-in probe over the credential store, the Node-gated launch, and the isolated instance home with its seeding |
| `astrid-local-agent-adapters.ts` | Launcher pin, profile name, Node floor. Deliberately kept out of `LOCAL_AGENT_ADAPTERS`, whose entries are Bun-run adapter packages |
| `astrid-local-agent-host.ts` | `session/load` → `session/resume`, and the replacement-session handling |
| `astrid/skills/harnesses/dsh.py` | Skills adapter writing into `$DSH_HOME/skills` |
| `astrid/skills/cli.py` | `dsh` added to `--harness` choices |

The launch itself is:

```bash
npx --yes @deepseek-ai/dsh@0.2.1-rc.3 --profile acp
```

`npx` is resolved next to the Node that meets the floor, and `DSH_HOME` points at the
seeded instance home.

In `astrid-app`, one `LOCAL_AGENT_TABLE` entry (ids and the schema keep deriving from
that table) plus an icon in `agentIdentity.tsx`; that map must cover every id in the
table, because the component indexes it directly.

## Sign-in states

The probe reports honestly rather than guessing:

| Store | `signIn` |
|---|---|
| A provider key in `.credentials.yaml`, or `DEEPSEEK_API_KEY` | `signed-in` |
| Store absent or empty | `signed-out` |
| Store present but unreadable | `unknown` |

`unknown` still counts as usable, matching how the other agents treat an unreadable
probe: Astrid does not invent a sign-in requirement it cannot confirm.

## Choosing the model provider (optional)

Astrid makes **no provider decision** for you. The `dsh-acp-app` bundle carries its own
default for ACP sessions — DeepSeek's cloud `deepseek-official` / `deepseek-v4-flash` —
and it honours that default regardless of `agent-default-model`, so a fresh install talks
to the cloud and nothing needs configuring.

To use a different provider (a self-hosted gateway, a proxy, another vendor), override the
`acp` entry in the **home-level patch layer**, `<DSH_HOME>/cordis.patch.yml`:

```yaml
- id: acp
  config:
    provider: my-provider      # as declared by your provider plugin
    model: my-model-id
```

Two properties make this the durable place for that choice:

- Astrid seeds the harness's instance home with a copy of that same file on every launch,
  so the override survives the instance home being recreated or reset.
- Leaving the entry out is a valid, complete configuration — it is exactly how you stay on
  the cloud default.

The `- id: acp` shape is what works; a `defaultModel:` nesting under the same id is
silently ignored, and the same entry placed only in `profiles/acp/cordis.patch.yml` works
too but is lost when the instance home is rebuilt.

### Private certificate authorities

If the provider's endpoint presents a certificate from a CA your system does not trust,
the harness fails TLS and falls back to whatever else it can authenticate — usually the
cloud key. Give the **process that launches the harness** the extra trust, for example:

```sh
NODE_EXTRA_CA_CERTS=/path/to/private-ca-bundle.pem
```

Prefer `NODE_EXTRA_CA_CERTS` over `SSL_CERT_FILE`: the latter *replaces* the trust store,
so a bundle holding only private CAs breaks every other HTTPS connection in the same
process tree. Both are per-process, so a service manager (launchd, systemd, a container)
must carry the variable itself — a shell export is not enough.

## Verification

```bash
bun test ./astrid/chat            # 210 pass
bun run build:chat                # rebuild astrid/chat/service.mjs
bun run check:chat-bundle         # verify the bundle is current
```

Against a real runtime: the setup card reports DSH as ready, a first message returns a
grounded reply, and a request to edit the timeline lands as a real clip. Booting an agent
turn leaves the developer's own settings and credentials untouched.

## Notes for the next harness

- Check the advertised `sessionCapabilities` before assuming `load`, `resume`, `fork`,
  or `close` exist; preprint adapters often implement a subset.
- If the harness migrates its own configuration on boot, keep it away from the
  user-owned home and seed a private one instead.
- Never symlink secrets into a home the harness may rewrite.
- Prefer failing with an actionable runtime error over spawning a child that exits with
  no output: silent exits are the most expensive failure to diagnose from the UI.
