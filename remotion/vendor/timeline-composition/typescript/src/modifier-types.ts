import type {ComponentType, ReactElement, ReactNode} from 'react';
import type {RuntimeTheme} from './ThemeContext';
import type {RenderBounds} from './lib/render-bounds';
import type {TimelineClip} from './types';

// Local renderer interface, not a replacement for the canonical schema projection.
export type ClipModifierReference = {
  id: string;
  element: string;
  params: Record<string, unknown>;
};

export type ModifierBounds =
  | {status: 'known'; rect: RenderBounds}
  | {status: 'unknown'; reason: string};

export type ModifierContext = {
  readonly clip: TimelineClip;
  readonly fps: number;
  /** The target's evaluated frame, including existing transition/speed freezes. */
  readonly frame: number;
  readonly keyframeFrame: number;
  readonly keyframeTime: number;
  readonly globalFrame: number;
  readonly bounds: ModifierBounds;
  readonly theme: RuntimeTheme;
};

export type ModifierProps<TParams> = {
  instanceId: string;
  params: TParams;
  context: ModifierContext;
  children: ReactNode;
};

export type ModifierDeclaration = {
  readonly kind: 'modifier';
  /** Exact qualified element identity supplied by the admitted element producer. */
  readonly element: string;
  readonly hosts: readonly string[];
  readonly bounds: 'preserve';
  readonly alpha: 'preserve';
  readonly support: 'preserve';
  readonly wrap: (instance: ClipModifierReference, context: ModifierContext, children: ReactNode) => ReactElement;
};

export type ModifierDefinition<TParams> = Omit<ModifierDeclaration, 'wrap'> & {
  component: ComponentType<ModifierProps<TParams>>;
  /** The declared schema boundary and existing animated-value evaluator.
   * Must reject invalid/unsupported values; the host never guesses a schema or clock.
   */
  resolveParams: (params: Record<string, unknown>, context: ModifierContext) => TParams;
};

export type ModifierRegistry = Readonly<Record<string, ModifierDeclaration>>;

export type ModifierFinding = {
  code: 'unknown-modifier-bounds';
  clipId: string;
  reason: string;
};
