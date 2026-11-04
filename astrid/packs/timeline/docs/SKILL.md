---
name: timeline
description: >-
  Edit and inspect Astrid video timelines: place, trim, move, and remove clips,
  then look at the result and report it.
---
# Timeline editing

Start with the Astrid skill for the current project. Find timelines with
`python3 -m astrid timelines list --recent`; if more than one fits,
ask the user and name the candidates. Never guess from files. Run SDK scripts
with the same Python that runs Astrid.

Tracks are named like `V1`, `V2`, `A1`, and `titles`. Clip times are timeline
seconds; video and audio also have source-file seconds. One commit is one undo.

## Open and edit

```python
from astrid.sdk.timeline.api import CommitConflict, Timeline

tl = Timeline.open("<project>", "<timeline>")

def tighten_intro(t):
    intro = t.clip("intro")
    intro.trim(1.0, 5.0)
    t.add_text("Chapter one", at=intro.end, hold=2, id="ch1")

try:
    r = tl.commit(tighten_intro, label="Tighten intro", idempotency_key="tighten-intro-ep01")
except CommitConflict as err:
    r = tl.retry(err, tighten_intro)
print(r["summary"])
```

The function runs on the latest timeline; if someone changed it first,
`tl.retry` runs the function again on top of their edit.

## Create a timeline

```bash
python3 -m astrid timelines create --project <project> --slug <timeline> --name "Opening"
python3 -c '
from astrid.sdk.timeline.api import Timeline; tl = Timeline.open("<project>", "<timeline>")
clips = [("<clip-one>", 0), ("<clip-two>", 4)]; tl.commit(lambda t: [t.add(path, at=at, track="V1") for path, at in clips], label="Add clips", idempotency_key="opening-clips")'
```

Add all files in one commit, so one undo removes them.

## Look and report

```bash
python3 -m astrid timelines visualize <timeline> --project <project> --at 0 --mode inputs
```

For a vague span such as "the intro", look first, say what you mean, then edit.
Report the edit summary, what you looked at, and the editor link
`r["data"]["editor_url"]`. Render only when asked.

Load `timeline-narrated` for words, beats, shots, or music beds;
`timeline-review` when a person approves exact values first; `timeline-series`
for one edit across many timelines.
Load `timeline-shots` to label sections, reuse shots, and arrange occurrences.
