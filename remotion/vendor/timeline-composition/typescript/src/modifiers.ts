import type {ModifierRegistry} from './modifier-types';

// Same exact-key dispatch shape as EFFECT_REGISTRY. No product modifier is
// admitted yet. Unknown entries refuse; consumers bind admitted declarations.
export const MODIFIER_REGISTRY: ModifierRegistry = Object.freeze({});

export {composeModifiers, declareModifier, ModifierAdmissionError} from './lib/modifiers';
export type {
  ClipModifierReference, ModifierBounds, ModifierContext, ModifierDeclaration,
  ModifierDefinition, ModifierFinding, ModifierProps, ModifierRegistry,
} from './modifier-types';
