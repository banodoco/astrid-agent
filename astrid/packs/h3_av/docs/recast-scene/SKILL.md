---
name: h3-av-recast-scene
description: Plan and make a scene with replaced characters and re-voiced dialogue.
---

# Recast and re-voice a scene

Keep the real performance wherever the script allows. Plan the scene in the cut, then choose the smallest edit each shot needs.

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

**Get the sequence right cheaply, then spend on generation, voice first.**
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
- Words under the picture: check stored transcripts with `astrid timelines transcripts refresh <timeline> --project P --dry-run`. Use the word tiles where timings are available.
- Region zoom on details that will change (lenses, mouth). If the tool cannot show what you need, that is a tool gap: fix it or file it. Do not fall back to outside tools.

Output: the **sequence map**, a table with one row per cut: time, picture, who is visible, line and words with times, source (footage, still, generated).

Plan: once the approved cut is stored as shots, record what you saw on each shot: `timelines shots plan set <shot> --project P --timeline T --set on_screen=Morpheus --set line="You have two options."`. The line's text itself stays in the shot's `voiceover_script` binding; `line` is the label.

## Stage 3 - Shot list: pick a method for each shot

For each cut, ask what must change, and pick the least invasive method that does it. A shot can need more than one.

| Method | Use when | H3 task skill |
|---|---|---|
| **V. Voice over** | The line is heard, but the speaker's mouth is not on screen | Voice track (stage 4) only |
| **LC. Lip sync by continuation** | Real footage of the speaker talking exists, and the new line can follow on from it: the shot starts with them saying something real, then continues with them saying the new line | Continuation |
| **LM. Lip sync by mask** | The speaker must say the new line inside the real shot's own frames (fixed length, fixed pose), often with other masked changes such as reflections | Lower-face mask with fixed replacement audio |
| **R. Region replace** | Something inside real footage must change: a reflection, a held object, a background figure | Masked region edit with a reference image of what should appear |
| **M. Still to motion** | The shot exists only as a still or a design frame and must move | For a silent reaction, use the still as a silent video source (Stage 5) |
| **E. Extend** | The real shot is shorter than its words | Continuation from the real footage (keeps the performance, adds length) |
| **K. Keep** | Nothing changes | None |

Rules of thumb:
- Prefer masks on real footage over regenerating a whole shot: the real eyes, head movement and lighting survive.
- LC is the safe default when the shot can grow longer; LM when its frames must stay as they are.
- If a speaking shot has no real footage, start from the nearest real footage of the speaker (E), then edit the mouth or reflections as needed. Avoid animating a still face into speech.
- Fixed references, declared once: each character's reference images and the speaker's real voice. Every pass uses the same ones.

Output: the **shot list**, the sequence map plus a method column, inputs (masks, references, voice slice) and a check for each shot.

Plan: for every shot, write `method`, `intent`, `check`, the fixed `inputs.references` (and `inputs.masks` once made) and `status=planned`, e.g. `timelines shots plan set <shot> --project P --timeline T --set method=LM,R --set inputs.references=<id>,<id> --set status=planned`. `K` shots get `method=K` too, so nothing is left unplanned. Check with `timelines shots plan list --project P --timeline T` (`unplanned` must be empty) and look with `timelines visualize T --project P --preset overview --shot-plans`.

## Stage 4 - The voice track

- Reference: the speaker's real voice, a clean clip of their real lines with its transcript.
- Use an approved continuous performance if available. To make a missing line in the speaker’s voice, use a continuation below.
- Replace the temporary voice on the timeline. Use any stored or imported word timings to adjust the cuts. The refresh report lists cuts that split a word. Look again (Stage 1 views).
- Owner listens. This is the gate before any picture generation.
- Plan: once the voice track is approved, write each speaking shot's slice of it: `timelines shots plan set <shot> --project P --timeline T --set inputs.voice.media=<voice track media id> --set inputs.voice.range=START,END`. The range is seconds into that voice track media (from its timed words), not timeline time, and needs the media.

### Make someone say a new line

| Available shot | Line length | Choose |
|---|---|---|
| The cut can grow and you have footage of the speaker talking | Allow time for the whole new line after the source | Continue picture and voice (LC) |
| The picture must stay as filmed | The complete recorded line fits the footage | Mouth inpaint with fixed audio (LM) |
| The picture must stay as filmed, but the line exists only in the script | Measure the line once spoken | Make the voice first, then use LM if it fits |

