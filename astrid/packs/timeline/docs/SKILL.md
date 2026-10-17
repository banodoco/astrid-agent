---
name: timeline_editing
description: Find, inspect, edit, preview, validate, and publish an existing runtime timeline through Astrid's current CLI and SDK.
---

# Timeline editing

Follow **find → show → edit → check → publish**. The first edit creates a private working copy; `show`, `check` and `visualize` read it until publish. `demo/main` is a disposable fixture; use your selected project and timeline for real work.

## The five-command loop

Use the address returned by `find` in every step:

1. Find: `python3 -m astrid timelines find main rocket --project demo`
2. Show: `python3 -m astrid timelines show main c1.rocket --project demo`
3. Edit: `python3 -m astrid timelines edit main --project demo --draft working --clip c1.rocket --on '"went"'`
4. Check: `python3 -m astrid timelines check main --project demo --draft working`
5. Publish: `python3 -m astrid timelines publish main --project demo --draft working -m "Move rocket to went" --json`

## Addresses: one shared vocabulary

| Address | Meaning |
|---|---|
| `c1` · `c1.rocket` · `c1.rocket.x` · `a-rocket` · `a-rock` | Cut, layer, parameter, exact clip id, or id prefix. |
| `"went"` · `"went viral"` · `"went" in s1 #2` | Word or phrase; scope to a line and choose a repeated occurrence. |
| `after "went" +8f` · `beat 2 after "went"` | Word boundary plus frame offset, or a beat relative to a word. |
| `2.4` · `1:02.4` · `@93.5` · `c1..c2` · `"went".."now"` · `1..3` | Seconds, m:ss, explicit time, or cut/word/time range. |
| `rocket` · `R` · `"Live."` | Bare layer, asset key, or on-screen text. |

`--on` and `--until` take a moment. Lengths accept seconds (`0.6`, `0.6s`) or frames (`18f`). Prefer word anchors when speech should carry a layer through re-recording.

## Change things: one command per recipe

| Want | Command | Scenario |
|---|---|---|
| Start on a word | `timelines edit main --project demo --draft working --clip c1.rocket --on '"went"'` | [timing.on.rocket-went][sc-on] |
| Hold until a word | `timelines edit main --project demo --draft working --clip c2.card --until '"now"'` | [timing.until.card-now][sc-until] |
| Nudge by frames | `timelines edit main --project demo --draft working --clip c1.rocket --nudge-frames -3` | [timing.nudge-frames.rocket][sc-nudge] |
| Set a parameter or look | `--clip c1.rocket --set x=120` · `--clip c1.rocket --opacity 0.6` | [look.set.rocket-x][sc-set] · [look.opacity.rocket-60][sc-look] |
| Clear a parameter | `--clip c1.rocket --clear x` | [look.clear.rocket-x](../../../../tests/editing_scenarios/look/look.clear.rocket-x.yaml) |
| Swap one picture or every use of its key | `--clip c1.rocket --swap-asset P` · `--replace-asset R=task:replace-fixture/sprites#0` | [look.swap-asset.rocket][sc-swap] · [look.replace-asset.everywhere][sc-replace] |
| Add a layer like another | `--command '{"op":"timeline.add","select":null,"args":{"from":"occurrence:occ-B/clip:b-type","at":"now","clip_id":"b-type-copy","relation_choices":[{"target":"occurrence:occ-B/clip:b-type","edge":"start","choice":"re-anchor","anchor":"now","offset_seconds":0.0},{"target":"occurrence:occ-B/clip:b-type","edge":"end","choice":"keep"}]}}'` | [look.add-like.card][sc-add-like] |
| Add music or an effect | `--add-media Q --track vo --on '"viral"' --for 0.3 --id blip` | [audio.add.sfx-viral][sc-sfx] · [audio.add.media-bed][sc-music] |
| Remove a layer | `--clip c2.card --remove` | [look.remove.card][sc-remove] |
| Split a cut | `--split c1 --on '"went"'` | [structure.split.c1-went][sc-split] |
| Move a cut with its layer offsets | `--cut c2 --on '"now"'` | [structure.cut-on.c2-now][sc-cut-move] |
| Crossfade two audio clips | `--crossfade A B --over 0.5` | [audio.crossfade.music][sc-crossfade] |
| Apply many sheet edits together | `timelines apply main edits.sheet --project demo --draft working` | [code.skill.batch-edit][sc-sheet] |

