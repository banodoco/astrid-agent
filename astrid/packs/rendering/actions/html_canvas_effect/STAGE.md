# HTML Canvas Effect Executor

Use `rendering.html_canvas_effect` when you want to create a local, editable
Remotion effect element that uses `<HtmlInCanvas>` for canvas post-processing.

This executor scaffolds the element. It does not render the final video and it
does not replace `rendering.render`; timelines should still render through the
normal Remotion timeline compositor.

Dry-run through the SDK:

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "rendering.html_canvas_effect",
    kind="executor",
    project="demo",
    inputs={"effect_id": "glass-product-card", "label": "Glass Product Card"},
    dry_run=True,
)
```

Equivalent SDK call:

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "rendering.html_canvas_effect",
    kind="executor",
    project="demo",
    inputs={
        "effect_id": 'glass-product-card',
        "label": 'Glass Product Card',
    },
    dry_run=True,
)
```

Output:

- `<editable-pack-root>/elements/effects/<effect-id>/component.tsx`
- `<editable-pack-root>/elements/effects/<effect-id>/element.yaml`
- report JSON at `--out`
- preview `timeline.json` and `assets.json` next to the report

Render the preview through the normal renderer, the `rendering.render` executor, with the preview
`timeline.json` and `assets.json` as its inputs (invocation and modes: `../render/STAGE.md`).

Rendering note:

The generated element intentionally declares HTML-in-canvas requirements in its
metadata. Current Astrid render should remain the default path, but using the
element in a timeline requires a Remotion version that exposes
`HtmlInCanvas` (`>=4.0.455`) and may require `--gl=angle` or `--gl=swangle` for
shader-heavy variants.
