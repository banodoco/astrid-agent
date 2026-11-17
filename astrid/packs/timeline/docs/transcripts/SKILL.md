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
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate audio --mode transcribe --audio-ref <media-id> --project <project>
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts import <media-id> --project <project> --from-run <run-id>
```

`--audio-ref` takes a project media id (or digest), as above, or a local file
without `--project`. The printed import line names the media id.

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

Both commands take optional hints for the model: `--language <code>` (default: detected
automatically) and `--prompt "<text>"` (names, spelling or vocabulary to expect; default: none),
or `--input language=...` / `--input prompt=...` on `generate`. The language list is the
node's own: today the pinned node declares only `auto`, so a named language is refused until
the node's schema is recaptured. A run made with hints is imported with the same hints
(`transcripts import --from-run <run> --prompt "<text>"`), because the import verifies the run
against the graph those hints make.

## Where a transcript comes from

One media file has one stored transcript per source, in that file's own seconds:

| Source | Written by |
|---|---|
| `astrid.transcription/<node>/<model>` (Whisper; the model and node revision are in the source id) | `transcripts run`, `refresh --run-missing`, `generate ... --transcribe` |
| `astrid.tts` | `generate audio --mode tts`, automatically (the measured word times of the speech) |
| `astrid.import` | `transcripts import`, and a narrated take's `--words` (`timelines edit --line ... --take ... --words`) |
| `astrid.narration` | `refresh --convert` (a timeline's old clip words), and the publish-time sync of an unconverted timeline |

Readers use the best available source in that order, unless the timeline pins one
(`refresh --use-source`). `generate audio|video ... --transcribe` adds the Whisper transcript
for each output that has audio, after the run completed, on the same `--placement` as the
generation. It is GPU work and refuses with `--dry-run` or `--no-wait`; a failed transcription
is reported and never undoes the generation (run `transcripts run <media-id>` later).

## Refresh a timeline's transcripts

Check every audio media in a timeline, its parent and each shot's sub-timeline, in one
command:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" timelines transcripts refresh <timeline> --project <project> [--shot <shot-id>] --dry-run
```

Each media is listed as `whisper`, `other source only (<source>)`, or `missing`. Without
`--run-missing` nothing is transcribed. With `--run-missing --placement <where>` (for example
`runpod`), each missing media is transcribed one at a time through the same path as `transcripts run`.
Transcription is GPU work, so `--run-missing` refuses without `--placement`. Run it with
`--dry-run` first to see the exact commands.

Refresh never pins a default pick. A timeline records a transcript choice only when you name one:
`--use-source <source>` (the stored transcript of that source for each media that has one) or
`--use-document <document-id>`. The choice is written to the timeline's `transcriptReferences` in
the working copy, which is created if there is none; publish to keep it. `--dry-run` writes nothing.
If a pinned source sits below a stored higher-ranked source, the report says `pinned to X; Y is available`.

The report also names two things a refresh cannot fix by itself:
- a word anchor pinned to an older document version (the transcript was replaced after the anchor
  was made); re-anchor it to the current words;
- a clip window whose in or out point falls inside a word; move the cut to a gap between words.

### Convert old clip words

A timeline made before transcripts were stored keeps its words on its VO clips (`app.words`, clip
seconds). `refresh --convert` moves them into `astrid.narration` media transcripts and removes the
words from those clips in one guarded commit of the published head (the clips keep `segment`,
`text` and `gap_after_s`). `--dry-run` prints the plan and writes nothing. Each media is converted
only if the words every reader would then show are identical (text, line, times, each placement);
otherwise it is `skipped` with the reason and its clips keep their words. One file used for several
lines is skipped this way: transcribe it (`--run-missing`) instead. A transcript is written and read
back before any clip loses its words. Once no clip holds words the timeline is marked converted and
the publish-time narration sync no longer runs for it. A working copy with unpublished edits blocks
`--convert`: publish or discard it first. Readers still fall back to clip words for timelines that
are not converted.

Trim, move, speed changes and repeated placements need no refresh: words are read through the
timeline as it stands. `transcripts sync` still works, but `refresh` replaces it.

`transcripts show --timeline` reads the Whisper transcript by default (what it always showed); `--source S`
shows source S instead. Clips with a managed media digest are included, video clips with embedded audio too.
A pinned transcript wins for its media, whatever its source. A pin that cannot be loaded is reported on stderr.

## Choose a cut point

Use the displayed word times to find a quiet gap between words. Place a cut in
the silence rather than inside a word. Listen to the source around the proposed
cut and check the picture; word times are estimates and can be off, especially
near speech boundaries. If the words do not leave a clean gap, choose the cut
by reviewing the media instead of forcing a transcript boundary.

Transcription can estimate which words were spoken and when. It does not
identify the speaker or establish exact mouth sync. Check the picture and audio
for those judgments.
