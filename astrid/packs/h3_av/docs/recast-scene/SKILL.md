---
name: h3-av-recast-scene
description: Plan and make a scene with replaced characters and re-voiced dialogue.
---

# Recast and re-voice a scene

Draft, 17 November: open questions are marked. The overarching skill for taking an existing scene and changing who is in it and what is said, while keeping the real performance where it can be kept. It is a planning method first and a generation method second. Each generation method it names has its own task skill, proven on a rented machine before this skill points to it.

Astrid commands here run through the launcher; its path on each OS is in the [astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths).

Skills this one leans on, by stage:
- Script and low-fidelity cut: [timeline](../../../timeline/docs/SKILL.md), [narrated timelines](../../../timeline/docs/narrated/SKILL.md), [creative work](../../../_core/docs/creative-work/SKILL.md).
- Mapping the cut: [timeline-visualize](../../../timeline/docs/visualize/SKILL.md) and [timeline-visualize-advanced](../../../timeline/docs/visualize-advanced/SKILL.md) (`timelines visualize`), [timeline-show](../../../timeline/docs/show/SKILL.md), [shots](../../../timeline/docs/shots/SKILL.md), [transcripts](../../../timeline/docs/transcripts/SKILL.md).
- The per-shot plan: [shot plans](../../../timeline/docs/shots/SKILL.md#shot-plans) (`timelines shots plan set/show/list`, `visualize --shot-plans`).
- Methods and requests: [H3](../SKILL.md) ([workflow choice](../SKILL.md#workflow-choice), [mask semantics](../SKILL.md#mask-semantics), [request shape](../SKILL.md#request-shape), [validation and approval](../SKILL.md#validation-and-approval)).
- Running on a GPU: [RunPod generation machines](../../../runpod/docs/SKILL.md#runpod-generation-machines).
- Approval and assembly: [review](../../../timeline/docs/review/SKILL.md), [series](../../../timeline/docs/series/SKILL.md), [rendering](../../../rendering/docs/SKILL.md).

Each shot's plan lives on the shot itself, as `metadata.plan` on the timeline, and is read and written only with the [shot plan commands](../../../timeline/docs/shots/SKILL.md#shot-plans). Stages 2-6 below say what each one writes there and reads from there; the plan is the shot list, not a separate table or file.

## When to use it

You have source footage (a film scene, an earlier cut), a new script, and new or changed characters. Some lines are spoken on camera, some over other pictures. Some shots exist as real footage, some only as stills or drafts.

## The principle

**Get the sequence right cheaply, then spend on generation, one method per shot, voice first.**
- Iterate on timing and picture order with a temporary voice and stills. That costs nothing and is where most creative decisions are made.
- Generate only once the sequence is approved, and only what each shot needs.
- Make the whole voice performance first, as one take, so the speaker sounds like one person across every cut. Then the picture follows the voice: cut lengths come from the words, and mouths are made to match the words.

## Stage 1 - Script and low-fidelity cut, with the owner, inside Astrid

Work with the owner on the script: every line, every speaker, and what is shown at each moment. Build it as a timeline in Astrid straight away: a temporary system voice for every line, and source footage or stills for the picture. Watch it in the Astrid editor and edit together until the owner approves the order and timing. No GPU. Most creative decisions happen here, because changing them costs nothing.

Output: an **approved low-fidelity cut** (a timeline revision) and the **final script**.

## Stage 2 - Map the approved cut (look, don't assume)

With `astrid timelines visualize` on that timeline, not on an exported file:
- `--preset overview`: one tile per cut. Name each: who is on screen, real footage or still, whose mouth is visible.
- `--cut N` and `--range` around each speaking shot: does the mouth move? What is in reflections, backgrounds, hands?
- Words under the picture: refresh the transcripts of the temporary voice (`astrid timelines transcripts refresh <timeline> --project P --dry-run`, then `--run-missing --placement runpod`), then look with the word tiles, so each line has a time.
- Region zoom on details that will change (lenses, mouth). If the tool cannot show what you need, that is a tool gap: fix it or file it. Do not fall back to outside tools.

Output: the **sequence map**, a table with one row per cut: time, picture, who is visible, line and words with times, source (footage, still, generated).

Plan: once the approved cut is stored as shots, record what you saw on each shot: `timelines shots plan set <shot> --project P --timeline T --set on_screen=Morpheus --set line="You have two options."`. The line's text itself stays in the shot's `voiceover_script` binding; `line` is the label.

## Stage 3 - Shot list: pick a method for each shot

For each cut, ask what must change, and pick the least invasive method that does it. A shot can need more than one.

| Method | Use when | H3 task skill |
|---|---|---|
| **V. Voice over** | The line is heard, but the speaker's mouth is not on screen | Voice track (stage 4) only |
| **LC. Lip sync by continuation** | Real footage of the speaker talking exists, and the new line can follow on from it: the shot starts with them saying something real, then continues with them saying the new line | Continuation (proven on one take) |
| **LM. Lip sync by mask** | The speaker must say the new line inside the real shot's own frames (fixed length, fixed pose), often with other masked changes such as reflections | Masked edit of the mouth; how the voice gets in is open (below) |
| **R. Region replace** | Something inside real footage must change: a reflection, a held object, a background figure | Masked region edit with a reference image of what should appear |
| **M. Still to motion** | The shot exists only as a still or a design frame and must move | Image-to-video from the still, with the character references |
| **E. Extend** | The real shot is shorter than its words | Continuation from the real footage (keeps the performance, adds length) |
| **K. Keep** | Nothing changes | None |

Rules of thumb:
- Prefer masks on real footage over regenerating a whole shot: the real eyes, head movement and lighting survive.
- LC is the safe default when the shot can grow longer; LM when its frames must stay as they are.
- If a speaking shot has no real footage, start from the nearest real footage of the speaker (E), then apply L and R. Avoid animating a still face into speech.
- Fixed references, declared once: each character's reference images and the speaker's real voice. Every pass uses the same ones.

Output: the **shot list**, the sequence map plus a method column, inputs (masks, references, voice slice) and a check for each shot.

Plan: for every shot, write `method`, `intent`, `check`, the fixed `inputs.references` (and `inputs.masks` once made) and `status=planned`, e.g. `timelines shots plan set <shot> --project P --timeline T --set method=LM,R --set inputs.references=<id>,<id> --set status=planned`. `K` shots get `method=K` too, so nothing is left unplanned. Check with `timelines shots plan list --project P --timeline T` (`unplanned` must be empty) and look with `timelines visualize T --project P --preset overview --shot-plans`.

## Stage 4 - The voice track

- Reference: the speaker's real voice, a clean clip of their real lines with its transcript.
- Generate the whole script as one continuous performance (voice-clone entry in `astrid generate`).
- Refresh the transcripts (`astrid timelines transcripts refresh <timeline> --project P --run-missing --placement runpod`, after a `--dry-run`), so the new voice has timed words; replace the temporary voice on the timeline, and re-time the cuts to the words (the refresh report lists any cut that splits a word). Look again (Stage 1 views).
- Owner listens. This is the gate before any picture generation.
- Plan: once the voice track is approved, write each speaking shot's slice of it: `timelines shots plan set <shot> --project P --timeline T --set inputs.voice.media=<voice track media id> --set inputs.voice.range=START,END`. The range is seconds into that voice track media (from its timed words), not timeline time, and needs the media.

Fallback, if lip sync to a fixed voice does not work in H3: generate each on-camera line with its own voice by continuation, which is proven. Then use that voice as the reference for the off-camera lines.

## Open: how the voice gets into a masked lip sync (LM)

Not settled; we are building this skill as we go. The masked edit today keeps the shot's own sound and takes no reference image. Candidates, each with the cheapest test that would settle it:
1. **Voice in the lead-in.** Put a snippet of the target voice (from the voice track) just before the masked span, as unmasked context. H3 then generates the masked span's audio and mouth together, in that voice, from the prompt's words. Test: one run on the 12-16 s close-up, with lead-in audio swapped for the voice-track slice.
2. **Fixed replacement audio.** Feed the shot's slice of the voice track as the audio, held fixed, and regenerate only the mouth pixels to match it. Needs a new input on the masked edit. Test: the same shot, mouth mask only; does the mouth follow?
3. **Voice as a reference.** A reference-audio input (H3's reference route has one, not exposed on edits). Test after it is exposed.
4. **Unmasked audio.** Mask the mouth and lenses in the picture but let all of the shot's audio regenerate from the prompt and the surrounding real sound. The simplest, but the voice may drift from the voice track.
Also open: mouth and lenses in one pass (two masks, one run, cheaper) or two passes (independent, easier to judge)? And how the edges of the masked span join the real footage in sound and picture.

## Stage 5 - Picture passes, shot by shot

- For a masked edit, make the mask with `astrid media mask`, check `--preview-on` over the shot you will edit, and put the media ID in `edit[].mask.asset`. See the [02c lens example](../SKILL.md#make-a-mask).

- Order: the riskiest, most reused method first, on its best shot. One paid run proves a method; then repeat it across shots.
- Each pass: a dry run that is the real path stopped before the GPU; one waiting claim with a spend cap and deadline; submit; read back; release.
- Reference images with black letterbox bars baked into the pixels are cleaned before generation: `astrid media crop <media> --project P --remove-bars` (dry run first, see [media crop](../../../_core/docs/SKILL.md#crop-still-images-and-video)); use the cleaned item as the reference, never the barred original.
- Check every take with `visualize` (region zoom on the changed area, a strip across the shot) and with timed words for speech. The owner judges look, voice and lip sync.
- Plan: read the shot's inputs from its plan (`timelines shots plan show <shot> --project P --timeline T`: references, masks, voice slice), never from memory or another file. Set `status=in_progress` when the pass starts. After read-back, append the take and mark it ready: `timelines shots plan set <shot> --project P --timeline T --add-take '{"generation": "<run id>", "media": "<media id>", "note": "..."}' --set status=take_ready`.
- When the owner approves a take, run `timelines shots plan set <shot> --project P --timeline T --set status=approved` (and say in a take's `note` which take was approved, if there are several).

## Stage 6 - Assemble and review

Place the takes on the timeline, look across every cut boundary (`--preset motion` around each cut), check that words and mouths agree, and render only when asked.

Plan: before assembling, `timelines shots plan list --project P --timeline T`: `unplanned`, `invalid` and `not_approved` must all be empty (K shots need no approval). Any shot left is named there; finish or re-plan it first. The final overview with `--shot-plans` shows every shot approved.

## Worked example: "Into the Minkhole" (Matrix pill scene)

Change: Neo is replaced everywhere by the old bearded man in the flat cap (his two reaction stills are the fixed reference), including in Morpheus's sunglasses. Morpheus speaks the new script in **his real voice, throughout**. The mink appears as orange pixel art.

| Time (s) | Picture | Line | Methods |
|---|---|---|---|
| 0-5 | Astrid logo, through the glasses | - | K |
| 5-8 | Morpheus medium shot, fists (real) | "You have two options." | LM + R (small reflections: old man); LC possible if the shot can grow |
| 8-9.75 | Hand, blue pill (real) | "You take the blue pill." | V |
| 9.75-12.2 | Old man reacts (still) | "You keep running tools one by one," | V + M |
| 12.2-16.5 | Morpheus close-up (real) | "tuning workflows by hand," (split from timed words) | LC proven here (one take, Neo still in the lenses); LM + R if the shot must keep its length |
| 16.5-19 | Old man reacts (still) | "searching through GitHub issues for answers." | V + M |
| 19-22 | Hands, red pill becomes the mink (stills) | "Or you get this cute little pixely creature," | V (+ M later) |
| 22-27 | Morpheus close-up (currently a still) | "who knows how to do everything in the open-source AI art space." | E/LC from the real close-up, then R (old man holding the mink) |
| 27-31 | Very close on the lenses (stills) | "And see just how deep the minkhole goes." | V + R/M (left reflection turns to pixel art) |
| 31-36 | Pixel world | "Let's go!" (the mink) | M with speech, generated in H3_AV (mink's own voice; open: its reference) |
| 36-43 | Pixel office, Astrid ending | closing introduction (Morpheus) | V |

The exact word splits across cuts come from Stage 4's timed words, not from this table.

Status, 17 November:
- Stage 1 is done: script and temporary-voice cut, watched in Astrid (v23, approved).
- Stage 2 must be redone through `visualize`, which is blocked by a broken install that is being fixed.
- Stage 3 is this table.
- Methods proven on a GPU: only LC/E (one continuation take, owner-approved lip sync).
- Gaps: voice-clone entry (exists in VibeComfy, not exposed); masked edit lacks a replacement-audio input (L) and a reference-image input (R); mask tracking (a fixed mask works first); `visualize` lacks region zoom.

## Task skills this one points to (each to be written and proven)

1. Voice track from a reference voice.
2. Lip sync by continuation (exists; the first take).
2b. Lip sync by mask (open: how the voice gets in).
3. Region replace by masked edit with a reference image.
4. Still to motion with character references.
5. Extend a shot by continuation (same route as 2).
6. Masks: fixed first, tracked later.
