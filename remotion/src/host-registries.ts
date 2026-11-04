import {resolveElementRegistries} from '@banodoco/timeline-composition/registries';
import type {CompositionRegistries} from '@banodoco/timeline-composition/registries';
import type {Theme} from '@banodoco/timeline-composition/theme-api';
import {EFFECT_REGISTRY, CLIP_TYPE_ALIASES} from './effects.generated';
import {ANIMATION_REGISTRY, ANIMATION_DEFAULTS, ANIMATION_META} from './animations.generated';
import {TRANSITION_REGISTRY, TRANSITION_DEFAULTS, TRANSITION_META} from './transitions.generated';

// Pass the complete discovered host maps to the shared render/evaluation path.
export const resolveHostRegistries = (theme: Pick<Theme, 'id'>): CompositionRegistries => {
  const bundled = resolveElementRegistries(theme);
  return {
    // The package's heterogeneous effect map uses never for its parameter slot.
    effects: {...bundled.effects, ...EFFECT_REGISTRY as CompositionRegistries['effects']},
    clipTypeAliases: {...bundled.clipTypeAliases, ...CLIP_TYPE_ALIASES},
    animations: {...bundled.animations, ...ANIMATION_REGISTRY},
    animationDefaults: {...bundled.animationDefaults, ...ANIMATION_DEFAULTS},
    animationMeta: {...bundled.animationMeta, ...ANIMATION_META},
    transitions: {...bundled.transitions, ...TRANSITION_REGISTRY},
    transitionDefaults: {...bundled.transitionDefaults, ...TRANSITION_DEFAULTS},
    transitionMeta: {...bundled.transitionMeta, ...TRANSITION_META},
  };
};
