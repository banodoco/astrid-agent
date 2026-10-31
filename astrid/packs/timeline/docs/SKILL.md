---
name: timeline
description: >-
  Edit and inspect Astrid video timelines: place, trim, move, and remove clips,
  then look at the result and report it.
---
# Timeline editing

For the current project and recent work, start with the Astrid skill. Find
recent timelines with `astrid timelines list --recent`. If more than one fits,
ask the user and name the candidates. Never guess from files. Use the same
Python that runs Astrid for SDK scripts.

A timeline has tracks such as `V1`, `V2`, `A1`, and `titles`. Clips use timeline
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

The edit runs on a fresh timeline; if someone else changed it, retry the same
function so the change is checked against their edit.

## Create a timeline

```bash
python3 -m astrid timelines create --project <project> --slug <timeline> --name "Opening"
python3 -c '
from astrid.sdk.timeline.api import Timeline; tl = Timeline.open("<project>", "<timeline>")
clips = [("<clip-one>", 0), ("<clip-two>", 4)]; tl.commit(lambda t: [t.add(path, at=at, track="V1") for path, at in clips], label="Add clips", idempotency_key="opening-clips")'
```

Create first, then add each file with `t.add` in one commit.

## Look and report

```bash
python3 -m astrid timelines visualize <timeline> --project <project> --at 0 --mode inputs
```

For a vague span such as “the intro,” look first, say what you mean, then edit.
Report the edit summary, what you inspected, and the editor link from
`r["data"]["editor_url"]`. Render only when asked.

For words, beats, shots, or music beds, load `astrid-timeline-narrated`. For
approval of exact values, load `astrid-timeline-review`. For batch edits, load
`astrid-timeline-series`.
