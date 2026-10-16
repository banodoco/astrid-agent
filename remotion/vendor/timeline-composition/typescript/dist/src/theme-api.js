/**
 * Stable public theme API (Sprint 4 surface; Sprint 5 physically lifted).
 *
 * Sprint 4 introduced this re-export to give theme components a stable import
 * sub-path (`@banodoco/timeline-composition/theme-api`) instead of deep
 * relative paths. Sprint 5 physically moved the source modules into this
 * package; the re-export shape is unchanged so theme packages migrated under
 * Sprint 4 keep compiling without churn.
 */
// lib/animations — runtime helpers that compose animation references for
// effect components. composeAnimations is the primary one used by
// themed-effect components.
export { composeAnimations, normalizeAnimationReferences, resolveAnimationReferences, } from "./lib/animations";
// ThemeContext — useTheme hook + types theme components use to read
// resolved theme values at render time.
export { ThemeProvider, useTheme, DEFAULT_THEME, } from "./ThemeContext";
export { keyframeOrigin, timelineKeyframeTime, spriteKeyframeFrame, declareKeyframeOrigin } from './lib/keyframe-origin';
export { loopPhase, positiveModulo, spriteSheetIndex } from './lib/beat-loop';
export { MODIFIER_REGISTRY, composeModifiers, declareModifier, ModifierAdmissionError } from './modifiers';
export { mediaModifierBounds } from './lib/modifier-bounds';
export { interpolateKeyframeInterval } from './lib/keyframe-easing';
export { motionCurveName, motionCurveProgress, motionCurveInverse } from './lib/motion-curves.generated';
