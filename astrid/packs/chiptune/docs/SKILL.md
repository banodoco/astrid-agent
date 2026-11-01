---
name: chiptune
description: Generate deterministic procedural retro music and sound effects locally without samples, network services, or models.
---

# Chiptune - Agent Guide

## When to use this pack

Use it to make original retro music and sound effects that are generated
programmatically, with no samples, network or models. Typical uses:

- A soundtrack bed for a video, with sections matched to chapters and a beat
  grid so cuts can snap to bars.
- Ducking the bed under voice-over, using the speech spans.
- One-shot UI or cartoon sound effects (stamp, flip, blip, whoosh, chime, error,
  coin, typewriter_tick, wipe, thud), singly or as a zip batch.

Do not use it for speech (use `generation.generate_speech`), AI-generated audio
(`generation.generate_audio`, `fal.fal_foley`), or sampled or licensed music.

## Entrypoints

All actions are deterministic. The same inputs give identical bytes.

```python
import astrid.sdk as sdk
from astrid.sdk import AstridClient

SECTIONS = [  # energy 0..1; mood: bright | tense | wistful | triumphant | silent
    {"start_s": 0, "end_s": 17, "energy": 0.6, "mood": "bright"},
    {"start_s": 17, "end_s": 36, "energy": 0.5, "mood": "wistful"},
    {"start_s": 36, "end_s": 60, "energy": 0.6, "mood": "tense"},
    {"start_s": 60, "end_s": 95, "energy": 0.95, "mood": "tense"},
    {"start_s": 95, "end_s": 110, "energy": 0.8, "mood": "triumphant"},
    {"start_s": 110, "end_s": 150, "energy": 0.4, "mood": "wistful"},
]

with AstridClient.open_from_launcher() as client:  # sdk.invoke needs an explicit client
    cue = sdk.invoke(
        "chiptune.compose",
        kind="action",
        project="almost-ready",
        inputs={
            "duration_s": 150,
            "bpm": 132,
            "key": "A minor",
            "seed": 7,
            # These ports are JSON-typed. Pass arrays/objects directly through the SDK;
            # the V3 command binding serializes them at the process boundary.
            "sections": SECTIONS,
            "hits": [4.5, 60.0],
            "vo_mask": [[2.0, 9.5], [20.0, 33.0]],  # music dips by duck_db
            "duck_db": -9,
            "master_db": -16,
        },
        client=client,
    )
    stamp = sdk.invoke(
        "chiptune.sfx",
        kind="action",
        project="almost-ready",
        inputs={"kind": "stamp", "variant": 2, "pitch": 0},
        client=client,
    )
    batch = sdk.invoke(
        "chiptune.sfx",
        kind="action",
        project="almost-ready",
        inputs={"kinds": "stamp,flip,blip,whoosh,chime,error,coin,typewriter_tick,wipe,thud", "variant": 1},
        client=client,
    )
```

Invocations are admitted as tasks. Identical inputs reuse the existing task, so
after a fix re-run with `python3 -m astrid tasks retry <task-id> --project almost-ready`.

## chiptune.compose

| Input | Default | Meaning |
|---|---|---|
| `duration_s` | required | Exact length. The WAV holds round(duration_s x 48000) frames. |
| `bpm` | 132 | Everything is tempo-locked to this grid. |
| `key` | "A minor" | Tonic and mode ("F# major", "Bb minor"). |
| `seed` | 1 | Drives the motif and the drum noise. |
| `sections` | required | JSON array of {start_s, end_s, energy, mood}. Edges snap to bar lines (about 1.8 s at 132 BPM). Gaps are silent. |
| `hits` | none | Accent stabs at exact times, voiced on the chord sounding then. |
| `vo_mask` | none | Speech spans [[start, end], ...]. Music dips by `duck_db` (default -9) with 60 ms in and 300 ms out. |
| `duck` | none | Explicit {start_s, end_s, gain_db} dips. |
| `master_db` | -16 | RMS target. The peak ceiling of -1.05 dBFS wins if they conflict. |

Musical model: 4-bar phrases (A, B, A, C) share one seeded motif that is
varied each time it returns. Chord progressions per mood, in minor keys: bright (III VII i VI),
wistful (i VI III VII), tense (i iv VI V), triumphant (I IV V I). In major keys:
bright (I V vi IV), wistful (vi IV I V), tense (vi IV ii V), triumphant (I IV V I). Energy controls
density: bass (under 0.25 is half notes, to 0.6 quarter notes, above that eighths),
arpeggios (8ths, then 16ths from 0.45, then octave-up from 0.75), kicks, snares
and hats (16ths from 0.8), and a lead octave from 0.7. Each section ends with a
snare fill on its last bar, and high-energy sections open with a crash.

Outputs: `music` (WAV, 48 kHz stereo 16-bit) and `beats` (JSON):
`{bpm, bar_s, beats[], downbeats[], bars[], sections[]}`. `downbeats` lists
every bar start. `bars` lists the 4-bar phrase starts. Each section has
`requested_start_s` and `requested_end_s`, the snapped `start_s` and `end_s`, `bars`,
`chords`, and the measured `peak_dbfs` and `rms_dbfs`. Cut at the snapped times.

## chiptune.sfx

| Input | Default | Meaning |
|---|---|---|
| `kind` | blip | stamp, flip, blip, whoosh, chime, error, coin, typewriter_tick, wipe, thud |
| `kinds` | none | Batch: comma list. Returns one zip (`sfx_batch`) of NN-kind-vN.wav plus JSON sidecars. |
| `variant` | 1 | Seed for a different take. Same variant gives identical bytes. |
| `pitch` | 0 | Semitones relative to A4. |
| `duration_s` | per kind | Exact length, 0.01 to 10 s. |

Single mode outputs `sfx` (mono 16-bit WAV, peak -3.1 dBFS) and `sfx_meta` (JSON).

## chiptune.midi

Play a Standard MIDI File as an NES chiptune. The highest note sounding is pulse 1 (the melody), the lowest under middle C is the triangle (the bass), and the inner chord notes arpeggiate on pulse 2. Note times are the file's own, so a Basic Pitch transcription lines up with the recording it came from: crossfade the chiptune into that recording and the tune continues.

The MIDI must be project media: import it once (`python3 -m astrid media import song.mid --project P`), then pass its `sha256:<digest>` handle. A local path is refused, because it doesn't reach the worker.

```python
cue = sdk.invoke(
    "chiptune.midi", kind="action", project="almost-ready", client=client,
    inputs={"midi": "sha256:<digest>", "start_s": 0, "transpose": 0, "master_db": -16},  # duration_s optional
)
```

The level is normalised to `master_db` over the rendered window, so a 30 s render and a full-length render of the same MIDI differ in gain. Render the window you will use.

Outputs: `music.wav` (48 kHz stereo) and `voices.json` (the MIDI summary and each voice's spans and seconds).

## Choosing values

- Chapter-matched cue: pass one section per chapter. Use `energy` for intensity
  and `mood` for the colour. Use `silent` for dead air.
- Trim the cue to its chapter boundaries with the snapped times in `beats.json`.
- Loudness is set by `master_db`, but the -1.05 dBFS peak ceiling always holds.
  Read the achieved RMS from `manifest.json` or `beats.json` before you tune it.

## Verification

`python -m pytest tests/packs/chiptune -q` covers determinism, exact duration,
the peak ceiling, DC offset, edge clicks, the batch zip, and beat tempo.
