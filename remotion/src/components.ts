import * as host from '@banodoco/timeline-composition/theme-api';
import type {ModifierDeclaration, ModifierDefinition, ModifierRegistry} from '@banodoco/timeline-composition/theme-api';

// Normalize contributed React-style registrations before Composition captures
// its canonical component; an explicit canonical value keeps precedence.
const componentHost = {
  ...host,
  declareModifier<TParams>(definition: ModifierDefinition<TParams> | (
    Omit<ModifierDefinition<TParams>, 'component'> & {Component: ModifierDefinition<TParams>['component']}
  )): ModifierDeclaration {
    return host.declareModifier('component' in definition
      ? definition
      : {...definition, component: definition.Component});
  },
};

export type ComponentBinding = {
  identity: string;
  implementation_digest: string;
  key: string;
  descriptor: {point: string; id: string; hosts: readonly string[];
    implementation: {export: string}};
  module: Record<string, unknown>;
};

export const bindComponentRegistry = (bindings: readonly ComponentBinding[]): ModifierRegistry => {
  const registry: Record<string, ModifierDeclaration> = {};
  for (const binding of bindings) {
    const {identity, implementation_digest: digest, descriptor, module} = binding;
    if (descriptor.point !== 'timeline.component' || identity !== `${descriptor.point}/${descriptor.id}`
      || !/^[a-f0-9]{64}$/.test(digest) || binding.key !== `${identity}@${digest}`
      || !descriptor.hosts.includes('composition') || Object.prototype.hasOwnProperty.call(registry, identity)) {
      throw new Error(`Invalid native component binding: ${identity}`);
    }
    const factory = module[descriptor.implementation.export];
    if (typeof factory !== 'function') throw new Error(`Missing declared component export: ${identity}`);
    const declaration = factory(componentHost, descriptor) as ModifierDeclaration;
    if (!declaration || declaration.element !== identity || declaration.kind !== 'modifier'
      || !declaration.hosts.includes('composition') || typeof declaration.wrap !== 'function') {
      throw new Error(`Component declaration identity mismatch: ${identity}`);
    }
    registry[identity] = declaration;
  }
  return Object.freeze(registry);
};
