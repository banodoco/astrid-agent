---
name: astrid-onboarding
description: "Connect the user's ComfyUI and set their default generation workflow per slot (image, edit, video, music, voice, foley): compare Astrid's default with what they already use, save the choices they approve, prove one real image, and show the result. Use after setup shows Connected, whenever the user asks to review or change default models or workflows, or when ComfyUI moved or changed."
metadata:
  short-description: "Connect ComfyUI and set default generation workflows per slot"
---

# Astrid onboarding: connect ComfyUI and set generation defaults

<!-- Ships as astrid/packs/_core/docs/onboarding/SKILL.md (task D3). Command names follow PROJECT-FINAL.md section 5. -->

Goal: the user ends with a connected local ComfyUI, one working default per slot that keeps their own best practice, and one real
image that proves it. Defaults always run on the user's own ComfyUI (theirs or Astrid-managed). A hosted service or RunPod is never a
saved default. You can run this skill at any time; running it again is safe and simply reviews the current state.

## Rules
- **Read first, write on a yes.** Writes are only the commands below. Show the full set of proposed changes, then get one explicit
  approval for that set. Do not save anything the user did not approve.
- **Keep their practice.** If the user already has a workflow they rely on for a slot, propose it before Astrid's default. Readiness
  alone does not tell you what they prefer: ask which one is their usual workflow.
- **Consent for installs, downloads and launches.** Show the plan (what, size, disk) and get an explicit yes before `engines install`,
  any repair, or starting the user's own ComfyUI.
- **No secrets in chat.** Never read, print or ask for a key value. If the user wants a provider key, give them
  `astrid-credential set <NAME>` to run in their own terminal. Keys only enable explicit one-off hosted runs, never defaults.
- **Honest readiness.** "Ready" means nodes and model files are present on that ComfyUI, not that a generation succeeded. Only the
  first-generation proof (step 6) shows that it works.
- **Only real commands.** Run a fix only if it appears as `argv` in a `fixes` entry. If an issue has no fix argv, tell the user exactly
  what is missing (from `issues`) and that Astrid cannot repair it automatically yet. Never invent a command.
- **One-off is not a default.** "Use X this time" → `--entry X` on that call. Save only when the user says "always", "by default",
  "remember", or similar.

## 1. Check the connection
Run `astrid status`. If the Runtime or workspace is not reachable, stop and follow the setup skill (`docs/setup/SKILL.md`).

## 2. Connect ComfyUI
```sh
astrid generate targets --json
```
- **Exactly one ComfyUI found:** save it: `astrid generate targets --save --json`.
- **Several found** (`ambiguous_target`): ask which one, then `astrid generate targets --target <url> --save --json`.
- **None found, but they have ComfyUI:** ComfyUI Desktop usually listens on :8000, the portable build on :8188. Ask them to start it
  (or ask before using a launch fix from `fixes`), then rerun `generate targets`.
- **No ComfyUI and they want one:** show `astrid engines install comfyui --dry-run` (downloads, sizes, disk). Install only on a yes:
  `astrid engines install comfyui`, then `astrid engines status comfyui`.
- **They decline:** explain that local generation needs a ComfyUI, skip to step 7 and show the current state.

The saved `[comfyui]` record (plus the install receipt for a managed install) is the durable record of the connected instance. If
ComfyUI later moves, stops or changes, generation reports `target_unreachable`; repeat this step. Defaults store entry ids, not
addresses, so they survive the move.

## 3. Inspect (read-only)
```sh
astrid preferences defaults show --json
```
Per slot you get: `configured` (the user's saved default, if any), `builtin` (Astrid's default ids), `selected` (what a run would use
now) with `source` (`user`, `builtin`, `fallback` or `none`), `candidates` (local entries of that kind and mode, with `ready`), and
`readiness` (`status`, `issues`, `fixes`). At the top: `target` and `library` (names of workflows saved in their ComfyUI).

