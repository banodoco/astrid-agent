/**
 * Live-scene identity types. A timeline clip persists a REFERENCE to its package object; the entry
 * HTML is loaded from the project object store when a reader needs it. Keeping the two shapes as
 * separate types lets the compiler reject any path that writes a loaded document onto a clip.
 */

/** What a timeline clip persists: the package object's identity and its exact JSON text (capped by the runtime). */
export type LiveSceneReference = {
  readonly revision: string;
  readonly source: { readonly objectId: string; readonly revision: string };
  readonly packageBody: string;
  /** Persisted references never carry HTML. Passing a loaded document here is a compile error. */
  readonly html?: never;
};

/** A loaded scene: the reference plus its verified entry HTML. In memory only, never written to a clip. */
export type LiveSceneDocument = {
  readonly revision: string;
  readonly source: { readonly objectId: string; readonly revision: string };
  readonly packageBody: string;
  readonly html: string;
};

/** What a clip's app data may hold: the reference, or a legacy clip's reference with its inline HTML. */
export type StoredLiveScene = LiveSceneReference | LiveSceneDocument;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** Reads the identity fields of a stored or loaded scene; undefined for anything else. Inline HTML is kept only when present. */
export function readLiveScene(value: unknown): StoredLiveScene | undefined {
  if (!isRecord(value) || typeof value.revision !== 'string' || typeof value.packageBody !== 'string'
    || !isRecord(value.source) || typeof value.source.objectId !== 'string' || !value.source.objectId
    || typeof value.source.revision !== 'string') {
    return undefined;
  }
  const base = {
    revision: value.revision,
    source: { objectId: value.source.objectId, revision: value.source.revision },
    packageBody: value.packageBody,
  };
  return typeof value.html === 'string' ? { ...base, html: value.html } : base;
}

/** True only for a scene that carries its entry HTML inline (legacy clips and loaded documents). */
export function hasInlineHtml(value: unknown): value is LiveSceneDocument {
  return readLiveScene(value) !== undefined && isRecord(value) && typeof value.html === 'string';
}

/** The exact persisted reference: identity and package text only. Any HTML on the input is dropped. */
export function persistedLiveScene(value: unknown): LiveSceneReference {
  const stored = readLiveScene(value);
  if (!stored) throw new Error('Live-scene reference is incomplete');
  return {
    revision: stored.revision,
    source: { objectId: stored.source.objectId, revision: stored.source.revision },
    packageBody: stored.packageBody,
  };
}
