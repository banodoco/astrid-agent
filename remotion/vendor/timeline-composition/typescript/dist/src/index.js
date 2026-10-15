/**
 * @banodoco/timeline-composition
 *
 * Sprint 5: real composition + theme-api + plugin registry. The Banodoco
 * CLI render path imports `TimelineComposition` from here; Reigh's
 * TimelineRenderer imports the EFFECT_REGISTRY-style dispatch entries
 * via the codegenned `registry.generated.ts`.
 */
export const TIMELINE_COMPOSITION_SCAFFOLD = "sprint-5";
// Composition surface (renamed Sprint 5: HypeComposition → TimelineComposition).
export { TimelineComposition, HypeComposition } from "./TimelineComposition";
// Theme api re-exports (the Sprint 4 stable sub-path also still works).
export { composeAnimations, normalizeAnimationReferences, resolveAnimationReferences, } from "./lib/animations";
export { ThemeProvider, useTheme, DEFAULT_THEME, toRuntimeTheme, } from "./ThemeContext";
// Duration helper used by the bundle root for calculateMetadata.
export { getTimelineDurationInFrames } from "./lib/duration";
// Plugin registry — codegenned from installed `@banodoco/timeline-theme-*`
// packages (Reigh-side) and from `themes/<id>/effects/` (Banodoco-side).
export { THEME_PACKAGE_REGISTRY, THEME_PACKAGE_CLIP_TYPES, } from "./registry.generated";
export { MODIFIER_REGISTRY, composeModifiers, declareModifier, ModifierAdmissionError } from './modifiers';
export { mediaModifierBounds } from './lib/modifier-bounds';
export { prepareCompositionScene } from './TimelineComposition';
export { prepareScene, querySceneFrame, queryCompositionSceneFrame } from './evaluation';
