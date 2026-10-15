import { createElement } from 'react';
import { admitModifiers, ModifierAdmissionError } from './modifier-admission';
export { ModifierAdmissionError } from './modifier-admission';
// The component is a child-content subtype, never an EffectComponent/overlay.
export const declareModifier = (definition) => ({
    kind: definition.kind,
    element: definition.element,
    hosts: definition.hosts,
    bounds: definition.bounds,
    alpha: definition.alpha,
    support: definition.support,
    wrap: (instance, context, children) => createElement(definition.component, {
        key: instance.id,
        instanceId: instance.id,
        params: definition.resolveParams(instance.params, context),
        context,
        children,
    }),
});
export const composeModifiers = ({ instances, registry, context, content, host = 'composition', onFinding }) => {
    if (instances.length === 0)
        return content;
    const declarations = admitModifiers(instances, registry, host);
    if (context.bounds.status === 'unknown') {
        onFinding?.({ code: 'unknown-modifier-bounds', clipId: context.clip.id, reason: context.bounds.reason });
        throw new ModifierAdmissionError('unknown-bounds', `Cannot apply modifiers to ${context.clip.id}: ${context.bounds.reason}`);
    }
    const rect = context.bounds.rect;
    if (![rect.x, rect.y, rect.width, rect.height, context.frame, context.fps,
        context.keyframeFrame, context.keyframeTime, context.globalFrame].every(Number.isFinite)
        || context.fps <= 0 || rect.width < 0 || rect.height < 0) {
        throw new ModifierAdmissionError('invalid-context', 'Modifier requires finite evaluated clock and nonnegative target bounds');
    }
    // Authored [A, B] is B(A(content)); no animation-hook grouping or reversal.
    return instances.reduce((children, instance, index) => declarations[index].wrap(instance, context, children), content);
};