## Look before publishing

| View | Try |
|---|---|
| One frame | `timelines visualize main --project demo --draft working --preset frame --at '"went"'` |
| A cut | `timelines visualize main --project demo --draft working --preset cut --cut c1` |
| Whole film | `timelines visualize main --project demo --draft working --preset overview` |
| Before and after | `timelines visualize main --project demo --draft working --compare published` |
| Will it render? | `timelines visualize main --project demo --draft working --mode composed --at '"went"'` |

`visualize` uses the shared scene for previews; `--mode inputs --plan` inspects the declared placement without capturing frames. `timelines render` produces a managed output; open the returned artifact path in your media viewer.

## Narration and re-flow

Replace a narration take with `--replace-line LINE --take FILE --words WORDS.json`; use `--from-script SCRIPT.json` for an ordered script re-flow. Script segments need stable occurrence and clip identities; exact text tokens bind to generated word timings. Re-check the full draft after re-flow.

For a text-only narration change, use `timelines lines <timeline> --project
<project> --line <id> --text "New line text"`; it keeps the current take and
word timings. Apply a JSON narration script with `timelines lines <timeline>
--project <project> --from-script <path>`. Repeat `--takes <folder>` or pass
several folders after one `--takes`; later folders override earlier matches.
The source path and SHA-256 are saved with the working timeline and shown by
`timelines status`. `timelines script <timeline>` remains a read-only view;
`--revision`, `--occurrence`, and `--kind` select its read.

If an old word disappears, `check --all` reports orphaned layers. Inspect the sheet, choose a surviving word or time, and re-anchor each layer with `--clip ADDRESS --on MOMENT`; then check again. This receiver has no automatic suggestion-acceptance option. Remove unwanted picture content with `--remove-line LINE --visuals remove`.

## Publish, history, undo, restore, render

Publish once the draft is checked. Keep the returned `accepted_revision_id`: the observed head may move later and does not replace that receipt. History lists Runtime revisions; the local log records edits. Local undo/redo changes the working copy. On a clean copy, undo (also spelled `revert`) after your own publish stages its inverse as guarded edits; publish that edit normally. Restore replaces the whole revision and leaves detached drafts untouched. Set `ASTRID_ACTOR=agent:<your-name>` to attribute history.

```bash
python3 -m astrid timelines history main --project demo --limit 10
python3 -m astrid timelines log main --project demo --verify
python3 -m astrid timelines undo main --project demo --draft working --steps 1
python3 -m astrid timelines redo main --project demo --draft working --steps 1
python3 -m astrid timelines restore main --project demo --from @prev --idempotency-key restore-demo
```

Use `--clear KEY` to remove a value and restore the element default; `--set KEY=null` is a literal null and can be refused by the schema. A sheet accepts `edit {"op":...,"select":...,"args":...}` lines for operations outside its table. `gap 0` preserves a music lead-in; `timeline.close-gap` closes silence before a spoken word.

## Working with others

Watch the saved receipt and public head separately. Refresh rebases the draft's recorded commands through the sync API. Inspect any same-property or dependent conflict, keep the draft, and choose values against the latest head; never force a stale write.

```bash
python3 -m astrid timelines publish main --project demo --draft working -m "Adjust the selected rocket" --json
python3 -m astrid timelines status main --project demo --draft working --watch --poll-seconds 2 --timeout-seconds 3600 --json
python3 -m astrid timelines checkout main --project demo --draft working --refresh --json
python3 -m astrid timelines status main --project demo --draft working --watch --poll-seconds 2 --timeout-seconds 3600 --json
```

## Python: the same loop

```python
from astrid.sdk.timeline_checkout import Checkout
tl = Checkout.open("demo", "main")
print(tl.word("went")); print(tl.clip("c1.rocket"))
with tl.edit("Move rocket"): tl.clip("c1.rocket").enter_at('"went"')
print(tl.check())
tl.publish("Move rocket to went")
```

## When it goes wrong

| You see | Try |
|---|---|
| No search hits | Search a shorter phrase; use the returned address. |
| Ambiguous word | Scope it: `"went" in s1`; inspect with `show`. |
| Empty selector | Copy the exact address printed by `find` or `show`. |
| Unknown parameter | Use a declared key; check `edit --help-all`. |
| Orphan after re-flow | Inspect `check --all` and the sheet; deliberately re-anchor or remove it. |
| Head moved | Run `status --watch`, then `checkout --refresh`; inspect conflicts before publishing. |
| Publish conflict | Keep the draft, choose values for the conflicting paths, check, then retry. |
| Render unavailable | Inspect `status --text --render`; rerun `timelines render --help` for current options. |

