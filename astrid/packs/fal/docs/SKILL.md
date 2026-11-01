---
name: fal
description: >
  fal.ai integration for short-clip Foley plus MiniMax H3 text-to-video and
  multimodal reference-to-video generation. Requires FAL_KEY.
---

# fal

The fal pack provides two focused actions for Hunyuan-Video Foley and MiniMax
H3 generation.

## Actions

| Action | What it does |
|---|---|
| [`fal.fal_foley`](../actions/fal_foley/STAGE.md) | Send a video clip (≤15s recommended) to fal.ai and receive a Foley audio track matched to the clip's duration. |
| [`fal.h3_video`](../actions/h3_video/STAGE.md) | Generate a 2K MiniMax H3 clip from text or ordered image/video/audio references. |

## When to use

- Use `fal.fal_foley` to generate Foley audio for a single short video clip.
- Use as a leaf provider action in spatial Foley pipelines (the `foley` pack's
  `foley_map` orchestrator calls this action per tile).
- Use `fal.h3_video` when H3's exact fal schema or multimodal reference inputs
  are needed. H3 prompts are limited to 2,000 characters and output duration
  to 5–15 seconds; output resolution is fixed at 2K.

## When NOT to use

- Do not use for orchestrating a full spatial-Foley pass over a whole video —
  use the `foley` pack's `foley_map`.
- For ordinary single-prompt video models already in Astrid's model catalog,
  prefer the `generation` pack.

## Credentials

| Env var | Used by |
|---|---|
| `FAL_KEY` | fal.fal_foley, fal.h3_video |

## SDK quick-start

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "fal.fal_foley",
    kind="action",
    project="demo",
    inputs={"clip": "./short_clip.mp4", "prompt": "wind through reeds"},
)

result = sdk.invoke(
    "fal.h3_video",
    kind="action",
    project="demo",
    inputs={
        "mode": "text-to-video",
        "prompt_file": "./prompt.txt",
        "duration": "15",
        "aspect_ratio": "16:9",
    },
)
```

For a finished video with Foley, use the returned runtime audio artifact in the
source video's timeline on an audio track aligned to the scored clip. Follow
[video editing — existing timeline route](../../video_editing/docs/SKILL.md) to
place and save it, then use rendering downstream to render it; Foley generation
alone returns audio, not a muxed video. Read the selected action's `STAGE.md`
before admission.
