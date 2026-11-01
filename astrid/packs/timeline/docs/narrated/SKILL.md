---
name: timeline-narrated
description: Place and arrange clips around spoken words, beats, cuts, and music beds.
---
# Narrated timelines

## Place to a word or beat

```python
from astrid.sdk.timeline.api import Timeline

tl = Timeline.open(project, timeline)

def place_to_words(t):
    t.clip("badge").on('after "Astrid"')
    t.clip("hit").on('beat 2 after "Astrid"')

result = tl.commit(place_to_words, label="Place to narration",
                   idempotency_key="place-to-words-episode-1")
```

`"Astrid"` is the word's start; `after "Astrid"` is its end. List nearby beat
addresses with `python3 -m astrid timelines words <timeline> Astrid --project <project> --beats`.

## Arrange selected shots

```python
from astrid.sdk.timeline.api import Timeline

tl = Timeline.open(project, timeline)

def start_ms(placement):
    return placement["at_ms"] if placement.get("at_ms") is not None else (placement.get("placement") or {}).get("start_ms", 0)

placements = tl.bundle["placements"]
if placements:
    order = [p["occurrence_id"] for p in sorted(placements, key=start_ms)]
    tl.commit(lambda t: t.arrange(order, at=0, gap=0.2), label="Arrange shots",
              idempotency_key="arrange-shots-episode-1")
```

Sort by start because stored order is not time order. Arranging leaves the old
slots empty and refuses to overlap a shot you did not select.

## Re-flow narration

Replace a line's take with
`python3 -m astrid timelines edit <timeline> --project <project> --line <line-id> --take <take-id> --words <words-file>`.
Clips on words follow the new word times. Clips on a removed line become
orphans; move, keep, or remove each one before publishing.

The music bed follows the re-flow policy: `keep-end` (the default) trims it at a
nearby bar so it ends with the film; `plays-on` leaves it whole, so its beats
can drift from later clips. Read back the sound section after a re-flow.

Flat ripple edits (`insert_time`, `ripple_delete`, `close_gap`) move later clips
on every track. They refuse clips with moment strings, animation, or
transitions.
