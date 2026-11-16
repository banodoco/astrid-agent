import { describe, expect, it, vi } from 'vitest';
import type {
  ProjectObjectMetadata,
  TimelineDiff,
  TimelineOps,
  TimelineReader,
  TimelineSnapshot,
} from '@astrid/editor-sdk';
import {
  LIVE_SCENE_CLIP_TYPE_ID,
  THREEJS_LIVE_EDITOR_EXTENSION_ID,
  ingestPreparedPackage,
  loadLiveSceneSource,
  parsePreparedMaplePackage,
  placeLiveScene,
  placementOperations,
  updateLiveSceneClip,
  type LiveScenePackageInput,
  type PreparedScenePlacement,
} from '../../../astrid/packs/rendering/ui/threejs-live-editor/authoring';
import { freeVisualTrack, newClipId, stretchDuration } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/model';

// Harness helpers below mirror tests/packs/rendering/test_live_scene_authoring.test.ts (not exported there).

async function digest(value: Uint8Array): Promise<string> {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', value as unknown as BufferSource);
  return `sha256:${Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

function bytes(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

const SCENE_HTML = '<html><body><div id="scene">World</div></body></html>';

async function preparedBytes(html = SCENE_HTML): Promise<Uint8Array> {
  const entry = bytes(html);
  const manifest = { formatVersion: 1, entry: 'world-v1.html', duration: 90, authoredFps: 30 };
  return bytes(JSON.stringify({
    manifest,
    entry: {
      digest: await digest(entry),
      media_type: 'text/html',
      size: entry.byteLength,
      filename: manifest.entry,
    },
    assets: [],
    html,
  }));
}

async function validPrepared(): Promise<LiveScenePackageInput> {
  return parsePreparedMaplePackage(await preparedBytes());
}

type RawClip = {
  id: string;
  at: number;
  from?: number;
  to?: number;
  speed?: number;
  track: string;
  clipType?: string;
  app?: Record<string, unknown>;
};

type Track = { id: string; kind: 'visual' | 'audio'; label: string; muted: boolean; role?: string };

type Harness = {
  readonly ctx: { creative: { reader: TimelineReader; projectObjects: unknown; timeline: TimelineOps } };
  readonly clips: RawClip[];
  readonly tracks: Track[];
  readonly objects: Map<string, Uint8Array>;
  readonly calls: string[];
  readonly patch: { current?: { version: number; operations: Array<Record<string, any>> } };
  readonly control: { version: number; projectId: string; timelineId: string; onFlush?: () => void };
};

function snapshotOf(clips: readonly RawClip[], tracks: readonly Track[], version: number, projectId: string, timelineId: string): TimelineSnapshot {
  return {
    projectId,
    timelineId,
    baseVersion: version,
    currentVersion: version,
    extensionRequirements: [],
    clips: clips.map((clip) => {
      const liveScene = clip.app?.liveScene as { revision?: string; source?: { objectId?: string; revision?: string } } | undefined;
      return {
        id: clip.id,
        track: clip.track,
        at: clip.at,
        clipType: clip.clipType,
        duration: clip.from !== undefined && clip.to !== undefined ? (clip.to - clip.from) / (clip.speed ?? 1) : 0,
        sourceOffset: clip.from,
        sourceEnd: clip.to,
        rate: clip.speed,
        app: clip.app,
        managed: clip.clipType === LIVE_SCENE_CLIP_TYPE_ID,
        managedBy: clip.clipType === LIVE_SCENE_CLIP_TYPE_ID ? THREEJS_LIVE_EDITOR_EXTENSION_ID : undefined,
        sourceRefs: liveScene?.source ? [{
          id: `source.live-scene.${liveScene.source.objectId}.${clip.id}`,
          clipId: clip.id,
          sourceKind: 'provider' as const,
          extensionId: THREEJS_LIVE_EDITOR_EXTENSION_ID,
          sourceObjectId: liveScene.source.objectId,
          sourceRevision: liveScene.source.revision,
          packageRevision: liveScene.revision,
        }] : [],
      };
    }),
    tracks,
    assetKeys: [],
    app: {},
  } as unknown as TimelineSnapshot;
}

function harness(options: {
  clips?: RawClip[];
  tracks?: Track[];
  projectId?: string;
  persistence?: boolean;
} = {}): Harness {
  const clips = options.clips ?? [];
  const tracks = options.tracks ?? [];
  const objects = new Map<string, Uint8Array>();
  const calls: string[] = [];
  const patch: Harness['patch'] = {};
  const control: Harness['control'] = {
    version: 7,
    projectId: options.projectId ?? 'project-a',
    timelineId: 'timeline-a',
  };
  const projectObjects = {
    async ingest(input: Uint8Array, mediaType: string, filename?: string): Promise<ProjectObjectMetadata> {
      const copy = new Uint8Array(input);
      const id = await digest(copy);
      objects.set(id, copy);
      calls.push(`ingest:${filename}`);
      return { object_id: id, digest: id, media_type: mediaType, size: copy.byteLength, filename: filename ?? '' };
    },
    async read(objectId: string): Promise<Uint8Array> {
      const value = objects.get(objectId);
      if (!value) throw new Error(`unknown object ${objectId}`);
      return new Uint8Array(value);
    },
  };
  const reader: TimelineReader = {
    snapshot: () => snapshotOf(clips, tracks, control.version, control.projectId, control.timelineId),
  };
  const timeline: TimelineOps = {
    validate: () => {
      calls.push('validate');
      return { valid: true, diagnostics: [] } as ReturnType<TimelineOps['validate']>;
    },
    preview: () => ({ diff: { version: 7, entries: [], affectedObjectIds: [] }, fullyPreviewable: true, diagnostics: [] }) as ReturnType<TimelineOps['preview']>,
    apply: (value) => {
      if (value.version !== control.version) throw new Error('stale timeline patch');
      calls.push('apply');
      patch.current = value as unknown as Harness['patch']['current'];
      for (const operation of value.operations as Array<Record<string, any>>) {
        if (operation.op === 'track.add') {
          tracks.push({ id: operation.target, kind: 'visual', label: operation.target, muted: false });
        }
        if (operation.op === 'clip.add') {
          const payload = operation.payload ?? {};
          clips.push({ id: operation.target, track: String(payload.track), at: Number(payload.at), clipType: String(payload.clipType) });
        }
        if (operation.op === 'clip.update') {
          const clip = clips.find((candidate) => candidate.id === operation.target);
          const payload = operation.payload ?? {};
          if (clip) {
            if ('from' in payload) clip.from = Number(payload.from);
            if ('to' in payload) clip.to = Number(payload.to);
            if ('speed' in payload) clip.speed = Number(payload.speed);
            if ('app' in payload) clip.app = payload.app as Record<string, unknown>;
          }
        }
      }
      control.version += 1;
      return {
        version: control.version,
        entries: [],
        affectedObjectIds: (value.operations as Array<Record<string, any>>).map((operation) => operation.target),
      } as unknown as TimelineDiff;
    },
    flush: async () => {
      calls.push('flush');
      control.onFlush?.();
      if (options.persistence === false) throw new Error('persistence unavailable');
      return { version: control.version } as Awaited<ReturnType<TimelineOps['flush']>>;
    },
    checkpoint: () => 'checkpoint',
    rollback: () => null,
    setAllTracksMuted: () => ({ version: 7, entries: [], affectedObjectIds: [] }) as unknown as TimelineDiff,
  } as unknown as TimelineOps;
  return {
    ctx: { creative: { reader, projectObjects, timeline } },
    clips,
    tracks,
    objects,
    calls,
    patch,
    control,
  };
}

/** Typed view of the harness context for the authoring functions. */
function ctxOf(h: Harness) {
  return h.ctx as unknown as Parameters<typeof placeLiveScene>[0];
}

function storageOf(h: Harness) {
  return h.ctx.creative.projectObjects as Parameters<typeof ingestPreparedPackage>[0];
}

const V1_VISUAL: Track = { id: 'V1', kind: 'visual', label: 'V1', muted: false };
const placement = (overrides: Partial<PreparedScenePlacement> = {}): PreparedScenePlacement => ({
  id: 'world-place-1', track: 'V1', at: 2, from: 10, to: 20, rate: 1, ...overrides,
});

describe('world explorer authoring: ingest and load', () => {
  it('loadLiveSceneSource returns the exact liveScene and manifest that ingest produced', async () => {
    const h = harness();
    const prepared = await validPrepared();
    const { package: pkg, liveScene } = await ingestPreparedPackage(storageOf(h), prepared);

    const loaded = await loadLiveSceneSource(storageOf(h), pkg.object_id, pkg.digest);

    expect(loaded.liveScene).toEqual(liveScene);
    expect(loaded.liveScene).toMatchObject({
      revision: pkg.digest,
      source: { objectId: pkg.object_id, revision: pkg.digest },
      html: SCENE_HTML,
    });
    expect(typeof loaded.liveScene.packageBody).toBe('string');
    expect(loaded.manifest).toEqual(prepared.manifest);
  });

  it('rejects a tampered package body with an integrity error', async () => {
    const h = harness();
    const { package: pkg } = await ingestPreparedPackage(storageOf(h), await validPrepared());
    const tampered = new TextEncoder().encode(
      JSON.stringify({ manifest: { formatVersion: 1, entry: 'world-v1.html', duration: 999, authoredFps: 30 } }),
    );
    h.objects.set(pkg.object_id, tampered);

    await expect(loadLiveSceneSource(storageOf(h), pkg.object_id, pkg.digest)).rejects.toThrow(/integrity/);
  });

  it('rejects a tampered HTML entry with an integrity error', async () => {
    const h = harness();
    const { package: pkg } = await ingestPreparedPackage(storageOf(h), await validPrepared());
    const body = JSON.parse(new TextDecoder().decode(h.objects.get(pkg.object_id)!)) as { entry: { object_id: string } };
    h.objects.set(body.entry.object_id, bytes('<html>tampered</html>'));

    await expect(loadLiveSceneSource(storageOf(h), pkg.object_id, pkg.digest)).rejects.toThrow(/integrity/);
  });

  it('rejects a wrong revision with an integrity error', async () => {
    const h = harness();
    const { package: pkg } = await ingestPreparedPackage(storageOf(h), await validPrepared());

    await expect(loadLiveSceneSource(storageOf(h), pkg.object_id, `sha256:${'0'.repeat(64)}`)).rejects.toThrow(/integrity/);
  });
});

describe('world explorer authoring: placementOperations', () => {
  const liveScene = { revision: 'sha256:r', source: { objectId: 'o', revision: 'sha256:r' }, packageBody: '{}', html: '<html/>' };

  it('adds no track when the placement track already exists', () => {
    const snapshot = snapshotOf([], [V1_VISUAL], 7, 'project-a', 'timeline-a');
    const ops = placementOperations(snapshot, [placement()], liveScene);

    expect(ops.map((op) => op.op)).toEqual(['clip.add', 'clip.update']);
    expect(ops.some((op) => op.op === 'track.add')).toBe(false);
  });

  it('emits track.add first for a missing track, then clip.add and clip.update with the merge payload', () => {
    const snapshot = snapshotOf([], [], 7, 'project-a', 'timeline-a');
    const ops = placementOperations(snapshot, [placement({ track: 'V2', at: 4, from: 55, to: 75, rate: 2 })], liveScene);

    expect(ops.map((op) => op.op)).toEqual(['track.add', 'clip.add', 'clip.update']);
    expect(ops[0]).toMatchObject({ op: 'track.add', target: 'V2', payload: { kind: 'visual', label: 'V2' } });
    expect(ops[1]).toMatchObject({ op: 'clip.add', target: 'world-place-1', payload: { track: 'V2', at: 4, clipType: LIVE_SCENE_CLIP_TYPE_ID } });
    expect(ops[2]).toMatchObject({
      op: 'clip.update',
      target: 'world-place-1',
      payload: { mode: 'merge', from: 55, to: 75, speed: 2, app: { liveScene } },
    });
  });

  it('orders clip.add before clip.update and gives two placements strictly increasing order', () => {
    const snapshot = snapshotOf([], [V1_VISUAL], 7, 'project-a', 'timeline-a');
    const ops = placementOperations(snapshot, [
      placement({ id: 'a', at: 0 }),
      placement({ id: 'b', at: 10 }),
    ], liveScene);

    expect(ops.map((op) => `${op.op}:${op.target}`)).toEqual([
      'clip.add:a', 'clip.update:a', 'clip.add:b', 'clip.update:b',
    ]);
    const orders = ops.map((op) => op.order);
    expect(orders).toEqual([0, 1, 2, 3]);
    for (let i = 1; i < orders.length; i++) expect(orders[i]).toBeGreaterThan(orders[i - 1]);
  });
});

describe('world explorer authoring: placeLiveScene', () => {
  const liveScene = { revision: 'sha256:r', source: { objectId: 'o', revision: 'sha256:r' }, packageBody: '{}', html: '<html/>' };

  it('rejects when the snapshot has no projectId', async () => {
    const h = harness({ projectId: '' , tracks: [V1_VISUAL] });
    await expect(placeLiveScene(ctxOf(h), liveScene, 90, [placement()])).rejects.toThrow(/timeline scope unavailable/);
    expect(h.calls).toEqual([]);
  });

  it('validates then applies at the snapshot baseVersion, then flushes', async () => {
    const h = harness({ tracks: [V1_VISUAL] });
    const result = await placeLiveScene(ctxOf(h), liveScene, 90, [placement()]);

    expect(h.calls).toEqual(['validate', 'apply', 'flush']);
    expect(h.patch.current?.version).toBe(7);
    expect(result).toEqual({ affectedPlacements: ['world-place-1'], acknowledgedTimelineVersion: 8 });
    expect(h.clips).toEqual([expect.objectContaining({ id: 'world-place-1', track: 'V1', at: 2, from: 10, to: 20, speed: 1 })]);
  });

  it('surfaces the validation diagnostic message and never applies', async () => {
    const h = harness({ tracks: [V1_VISUAL] });
    (h.ctx.creative.timeline as { validate: TimelineOps['validate'] }).validate = (() => ({
      valid: false,
      diagnostics: [{ message: 'clip overlaps a locked region' }],
    })) as unknown as TimelineOps['validate'];

    await expect(placeLiveScene(ctxOf(h), liveScene, 90, [placement()])).rejects.toThrow(
      'Prepared scene timeline patch rejected: clip overlaps a locked region',
    );
    expect(h.patch.current).toBeUndefined();
  });

  it('reports durable persistence unavailable when flush fails', async () => {
    const h = harness({ tracks: [V1_VISUAL], persistence: false });
    await expect(placeLiveScene(ctxOf(h), liveScene, 90, [placement()])).rejects.toThrow(
      'durable timeline persistence unavailable: persistence unavailable',
    );
    expect(h.calls).toContain('apply');
  });

  it('rejects a placement whose id collides with an existing clip', async () => {
    const h = harness({
      tracks: [V1_VISUAL],
      clips: [{ id: 'world-place-1', at: 0, from: 0, to: 5, speed: 1, track: 'V1', clipType: 'other' }],
    });
    await expect(placeLiveScene(ctxOf(h), liveScene, 90, [placement()])).rejects.toThrow(/ID collision: world-place-1/);
    expect(h.patch.current).toBeUndefined();
  });

  it('rejects a placement whose `to` exceeds the scene duration', async () => {
    const h = harness({ tracks: [V1_VISUAL] });
    await expect(placeLiveScene(ctxOf(h), liveScene, 90, [placement({ from: 80, to: 100 })])).rejects.toThrow(
      'Invalid prepared scene placement world-place-1',
    );
    expect(h.patch.current).toBeUndefined();
  });
});

describe('world explorer authoring: updateLiveSceneClip', () => {
  const liveScene = { revision: 'sha256:r', source: { objectId: 'o', revision: 'sha256:r' }, packageBody: '{}', html: '<html/>' };
  const liveClip: RawClip = {
    id: 'live-1', at: 0, from: 0, to: 10, speed: 1, track: 'V1',
    clipType: LIVE_SCENE_CLIP_TYPE_ID, app: { liveScene },
  };

  it('issues a single clip.update with only the merge stretch fields (no app) and returns the flushed version', async () => {
    const h = harness({ tracks: [V1_VISUAL], clips: [liveClip] });
    const result = await updateLiveSceneClip(ctxOf(h), 'live-1', { from: 2, to: 8, speed: 2 });

    expect(h.calls).toEqual(['validate', 'apply', 'flush']);
    expect(h.patch.current?.version).toBe(7);
    expect(h.patch.current?.operations).toEqual([{
      op: 'clip.update',
      target: 'live-1',
      payload: { mode: 'merge', from: 2, to: 8, speed: 2 },
      order: 0,
    }]);
    expect(h.patch.current?.operations[0].payload).not.toHaveProperty('app');
    expect(result).toEqual({ acknowledgedTimelineVersion: 8 });
  });

  it('rejects a clip that does not exist', async () => {
    const h = harness({ tracks: [V1_VISUAL], clips: [liveClip] });
    await expect(updateLiveSceneClip(ctxOf(h), 'missing', { from: 0, to: 5, speed: 1 })).rejects.toThrow(
      'Live-scene clip missing does not exist',
    );
    expect(h.patch.current).toBeUndefined();
  });

  it('rejects a clip that is not a live-scene clip', async () => {
    const h = harness({
      tracks: [V1_VISUAL],
      clips: [{ id: 'plain', at: 0, from: 0, to: 10, speed: 1, track: 'V1', clipType: 'haus.astrid.video' }],
    });
    await expect(updateLiveSceneClip(ctxOf(h), 'plain', { from: 0, to: 5, speed: 1 })).rejects.toThrow(
      'Clip plain is not a live-scene clip',
    );
    expect(h.patch.current).toBeUndefined();
  });

  it.each([
    { name: 'to equal to from', stretch: { from: 5, to: 5, speed: 1 } },
    { name: 'to before from', stretch: { from: 8, to: 3, speed: 1 } },
    { name: 'zero speed', stretch: { from: 0, to: 5, speed: 0 } },
    { name: 'negative speed', stretch: { from: 0, to: 5, speed: -1 } },
    { name: 'non-finite speed', stretch: { from: 0, to: 5, speed: Number.NaN } },
  ])('rejects an invalid stretch ($name) without applying', async ({ stretch }) => {
    const h = harness({ tracks: [V1_VISUAL], clips: [liveClip] });
    await expect(updateLiveSceneClip(ctxOf(h), 'live-1', stretch)).rejects.toThrow('Invalid live-scene stretch for live-1');
    expect(h.patch.current).toBeUndefined();
  });
});

describe('world explorer authoring: add-to-video with the explorer model', () => {
  const liveScene = { revision: 'sha256:r', source: { objectId: 'o', revision: 'sha256:r' }, packageBody: '{}', html: '<html/>' };
  // Stretch 5..15 at rate 2 occupies 5 seconds of timeline.
  const stretch = { from: 5, to: 15, speed: 2 };
  const playhead = 3;

  it('places on V2 with a new track when V1 is busy at the playhead', async () => {
    const h = harness({
      tracks: [V1_VISUAL],
      clips: [{ id: 'existing', at: 0, from: 0, to: 10, speed: 1, track: 'V1', clipType: 'other' }],
    });
    const snapshot = h.ctx.creative.reader.snapshot();
    const duration = stretchDuration(stretch);
    const { track, isNew } = freeVisualTrack(snapshot, playhead, duration);
    const id = newClipId(snapshot);

    expect(track).toBe('V2');
    expect(isNew).toBe(true);
    expect(id).toBe('world-1');

    await placeLiveScene(ctxOf(h), liveScene, 90, [{ id, track, at: playhead, from: stretch.from, to: stretch.to, rate: stretch.speed }]);

    const ops = h.patch.current!.operations;
    expect(ops.find((op) => op.op === 'track.add')).toMatchObject({ target: 'V2' });
    expect(ops.find((op) => op.op === 'clip.add')).toMatchObject({
      target: 'world-1',
      payload: { track: 'V2', at: playhead, clipType: LIVE_SCENE_CLIP_TYPE_ID },
    });
    expect(ops.find((op) => op.op === 'clip.update')?.payload).toMatchObject({ mode: 'merge', from: 5, to: 15, speed: 2 });
    expect(h.clips.find((c) => c.id === 'world-1')).toMatchObject({ track: 'V2', at: playhead, from: 5, to: 15, speed: 2 });
  });

  it('uses the free existing V1 when nothing overlaps the playhead', async () => {
    const h = harness({
      tracks: [V1_VISUAL],
      clips: [{ id: 'later', at: 20, from: 0, to: 4, speed: 1, track: 'V1', clipType: 'other' }],
    });
    const snapshot = h.ctx.creative.reader.snapshot();
    const { track, isNew } = freeVisualTrack(snapshot, playhead, stretchDuration(stretch));
    expect({ track, isNew }).toEqual({ track: 'V1', isNew: false });

    await placeLiveScene(ctxOf(h), liveScene, 90, [{ id: newClipId(snapshot), track, at: playhead, from: 5, to: 15, rate: 2 }]);

    expect(h.patch.current!.operations.some((op) => op.op === 'track.add')).toBe(false);
    expect(h.patch.current!.operations.find((op) => op.op === 'clip.add')).toMatchObject({ payload: { track: 'V1' } });
  });

  it('picks a collision-free clip id when the default id is already taken', async () => {
    const h = harness({
      tracks: [V1_VISUAL],
      clips: [
        { id: 'world-1', at: 50, from: 0, to: 4, speed: 1, track: 'V1', clipType: 'other' },
        { id: 'existing', at: 0, from: 0, to: 10, speed: 1, track: 'V1', clipType: 'other' },
      ],
    });
    const snapshot = h.ctx.creative.reader.snapshot();
    const { track } = freeVisualTrack(snapshot, playhead, stretchDuration(stretch));
    const id = newClipId(snapshot);
    expect(id).toBe('world-2');

    await placeLiveScene(ctxOf(h), liveScene, 90, [{ id, track, at: playhead, from: 5, to: 15, rate: 2 }]);

    expect(h.patch.current!.operations.find((op) => op.op === 'clip.add')).toMatchObject({ target: 'world-2' });
    expect(h.clips.find((c) => c.id === 'world-2')).toMatchObject({ speed: 2, from: 5, to: 15 });
  });
});
