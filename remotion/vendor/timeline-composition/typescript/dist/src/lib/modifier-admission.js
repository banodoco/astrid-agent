export class ModifierAdmissionError extends Error {
    code;
    constructor(code, message) {
        super(message);
        this.code = code;
        this.name = 'ModifierAdmissionError';
    }
}
export const admitModifiers = (instances, registry, host = 'composition') => {
    const ids = new Set();
    // Admit the whole list before invoking any schema resolver/component.
    const declarations = instances.map((instance) => {
        if (typeof instance.id !== 'string' || !instance.id.trim() || ids.has(instance.id)) {
            throw new ModifierAdmissionError('invalid-instance-id', 'Modifier instance ids must be nonempty and unique within their owning clip');
        }
        ids.add(instance.id);
        if (typeof instance.element !== 'string' || !instance.element || !Object.hasOwn(registry, instance.element)) {
            throw new ModifierAdmissionError('unknown-modifier', `No declared modifier ${instance.element}`);
        }
        const declaration = registry[instance.element];
        if (!declaration || declaration.kind !== 'modifier' || declaration.element !== instance.element
            || !Array.isArray(declaration.hosts) || typeof declaration.wrap !== 'function') {
            throw new ModifierAdmissionError('invalid-declaration', `Modifier declaration identity/subtype mismatch for ${instance.element}`);
        }
        if (!declaration.hosts.includes(host)) {
            throw new ModifierAdmissionError('unsupported-host', `${instance.element} does not support ${host}`);
        }
        if ((declaration.bounds !== 'preserve' && declaration.bounds !== 'unknown') || declaration.alpha !== 'preserve' || declaration.support !== 'preserve') {
            throw new ModifierAdmissionError('unsupported-geometry', `${instance.element} must declare preserve/unknown bounds and preserve alpha and support`);
        }
        if (!instance.params || typeof instance.params !== 'object' || Array.isArray(instance.params)) {
            throw new ModifierAdmissionError('invalid-params', `${instance.element} params must be an object`);
        }
        return declaration;
    });
    return declarations;
};
