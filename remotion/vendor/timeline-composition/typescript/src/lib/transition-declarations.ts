// Authoring admission only. Legacy references keep their existing renderer path;
// admitted declarations use the same frame planner, never repair clip timing.
import type {Align, ClipSpan, TransitionSpec} from './transition-spans';

export type AppTransition = {
  type: string;
  revision?: string; // contributed resource digest; builtins retain their legacy shape
  duration: number; // seconds, as stored by the App
  params?: Record<string, unknown>;
};

export type TransitionImplementation = Readonly<{host: 'timeline-composition'; id: 'cross-fade' | 'fade'}>;

// The existing registry/closure supplies this resource and its expected reference.
// Composition validates it; it does not resolve contribution IDs or activate packs.
export type TransitionContributionResource = Readonly<{
  owner: string;
  id: string;
  kind: 'transition';
  transitionId: string;
  implementation: TransitionImplementation;
  // The existing planner implements linear opacity only. Never accept ignored knobs.
  supportedParams: readonly 'easing'[];
  defaults: Readonly<{easing?: 'linear'}>;
}>;
export type TransitionContributionReference = Readonly<{
  owner: string; id: string; transitionId: string; revision: string;
}>;
export type AdmittedTransitionContribution = Readonly<{
  resource: TransitionContributionResource;
  revision: string;
}>;
const admittedContributions = new WeakSet<AdmittedTransitionContribution>();

export type TransitionDeclaration = {
  owner: {trackId: string; clipId: string}; // outgoing clip owns the boundary
  implementation: TransitionImplementation;
  contribution?: AdmittedTransitionContribution;
  spec: TransitionSpec;
  params: Record<string, unknown>;
};

export class TransitionAdmissionError extends Error {
  readonly code: string;
  readonly nextStep: string;

  constructor(code: string, message: string, nextStep: string) {
    super(`${message} Next step: ${nextStep}`);
    this.name = 'TransitionAdmissionError';
    this.code = code;
    this.nextStep = nextStep;
  }
}

const refuse = (code: string, message: string, nextStep: string): never => {
  throw new TransitionAdmissionError(code, message, nextStep);
};

const requireHost = (host: string): void => {
  if (host !== 'timeline-composition') {
    refuse('transition/unsupported-host', `Transition host '${host}' is unsupported.`,
      'Use the timeline-composition host.');
  }
};

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === 'object' && !Array.isArray(value)
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
const text = (value: unknown): value is string => typeof value === 'string' && value.trim().length > 0;
const builtin = (type: string): boolean => ['crossfade', 'cross-fade', 'fade'].includes(type);

const validateResource = (resource: TransitionContributionResource): void => {
  if (!object(resource) || !text(resource.owner) || !text(resource.id) || resource.kind !== 'transition'
      || !text(resource.transitionId) || builtin(resource.transitionId)
      || Object.keys(resource).some(key => !['owner', 'id', 'kind', 'transitionId', 'implementation', 'supportedParams', 'defaults'].includes(key))) {
    refuse('transition/invalid-resource', 'Contribution identity is malformed or shadows a protected builtin.',
      'Supply the exact namespaced transition resource from the active registry.');
  }
  if (!object(resource.implementation)) {
    refuse('transition/invalid-binding', 'Contribution implementation binding is missing.', 'Repin the complete resource.');
  }
  requireHost(resource.implementation.host);
  if (!['cross-fade', 'fade'].includes(resource.implementation.id)
      || Object.keys(resource.implementation).some(key => !['host', 'id'].includes(key))) {
    refuse('transition/unsupported-implementation', 'Contribution implementation is unsupported.', 'Use cross-fade or fade.');
  }
  if (!Array.isArray(resource.supportedParams) || new Set(resource.supportedParams).size !== resource.supportedParams.length
      || resource.supportedParams.some(key => key !== 'easing') || !object(resource.defaults)
      || Object.entries(resource.defaults).some(([key, value]) => key !== 'easing' || value !== 'linear'
        || !resource.supportedParams.includes(key))) {
    refuse('transition/invalid-params', 'Contribution declares parameters the planner cannot implement.',
      'Declare only optional easing=linear, or no parameters.');
  }
};

