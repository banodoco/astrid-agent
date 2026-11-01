declare module '@workspace-effects/*' {
  import type {EffectComponent} from '@banodoco/timeline-composition/theme-api';
  const component: EffectComponent;
  export default component;
}

declare module '@workspace-animations/*' {
  import type {AnimationComponent} from '@banodoco/timeline-composition/theme-api';
  const component: AnimationComponent;
  export default component;
}

declare module '@workspace-transitions/*' {
  import type {TransitionComponent} from '@banodoco/timeline-composition/theme-api';
  const component: TransitionComponent;
  export default component;
}
