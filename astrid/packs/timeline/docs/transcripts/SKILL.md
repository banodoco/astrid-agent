---
name: timeline-transcripts
description: Import source-media transcripts and read timed words to choose or review timeline cuts.
---
# Source-media transcripts

Astrid commands here run through the launcher; its path on each OS is in the
[astrid-setup skill's Launcher paths](../../../setup/docs/SKILL.md#launcher-paths).
The examples show the Linux launcher.

A transcript covers a whole managed media file and stores word times in seconds
from the start of that media. Use those times to choose or review timeline cuts.

## Import and read

Import a complete JSON words file for a managed media item in a project. If you
already have a completed Astrid transcription run for that media, import its
output with `--from-run`.

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts import <media-id> <words-file> --project <project>
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
uses of the same media are shown at each occurrence's timeline position. A word
is shown when it starts inside the clip's source window, and speed is applied.

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts show --project <project> --timeline <timeline> --clip <clip-id>
```

## Refresh a timeline's transcripts

Check every audio media in a timeline, its parent and each shot's sub-timeline, in one
command:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts refresh <timeline> --project <project> [--shot <shot-id>] --dry-run
```

Each media is listed as `whisper`, `other source only (<source>)`, or `missing`.

Refresh never pins a default pick. A timeline records a transcript choice only when you name one:
`--use-source <source>` (the stored transcript of that source for each media that has one) or
`--use-document <document-id>`. The choice is written to the timeline's `transcriptReferences` in
the working copy, which is created if there is none; publish to keep it. `--dry-run` writes nothing.
If a pinned source sits below a stored higher-ranked source, the report says `pinned to X; Y is available`.

The report also names two things a refresh cannot fix by itself:
- a word anchor pinned to an older document version (the transcript was replaced after the anchor
  was made); re-anchor it to the current words;
- a clip window whose in or out point falls inside a word; move the cut to a gap between words.

Trim, move, speed changes and repeated placements need no refresh: words are read through the
timeline as it stands. `transcripts sync` still works, but `refresh` replaces it.

`transcripts show --timeline` reads the Whisper transcript by default (what it always showed); `--source S`
shows source S instead (and ignores pins). Clips with a managed media digest are included, video clips with embedded audio too.
A pinned transcript wins for its media, whatever its source, unless `--source` is given: then that
source wins and pins are ignored. A pin that cannot be loaded is reported on stderr.

## Choose a cut point

Use the displayed word times to find a quiet gap between words. Place a cut in
the silence rather than inside a word. Listen to the source around the proposed
cut and check the picture; word times are estimates and can be off, especially
near speech boundaries. If the words do not leave a clean gap, choose the cut
by reviewing the media instead of forcing a transcript boundary.

Transcription can estimate which words were spoken and when. It does not
identify the speaker or establish exact mouth sync. Check the picture and audio
for those judgments.
