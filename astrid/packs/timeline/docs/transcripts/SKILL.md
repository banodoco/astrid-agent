---
name: timeline-transcripts
description: Transcribe source media and read timed words to choose or review timeline cuts.
---
# Transcribe media

Astrid commands here run through the launcher; its path on each OS is in the
[astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths).
The examples show the Linux launcher.

Transcribe the source before choosing a cut point, and transcribe every
generated take before judging it. A transcript records what the media contains;
a script, prompt, or guide is not evidence of what was actually spoken. One
transcript covers the whole managed media file and stores word times in seconds
from the start of that media.

## Transcribe and read

Run transcription for a managed media item in a project. Astrid uses its
transcription workflow and stores the timed words against that media:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts run <media-id> --project <project>
```

The same transcription can be run directly with the generation command. It uses
the `transcription` entry, which is the default for audio transcribe, so
`--entry` is optional. Its result is a Runtime run that imports exactly as
`transcripts run` does:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate audio --mode transcribe --audio-ref <file>
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts import <media-id> --project <project> --from-run <run-id>
```

Importing the same media again from the same source compares the content. An
identical result reports `unchanged`. A different result is refused, and the
refusal names the replacement. Replace only with that version:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts import <media-id> --project <project> --from-run <run-id> --replace --expected-version <N>
```

Read all words for a media item:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts show <media-id> --project <project>
```

To read the words used by a particular timeline clip, identify both its
timeline and clip. The output labels source-media and timeline times; repeated
uses of the same media are shown at each occurrence's timeline position.

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts show --project <project> --timeline <timeline> --clip <clip-id>
```

## Choose a cut point

Use the displayed word times to find a quiet gap between words. Place a cut in
the silence rather than inside a word. Listen to the source around the proposed
cut and check the picture; word times are estimates and can be off, especially
near speech boundaries. If the words do not leave a clean gap, choose the cut
by reviewing the media instead of forcing a transcript boundary.

Transcription can estimate which words were spoken and when. It does not
identify the speaker or establish exact mouth sync. Check the picture and audio
for those judgments.
