# Placeholder voiceover for a timeline draft

Use this when the user wants a basic spoken draft for timing or editorial
feedback. Script text and playable speech are separate assets: create the audio,
place it on the timeline, and pin the same words as the shot's registered
`voiceover_script`. Updating a text binding alone does not generate speech.

## Choose and synthesize the voice

Reuse the project's established voice when its provenance is known. Astrid
Intro used Edge TTS with `en-US-ChristopherNeural` at normal rate, pitch, and
volume. This is a project precedent, not a mandatory voice for all projects.
Edge TTS calls an online speech service; it is not an offline synthesizer. Astrid
exposes it as `generation.generate_speech`, which writes WAV and managed provenance with
the exact text, text hash, voice settings, audio hash, and measured duration.
The reusable timeline workflow in the [video editing skill](../../../video_editing/docs/SKILL.md)
calls this executor and verifies its managed artifacts. If the service is
unavailable, report that instead of silently changing the voice.

For manual generation through the runtime SDK (pass an already connected
`AstridClient` as `client`):

```python
import astrid.sdk as sdk

speech = sdk.invoke(
    "generation.generate_speech",
    kind="executor",
    project="my-project",
    inputs={
        "text": "Exact approved spoken words go here.",
        "provider": "edge-tts",
        "voice": "en-US-ChristopherNeural",
        "rate": "+0%",
        "volume": "+0%",
        "pitch": "+0Hz",
    },
    client=client, wait=True,
)
speech.outputs["artifacts"]  # Runtime-managed speech WAV and speech_manifest provenance
```

Write the intended spoken words to a UTF-8 file, excluding shot labels and
visual instructions. Keep that exact file for script registration. For manual
generation outside an Astrid runtime run, from a scratch directory containing
`script.txt`:

```bash
edge-tts --help
# Optional voice discovery:
edge-tts --list-voices
edge-tts --voice en-US-ChristopherNeural --rate=+0% --pitch=+0Hz --volume=+0% \
  --file script.txt --write-media narration.mp3

# Decode-check and measure before choosing the shot duration.
ffmpeg -v error -i narration.mp3 -f null -
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 narration.mp3
```

Keep the MP3 extension for Edge TTS's media output. If PCM WAV is needed,
convert the bytes rather than renaming the file:

```bash
ffmpeg -i narration.mp3 -c:a pcm_s16le narration.wav
```

For complete sentences, one file per beat is a simple first-cut workflow. If a
sentence spans several images, synthesize it once and either keep one continuous
audio clip or split its decoded PCM using real speech-boundary timing. Do not
estimate word timing from character counts. Astrid Intro's connected phrases
were synthesized together and split at provider word boundaries, with adjacent
PCM slices covering every sample exactly once. That preserves delivery without
inventing pauses between images. Provider boundaries are timing evidence, not a
guarantee of perceptual alignment; listen at the cuts when reviewing speech.

## Place, register, and check in

Follow the [document checkout recipe](document-checkout.md) to obtain a fresh
editable bundle. Preserve recorded or final audio. A legacy clip may be replaced
by the reusable sync only when its exact ID is explicitly supplied in
`adopt_clip_ids`; the helper does not assume that existing draft audio belongs
to it. In a manual edit, replace only the clip the user selected. In the
selected shot's internal timeline, point the chosen VO registry entry's
`local_path` at the generated file, or create a new asset entry and ordinary
media clip on an audio track. Relative paths resolve from the checkout JSON's
directory. For example, within an already selected `shot`:

```python
internal = shot["internal_timeline"]
internal.setdefault("registry", {}).setdefault("assets", {})["draft_vo"] = {
    "local_path": "media/narration.mp3",
}
# Use the actual measured duration in seconds for to; select an existing audio
# track or add one. Replace the previous VO clip rather than layering both.
```

Set the VO clip's `asset` to `draft_vo`, `clipType` to `media`, `from` to `0`,
`to` to the measured duration, and `at` to its intended start within the shot.
Set or extend the placement's row-level `duration_ms` to cover the audio and
any intentional tail pause. Apply the explicit ripple policy to later shots
and parent overlays; audio replacement does not retime them automatically.

Use the checkout helper's `bind-script` command with the same `script.txt`, the
registered shot ID, and the observed binding head (`0` only for a new binding).
Then run `check`, inspect the diff, and `publish` as documented in the main
skill. For a new shot, follow the main skill's registration-before-binding
sequence. Do not substitute unregistered inline text for a registered descriptor.

Publication imports manual file output through the existing project media
catalog and replaces the temporary path with a managed ID. The SDK
`generation.generate_speech` route has its own task/run and records text, voice
settings, and output hashes in its manifest. Keep the TTS engine, voice, rate,
and placeholder status in shot metadata or a managed production note when they
need to be reproducible. Confirm the returned audio
record appears under Astrid App's **Audio** gallery filter; open the gallery pane so
its query is active. `media.import_file` alone is not the gallery-aware route.

After saving, read `timelines script` to confirm the pinned words and preview
the audio/timeline in Astrid App. Decode checks establish file integrity; only an
auditory review establishes pronunciation and pacing. Report which was checked.
No video render is needed unless the user requests one.
