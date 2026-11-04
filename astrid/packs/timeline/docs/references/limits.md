# Timeline editing limits

Load `timeline` for the supported path. These operations are not available in
one native edit:

- Ripple on one track only.
- Editing existing text in place with `clip.set(text=...)`.
- One commit or undo across several timelines; loop once per timeline.
- A preview or reviewed list as a file. The Python SDK holds it in memory.
- Finding clips by what their pixels show. Use an understanding tool to find
  asset keys, then read clips by asset.
- A flat ripple across clips with moment strings, animation, or transitions;
  it refuses those edits.

Python can create an empty timeline with
`client.timelines.create_empty(project=..., config=...)`; the CLI `timelines
create` also makes an empty timeline.
