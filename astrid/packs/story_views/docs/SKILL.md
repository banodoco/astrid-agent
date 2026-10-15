---
name: story_views
description: Inspect exact-revision story chapters and beats through the generic contributed-view surfaces.
---

# Story views

List declarations with `python3 -m astrid timelines show --list-views` or
`python3 -m astrid timelines visualize --list-views`; neither opens Runtime.
Use `--as timeline.view/story_views.story_overview` on show or
`--view timeline.view/story_views.story_overview` on visualize. Supply a project,
timeline and optionally `--revision-id`; a resolved head is pinned before reading.
Outputs retain project, timeline, revision and content digest. Read findings:
unknown/invalid anchors remain visible and unresolved edges stay null.
No draft, media, frame capture, save, journal or mutation authority is exposed.
An unavailable producer or browser transport reports `unsupported`.
