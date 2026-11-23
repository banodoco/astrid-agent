---
name: rendering
description: Render timelines, inspect render output, author worlds and effects, and add generated visual elements.
---

Editing a timeline? Load the `timeline` skill.

# Rendering

Use `rendering.render` for a published timeline render, only when asked. Inspect renders with the [rendering guide](references/render-inspect-transfer.md), which covers playback and transfer.

An installed host started directly with `--register` refuses startup if its personal collection adds a Remotion-capable pack outside the supplied `--pack-root` roots (`installed Remotion workers require product-owned pack roots`); remove that pack from the collection or start the host through Runtime with its approved pack roots.

## Worlds

Build and inspect live scenes with the [world authoring guide](references/worlds-authoring.md). It covers the Three.js Live Editor and importing a world into a timeline.

## Effects and generated elements

Use the [generated elements guide](references/generated-elements-authoring.md) to author effects and reusable visual elements. Effects live in one project pack (init, validate, admit); then stage placements and related edits in one `Timeline.commit` with `Clip.set_element(element)`. Keep `timelines place-element` for a single-target placement. Never edit built-in or other packs' elements; fork them under a new id. No core or vendored code change is ever needed for an effect. Use [worlds](references/worlds-authoring.md) for live scene work.
