# Timeline editing helpers

Load `timeline` for the normal edit path and this reference for helper details.

| Call | Use |
|---|---|
| `t.add(path, at=, hold=, track=, id=)` | Imports a local image, video, or audio file and places it. With `hold`, a still lasts that long and video or audio plays source seconds 0 through the hold; without it, the whole file plays. |
| `t.add_text(text, at=, hold=, track="titles", id=)` | Adds plain text at timeline seconds. It creates a missing picture track or uses the named track. Give it an id, then read it back. |
| `t.track(name, above=, kind=)` | Creates or selects a track; `above` sets its order when new. |
| `clip.on(seconds)` | Moves the clip start in timeline seconds and keeps its length. Use a moment string to follow a word. |
| `clip.trim(in_s, out_s)` | Sets the source window in file seconds; the timeline start stays fixed. |
| `clip.hold_for(seconds)` | Sets a literal length and keeps the source in-point. It refuses a video or audio length the file cannot fill. |
| `clip.set_track(name)` | Moves a clip to an existing track. |
| `clip.split(at, id=)` | Splits at timeline seconds. The left keeps its id; the right receives the new id. It refuses edge cuts, moving clips without a source window, and clips with moments, animation, or transitions. |
| `clip.swap_asset(path_or_key)` | Replaces the source while keeping the start. Images and video keep their length; audio that played its whole file plays the new file whole. |
| `clip.remove()` | Deletes the clip and leaves a gap. |
| `t.arrange(ids, at=0, gap=0)` | Places shot occurrences in the supplied order. It leaves old slots empty and refuses overlap with an unselected shot. |

`t.add` uses timeline seconds for `at`. A missing track is created to match
the file; a track of the wrong kind refuses. Read back new clips and check for
unexpected overlaps. New clips should have explicit ids.

`t.insert_time`, `t.ripple_delete`, and `t.close_gap` act on every track. See
`timeline-narrated` for music-bed behavior and ripple limits.