## 4. Propose: ours versus theirs
Show one short table, in plain words:

| Slot | Astrid default | You have | Proposal |
|---|---|---|---|
| Image | Qwen-Image 2.1 Turbo, ready on your ComfyUI | `portrait-flux` (ready) | Keep `portrait-flux` |
| Video | LTX-2.3, missing a node pack and a VAE | none | Astrid default once repaired |
| Foley | no local default yet | none | Leave unset |

- Mention `library` names that look like a slot's workflow and ask: "Is `flux_portrait.json` your usual image workflow?" Import only
  the ones the user picks.
- Where the user has nothing, propose Astrid's default; if it is not ready, say what is missing and offer its fix (with consent).
- Then ask for one approval of the whole set.

## 5. Set the approved defaults
- Their existing entry: `astrid preferences defaults set <slot> <entry-id> --json`.
- A library workflow they picked: import it first, from the readable file (local ComfyUI: `<base>/user/default/workflows/<name>`):
  `astrid generate workflow "<file>" --as-entry <short-id> --json`. Review the `needs`/`binds` it reports and resolve any ambiguity with
  the user (never guess the audio mode), then `set <slot> <short-id>`. Tell them the imported entry is a snapshot of that file.
- Astrid's default and no override saved: do nothing. If an override exists and they now want Astrid's: `astrid preferences defaults reset <slot>`.
- `set video X` saves every video mode X supports and prints them. `set` refuses hosted-only entries, entries of the wrong kind or mode,
  ambiguous ids, and (for foley) entries without a video input. Explain the refusal; do not work around it.
- After a repair or install, rerun `show --json` and confirm the slot's `readiness.status` is `ready`.

## 6. First-generation proof (always for image)
```sh
astrid generate image --prompt "a red paper boat on a calm pond, morning light" --project <project> --json
astrid tasks follow <task_id>
astrid tasks show <task_id> --json
astrid media list --project <project> --json
```
Do not pass `--entry`: the point is to prove the default. **Success means all three:** the task is `COMPLETED` in the user's Runtime,
the generation appears in `media list`, and the output file exists locally (also when the GPU is remote). Show the user the file path
(or a preview). A ComfyUI render without these is not success: report the actual error, its `issues` and `fixes`, and stop.
Offer the same proof for video, music or voice only if the user wants it (these are slower), using `generate video|music|voice --prompt "…"`.

## 7. Finish with `show`
```sh
astrid preferences defaults show
```
Summarise each slot: what it uses and where that came from (yours or Astrid's), anything still not ready and its fix, and the proof's
file path. Tell them how to change things later: "Ask me, or run `astrid preferences defaults set <slot> <entry>`; `reset <slot>`
returns to Astrid's default."

## Later updates (any agent)
- "Make X my default for video": check X in `astrid preferences defaults show video --json` (it must be a local entry of that kind and
  mode), then `set video X`. If X is not ready, say so and give its fix.
- "Remember that I like X" about a generation model or workflow: use `preferences defaults set`, never Markdown preferences.
- A run fails with `missing_dependency`, `target_unreachable` or `input_rejected` and `details.default_source: "user"`: Astrid does not
  fall back silently. Show the issue and its fixes; run a repair only with consent, or offer `reset <slot>`; for an unreachable target, go to step 2.
- Never change a default because another entry happened to work once. Change one only when the user asks.

## Failure handling
| Situation | Do |
|---|---|
| Runtime unreachable, or `show` fails to read defaults | Stop; follow setup troubleshooting (`astrid status`) |
| Install or repair fails | Report the actual error and log path; leave defaults unchanged |
| Import reports unbound or ambiguous inputs | Ask the user; do not guess modes (`tts`/`music`/`sfx`) |
| `set` hits a version conflict (changed elsewhere) | Rerun `show`, tell the user what changed, ask again |
| First generation fails | Show the task id, issues and fixes; do not claim success |
