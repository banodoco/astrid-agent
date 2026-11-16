import { describe, expect, it, vi } from 'vitest';
import type { ProjectObjectMetadata, TimelineSnapshot } from '@astrid/editor-sdk';
import {
  ingestPreparedPackage,
  parsePreparedScenePackage,
  placementOperations,
  publishLiveSceneEdit,
  type LiveScenePackageInput,
  type LiveScenePublicationCapture,
} from '../../../astrid/packs/rendering/ui/threejs-live-editor/authoring';
import { THREEJS_LIVE_EDITOR_EXTENSION_ID, LIVE_SCENE_CLIP_TYPE_ID } from '../../../astrid/packs/rendering/ui/threejs-live-editor/identity';
import {
  hasInlineHtml,
  persistedLiveScene,
  readLiveScene,
  type LiveSceneDocument,
  type LiveSceneReference,
} from '../../../astrid/packs/rendering/ui/threejs-live-editor/liveSceneDocument';
import {
  createLiveSceneDocuments,
  peekLiveScene,
  resolveLiveScene,
  type LiveSceneLoader,
} from '../../../astrid/packs/rendering/ui/threejs-live-editor/liveSceneCache';
import { SAMPLE_WORLD } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/worlds/sample';

const encode = (value: string) => new TextEncoder().encode(value);

