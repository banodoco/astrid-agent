# Phase 2 default-selection boundary

The requested defaults are H3_AV video, Qwen TTS voice, MiniMax Music 3 music,
HunyuanVideo-Foley sound effects, and the existing Qwen-Image 2.1 Turbo image
and edit workflows. This change repairs audio inventory mode projection;
it does not promote those defaults or claim that target preparation is ready.

The frozen VibeComfy candidate publishes these canonical workflow contracts:

| Slot | Workflow | Required public inputs | Output |
| --- | --- | --- | --- |
| Voice | `audio/qwen3_tts_custom_voice` | `text` | MP3 audio |
| Music | `audio/minimax_music_3` | `prompt` | MP3 audio |
| Sound effects | `audio/hunyuan_video_foley` | `prompt`, `video_ref` | Audio |
| Image | `image/qwen_image_2_1_turbo` | `prompt` | PNG image |
| Image edit | `edit/qwen_image_2_1_turbo_edit` | `prompt`, `image` | PNG image |

`entries._ready_entries_for` projects native coverage task labels into Agent
modes. In particular, `tts` maps to `tts`, `music` maps to `music`, and
`video_to_audio_foley` maps to `sfx`. Input contracts continue to come from the workflows themselves. These
inventory rows describe workflow capability, not installed GPU/model readiness.

H3_AV serves generation entry `h3` as a `vibecomfy.workflows` contribution
(modes `i2v`, `v2v`, `video-edit`; inputs `request` and `asset_map`, a file map).
Its `build` hook picks the graph from the request and its `after_outputs` hook composes
and verifies the candidate (see `docs/packs/workflows-point.md`). The frozen
VibeComfy candidate has no H3 ready-template ID. The Agent H3 graph builder
requires at least one image reference for generation. A bare video prompt
infers `t2v`; assigning that prompt to `h3` would require a new adapter contract.
The task must stop at that boundary rather than invent the adapter or choose a
different video model.

The supported VibeComfy dependency revision is read through the engine recipe
and the shared `packs/runpod/pins.json` authority. Updating another pins file
alone cannot select the candidate. The pin, affected recipe digests, model and
node locks, and any required staged-offload policy belong to the RunPod owner.
Keep that ownership chain intact before default promotion.

Completion requires the existing shared target path and output publication
path for both local and RunPod execution. Audio must default to placement `auto`;
a missing local GPU must report `needs a local GPU or --placement runpod`, with
Edge TTS named only as an explicitly selected hosted voice option. No hosted
provider may become an automatic fallback. The current explicit RunPod Foley
refusal must be reconciled with the owner's preparation contract before it is
removed. No GPU inference, pod, installation, or hosted request is needed to
reproduce these routing boundaries with `generate list`, `describe`, and
`--dry-run`.
