import {createElement, useId, type ReactNode} from 'react';
import type {
  ModifierDeclaration, ModifierDefinition, ModifierProps,
} from '@banodoco/timeline-composition/theme-api';

type Params = {strength: number};
type Declaration = {
  point: string; id: string; kind: string; hosts: readonly string[];
  bounds?: 'preserve'; alpha: string; support: string;
};
type Host = {
  declareModifier: <T>(definition: ModifierDefinition<T>) => ModifierDeclaration;
};

// Frozen falloff: transparent center through 50% radius; linear attenuation
// to strength at the ellipse edge. The image is opaque: arithmetic multiplication
// changes premultiplied RGB, while multiplying source alpha by exactly one.
export const attenuationImage = (strength: number): string => {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><defs><radialGradient id="v"><stop offset="50%" stop-color="white"/><stop offset="100%" stop-color="rgb(${(1-strength)*100}%,${(1-strength)*100}%,${(1-strength)*100}%)"/></radialGradient></defs><rect width="100" height="100" fill="url(#v)"/></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
};

export const Vignette = ({params, context, children}: ModifierProps<Params>): ReactNode => {
  // React's tree identity avoids collisions between repeated owning occurrences.
  // It is unrelated to time and creates no independent frame origin.
  const filterId = `vignette-${useId().replace(/[^a-zA-Z0-9_-]/g, (c) => `_${c.charCodeAt(0)}_`)}`;
  if (params.strength === 0) return children;
  if (context.bounds.status !== 'known') throw new Error('unknown-target-bounds: vignette target');
  const {x, y, width, height} = context.bounds.rect;
  return createElement('div', {style: {position: 'absolute', inset: 0, filter: `url(#${filterId})`}},
    createElement('svg', {width: 0, height: 0, style: {position: 'absolute'}, 'aria-hidden': true},
      createElement('defs', null,
        createElement('filter', {id: filterId, filterUnits: 'userSpaceOnUse', primitiveUnits: 'userSpaceOnUse',
          x, y, width, height, colorInterpolationFilters: 'sRGB'},
          createElement('feImage', {href: attenuationImage(params.strength), x, y, width, height,
            preserveAspectRatio: 'none', result: 'attenuation'}),
          createElement('feComposite', {in: 'SourceGraphic', in2: 'attenuation', operator: 'arithmetic',
            k1: 1, k2: 0, k3: 0, k4: 0})))), children);
};

// `host` is the existing Composition/theme-api module. It supplies the sole
// declaration wrapper and evaluated context; no renderer or clock is added here.
export const bindVignette = (host: Host, declaration: Declaration): ModifierDeclaration => {
  if (declaration.point !== 'timeline.component' || declaration.kind !== 'modifier'
    || !/^[a-z][a-z0-9_]*\.vignette$/.test(declaration.id)
    || declaration.alpha !== 'preserve' || declaration.support !== 'preserve'
    || declaration.hosts.some((value) => value !== 'composition' && value !== 'app')) {
    throw new Error('Invalid vignette declaration identity/preservation/host');
  }
  const hasDeclaredBounds = Object.prototype.hasOwnProperty.call(declaration, 'bounds');
  if (hasDeclaredBounds && declaration.bounds !== 'preserve') {
    throw new Error('Invalid vignette bounds declaration');
  }
  return host.declareModifier({kind: 'modifier', element: `${declaration.point}/${declaration.id}`,
    hosts: declaration.hosts, bounds: hasDeclaredBounds ? 'preserve' : 'unknown',
    alpha: 'preserve', support: 'preserve',
    component: Vignette,
    resolveParams: (params) => {
      const strength = params.strength;
      if (Object.keys(params).length !== 1 || typeof strength !== 'number'
        || !Number.isFinite(strength) || strength < 0 || strength > 1) {
        throw new Error('Vignette strength must be a finite static number in [0,1]');
      }
      return {strength};
    },
  });
};
