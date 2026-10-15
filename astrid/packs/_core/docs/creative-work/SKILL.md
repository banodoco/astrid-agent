---
name: creative-work
description: Route requests to make, inspect, edit, render, or publish creative work to the existing Astrid pack skill that owns the operation.
---

# Creative work

Use this skill after [Astrid core orientation](../SKILL.md) when the user wants
an actual creative result. Choose the narrowest existing pack route, read that
pack's `SKILL.md`, then follow its capability and `STAGE.md` instructions.
When a pack names a capability without linking its contract, the usual source
path is `astrid/packs/<pack>/executors/<slug>/STAGE.md` (or
`orchestrators/<slug>/STAGE.md`), relative to the checkout root.
Packs own execution guidance; this skill does not duplicate their procedures or
create a new runtime pack.

When delivering a video preview for feedback on reference frames, storyboards,
timing, or an ongoing edit, use the rendering skill's review mode by default.
The user needs visible shot names and timecodes to identify what to change.
Keep review mode on through subsequent revisions unless the user requests a
clean export; a separate filmstrip viewer does not replace labels in the video.
Remotion's default review export uses its `--scale` option to fit the authored
canvas inside 640x360 with the original aspect ratio. The props and authored
coordinates remain at the canonical canvas size, and the actual scaled profile
is probed and recorded. The exported video marks this state with `Low Res
Render` at top left; shot names and timecodes stay at top right. Clean exports
and explicit profiles retain their existing resolution behavior.

Review labels and subtitles are inspection overlays and must not drive the
creative layout. Center authored content in the full canvas and avoid
reserving space for captions unless the user explicitly asks for a
caption-safe design; scale review subtitles with the authored canvas while
keeping the low-resolution badge readable.

## Route by intent

| User intent | Read and use | Typical entrypoints |
| --- | --- | --- |
| Generate an image, video, or audio asset from a prompt | [generation](../../../generation/docs/SKILL.md) | `generation.generate_image`, `generation.generate_video`, `generation.generate_audio` |
| Inspect, edit, validate, or run a ComfyUI/VibeComfy graph | [vibecomfy](../../../vibecomfy/docs/SKILL.md) | `vibecomfy.inspect`, `vibecomfy.edit`, `vibecomfy.validate`, `vibecomfy.run` |
| Understand or describe an image, audio clip, or video | [understanding](../../../understanding/docs/SKILL.md) | `understanding.understand`, `understanding.scene_describe` |
| Transcribe, detect scenes/shots, arrange clips, review, or validate editorial work | [editorial](../../../editorial/docs/SKILL.md) | `editorial.transcribe`, `editorial.scenes`, `editorial.shots`, `editorial.arrange`, `editorial.validate` |
| Trim or repair media, or search/download GIFs | [media](../../../media/docs/SKILL.md) | `media.clip_extract`, `media.speech_repair_lavasr`, `media.gif_search` |
| Assemble a production video, talk, thumbnail, logo grid, or image animation | [video editing](../../../video_editing/docs/SKILL.md) | `video_editing.hype`, `video_editing.event_talks`, `video_editing.thumbnail_maker` |
| Find, inspect, edit, preview, or publish a runtime timeline | [timeline editing](../../../timeline/docs/SKILL.md) | `timelines find`/`timelines show` locate and inspect; `timelines visualize` shows the selected draft |
| Build an iteration video or compare experiment outputs | [iteration](../../../iteration/docs/SKILL.md) | `iteration.assemble`, `iteration.experiment_review` |
| Add sound to one short video clip | [fal](../../../fal/docs/SKILL.md), then [timeline editing](../../../timeline/docs/SKILL.md) to place and review it | `fal.fal_foley` produces audio; place it alongside the source video on a detached timeline candidate |
| Make a spatial soundscape from video tiles | [foley](../../../foley/docs/SKILL.md) | `foley.foley_map` produces per-tile audio and a review viewer |
| Distill a long stream or event into reviewable clips | [stream content](../../../stream_content/docs/SKILL.md) | `stream_content.distill` |
| Build a training dataset or run LoRA training | [training](../../../training/docs/SKILL.md) | `training.dataset_build`, `training.training_run` |
| Render a standalone Blender scene or terminal screenplay | [blender](../../../blender/docs/SKILL.md) or [moirae](../../../moirae/docs/SKILL.md) | `blender.render`, `moirae.moirae` |
| Acquire or publish YouTube media | [youtube](../../../youtube/docs/SKILL.md) | `youtube.youtube_audio`, `youtube.upload` |

The complete discovered pack catalog is available in
[pack references](references/packs.md) when an exact route outside this table
is needed. Reusable characters, places, objects, logos, clothing, styles, and
layouts use the [references guide](../../../media/docs/references.md), which
owns that workflow rather than this execution router.

## Execution shape

Most pack work is admitted through the SDK and belongs to a selected runtime
project. Use the exact qualified capability id, required `kind`, inputs, and
project binding from the selected skill and its `STAGE.md`. Inspect the
returned result, run evidence, and artifacts through the runtime; do not read a
local task store or call a pack's `run.py` directly.
The runtime supplies the selected pack's temporary output workspace and owns
publication of the resulting assets. Use Astrid's configured storage for
durable outputs, independent of the shell's current directory. Do not create
an extra `runs/` tree, download managed assets into a convenience directory,
or choose an alternate output path unless the user explicitly asks for an
export there. Supporting prompts, manifests, and review evidence that need
preserving must also be registered with the project/run; leaving them beside
a local video does not register them. External generation tools must return
their outputs through the managed import/publication boundary before those
outputs are used as project assets.

## Hivemind before creative decisions

Search [Hivemind](../SKILL.md#shared-knowledge-hivemind) before choosing an unfamiliar model, setting, workflow pattern,
or workaround. This is especially useful for ComfyUI/VibeComfy graphs,
generation settings, rendering failures, and known community solutions. Use
`hivemind.get_item` for the full evidence behind a useful result. Treat
retrieved advice as input to the selected pack, not as a replacement for its
current contract.

If the user asks to publish a finding, prepare a reviewable sanitized payload
and ask for explicit confirmation immediately before
`hivemind.contribute`. Never publish private paths, prompts, media, or URLs by
default.

## New capabilities

Do not invent a new pack merely to organize instructions. If the user wants a
new reusable pack or capability, follow the [pack-builder skill](../pack-builder/SKILL.md)
and keep execution routing here limited to existing pack skills.
