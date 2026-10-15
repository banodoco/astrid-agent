---
name: rendering
description: Render and inspect output from an existing runtime timeline with the supported CLI and rendering capabilities.
---

# Rendering

Use this skill when the requested result needs rendered video or render
evidence. For timeline discovery and edits, follow the
[timeline editing skill](../../timeline/docs/SKILL.md), which owns the
find → show → edit → see → publish loop. A rendered export is an optional
downstream step after the timeline is validated and published.

## Preview and render

Use `timelines visualize` from the timeline skill for a bounded composed
preview. Render when the user requests an export or when the task needs a
rendered artifact:

```bash
python3 -m astrid timelines render <timeline> --project <project> --json
```

The command returns the managed run and its output receipt. Inspect those
through the Runtime. Rendering requires a configured workspace and an admitted
renderer capability. Use the exact timeline slug or ID, and retain the
returned run identity with the result.

For a request specifically asking to edit the complete JSON authoring bundle,
use the existing [document checkout reference](references/document-checkout.md).
Ordinary timing, clip, text, and layout changes belong on the CLI edit route in
the [timeline skill](../../timeline/docs/SKILL.md).

## SDK

Embedded programs can use the public SDK invocation form:

```python
import astrid.sdk as sdk

result = sdk.invoke(
    "rendering.render",
    kind="action",
    project="<project>",
    inputs={"timeline_ref": "<timeline>"},
)
result.raise_for_error()
```

Use a returned receipt or run ID for later inspection. The input is a managed
timeline reference, not a local timeline file.

## Renderer contributions

When adding or extending a renderer, read the
[pack-builder skill](../../_core/docs/pack-builder/SKILL.md) and
[render backend contract](../../../../docs/contracts/render-backend-v1.md).
Renderer packs declare their contribution through the manifest; timeline
editing and publication stay on the public Runtime route.

For native Three.js scene code or a `com.reigh.astrid.liveScene` clip, read
[live-scene authoring](references/live-scenes-authoring.md) for the registered
extension and its supported edit and publication flow.
