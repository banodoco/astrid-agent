---
name: clip-looks
description: Darken the edges of one owning clip with a declared vignette modifier.
---

Resolve `timeline.component/clip_looks.vignette` through the SDK component facade.
Bind its admitted TypeScript export to the existing modifier host and inject the
returned declaration in `modifierRegistry`. Use stable `id`, exact `element`, and
`params: {strength: 0.5}` in the owning `clip.modifiers`. Strength is static [0,1].
The fixed radial falloff preserves target bounds, alpha and support; zero is exact
child identity. Unknown bounds and unsupported hosts refuse. This pack adds no
App add-menu integration, CLI edit operation, renderer or independent clock.
