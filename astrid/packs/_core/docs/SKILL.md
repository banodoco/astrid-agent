---
name: "astrid"
short_description: "Astrid — the file-based toolkit for making video, image, and audio art alongside a human."
description: "Use for Astrid orientation: connect to the workspace runtime, find or open project work, choose the creative-work route, and use Hivemind for shared knowledge."
---

# Astrid

Astrid is a client and pack toolkit for making creative work with a human.
The workspace runtime owns projects, media, timelines, tasks, runs, receipts,
and events. The checkout supplies the CLI, SDK, and capability packs; it is not
the product database or a second run store.

This `_core` directory is a manifestless gateway, not a runtime pack. Its
authored guidance lives under `docs/` and is composed by Astrid's existing
skill sync; pack-local `docs/SKILL.md` files are discovered from their declared
manifest pointers.

Choose the route by the requested result. Read only that route's guidance:

| Request | Start here |
| --- | --- |
| Generate with the user's own ComfyUI: "my ComfyUI", "my saved workflow", local generation | [Run on your own ComfyUI](../../vibecomfy/docs/SKILL.md#run-on-your-own-comfyui) |
| Find a project, inspect a run, or open a render | The CLI recipes below |
| Change timing, text, layout, effects, or existing timeline clips, then publish and point the user to the editor (render only if asked) | [Timeline editing](../../rendering/docs/SKILL.md) |
| Generate images, video, or audio (including Foley); understand, assemble, compare, or publish work | [Creative work](creative-work/SKILL.md), then the relevant pack |
| Reuse a character or other saved subject | [References](../../media/docs/references.md) |
| Build your own reusable tool, workflow, or visual element | [Pack builder](pack-builder/SKILL.md) |
| Find Banodoco advice, model settings, or workflow precedents | [Hivemind contract](../../../../docs/reference/hivemind-pack-contract.md) |

Using an existing workflow and building a reusable extension are different
requests: pack builder handles the latter, including deciding what to reuse.

On a new machine, open Terminal on macOS or Linux, or PowerShell on Windows,
then run the install line for its OS. Running the line again repairs or restarts
Astrid and keeps existing work.
On macOS or Linux, run:

```sh
curl -fsSL https://astrid.haus/install | sh
```

On Windows, run in PowerShell:

```powershell
irm https://astrid.haus/install.ps1 | iex
```

The installer checks that Astrid is healthy and opens astrid.haus itself. Tell
the user to choose Allow if Chrome or Edge asks to reach their computer, then
confirm astrid.haus shows Connected. That is the end of setup. To generate
with a ComfyUI the user already runs, follow [Run on your own
ComfyUI](../../vibecomfy/docs/SKILL.md#run-on-your-own-comfyui); it covers
Wan2GP results too. Do not install or clone `astrid-app`; it is for app
developers.

## Commands for outside agents

Synced skills use the absolute path of Astrid's own Python, so their
commands work from a terminal without changing PATH.

## Start here

Before acting on a project from a terminal, run `python3 -m astrid projects current`.
If no project is selected, ask the user which project to use. Always name the
project in your reply.

## Preferences and project documents

New Astrid agent sessions already include a bounded snapshot of the user's
preferences and the selected project's preferences. Read them again when the
project changes or before editing. Current explicit instructions take
precedence over project preferences, then user preferences, then ordinary
defaults. Temporary choices stay in the conversation. Save a lasting default
only when the user asks to remember it; "for this project" means project scope,
while "remember my default" means user scope. One exception: after the first
successful run on the user's own ComfyUI, save its machine facts (URL, folders,
saved workflow names) to the user scope and say so; see the VibeComfy skill.
Never put passwords, API keys, or other secrets in preferences; they are
included in model prompts.

Use `python3 -m astrid preferences` for both scopes,
`python3 -m astrid preferences user` for the user document, and
`python3 -m astrid preferences project` for the selected project's
preferences. To change lasting guidance, read the correct scope, check it out
to a new file, edit it, then check it in. For example:

```bash
python3 -m astrid preferences checkout --scope user --file /tmp/prefs.md
python3 -m astrid preferences checkin /tmp/prefs.md
```

Read the [preferences and project documents guide](../../../../docs/guides/preferences-and-documents.md)
for conflict recovery and the full CLI surface. Do not edit Runtime storage directly.

Use `python3 -m astrid documents list` to list reusable project documents and
`python3 -m astrid documents create --kind KIND --file FILE` to create one. The `show`,
`checkout`, and `checkin` verbs read a document and check a working copy in
and out. Generic documents do not enter prompt context automatically.

**Establish the current project when opening or switching project work.**
Discover the requested project with `projects list`, then persist it with
`projects select <project>` (workspace scope by default). Commands such as `runs open` use that selection without `--project`. Some
family parsers still require an explicit `--project`; check their help.
Use explicit `--project` when the command requires it or when inspecting another project
without changing the current selection.

The persisted selection is the default for project-bound SDK invocations too:
an explicit project overrides it. Read-only Hivemind search and retrieval do
not need a selected project; they can run at workspace scope.

```bash
python3 -m astrid projects list
python3 -m astrid projects select <project>
python3 -m astrid runs open
```

`--json` is optional machine-readable output, not a required argument or input
payload. Use it when structured results help; ordinary commands and examples
for users can omit it.

After installation, the commands below work with the installed Runtime. For a
new machine, follow the setup path above and open astrid.haus.

```bash
curl -fsSL https://astrid.haus/install | sh -s -- --doctor
```

On Windows PowerShell use
`& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --doctor`.

If Astrid isn't running, run the install line again.

