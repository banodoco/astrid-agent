import type {ReactElement} from 'react';

// `EffectProps` is the registry-side contract that the Remotion
// timeline-composition package passes into every effect component. Re-exported
// here as `ElementComponentProps` so first-party packs depend on a stable name
// without having to dip into the upstream package path.
import type {EffectProps} from '@banodoco/timeline-composition/theme-api';

export type {
  AnimationComponent,
  BaseAnimationProps as AnimationComponentProps,
  TransitionComponent,
  TransitionComponentResult as TransitionResult,
} from '@banodoco/timeline-composition/theme-api';

// Contract for effect element components (text-card, model-trends, etc.).
// The registry calls components with `params: unknown` because the upstream
// EffectComponent registry can't know each effect's params shape. Components
// own the boundary: declare a `Params` type, then call `narrowParams` at the
// top of the render to convert `unknown` into a real `Params` value.
export type ElementComponentProps = EffectProps<unknown>;

export type ElementComponent = (
  props: ElementComponentProps,
) => ReactElement | null;

// `narrowParams` validates that `raw` is a non-null object and returns it
// typed as `T`. It does NOT deep-validate field shapes (each component knows
// which fields it touches and tolerates missing ones). It exists to make the
// boundary explicit instead of an `as T` lie at every call site.
export const narrowParams = <T extends object>(raw: unknown): T => {
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    return raw as T;
  }
  return {} as T;
};
