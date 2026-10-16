// Same exact-key dispatch shape as EFFECT_REGISTRY. No product modifier is
// admitted yet. Unknown entries refuse; consumers bind admitted declarations.
export const MODIFIER_REGISTRY = Object.freeze({});
export { composeModifiers, declareModifier, ModifierAdmissionError } from './lib/modifiers';