// Canonical UTF-8 digest input. Revision = "sha256:" + lowercase SHA-256 hex.
// Sorted parameter names make declaration order irrelevant; no implementation copy
// is stored in the clip. Changing owner, IDs, implementation or defaults repins it.
export const transitionContributionIdentity = (resource: TransitionContributionResource): string => {
  validateResource(resource);
  return JSON.stringify({owner: resource.owner, id: resource.id, kind: resource.kind,
    transitionId: resource.transitionId,
    implementation: {host: resource.implementation.host, id: resource.implementation.id},
    supportedParams: [...resource.supportedParams].sort(), defaults: {...resource.defaults}});
};

export const admitTransitionContribution = async (
  resource: TransitionContributionResource,
  reference: TransitionContributionReference,
  active: boolean,
): Promise<AdmittedTransitionContribution> => {
  // Capture before the async digest; subsequent registry mutation cannot swap it.
  const identity = transitionContributionIdentity(resource);
  if (active !== true) {
    refuse('transition/inactive-contribution', 'Contribution is inactive.', 'Activate the pinned contribution before admission.');
  }
  if (!object(reference) || !text(reference.owner) || !text(reference.id) || !text(reference.transitionId)
      || typeof reference.revision !== 'string' || !/^sha256:[a-f0-9]{64}$/.test(reference.revision)
      || reference.owner !== resource.owner || reference.id !== resource.id || reference.transitionId !== resource.transitionId) {
    refuse('transition/reference-mismatch', 'Contribution owner, identity or revision is missing or mismatched.',
      'Resolve the exact pinned resource from the existing registry/closure.');
  }
  const revision = reference.revision;
  const snapshot = JSON.parse(identity) as TransitionContributionResource;
  const bytes = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(identity));
  const digest = 'sha256:' + Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
  if (revision !== digest) {
    refuse('transition/revision-mismatch', 'Contribution digest does not match its pinned revision.', 'Explicitly repin the changed resource.');
  }
  Object.freeze(snapshot.implementation);
  Object.freeze(snapshot.supportedParams);
  Object.freeze(snapshot.defaults);
  Object.freeze(snapshot);
  const binding = Object.freeze({resource: snapshot, revision});
  admittedContributions.add(binding);
  return binding;
};

const contributionParams = (params: unknown, binding: AdmittedTransitionContribution): Record<string, unknown> => {
  if (!object(params)) return refuse('transition/invalid-params', 'Contributed transition params must be an object.',
    'Supply only the pinned supported parameters.');
  if (Object.entries(params).some(([key, value]) => key !== 'easing'
      || !binding.resource.supportedParams.includes(key) || value !== 'linear')) {
    refuse('transition/invalid-params', 'Contributed transition params are malformed or unsupported.',
      'Use only the pinned supported parameters and defaults.');
  }
  return {...binding.resource.defaults, ...params};
};

const requireContribution = (binding: AdmittedTransitionContribution): void => {
  if (!binding || !admittedContributions.has(binding)) {
    refuse('transition/invalid-binding', 'Contribution binding has not been admitted.',
      'Admit the exact active resource and pinned reference from the existing closure.');
  }
};