The gateway is the stable command surface. Begin with its census, then inspect
one family when needed:

```bash
python3 -m astrid --help
python3 -m astrid projects --help
python3 -m astrid timelines --help
```

The top-level families are `projects`, `timelines`, `media`, `tasks`, `runs`,
`doctor`, and `backup`. `timelines shots` and `media references` are nested
mounts. Packs and capabilities are invoked through the SDK, not as extra
gateway verbs.

## Find and open work

Use the runtime to discover work. Do not infer current state from checkout
filenames, modification times, a local database, or event-log files.

```bash
python3 -m astrid projects list --json
python3 -m astrid projects current --json
python3 -m astrid timelines list --project <project>
python3 -m astrid runs list --project <project>
```

To return to the latest successful render for a project:

```bash
python3 -m astrid projects select <project>
python3 -m astrid runs open
```

Use an exact run id when one is known. Use `--default-timeline` when the
request is to open the project's main/default timeline render, and
`--timeline <slug-or-id>` for another named timeline:

```bash
python3 -m astrid runs open <run-id>
python3 -m astrid runs open --default-timeline
python3 -m astrid runs open --timeline <timeline>
```

`runs open` selects a successful `rendering.render` run. It does not claim that
the render is editorially approved; the runtime has no separate promotion
pointer yet. If no matching render exists, the command fails clearly.
`opened` says whether the OS opener ran. When it did not, `open_error` gives the reason and
`local_path` is the file to open by hand; `--no-open` materializes without opening.

For paused work, include archived resources in discovery and recover through
the runtime:

```bash
python3 -m astrid timelines list --project <project> --include-archived --json
python3 -m astrid timelines recover <timeline> --project <project> --json
python3 -m astrid media references list --project <project> --include-archived --json
```

Use `timelines show`, `history`, `diff`, `visualize`, and `render` for
timeline work; render and composed `visualize` frames only when the user asks,
and otherwise point them to the editor link. Whole-document timeline saves are retired; edits are published
through the detached authoring bundle. Read the [timeline editing skill](../../rendering/docs/SKILL.md)
for the canonical authoring, Remotion, visualization, effects, and playback
route. Use `tasks` and `runs` to inspect or manage admitted work:

```bash
python3 -m astrid tasks list --project demo --json
python3 -m astrid runs show <run-id> --project demo --json --evidence
python3 -m astrid runs events <run-id> --project demo --json
```

For a failed run, start with `runs show --evidence`; if the failure detail is
empty, inspect `runs events`. Correct the reported cause before retrying, and
follow the runtime's retryability guidance. See
[debugging](../../../../docs/guides/debugging.md) for deeper diagnosis.

Every product command supports `--json` as its machine-readable surface. The
normal product result is an `ok` / `data` / `error` / `receipt` /
`idempotency_key` envelope. Exit code `0` means success, `1` a typed
runtime/SDK error, and `2` a usage or parse error.

## Shared knowledge: Hivemind

For community practice, model behavior, settings, known failures, and workflow
precedents, read the managed external Hivemind pack's skill when it is
installed. Use its search capability, then retrieve the full source behind
useful hits before presenting community advice. The
Astrid-side v2 compatibility and contributor-write contract is documented in
[`docs/reference/hivemind-pack-contract.md`](../../../../docs/reference/hivemind-pack-contract.md).

The skill and executors belong to that pack. If the default pack or its skill is
missing, follow the pack's documented installation recovery; do not redirect to
an unrelated personal skill installation. Hivemind contributions are public;
preview the payload and obtain explicit publication authorization before writing.

## Runtime and SDK boundary

All live capability admission and lifecycle transitions cross the workspace
runtime. Use the SDK for packs.

Connect explicitly through the canonical launcher before invoking a pack:

```python
import json
from astrid.sdk import AstridClient

with AstridClient.open_from_launcher() as client:
    result = client.invoke_result(
        "hivemind.search",
        kind="executor",
        inputs={"query": "MiniMax H3 dialogue", "limit": 10},
        wait=True,
    )
    if result.ok:
        artifact = next(a for a in result.outputs["artifacts"] if a["name"] == "results")
        matches = json.loads(client.media.read_bytes(artifact["digest"]))
```

Check `result.ok` and the runtime result/artifacts. The module-level SDK
helper requires a connected `client`; importing `astrid.sdk` alone does not
connect or start a runtime.

Read the selected pack's `SKILL.md` and capability `STAGE.md` before invoking
work. Pass `kind` explicitly and bind execution to a project when the
capability contract requires it. Do not invoke `run.py` modules directly; do
not edit runtime state, receipts, or event streams by hand.

Capability discovery is available through `sdk.discover()`. A generated
[capability catalog](references/capabilities.md) is available when an exact id
is needed.

## Safety and ownership

Use Astrid's configured, runtime-owned storage for generated assets. Never
derive durable output paths from the shell's current directory or create a
workspace-relative `runs/` tree as an implicit export. An alternate output
location must be explicitly requested by the user. Keep run evidence and
supporting artifacts associated with their runtime project and run; a local
file alone does not establish database registration. Open managed outputs
through the runtime rather than downloading another convenience copy.
Do not commit source media, rendered videos, local dependency environments, or
secrets. Runtime-owned media and timeline references must be changed through
the CLI/SDK. A route that the connected runtime does not expose returns a
typed `unavailable` result; do not fall back to a local store or invent a
second authority.

Resolve this skill's installed symlink before following checkout-relative links.
Creative work and pack builder are linked guidance within this core skill;
they do not introduce new runtime packs.