## Reference and extending

Add an SDK mutator in `astrid/sdk/timeline/checkout.py` and its `_commands.register(...)` tuple; `astrid/sdk/timeline/commands.py` owns that registry and `EDIT_FLAG_OPERATIONS`. Add the CLI flag in `astrid/packs/timeline/cli.py`; `tests/sdk/test_timeline_commands.py` checks the flag mapping. Update `tests/parity/edit-command-flags.json`, regenerate the [flag-to-operation table](references/edit-command-flags.md) with `python3 -m tools.parity.edit_flag_reference`, and verify it in `tests/sdk/test_edit_command_contract.py`. For a lint check, follow `astrid/packs/rendering/docs/references/visualize-extend.md`: export `CHECK = Check(...)` from `astrid/packs/<pack>/rendering/visualize_layers/<name>.py`; discovery and `register_check` live in `astrid/packs/rendering/actions/timeline_visualize/layers/base.py`. The guide pattern is tested by `test_the_guide_examples_register_and_run` in `tests/packs/rendering/test_timeline_visualize_motion_views.py` and listed by `python3 -m astrid timelines lint --list-checks`. Add an editing scenario under `tests/editing_scenarios/` and cover it in `tests/editing_conformance/test_conformance.py`. `timelines edit --help` is the compact routine list (≤40 lines); `--help-all` shows every option.

The batch scenario previews before applying; both commands are one transaction and one undo step. A failed batch rolls back. The [preview scenario][sc-sheet] and [apply scenario](../../../../tests/editing_scenarios/code/code.skill.batch-edit.yaml) execute these exact commands. A single structured edit can preview without writing; the [structured-edit scenario](../../../../tests/editing_scenarios/code/code.skill.structured-edit.yaml) covers it.

```bash
python3 -m astrid timelines checkout main --project demo --draft working
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"timeline.batch","select":null,"args":{"commands":[{"op":"clip.set","select":"c1.rocket","args":{"x":125}},{"op":"clip.set","select":"c1.rocket","args":{"y":40}}]}}' --dry-run
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"timeline.batch","select":null,"args":{"commands":[{"op":"clip.set","select":"c1.rocket","args":{"x":125}},{"op":"clip.set","select":"c1.rocket","args":{"y":40}}]}}'
python3 -m astrid timelines check main --project demo --draft working
python3 -m astrid timelines visualize main --project demo --draft working --mode inputs --plan
```

```bash
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"clip.set","select":"c1.rocket","args":{"x":120}}' --dry-run
```

[sc-on]: ../../../../tests/editing_scenarios/timing/timing.on.rocket-went.yaml
[sc-until]: ../../../../tests/editing_scenarios/timing/timing.until.card-now.yaml
[sc-nudge]: ../../../../tests/editing_scenarios/timing/timing.nudge-frames.rocket.yaml
[sc-set]: ../../../../tests/editing_scenarios/look/look.set.rocket-x.yaml
[sc-look]: ../../../../tests/editing_scenarios/look/look.opacity.rocket-60.yaml
[sc-swap]: ../../../../tests/editing_scenarios/look/look.swap-asset.rocket.yaml
[sc-replace]: ../../../../tests/editing_scenarios/look/look.replace-asset.everywhere.yaml
[sc-add]: ../../../../tests/editing_scenarios/look/look.add.sprite.yaml
[sc-add-like]: ../../../../tests/editing_scenarios/look/look.add-like.card.yaml
[sc-sfx]: ../../../../tests/editing_scenarios/audio/audio.add.sfx-viral.yaml
[sc-music]: ../../../../tests/editing_scenarios/audio/audio.add.media-bed.yaml
[sc-crossfade]: ../../../../tests/editing_scenarios/audio/audio.crossfade.music.yaml
[sc-remove]: ../../../../tests/editing_scenarios/look/look.remove.card.yaml
[sc-split]: ../../../../tests/editing_scenarios/structure/structure.split.c1-went.yaml
[sc-cut-move]: ../../../../tests/editing_scenarios/structure/structure.cut-on.c2-now.yaml
[sc-sheet]: ../../../../tests/editing_scenarios/code/code.skill.batch-edit.yaml
