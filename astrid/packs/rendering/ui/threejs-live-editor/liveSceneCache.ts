import { hasInlineHtml, readLiveScene, type LiveSceneDocument } from './liveSceneDocument';

/**
 * The part of the editor's project object storage a scene reader needs. Declared locally and
 * structurally so this module (and SceneSurface, which the Remotion build bundles) never imports the
 * editor SDK; the SDK's ProjectObjectStorage satisfies it.
 */
export interface SceneObjectStorage {
  read(objectId: string): Promise<Uint8Array>;
}
type ProjectObjectStorage = SceneObjectStorage;

/** Loads and integrity-verifies one scene from the object store (authoring.loadLiveSceneSource). */
export type LiveSceneLoader = (storage: SceneObjectStorage, objectId: string, revision: string) => Promise<LiveSceneDocument>;

type Entry = { readonly promise: Promise<LiveSceneDocument>; document?: LiveSceneDocument };

export type LiveSceneDocuments = {
  /** The document when it is already in memory for this storage. Never starts a load. */
  peek(storage: ProjectObjectStorage, objectId: string, revision: string): LiveSceneDocument | undefined;
  /** Loads each object and revision once per storage. A failed load is forgotten so a later mount retries it. */
  load(storage: ProjectObjectStorage, objectId: string, revision: string): Promise<LiveSceneDocument>;
};

const unconfiguredLoader: LiveSceneLoader = async () => {
  throw new Error('No live-scene loader is configured');
};

let configuredLoader: LiveSceneLoader = unconfiguredLoader;

/**
 * Installs the loader used for references without inline HTML. The editor entry points (the extension
 * and the explorer panel) install one that calls authoring.loadLiveSceneSource; the Remotion build
 * never does, because every render path hands it inline HTML through the props.
 */
export function configureLiveSceneLoader(loader: LiveSceneLoader): void {
  configuredLoader = loader;
}

export function createLiveSceneDocuments(loader: LiveSceneLoader = (...args) => configuredLoader(...args)): LiveSceneDocuments {
  const scopes = new WeakMap<ProjectObjectStorage, Map<string, Entry>>();
  const entriesFor = (storage: ProjectObjectStorage): Map<string, Entry> => {
    let entries = scopes.get(storage);
    if (!entries) {
      entries = new Map();
      scopes.set(storage, entries);
    }
    return entries;
  };
  return {
    peek(storage, objectId, revision) {
      return entriesFor(storage).get(`${objectId}@${revision}`)?.document;
    },
    load(storage, objectId, revision) {
      const entries = entriesFor(storage);
      const key = `${objectId}@${revision}`;
      const existing = entries.get(key);
      if (existing) return existing.promise;
      const promise = Promise.resolve().then(() => loader(storage, objectId, revision)).then(
        (document) => {
          entry.document = document;
          return document;
        },
        (error: unknown) => {
          if (entries.get(key) === entry) entries.delete(key);
          throw error;
        },
      );
      const entry: Entry = { promise };
      entries.set(key, entry);
      return promise;
    },
  };
}

/** The shared cache for the editor. Tests build their own with a fake loader. */
export const liveSceneDocuments: LiveSceneDocuments = createLiveSceneDocuments();

/**
 * The document a reader needs for a stored value. Inline HTML (legacy clips, loaded documents) is used
 * as-is and never reloaded; a reference without HTML is loaded from the object store once and cached.
 */
export function resolveLiveScene(
  storage: ProjectObjectStorage,
  value: unknown,
  documents: LiveSceneDocuments = liveSceneDocuments,
): Promise<LiveSceneDocument> {
  if (hasInlineHtml(value)) return Promise.resolve(value);
  const stored = readLiveScene(value);
  if (!stored) return Promise.reject(new Error('Missing scene source/revision'));
  return documents.load(storage, stored.source.objectId, stored.revision);
}

/** The document synchronously, when it is inline or already loaded; undefined otherwise. */
export function peekLiveScene(
  storage: ProjectObjectStorage | undefined,
  value: unknown,
  documents: LiveSceneDocuments = liveSceneDocuments,
): LiveSceneDocument | undefined {
  if (hasInlineHtml(value)) return value;
  const stored = readLiveScene(value);
  if (!storage || !stored) return undefined;
  return documents.peek(storage, stored.source.objectId, stored.revision);
}
