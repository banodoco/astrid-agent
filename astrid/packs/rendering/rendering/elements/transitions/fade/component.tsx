import type {TransitionResult} from '../../../../shared/element_contracts';

// Builtin null-pair: themes override this with real transition presentation/timing.
export default function FadeTransition(): TransitionResult {
  return {presentation: null, timing: null};
}