async function digest(bytes: Uint8Array): Promise<string> {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', bytes as unknown as BufferSource);
  return `sha256:${Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

/** Content-addressed in-memory object store with real digests. */
function makeStorage(objects = new Map<string, Uint8Array>()) {
  const reads: string[] = [];
  return {
    objects,
    reads,
    async ingest(input: Uint8Array, mediaType: string, filename?: string): Promise<ProjectObjectMetadata> {
      const bytes = new Uint8Array(input);
      const id = await digest(bytes);
      objects.set(id, bytes);
      return { object_id: id, digest: id, media_type: mediaType, size: bytes.byteLength, filename };
    },
    async read(objectId: string): Promise<Uint8Array> {
      reads.push(objectId);
      const value = objects.get(objectId);
      if (!value) throw new Error(`unknown object ${objectId}`);
      return new Uint8Array(value);
    },
  };
}

const HTML = '<html><head></head><body>Lantern</body></html>';
const EXTENSION_ID = THREEJS_LIVE_EDITOR_EXTENSION_ID;

function packageInput(html = HTML): LiveScenePackageInput {
  return {
    manifest: { formatVersion: 1, entry: 'scene.html', duration: 4, authoredFps: 30 },
    entry: { bytes: encode(html), mediaType: 'text/html', filename: 'scene.html' },
    assets: [],
  };
}

function snapshotWith(clips: TimelineSnapshot['clips'], version = 7): TimelineSnapshot {
  return {
    projectId: 'project-a',
    timelineId: 'timeline-a',
    baseVersion: version,
    currentVersion: version,
    extensionRequirements: [],
    clips,
    tracks: [{ id: 'V1', kind: 'visual', label: 'V1', muted: false }],
    assetKeys: [],
    app: {},
  } as unknown as TimelineSnapshot;
}

function clipFor(id: string, liveScene: unknown, sourceObjectId: string, packageRevision: string) {
  return {
    id,
    track: 'V1',
    at: 0,
    clipType: LIVE_SCENE_CLIP_TYPE_ID,
    duration: 4,
    sourceOffset: 0,
    sourceEnd: 4,
    rate: 1,
    app: { liveScene },
    managed: true,
    managedBy: EXTENSION_ID,
    sourceRefs: [{
      id: `source.live-scene.${sourceObjectId}.${id}`,
      clipId: id,
      sourceKind: 'provider' as const,
      extensionId: EXTENSION_ID,
      sourceObjectId,
      sourceRevision: packageRevision,
      packageRevision,
    }],
  };
}

describe('persisted live-scene reference', () => {
  it('the bundled Lantern Valley sample places as a small reference with no html key', async () => {
    const storage = makeStorage();
    const parsed = await parsePreparedScenePackage(encode(SAMPLE_WORLD.packageJson));
    // The entry HTML is the bulk of the sample; the clip must not carry it.
    expect(parsed.entry.bytes.byteLength).toBeGreaterThan(500_000);
    const { liveScene } = await ingestPreparedPackage(storage, parsed);
    const ops = placementOperations(
      snapshotWith([]),
      [{ id: 'lantern-1', track: 'V1', at: 0, from: 0, to: parsed.manifest.duration, rate: 1 }],
      liveScene,
    );
    const json = JSON.stringify(ops);
    expect(json.length).toBeLessThan(100_000);
    expect(json).not.toContain('"html"');
    const update = ops.find((op) => op.op === 'clip.update');
    expect(update).toMatchObject({ op: 'clip.update', payload: { app: { liveScene } } });
    expect(Object.keys((update!.payload as { app: { liveScene: object } }).app.liveScene).sort())
      .toEqual(['packageBody', 'revision', 'source']);
  });

  it('placement drops inline HTML even when handed a loaded document', () => {
    const document: LiveSceneDocument = {
      revision: 'sha256:' + 'a'.repeat(64),
      source: { objectId: 'pkg', revision: 'sha256:' + 'a'.repeat(64) },
      packageBody: '{"manifest":{}}',
      html: '<html>must not persist</html>',
    };
    // A document is not a reference: the type rejects it, and the runtime strips it as a second guard.
    const ops = placementOperations(snapshotWith([]), [{ id: 'c', track: 'V1', at: 0, from: 0, to: 4, rate: 1 }], document as unknown as LiveSceneReference);
    expect(JSON.stringify(ops)).not.toContain('must not persist');
    expect(ops.find((op) => op.op === 'clip.update')?.payload).toMatchObject({ app: { liveScene: { packageBody: document.packageBody } } });
  });

  it('persistedLiveScene keeps exactly the identity fields', () => {
    const stored = readLiveScene({ revision: 'r', source: { objectId: 'o', revision: 'r' }, packageBody: '{}', html: '<x/>', extra: 1 });
    expect(stored).toEqual({ revision: 'r', source: { objectId: 'o', revision: 'r' }, packageBody: '{}', html: '<x/>' });
    expect(persistedLiveScene(stored)).toEqual({ revision: 'r', source: { objectId: 'o', revision: 'r' }, packageBody: '{}' });
    expect(hasInlineHtml(stored)).toBe(true);
    expect(hasInlineHtml(persistedLiveScene(stored))).toBe(false);
    expect(readLiveScene({ revision: 'r', source: { objectId: '' , revision: 'r' }, packageBody: '{}' })).toBeUndefined();
    expect(() => persistedLiveScene({ revision: 'r' })).toThrow('incomplete');
  });
});

describe('live-scene document cache', () => {
  const docFor = (objectId: string, revision: string): LiveSceneDocument => ({
    revision, source: { objectId, revision }, packageBody: '{}', html: `<html>${objectId}</html>`,
  });

  it('uses inline HTML as-is and never calls the loader', async () => {
    const loader = vi.fn<LiveSceneLoader>();
    const documents = createLiveSceneDocuments(loader);
    const inline = docFor('o', 'r');
    await expect(resolveLiveScene(makeStorage(), inline, documents)).resolves.toBe(inline);
    expect(peekLiveScene(makeStorage(), inline, documents)).toBe(inline);
    expect(loader).not.toHaveBeenCalled();
  });

  it('loads a reference once per revision, shares concurrent loads, and serves re-mounts from memory', async () => {
    const loader = vi.fn<LiveSceneLoader>(async (_storage, objectId, revision) => docFor(objectId, revision));
    const documents = createLiveSceneDocuments(loader);
    const storage = makeStorage();
    const reference: LiveSceneReference = { revision: 'r1', source: { objectId: 'pkg', revision: 'r1' }, packageBody: '{}' };

    expect(peekLiveScene(storage, reference, documents)).toBeUndefined();
    const [first, second] = await Promise.all([
      resolveLiveScene(storage, reference, documents),
      resolveLiveScene(storage, reference, documents),
    ]);
    expect(first).toEqual(docFor('pkg', 'r1'));
    expect(second).toBe(first);
    expect(loader).toHaveBeenCalledTimes(1);

    // A re-mount (scrubbing, a new frame, a new candidate) reads memory only.
    expect(peekLiveScene(storage, reference, documents)).toBe(first);
    await expect(resolveLiveScene(storage, reference, documents)).resolves.toBe(first);
    expect(loader).toHaveBeenCalledTimes(1);

    // Another storage is a separate scope and loads again.
    await resolveLiveScene(makeStorage(), reference, documents);
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('a missing object rejects with the loader error, and a later mount retries', async () => {
    let attempts = 0;
    const loader = vi.fn<LiveSceneLoader>(async (_storage, objectId, revision) => {
      attempts += 1;
      if (attempts === 1) throw new Error('unknown object pkg');
      return docFor(objectId, revision);
    });
    const documents = createLiveSceneDocuments(loader);
    const storage = makeStorage();
    const reference = { revision: 'r1', source: { objectId: 'pkg', revision: 'r1' }, packageBody: '{}' };

    await expect(resolveLiveScene(storage, reference, documents)).rejects.toThrow('unknown object pkg');
    expect(peekLiveScene(storage, reference, documents)).toBeUndefined();
    await expect(resolveLiveScene(storage, reference, documents)).resolves.toMatchObject({ html: '<html>pkg</html>' });
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it('rejects a value that is neither inline nor a reference', async () => {
    const loader = vi.fn<LiveSceneLoader>();
    await expect(resolveLiveScene(makeStorage(), { revision: 'r' }, createLiveSceneDocuments(loader))).rejects.toThrow('Missing scene source/revision');
    expect(loader).not.toHaveBeenCalled();
  });
});

describe('publication source checks accept both clip shapes', () => {
  async function timelineWith(stored: 'reference' | 'legacy' | 'legacy-mismatch') {
    const storage = makeStorage();
    const input = packageInput();
    const { package: pkg, liveScene } = await ingestPreparedPackage(storage, input);
    const entryRevision = await digest(input.entry.bytes);
    const legacyHtml = stored === 'legacy-mismatch' ? '<html>stale cache</html>' : HTML;
    const clipValue = stored === 'reference' ? liveScene : { ...liveScene, html: legacyHtml };
    const clips = [clipFor('scene-a', clipValue, pkg.object_id, pkg.digest), clipFor('scene-b', clipValue, pkg.object_id, pkg.digest)];
    const applied: Array<{ target: string; app: Record<string, unknown> }> = [];
    const snapshot = snapshotWith(clips);
    const ctx = {
      creative: {
        projectObjects: storage,
        reader: { snapshot: () => snapshot },
        timeline: {
          validate: () => ({ valid: true, diagnostics: [] }),
          apply: (patch: { operations: Array<{ op: string; target: string; payload: { app?: Record<string, unknown> } }> }) => {
            for (const op of patch.operations) {
              if (op.op !== 'clip.update') continue;
              applied.push({ target: op.target, app: op.payload.app ?? {} });
              const clip = clips.find((candidate) => candidate.id === op.target)!;
              clip.app = { ...clip.app, ...op.payload.app };
            }
            return { affectedObjectIds: patch.operations.map((op) => op.target) };
          },
          flush: async () => ({ version: 8 }),
        },
      },
    };
    const capture: LiveScenePublicationCapture = {
      projectId: 'project-a', timelineId: 'timeline-a', capturedTimelineVersion: 7,
      packageRevision: pkg.digest, entryRevision,
    };
    return { ctx, clips, applied, capture, storage };
  }

  it('publishes over a reference-only clip and writes a reference-only clip back', async () => {
    const { ctx, clips, applied, capture } = await timelineWith('reference');
    const next = packageInput('<html>Edited</html>');
    const result = await publishLiveSceneEdit(ctx as never, {
      placementIds: ['scene-a', 'scene-b'], package: next, capture,
    });
    expect(result.affectedPlacements).toEqual(['scene-a', 'scene-b']);
    expect(applied).toHaveLength(2);
    for (const clip of clips) {
      const scene = clip.app.liveScene as Record<string, unknown>;
      expect(scene).not.toHaveProperty('html');
      expect(JSON.stringify(clip.app)).not.toContain('"html"');
      expect(scene.revision).toBe(result.revision);
    }
  });

  it('publishes over a legacy clip whose inline HTML matches the entry, and migrates it to a reference', async () => {
    const { ctx, clips, capture } = await timelineWith('legacy');
    await publishLiveSceneEdit(ctx as never, { placementIds: ['scene-a', 'scene-b'], package: packageInput('<html>Edited</html>'), capture });
    for (const clip of clips) expect(clip.app.liveScene).not.toHaveProperty('html');
  });

  it('rejects a legacy clip whose inline HTML disagrees with the canonical entry, before any apply', async () => {
    const { ctx, applied, capture } = await timelineWith('legacy-mismatch');
    await expect(publishLiveSceneEdit(ctx as never, { placementIds: ['scene-a', 'scene-b'], package: packageInput('<html>Edited</html>'), capture }))
      .rejects.toThrow('HTML cache does not match the canonical entry');
    expect(applied).toHaveLength(0);
  });
});