export const declareAppTransition = (
  trackId: string,
  clipId: string,
  transition: AppTransition,
  fps: number,
  host: string,
  contribution?: AdmittedTransitionContribution,
): TransitionDeclaration => {
  requireHost(host);
  if (!text(trackId) || !text(clipId)) {
    refuse('transition/invalid-owner', 'A transition needs one track and outgoing clip owner.',
      'Supply the owning track and outgoing clip IDs.');
  }
  // These are the two implementations present in the admitted composition.
  // The browser's crossfade spelling names the existing cross-fade component.
  const isBuiltin = builtin(transition.type);
  if (!isBuiltin) {
    if (!contribution) {
      refuse('transition/unsupported-implementation', `No admitted contribution for '${transition.type}'.`,
        'Resolve and admit the exact active resource; unknown IDs never fall back.');
    }
    requireContribution(contribution!);
    if (transition.type !== contribution!.resource.transitionId || transition.revision !== contribution!.revision) {
      refuse('transition/reference-mismatch', 'Transition type or revision differs from the admitted contribution.',
        'Use the persisted unique type and exact pinned revision.');
    }
  }
  // Protected builtin names always take their original path, even if a binding is supplied.
  const binding = isBuiltin ? undefined : contribution;
  const id = binding ? binding.resource.implementation.id
    : transition.type === 'crossfade' ? 'cross-fade' : transition.type;
  if (id !== 'cross-fade' && id !== 'fade') {
    refuse('transition/unsupported-implementation', `Transition '${transition.type}' is unsupported by '${host}'.`,
      'Choose cross-fade or fade for this host.');
  }
  const T = Math.round(transition.duration * fps);
  if (!Number.isFinite(fps) || fps <= 0 || !Number.isFinite(transition.duration)
      || transition.duration <= 0 || !Number.isSafeInteger(T) || T <= 0) {
    refuse('transition/invalid-duration', 'Transition duration must resolve to positive whole frames.',
      'Supply a positive duration in seconds and a positive finite frame rate.');
  }
  const params = binding ? contributionParams(transition.params === undefined ? {} : transition.params, binding)
    : transition.params ?? {};
  return {
    owner: {trackId, clipId},
    implementation: binding?.resource.implementation ?? {host: 'timeline-composition', id: id as 'cross-fade' | 'fade'},
    ...(binding ? {contribution: binding} : {}),
    spec: {T, kind: id === 'fade' ? 'dip' : 'dissolve', align: 'end'},
    params: {...params},
  };
};

// Validate resolved frame spans; no sorting, clamping, shifting or timing engine.
// Gaps and touching half-open intervals are valid. Draw-window extensions are
// intentionally excluded: the shared planner owns those transition overlaps.
export const admitDeclaredTransitions = (
  trackId: string,
  clips: readonly (ClipSpan & {id: string})[],
  declarations: readonly TransitionDeclaration[],
  host: string,
): Array<TransitionSpec | null> => {
  requireHost(host);
  const indices = new Map<string, number>();
  for (let index = 0; index < clips.length; index += 1) {
    const clip = clips[index];
    if (!clip.id || indices.has(clip.id) || !Number.isSafeInteger(clip.start)
        || !Number.isSafeInteger(clip.duration) || clip.duration <= 0
        || !Number.isSafeInteger(clip.start + clip.duration)) {
      refuse('transition/invalid-span', 'Track clips need unique IDs and finite whole-frame spans.',
        'Supply unique clip IDs and resolved positive whole-frame durations.');
    }
    indices.set(clip.id, index);
    const previous = clips[index - 1];
    if (previous && clip.start < previous.start + previous.duration) {
      refuse('transition/same-track-overlap',
        `Clips '${previous.id}' and '${clip.id}' overlap or are out of order on track '${trackId}'.`,
        'Move or trim the clips into non-overlapping track order, then retry; gaps are allowed.');
    }
  }
  const transitions: Array<TransitionSpec | null> = clips.map(() => null);
  for (const declaration of declarations) {
    requireHost(declaration.implementation.host);
    if (declaration.contribution) {
      requireContribution(declaration.contribution);
      if (declaration.implementation !== declaration.contribution.resource.implementation) {
        refuse('transition/invalid-binding', 'Declaration implementation differs from its admitted binding.',
          'Pass the exact admitted implementation through declareAppTransition.');
      }
      contributionParams(declaration.params, declaration.contribution);
    }
    const index = indices.get(declaration.owner.clipId);
    if (declaration.owner.trackId !== trackId || index === undefined || index >= clips.length - 1) {
      return refuse('transition/invalid-owner', 'Transition owner must be an outgoing clip on this track.',
        'Attach the transition to one clip with a successor on the same track.');
    }
    if (transitions[index]) {
      refuse('transition/duplicate-owner', `Clip '${declaration.owner.clipId}' has multiple transition declarations.`,
        'Keep exactly one outgoing transition declaration on this clip.');
    }
    const {id} = declaration.implementation;
    const {T, kind, align} = declaration.spec;
    if ((id !== 'cross-fade' && id !== 'fade') || kind !== (id === 'fade' ? 'dip' : 'dissolve')
        || !(['end', 'start', 'center'] as Align[]).includes(align)
        || !Number.isSafeInteger(T) || T <= 0) {
      refuse('transition/invalid-declaration', 'Transition declaration does not match its declared implementation.',
        'Recreate the declaration from the App transition for this host.');
    }
    transitions[index] = {...declaration.spec};
  }
  return transitions;
};
