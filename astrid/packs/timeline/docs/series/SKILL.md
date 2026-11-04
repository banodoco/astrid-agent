---
name: timeline-series
description: Edit a group of timelines safely, record each outcome, and recover failures.
---
# Edit a timeline series

```python
from astrid.sdk.timeline.api import CommitConflict, Timeline

def add_outro(t):
    end = max((c.end for c in t.clips(track="titles")), default=0)
    t.add_text("Chapter end", at=end, hold=2, track="titles", id="outro")

for episode in episodes:
    tl = Timeline.open(project, episode)
    try:
        result = tl.commit(
            add_outro,
            label="Add outro",
            idempotency_key=f"add-outro-{episode}",
        )
        print(episode, result["status"], result["summary"])
    except CommitConflict as err:
        if err.pending_id:
            result = tl.retry(err, add_outro)
            print(episode, result["status"], result.get("summary"))
        else:
            print(episode, "not landed:", err)
```

One key per timeline makes a rerun safe; the loop moves on when one timeline
refuses the edit.

Record every outcome before moving on, and record an item that is already done
as skipped. Do not retry a saved edit that needs approval or re-derivation
until that is resolved (see `timeline-review`). If the run must survive a
process restart, keep the record in a file outside the loop.

Make a variant with `tl.duplicate(as_="variant-a")`, then open it with
`Timeline.open(project, "variant-a")`. Archive an unwanted copy with
`python3 -m astrid timelines archive variant-a --project <project>`;
`timelines recover` brings it back.