1. **Continue from their real voice (LC).** Start with a real clip of the person speaking. Prompt the exact words to follow. Size the speaking window to the line: output duration = source duration + the line's natural length + a short hold, and nothing more. A window much longer than the line invites filler words. On 02c, a 4.25 s window for a 2 s line produced "…the design of voice in VoDs," before the line. A 2.5 s window gave exactly "tuning workflows by hand" (transcript 0.0–2.48 s). Use the default lead-in mode (`drop`). Read the usable `range` in the take’s `h3/take.json` and run `astrid media trim TAKE --project P --range START,END` to keep just the new performance. Check the voice, mouth and final word. This worked on 01, 04 and 02c.
2. **Make the voice first.** Continue any clean clip of the person speaking. The picture can be unrelated. Trim the new line and listen to it against the real voice. Use its audio to drive the mouth pass below. The voice-only continuation produced the line on 02c. For a small image-guided speech take, use the [512×288 request example](../examples/image-guided-speech.json); its speech quality and timing still need a live evaluation ([astrid-dev#885](https://github.com/banodoco/astrid-dev/issues/885)).
3. **Keep the filmed shot (LM).** Listen to the exact audio range in the shot plan before submitting. Mask the lower face, including jaw and cheeks. Feed the line as a replacement audio span of matching length. It plays unchanged while H3 edits the mouth. The combined mouth and lens edit on 02c worked technically, but the plan pointed to the temporary computer voice-over. The 3.25 s close-up also clipped the end of the roughly 3.8 s line. If the words overrun, find longer footage or use LC where the cut can grow. Check the speaker, last syllable and lip sync before approving.
4. **Use a start image plus a speaker reference (v2).** H3 accepts reference videos with their soundtracks, or standalone audio clips, alongside still-image references. Use the [image-guided speech example](../examples/image-guided-speech.json) or [mouth-mask speaker recipe](../recipes/mouth-mask-speaker.json) as a request template.

**Speaker-reference limits and behavior.** Stay within the shared [H3 limits](../SKILL.md#speaker-references-v2): at most 9 images, 3 videos and 3 audio clips (12 inputs total), with each selected clip 2–15 seconds and video/audio totals capped at 15 seconds. For a start-still request, place the still at frame 0: it is the exact first frame, and H3 generates from frame 1. A mouth-mask edit regenerates both speech and room tone inside its audio window. Voice tags such as `<Audio 1> ... (S1)` are an unverified model prompt convention, not a binding between a voice and a face. CPU example admission proves request compilation only; it does not prove the model follows the prompt or produces a particular voice or lip sync.

**Output size and masks.** For image-guided generation and masked edits, `output.size: [width, height]` accepts integer dimensions from 32 through 16384 inclusive, each a multiple of 32. Omit it for the 1024×576 default; continuation is limited to that size. The [512×288 image-guided speech example](../examples/image-guided-speech.json) and [768×448 masked-edit example](../examples/rectangle-edit.json) are covered by CPU compile admission; other accepted sizes are uncharacterized. For rectangle masks, `mask.canvas` gives the authored coordinate dimensions; if omitted, coordinates use the legacy 1024×576 canvas. Still-image masks use their own decoded pixel dimensions.

**Listen to every voiced take.** Where a transcript is available, use `astrid timelines transcripts show TAKE --project P` to check the words and find cut points. On 01 the take opens with the film's "your last chance." and the new line starts at 2.6 s.

## Stage 5 - Picture passes, shot by shot

**Lenses and other regions.** Inspect a frame from the actual source and read the script line. Brief each lens separately. On 04 the left lens needed the hand offering the mink and the right needed the hand offering the blue pill. Edit that frame to make the reference. Composite the real hand, mirroring it if needed, and take the mink from the project’s own art. Make the lens mask with `astrid media mask` and check its edges across the shot with `--preview-on CLIP --range START,END`. Set the video edit’s mask and image guide and leave audio untouched. Compare both lenses over time and listen back. The masked pass preserves picture outside the mask and the source audio, including dialogue from a previous LC take. See the [mask recipe](../SKILL.md#make-a-mask) and [edit inputs](../SKILL.md#edit-inputs-reference-images-and-replacement-audio).

**Silent reaction from a still.** For 02b, FFmpeg looped the still at 24 fps for 2.5 s, cropped it to 1024×576 and added a silent WAV as the audio stream. Astrid lacks this preparation step ([astrid-dev#863](https://github.com/banodoco/astrid-dev/issues/863)). Use that video as the edit source. Keep the first four frames. Regenerate the full frame from `"1/6"` s to the end with a video-only edit. Describe visible action without sound words: “His lips stay gently together. His gaze shifts and he blinks once.” Watch the jaw and check that the take plays silently.

- Snap source ranges to H3’s 24 fps grid. The 02c close-up used `["1945/24","2023/24"]`. If a take’s stream durations prevent reuse as a source, use `astrid media trim` on the usable range first.
- Start with the riskiest method on its best shot. Make one take per approach. Sweep seeds only when asked.
- Dry-run each request to check validity. Judge the picture on the returned take. Claim one machine with a spend cap and deadline. Admit jobs with `--no-wait`, follow their IDs with `astrid tasks show`, fetch each take with `astrid media open --materialize`, then release the machine. For a lossless Matroska/FFV1 take, make a browser viewing copy ([astrid-dev#864](https://github.com/banodoco/astrid-dev/issues/864)).
- Reference images with black letterbox bars baked into the pixels are cleaned before generation: `astrid media crop <media> --project P --remove-bars` (dry run first, see [media crop](../../../_core/docs/SKILL.md#crop-still-images-and-video)); use the cleaned item as the reference, never the barred original.
- Check every take with `visualize` (region zoom on the changed area, a strip across the shot) and with timed words for speech. The owner judges look, voice and lip sync.
- Plan: read the shot's inputs from its plan (`timelines shots plan show <shot> --project P --timeline T`: references, masks, voice slice), never from memory or another file. Set `status=in_progress` when the pass starts. After checking the take in visualize, record the methods it satisfies: `timelines shots plan set <shot> --project P --timeline T --add-take '{"generation": "<run id>", "media": "<media id>", "note": "...", "covers": ["LC"]}'`. Keep the shot in progress until one take covers every planned method, then set `status=take_ready`.
- When the owner approves a take, run `timelines shots plan set <shot> --project P --timeline T --set status=approved` (and say in a take's `note` which take was approved, if there are several).

## Stage 6 - Assemble and review

Place the takes on the timeline, look across every cut boundary (`--preset motion` around each cut), check that words and mouths agree, and render only when asked.

LC takes go in with their `range` (see Stage 4): the clip's `from`/`to` is the new line, so the lead-in does not play twice. Keep the lead-in only when the real line before it belongs in the cut.

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

On 18 November the GPU runs covered dialogue continuation, reference-guided lens edits and the silent reaction. The 02c voice-only take produced the line. Its separate fixed-audio mouth test still needed a longer shot and the correct voice before approval.
