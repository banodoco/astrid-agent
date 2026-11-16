import { describe, expect, it, vi } from 'vitest';
import type {
  ProjectObjectMetadata,
  TimelineDiff,
  TimelineOps,
  TimelineReader,
  TimelineSnapshot,
} from '@astrid/editor-sdk';
import {
  MAPLE_ACCEPTANCE_PLACEMENTS,
  MAPLE_CAMERA_EDIT_FOV,
  MAPLE_CAMERA_EDIT_SOURCE_TIME,
  defaultPreparedScenePlacement,
  importPreparedSceneFile,
  importPreparedScene,
  importPreparedMapleScene,
  parsePreparedScenePackage,
  parsePreparedMaplePackage,
  prepareBoundedLiveSceneEdit,
  publishLiveSceneEdit,
  executeLiveSceneAuthoring,
  type LiveScenePackageInput,
  type LiveScenePublicationCapture,
  type PreparedScenePlacement,
  type PreparedMaplePlacement,
} from '../../../astrid/packs/rendering/ui/threejs-live-editor/authoring';
import type { LiveSceneRequest, LiveSceneExecution } from '@astrid/editor-sdk';

describe('pack-owned ACP source authoring', () => {
  async function fixture(html = MAPLE_HTML) {
    const testHarness = await harness();
    const original = await parsePreparedMaplePackage(await preparedBytes(html));
    await importPreparedMapleScene(testHarness.ctx, original, PLACEMENTS);
    testHarness.calls.length = 0;
    testHarness.patch.current = undefined;
    const cancel = new AbortController();
    const request: LiveSceneRequest = {
      schema: 'astrid.live-scene-request/v1', requestId: 'read',
      scope: { sessionId: 'session', turnId: 'turn', projectId: 'project-a', timelineId: 'timeline-a', capturedTimelineVersion: 8 },
      placementIds: PLACEMENTS.map(({ id }) => id), action: 'read', offset: 0, length: 8192,
    };
    const execution: LiveSceneExecution = { signal: cancel.signal, assertActive: () => {
      if (cancel.signal.aborted) throw new Error('cancelled before publication');
      if (testHarness.control.projectId !== request.scope.projectId || testHarness.control.timelineId !== request.scope.timelineId) throw new Error('scope changed');
      if (testHarness.control.version !== request.scope.capturedTimelineVersion) throw new Error('stale capture');
    } };
    const read = await executeLiveSceneAuthoring(testHarness.ctx, request, execution);
    if (read.kind !== 'read') throw new Error('Expected read result');
    const publish: LiveSceneRequest = { schema: request.schema, requestId: 'publish', scope: request.scope, placementIds: request.placementIds,
      action: 'publish', capture: read.capture, replacements: [{ before: '"t":55,"fov":41', after: '"t":55,"fov":49' }] };
    return { testHarness, original, cancel, request, execution, read, publish };
  }

  it('returns bounded canonical source, immutable identities and independent timing', async () => {
    const f = await fixture();
    expect(f.read.capture).toMatchObject({ projectId: 'project-a', timelineId: 'timeline-a', capturedTimelineVersion: 8 });
    expect(f.read.capture.packageObjectId).toBeTruthy();
    expect(f.read.capture.entryObjectId).toBeTruthy();
    expect(f.read.timing).toMatchObject([{ at: 2, sourceOffset: 55, sourceEnd: 75, rate: 1 }, { at: 22, sourceOffset: 20, sourceEnd: 30, rate: 2 }]);
    const excerpt = await executeLiveSceneAuthoring(f.testHarness.ctx, { ...f.request, action: 'read', find: '"t":55', offset: 5, length: 30 }, f.execution);
    expect(excerpt.kind).toBe('read');
    if (excerpt.kind === 'read') {
      expect(excerpt.source.text).toBe(MAPLE_HTML.slice(MAPLE_HTML.indexOf('"t":55') - 5, MAPLE_HTML.indexOf('"t":55') + 25));
      expect(excerpt.source.totalLength).toBe(MAPLE_HTML.length);
    }
  });

  it('prepares exact source edits and invokes the same publisher with unrelated spans and metadata intact', async () => {
    const f = await fixture();
    for (const clip of f.testHarness.clips) clip.app = { ...clip.app, sentinel: { retained: true } };
    const result = await executeLiveSceneAuthoring(f.testHarness.ctx, f.publish, f.execution);
    expect(result).toMatchObject({ kind: 'published', projectId: 'project-a', timelineId: 'timeline-a', flushReceipt: { version: 9 }, acknowledgedTimelineVersion: 9 });
    expect(f.testHarness.calls.indexOf('apply')).toBeLessThan(f.testHarness.calls.indexOf('flush'));
    const expected = MAPLE_HTML.replace('"t":55,"fov":41', '"t":55,"fov":49');
    for (const clip of f.testHarness.clips) {
      expect(sceneOf(clip).html).toBe(expected);
      expect(clip.app?.sentinel).toEqual({ retained: true });
    }
  });

  it('edits ordinary code and data while retaining untouched UTF-8 bytes', async () => {
    const html = '<html><script>const spin = 1; const label = "before";</script>café🚀</html>';
    const f = await fixture(html);
    const result = await executeLiveSceneAuthoring(f.testHarness.ctx, { ...f.publish, action: 'publish', replacements: [
      { before: 'const spin = 1;', after: 'const spin = 2;' },
      { before: 'const label = "before";', after: 'const label = "after";' },
    ] }, f.execution);
    expect(result.kind).toBe('published');
    const expected = html.replace('const spin = 1;', 'const spin = 2;').replace('const label = "before";', 'const label = "after";');
    expect(bytes(String(sceneOf(f.testHarness.clips[0]).html))).toEqual(bytes(expected));
  });

  it.each([
    { replacements: [{ before: 'absent', after: 'new' }] },
    { replacements: [{ before: 'fov', after: 'camera' }] },
    { replacements: [{ before: '"t":55,"fov":41', after: '"t":55,"fov":49' }, { before: '"t":55', after: '"t":56' }] },
  ])('rejects missing, ambiguous or overlapping replacements without an apply (%j)', async ({ replacements }) => {
    const f = await fixture();
    await expect(executeLiveSceneAuthoring(f.testHarness.ctx, { ...f.publish, action: 'publish', replacements }, f.execution)).rejects.toThrow(/missing|ambiguous|overlap/);
    expect(f.testHarness.patch.current).toBeUndefined();
  });

  it('rejects a changed canonical object identity or stale version before publication', async () => {
    const f = await fixture();
    await expect(executeLiveSceneAuthoring(f.testHarness.ctx, { ...f.publish, action: 'publish', capture: { ...f.read.capture, entryObjectId: 'other' } }, f.execution)).rejects.toThrow('identity changed');
    f.testHarness.control.version = 9;
    await expect(executeLiveSceneAuthoring(f.testHarness.ctx, f.publish, f.execution)).rejects.toThrow('stale');
    expect(f.testHarness.patch.current).toBeUndefined();
  });

  it('fences cancellation and project changes during candidate ingestion', async () => {
    for (const mode of ['cancel', 'project', 'timeline', 'stale']) {
      const f = await fixture();
      f.testHarness.control.onIngest = () => {
        if (mode === 'cancel') f.cancel.abort();
        if (mode === 'project') f.testHarness.control.projectId = 'other';
        if (mode === 'timeline') f.testHarness.control.timelineId = 'other';
        if (mode === 'stale') f.testHarness.control.version += 1;
      };
      await expect(executeLiveSceneAuthoring(f.testHarness.ctx, f.publish, f.execution)).rejects.toThrow(/cancelled|scope changed|stale/);
      expect(f.testHarness.patch.current).toBeUndefined();
      expect(sceneOf(f.testHarness.clips[0]).html).toBe(MAPLE_HTML);
    }
  });

  it('waits for flush and reports durable success when cancellation follows commit', async () => {
    const f = await fixture();
    let release!: () => void;
    f.testHarness.control.flushBarrier = new Promise<void>((resolve) => { release = resolve; });
    f.testHarness.control.onFlush = () => f.cancel.abort();
    let settled = false;
    const publication = executeLiveSceneAuthoring(f.testHarness.ctx, f.publish, f.execution).then((value) => { settled = true; return value; });
    await vi.waitFor(() => expect(f.testHarness.calls).toContain('flush'));
    expect(settled).toBe(false);
    release();
    expect(await publication).toMatchObject({ kind: 'published', flushReceipt: { version: 9 } });
    expect(f.cancel.signal.aborted).toBe(true);
  });

  it('never reports publication success when durable persistence rejects', async () => {
    const f = await fixture();
    f.testHarness.control.onFlush = () => { throw new Error('Runtime CAS conflict'); };
    await expect(executeLiveSceneAuthoring(f.testHarness.ctx, f.publish, f.execution)).rejects.toThrow('durable timeline persistence unavailable: Runtime CAS conflict');
  });
});

const EXTENSION_ID = 'haus.astrid.threejs-live-editor';
const CLIP_TYPE = 'haus.astrid.liveScene';
const PLACEMENTS: readonly PreparedMaplePlacement[] = [
  { id: 'maple-acceptance-position-2', track: 'V1', at: 2, from: 55, to: 75, rate: 1 },
  { id: 'maple-acceptance-position-22', track: 'V1', at: 22, from: 20, to: 30, rate: 2 },
];

const MAPLE_HTML = '<html><script id="mapleJourneyData" type="application/json">{"schema":"maple-hollow/continuous-journey@1","camera":{"knots":[{"t":52,"fov":41,"label":"before"},{"t":55,"fov":41,"label":"target"},{"t":57,"fov":35,"label":"after"}]},"captions":[{"start":55,"end":75,"text":"At the counter"}]}</script></html>';

async function digest(value: Uint8Array): Promise<string> {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', value as unknown as BufferSource);
  return `sha256:${Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

function bytes(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

async function preparedBytes(
  html = '<html><body><div id="scene">Maple</div></body></html>',
  manifestOverrides: Partial<{ entry: string; duration: number; authoredFps: number }> = {},
): Promise<Uint8Array> {
  const entry = bytes(html);
  const manifest = {
    formatVersion: 1,
    entry: 'maple-hollow-v39.html',
    duration: 90,
    authoredFps: 30,
    ...manifestOverrides,
  };
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

type RawClip = {
  id: string;
  at: number;
  from?: number;
  to?: number;
  speed?: number;
  track: string;
  clipType?: string;
  app?: Record<string, unknown>;
  sourceRefs?: TimelineSnapshot['clips'][number]['sourceRefs'];
};

type Harness = {
  readonly ctx: Parameters<typeof importPreparedMapleScene>[0];
  readonly clips: RawClip[];
  readonly tracks: Array<{ id: string; kind: 'visual' | 'audio'; label: string; muted: boolean }>;
  readonly objects: Map<string, Uint8Array>;
  readonly ingested: Array<{ id: string; bytes: Uint8Array }>;
  readonly calls: string[];
  readonly patch: { current?: { version: number; operations: readonly Record<string, unknown>[] } };
  readonly control: {
    version: number;
    projectId: string;
    timelineId: string;
    onIngest?: (filename?: string) => void | Promise<void>;
    onRead?: (objectId: string) => void | Promise<void>;
    onFlush?: () => void;
    flushBarrier?: Promise<void>;
  };
};

function snapshotOf(
  clips: readonly RawClip[],
  tracks: readonly Harness['tracks'][number][],
  version: number,
): TimelineSnapshot {
  return {
    projectId: 'project-a',
    timelineId: 'timeline-a',
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
        duration: clip.from !== undefined && clip.to !== undefined
          ? (clip.to - clip.from) / (clip.speed ?? 1) : 0,
        sourceOffset: clip.from,
        sourceEnd: clip.to,
        rate: clip.speed,
        app: clip.app,
        managed: clip.clipType === CLIP_TYPE,
        managedBy: clip.clipType === CLIP_TYPE ? EXTENSION_ID : undefined,
        sourceRefs: clip.sourceRefs ?? (liveScene?.source ? [{
          id: `source.live-scene.${liveScene.source.objectId}.${clip.id}`,
          clipId: clip.id,
          sourceKind: 'provider' as const,
          extensionId: EXTENSION_ID,
          sourceObjectId: liveScene.source.objectId,
          sourceRevision: liveScene.source.revision,
          packageRevision: liveScene.revision,
        }] : []),
      };
    }),
    tracks,
    assetKeys: [],
    app: {},
  };
}

async function harness(options: {
  clips?: RawClip[];
  tracks?: Harness['tracks'];
  persistence?: boolean;
  staleAfterIngest?: boolean;
} = {}): Promise<Harness> {
  const clips = options.clips ?? [];
  const tracks = options.tracks ?? [];
  const objects = new Map<string, Uint8Array>();
  const ingested: Array<{ id: string; bytes: Uint8Array }> = [];
  const calls: string[] = [];
  const patch: { current?: { version: number; operations: readonly Record<string, unknown>[] } } = {};
  const control: Harness['control'] = { version: 7, projectId: 'project-a', timelineId: 'timeline-a' };
  let snapshotCalls = 0;
  const projectObjects = {
    async ingest(input: Uint8Array, mediaType: string, filename?: string): Promise<ProjectObjectMetadata> {
      const copy = new Uint8Array(input);
      const id = await digest(copy);
      ingested.push({ id, bytes: copy });
      objects.set(id, copy);
      calls.push(`ingest:${filename}`);
      await control.onIngest?.(filename);
      return { object_id: id, digest: id, media_type: mediaType, size: copy.byteLength, filename };
    },
    async read(objectId: string): Promise<Uint8Array> {
      calls.push(`read:${objectId}`);
      await control.onRead?.(objectId);
      const value = objects.get(objectId);
      if (!value) throw new Error(`unknown object ${objectId}`);
      return new Uint8Array(value);
    },
  };
  const reader: TimelineReader = {
    snapshot: () => {
      snapshotCalls += 1;
      return {
        ...snapshotOf(clips, tracks, options.staleAfterIngest && snapshotCalls > 1 ? 8 : control.version),
        projectId: control.projectId,
        timelineId: control.timelineId,
      };
    },
  };
  const timeline: TimelineOps = {
    validate: (_value) => {
      calls.push('validate');
      return { valid: true, diagnostics: [] };
    },
    preview: () => ({ diff: { version: 7, entries: [], affectedObjectIds: [] }, fullyPreviewable: true, diagnostics: [] }),
    apply: (value) => {
      if (value.version !== control.version) throw new Error('stale timeline patch');
      calls.push('apply');
      patch.current = value as unknown as { version: number; operations: readonly Record<string, unknown>[] };
      for (const operation of value.operations) {
        if (operation.op === 'track.add') {
          tracks.push({ id: operation.target, kind: 'visual', label: operation.target, muted: false });
        }
        if (operation.op === 'clip.add') {
          const payload = operation.payload ?? {};
          clips.push({
            id: operation.target,
            track: String(payload.track),
            at: Number(payload.at),
            clipType: String(payload.clipType),
          });
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
      return { version: control.version, entries: [], affectedObjectIds: value.operations.map((operation) => operation.target) } as TimelineDiff;
    },
    flush: async () => {
      calls.push('flush');
      control.onFlush?.();
      if (options.persistence === false) throw new Error('persistence unavailable');
      await control.flushBarrier;
      return { version: control.version };
    },
    checkpoint: () => 'checkpoint',
    rollback: () => null,
    setAllTracksMuted: () => ({ version: 7, entries: [], affectedObjectIds: [] }),
  };
  return {
    ctx: { creative: { reader, projectObjects, timeline } } as Harness['ctx'],
    clips,
    tracks,
    objects,
    ingested,
    calls,
    patch,
    control,
  };
}

async function validPrepared(): Promise<LiveScenePackageInput> {
  return parsePreparedMaplePackage(await preparedBytes());
}

describe('prepared Maple package validation', () => {
  it('accepts the self-contained prepare_maple shape and rejects malformed boundaries', async () => {
    const value = await parsePreparedMaplePackage(await preparedBytes());
    expect(value.assets).toEqual([]);
    expect(value.manifest.entry).toBe('maple-hollow-v39.html');

    await expect(parsePreparedMaplePackage(new Uint8Array([0xff, 0xfe]))).rejects.toThrow('UTF-8');
    const raw = JSON.parse(new TextDecoder().decode(await preparedBytes())) as Record<string, unknown>;
    await expect(parsePreparedMaplePackage(bytes(JSON.stringify({ ...raw, revision: 'sha256:persisted' })))).rejects.toThrow('unsupported');
    await expect(parsePreparedMaplePackage(bytes(JSON.stringify({ ...raw, assets: [{ filename: 'not-allowed' }] })))).rejects.toThrow('assets: []');
    await expect(parsePreparedMaplePackage(bytes(JSON.stringify({ ...raw, manifest: { ...raw.manifest as object, duration: 0 } })))).rejects.toThrow('timing');
    await expect(parsePreparedMaplePackage(bytes(JSON.stringify({ ...raw, entry: { ...(raw.entry as object), size: 1 } })))).rejects.toThrow('size');
    await expect(parsePreparedMaplePackage(bytes(JSON.stringify({ ...raw, entry: { ...(raw.entry as object), digest: 'sha256:' + '0'.repeat(64) } })))).rejects.toThrow('digest');
  });

  it('does not mutate the prepared bytes before import', async () => {
    const prepared = await parsePreparedMaplePackage(await preparedBytes(MAPLE_HTML));
    const testHarness = await harness();
    const result = await importPreparedMapleScene(testHarness.ctx, prepared, PLACEMENTS);
    expect(testHarness.objects.get(result.entry.object_id)).toEqual(prepared.entry.bytes);
    expect(new TextDecoder().decode(testHarness.objects.get(result.entry.object_id)!)).toBe(MAPLE_HTML);
    expect(result.capture.entryRevision).toBe(await digest(prepared.entry.bytes));
  });
});

describe('prepared scene command orchestration', () => {
  async function commandContext(options: Parameters<typeof harness>[0] = {}) {
    const testHarness = await harness(options);
    const diagnostics = { report: vi.fn(), diagnostics: [] };
    const toast = vi.fn();
    const ctx = {
      ...testHarness.ctx,
      services: { diagnostics, i18n: { t: (key: string) => key } },
      chrome: { toast },
    } as unknown as Parameters<typeof importPreparedSceneFile>[0];
    return { testHarness, diagnostics, toast, ctx };
  }

  it('reports durable success after the picker payload is imported', async () => {
    const f = await commandContext();
    await importPreparedSceneFile(f.ctx, await preparedBytes(), new AbortController().signal);

    expect(f.testHarness.patch.current).toBeDefined();
    expect(f.testHarness.calls).toContain('flush');
    expect(f.diagnostics.report).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'info', code: 'threejs-live-editor/authoring-published',
    }));
    expect(f.toast).toHaveBeenCalledWith('Prepared scene imported (1 placements, timeline 8).', 'info');
  });

  it('reports cancellation without parsing or publishing', async () => {
    const f = await commandContext();
    const cancel = new AbortController();
    cancel.abort();

    await importPreparedSceneFile(f.ctx, await preparedBytes(), cancel.signal);

    expect(f.testHarness.ingested).toHaveLength(0);
    expect(f.testHarness.patch.current).toBeUndefined();
    expect(f.diagnostics.report).not.toHaveBeenCalled();
    expect(f.toast).toHaveBeenCalledWith('Prepared scene import cancelled.', 'info');
  });

  it('reports and rethrows prepared-package import failures', async () => {
    const f = await commandContext();

    await expect(importPreparedSceneFile(
      f.ctx,
      new TextEncoder().encode('{}'),
      new AbortController().signal,
    )).rejects.toThrow('assets: []');

    expect(f.testHarness.patch.current).toBeUndefined();
    expect(f.diagnostics.report).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'error', code: 'threejs-live-editor/authoring-failed',
      detail: { stage: 'import-prepared-scene', error: expect.stringContaining('assets: []') },
    }));
    expect(f.toast).toHaveBeenCalledWith(expect.stringContaining('Prepared scene import failed:'), 'error');
  });

  it('reports and rethrows durable persistence failures', async () => {
    const f = await commandContext({ persistence: false });

    await expect(importPreparedSceneFile(
      f.ctx,
      await preparedBytes(),
      new AbortController().signal,
    )).rejects.toThrow('durable timeline persistence unavailable: persistence unavailable');

    expect(f.testHarness.calls).toContain('flush');
    expect(f.diagnostics.report).toHaveBeenCalledWith(expect.objectContaining({
      severity: 'error', code: 'threejs-live-editor/authoring-failed',
      detail: { stage: 'import-prepared-scene', error: expect.stringContaining('durable timeline persistence unavailable') },
    }));
    expect(f.toast).toHaveBeenCalledWith(expect.stringContaining('durable timeline persistence unavailable'), 'error');
  });
});

describe('prepared scene admission', () => {
  const TEMPLATE_HTML = '<html><body><div id="shot">WIDE</div><script>/* bundled non-Maple scene */</script></body></html>';

  it('admits a supported non-Maple four-second package with the default source span', async () => {
    const prepared = await parsePreparedScenePackage(await preparedBytes(TEMPLATE_HTML, {
      entry: 'two-shot-threejs.html', duration: 4,
    }));
    const testHarness = await harness();
    const result = await importPreparedScene(testHarness.ctx, prepared, [defaultPreparedScenePlacement(prepared.manifest)]);

    expect(result.affectedPlacements).toEqual(['live-scene-import']);
    expect(testHarness.clips).toMatchObject([{ id: 'live-scene-import', at: 0, from: 0, to: 4, speed: 1 }]);
    expect(testHarness.patch.current?.operations.map((operation) => operation.op)).toEqual([
      'track.add', 'clip.add', 'clip.update',
    ]);
    expect(testHarness.objects.get(result.entry.object_id)).toEqual(bytes(TEMPLATE_HTML));
    expect(testHarness.calls[testHarness.calls.length - 1]).toBe('flush');
  });

  it('chooses a collision-free default ID for a second normal import', async () => {
    const prepared = await parsePreparedScenePackage(await preparedBytes(TEMPLATE_HTML, {
      entry: 'two-shot-threejs.html', duration: 4,
    }));
    const testHarness = await harness();
    const firstPlacement = defaultPreparedScenePlacement(prepared.manifest);
    await importPreparedScene(testHarness.ctx, prepared, [firstPlacement]);
    const secondPlacement = defaultPreparedScenePlacement(prepared.manifest, testHarness.clips.map(({ id }) => id));

    expect(secondPlacement.id).toBe('live-scene-import-2');
    await importPreparedScene(testHarness.ctx, prepared, [secondPlacement]);
    expect(testHarness.clips.map(({ id, from, to, speed }) => ({ id, from, to, speed }))).toEqual([
      { id: 'live-scene-import', from: 0, to: 4, speed: 1 },
      { id: 'live-scene-import-2', from: 0, to: 4, speed: 1 },
    ]);
  });

  it('rejects a caller placement outside the manifest duration before ingestion', async () => {
    const prepared = await parsePreparedScenePackage(await preparedBytes(TEMPLATE_HTML, {
      entry: 'two-shot-threejs.html', duration: 4,
    }));
    const testHarness = await harness();
    const placement: PreparedScenePlacement = {
      id: 'too-long', track: 'V1', at: 0, from: 0, to: 4.01, rate: 1,
    };
    await expect(importPreparedScene(testHarness.ctx, prepared, [placement])).rejects.toThrow('Invalid prepared scene placement');
    expect(testHarness.ingested).toHaveLength(0);
    expect(testHarness.patch.current).toBeUndefined();
  });
});

describe('Astrid live-scene Maple bootstrap', () => {
  it('imports into an empty timeline with ordered add then update operations', async () => {
    const testHarness = await harness();
    const result = await importPreparedMapleScene(testHarness.ctx, await validPrepared(), PLACEMENTS);

    expect(result.affectedPlacements).toEqual(PLACEMENTS.map((placement) => placement.id));
    expect(testHarness.patch.current?.version).toBe(7);
    expect(testHarness.patch.current?.operations.map((operation) => operation.op)).toEqual([
      'track.add', 'clip.add', 'clip.update', 'clip.add', 'clip.update',
    ]);
    const operations = testHarness.patch.current?.operations ?? [];
    expect(Object.keys(operations[1].payload ?? {}).sort()).toEqual(['at', 'clipType', 'track']);
    expect(operations[2].payload).toMatchObject({ from: 55, to: 75, speed: 1 });
    expect(operations[4].payload).toMatchObject({ from: 20, to: 30, speed: 2 });
    expect(testHarness.clips.map((clip) => ({ id: clip.id, at: clip.at, from: clip.from, to: clip.to, speed: clip.speed }))).toEqual([
      { id: PLACEMENTS[0].id, at: 2, from: 55, to: 75, speed: 1 },
      { id: PLACEMENTS[1].id, at: 22, from: 20, to: 30, speed: 2 },
    ]);
    expect(testHarness.calls[testHarness.calls.length - 1]).toBe('flush');
  });

  it('preserves unrelated timeline content and rejects placement collisions or stale reads before apply', async () => {
    const unrelated = { id: 'existing', at: 0, track: 'V1', clipType: 'hold', app: { sentinel: 'keep' } } satisfies RawClip;
    const collision = await harness({ clips: [{ ...unrelated, id: PLACEMENTS[0].id }] });
    await expect(importPreparedMapleScene(collision.ctx, await validPrepared(), PLACEMENTS)).rejects.toThrow('collision');
    expect(collision.ingested).toHaveLength(0);

    const stale = await harness({ clips: [unrelated], tracks: [{ id: 'V1', kind: 'visual', label: 'V1', muted: false }], staleAfterIngest: true });
    await expect(importPreparedMapleScene(stale.ctx, await validPrepared(), PLACEMENTS)).rejects.toThrow('stale');
    expect(stale.patch.current).toBeUndefined();
    expect(stale.clips).toEqual([unrelated]);
  });

  it('reports durable persistence failure after one applied batch and flush fence', async () => {
    const testHarness = await harness({ persistence: false });
    await expect(importPreparedMapleScene(testHarness.ctx, await validPrepared(), PLACEMENTS)).rejects.toThrow('durable timeline persistence unavailable');
    expect(testHarness.calls.indexOf('apply')).toBeGreaterThan(-1);
    expect(testHarness.calls[testHarness.calls.length - 1]).toBe('flush');
  });
});

describe('bounded visible Maple edit', () => {
  it('changes only the target camera FOV at source 55, not a caption or HTML comment', async () => {
    const html = MAPLE_HTML;
    const entryBytes = bytes(html);
    const entry: ProjectObjectMetadata = {
      object_id: 'entry-old', digest: await digest(entryBytes), media_type: 'text/html', size: entryBytes.byteLength, filename: 'maple-hollow-v39.html',
    };
    const packageBody = JSON.stringify({ manifest: { formatVersion: 1, entry: entry.filename, duration: 10, authoredFps: 30 }, entry, assets: [] });
    const packageBytes = bytes(packageBody);
    const packageObject: ProjectObjectMetadata = {
      object_id: 'package-old', digest: await digest(packageBytes), media_type: 'application/json', size: packageBytes.byteLength, filename: 'scene.package.json',
    };
    const sceneClip: RawClip = {
      id: 'scene-a', at: 0, track: 'V1', clipType: CLIP_TYPE,
      app: { unrelated: { sentinel: 'keep' }, liveScene: { revision: packageObject.digest, source: { objectId: packageObject.object_id, revision: packageObject.digest }, packageBody, html } },
    };
    const testHarness = await harness({ clips: [sceneClip], tracks: [{ id: 'V1', kind: 'visual', label: 'V1', muted: false }] });
    testHarness.objects.set(entry.object_id, entryBytes);
    testHarness.objects.set(packageObject.object_id, packageBytes);

    const edited = await prepareBoundedLiveSceneEdit(testHarness.ctx, 'scene-a');
    const editedHtml = new TextDecoder().decode(edited.entry.bytes);
    const changedIndexes = [...editedHtml].flatMap((character, index) => character === html[index] ? [] : [index]);
    expect(changedIndexes).toHaveLength(1);
    expect(html.slice(changedIndexes[0] - 6, changedIndexes[0] + 1)).toBe('fov":41');
    expect(editedHtml.slice(changedIndexes[0] - 6, changedIndexes[0] + 1)).toBe('fov":49');
    const journey = JSON.parse(editedHtml.match(/<script[^>]+>([\s\S]*?)<\/script>/)![1]) as { camera: { knots: Array<{ t: number; fov: number }> }; captions: Array<{ text: string }> };
    expect(journey.camera.knots.find((knot) => knot.t === MAPLE_CAMERA_EDIT_SOURCE_TIME)?.fov).toBe(MAPLE_CAMERA_EDIT_FOV);
    expect(journey.camera.knots.find((knot) => knot.t === 52)?.fov).toBe(41);
    expect(journey.camera.knots.find((knot) => knot.t === 57)?.fov).toBe(35);
    expect(journey.captions[0].text).toBe('At the counter');
    expect(editedHtml).not.toContain('astrid-l2a-authoring-edit');
  });
});

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

async function importedScene(prepared?: LiveScenePackageInput): Promise<{
  testHarness: Harness;
  original: LiveScenePackageInput;
  capture: LiveScenePublicationCapture;
}> {
  const testHarness = await harness();
  const original = prepared ?? await validPrepared();
  const imported = await importPreparedMapleScene(testHarness.ctx, original, PLACEMENTS);
  for (const [index, clip] of testHarness.clips.entries()) {
    clip.app = { ...clip.app, sentinel: { placement: index }, unrelated: `keep-${index}` };
  }
  testHarness.calls.length = 0;
  testHarness.ingested.length = 0;
  testHarness.patch.current = undefined;
  return {
    testHarness,
    original,
    capture: { ...imported.capture, capturedTimelineVersion: imported.acknowledgedTimelineVersion },
  };
}

function changedEntry(original: LiveScenePackageInput): LiveScenePackageInput {
  return { ...original, entry: { ...original.entry, bytes: bytes('<html><body>Edited Maple caption</body></html>') } };
}

function sceneOf(clip: RawClip): {
  revision: string;
  source: { objectId: string; revision: string };
  packageBody: string;
  html: string;
} {
  return clip.app!.liveScene as ReturnType<typeof sceneOf>;
}

describe('captured old-source live-scene publication', () => {
  it('imports original Maple bytes, then publishes the captured two-placement camera edit with a durable receipt', async () => {
    const { testHarness, original, capture } = await importedScene(
      await parsePreparedMaplePackage(await preparedBytes(MAPLE_HTML)),
    );
    const importedEntry = testHarness.objects.get(capture.entryRevision);
    expect(importedEntry).toEqual(original.entry.bytes);
    const candidate = await prepareBoundedLiveSceneEdit(testHarness.ctx, PLACEMENTS[0].id);
    const result = await publishLiveSceneEdit(testHarness.ctx, {
      placementIds: MAPLE_ACCEPTANCE_PLACEMENTS.map(({ id }) => id), package: candidate, capture,
    });
    expect(result.affectedPlacements).toEqual(MAPLE_ACCEPTANCE_PLACEMENTS.map(({ id }) => id));
    expect(result.capture).toEqual(capture);
    expect(result.revision).not.toBe(capture.packageRevision);
    expect(result.entry.digest).not.toBe(capture.entryRevision);
    expect(result.flushReceipt.version).toBe(9);
    expect(testHarness.calls[testHarness.calls.length - 1]).toBe('flush');
    for (const clip of testHarness.clips) {
      const html = sceneOf(clip).html;
      const journey = JSON.parse(html.match(/<script[^>]+>([\s\S]*?)<\/script>/)![1]) as { camera: { knots: Array<{ t: number; fov: number }> } };
      expect(journey.camera.knots.find((knot) => knot.t === MAPLE_CAMERA_EDIT_SOURCE_TIME)?.fov).toBe(MAPLE_CAMERA_EDIT_FOV);
    }
    expect(testHarness.clips.map(({ id, at, from, to, speed }) => ({ id, at, from, to, speed }))).toEqual([
      { id: PLACEMENTS[0].id, at: 2, from: 55, to: 75, speed: 1 },
      { id: PLACEMENTS[1].id, at: 22, from: 20, to: 30, speed: 2 },
    ]);
  });

  it('publishes changed entry bytes to exactly both placements and waits for durable flush', async () => {
    const { testHarness, original, capture } = await importedScene();
    const untargeted: RawClip = {
      ...testHarness.clips[0], id: 'untargeted', at: 42,
      app: { ...testHarness.clips[0].app, sentinel: 'untargeted' },
    };
    testHarness.clips.push(untargeted);
    const oldTiming = testHarness.clips.map(({ id, track, at, from, to, speed }) => ({ id, track, at, from, to, speed }));
    const flushStarted = deferred();
    const flushReleased = deferred();
    testHarness.control.onFlush = flushStarted.resolve;
    testHarness.control.flushBarrier = flushReleased.promise;
    testHarness.control.onIngest = () => {
      testHarness.clips[1].app = { ...testHarness.clips[1].app, duringPublication: 'keep-fresh-app' };
    };
    let returned = false;
    const publication = publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
    }).then((result) => { returned = true; return result; });
    await flushStarted.promise;
    expect(returned).toBe(false);
    expect(testHarness.calls.filter((call) => call === 'apply')).toHaveLength(1);
    expect(testHarness.patch.current?.version).toBe(capture.capturedTimelineVersion);
    expect(testHarness.patch.current?.operations.map(({ op, target }) => ({ op, target }))).toEqual(
      PLACEMENTS.map(({ id }) => ({ op: 'clip.update', target: id })),
    );
    flushReleased.resolve();
    const result = await publication;
    expect(result.revision).not.toBe(capture.packageRevision);
    expect(result.entry.digest).not.toBe(capture.entryRevision);
    expect(result.flushReceipt.version).toBe(9);
    expect(result.acknowledgedTimelineVersion).toBe(result.flushReceipt.version);
    expect(result.capture).toEqual(capture);
    for (const [index, clip] of testHarness.clips.slice(0, 2).entries()) {
      expect(sceneOf(clip)).toMatchObject({
        revision: result.revision, source: { objectId: result.package.object_id, revision: result.revision },
        html: '<html><body>Edited Maple caption</body></html>',
      });
      expect(clip.app).toMatchObject({ sentinel: { placement: index }, unrelated: `keep-${index}` });
      expect(testHarness.objects.get(result.package.object_id)).toEqual(bytes(sceneOf(clip).packageBody));
    }
    expect(testHarness.objects.get(result.entry.object_id)).toEqual(changedEntry(original).entry.bytes);
    expect(testHarness.clips[1].app?.duringPublication).toBe('keep-fresh-app');
    expect(testHarness.clips.map(({ id, track, at, from, to, speed }) => ({ id, track, at, from, to, speed }))).toEqual(oldTiming);
    expect(sceneOf(untargeted).revision).toBe(capture.packageRevision);
    expect(testHarness.calls[testHarness.calls.length - 1]).toBe('flush');
  });

  it('permits a manifest-only edit with a new package digest and the same entry digest', async () => {
    const { testHarness, original, capture } = await importedScene();
    const result = await publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id),
      package: { ...original, manifest: { ...original.manifest, authoredFps: 60 } }, capture,
    });
    expect(result.revision).not.toBe(capture.packageRevision);
    expect(result.entry.digest).toBe(capture.entryRevision);
    expect(testHarness.clips.map((clip) => sceneOf(clip).revision)).toEqual([result.revision, result.revision]);
    expect(JSON.parse(sceneOf(testHarness.clips[0]).packageBody).manifest.authoredFps).toBe(60);
  });

  it.each(['packageRevision', 'entryRevision'] as const)('rejects a mismatched captured old %s before candidate ingestion', async (field) => {
    const { testHarness, original, capture } = await importedScene();
    const before = structuredClone(testHarness.clips);
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original),
      capture: { ...capture, [field]: 'sha256:' + '0'.repeat(64) },
    })).rejects.toThrow(field === 'packageRevision' ? 'old package identity' : 'old entry identity');
    expect(testHarness.ingested).toHaveLength(0);
    expect(testHarness.patch.current).toBeUndefined();
    expect(testHarness.clips).toEqual(before);
  });

  it.each(['package', 'entry', 'packageBody', 'html'] as const)('cross-checks persisted %s and rejects inconsistent inline caches before ingestion', async (field) => {
    const { testHarness, original, capture } = await importedScene();
    const scene = sceneOf(testHarness.clips[1]);
    if (field === 'package') testHarness.objects.set(scene.source.objectId, bytes('{}'));
    if (field === 'entry') {
      const entry = JSON.parse(scene.packageBody).entry as ProjectObjectMetadata;
      testHarness.objects.set(entry.object_id, bytes('corrupt HTML'));
    }
    if (field === 'packageBody') testHarness.clips[1].app = { ...testHarness.clips[1].app, liveScene: { ...scene, packageBody: scene.packageBody + ' ' } };
    if (field === 'html') testHarness.clips[1].app = { ...testHarness.clips[1].app, liveScene: { ...scene, html: '<html>unpersisted cache</html>' } };
    const before = structuredClone(testHarness.clips);
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
    })).rejects.toThrow(/canonical|integrity/);
    expect(testHarness.ingested).toHaveLength(0);
    expect(testHarness.patch.current).toBeUndefined();
    expect(testHarness.clips).toEqual(before);
  });

  it('rejects a conflicting public source ref even when the inline scene still matches capture', async () => {
    const { testHarness, original, capture } = await importedScene();
    testHarness.clips[1].sourceRefs = [{
      id: 'inconsistent-ref', clipId: testHarness.clips[1].id, sourceKind: 'provider', extensionId: EXTENSION_ID,
      sourceObjectId: sceneOf(testHarness.clips[1]).source.objectId,
      sourceRevision: 'sha256:' + '0'.repeat(64), packageRevision: capture.packageRevision,
    }];
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
    })).rejects.toThrow('old package identity');
    expect(testHarness.ingested).toHaveLength(0);
    expect(testHarness.patch.current).toBeUndefined();
  });

  it.each(['filename', 'media_type', 'size'] as const)('verifies canonical entry %s metadata against the manifest and actual object', async (field) => {
    const { testHarness, original, capture } = await importedScene();
    const oldScene = sceneOf(testHarness.clips[0]);
    const body = JSON.parse(oldScene.packageBody) as { entry: ProjectObjectMetadata };
    body.entry = { ...body.entry, [field]: field === 'size' ? body.entry.size + 1 : 'incorrect-entry-metadata' };
    const packageBody = JSON.stringify(body);
    const packageRevision = await digest(bytes(packageBody));
    testHarness.objects.set(packageRevision, bytes(packageBody));
    for (const clip of testHarness.clips) {
      clip.app = { ...clip.app, liveScene: {
        ...oldScene, revision: packageRevision, source: { objectId: packageRevision, revision: packageRevision }, packageBody,
      } };
    }
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original),
      capture: { ...capture, packageRevision },
    })).rejects.toThrow(/old entry identity|integrity/);
    expect(testHarness.ingested).toHaveLength(0);
    expect(testHarness.patch.current).toBeUndefined();
  });

  it.each(['type', 'track', 'removed', 'project', 'timeline'] as const)('rejects initial %s mismatches before reading or ingesting objects', async (field) => {
    const { testHarness, original, capture } = await importedScene();
    if (field === 'type') testHarness.clips[1].clipType = 'hold';
    if (field === 'track') testHarness.tracks[0].kind = 'audio';
    if (field === 'removed') testHarness.clips.splice(1, 1);
    if (field === 'project') testHarness.control.projectId = 'other-project';
    if (field === 'timeline') testHarness.control.timelineId = 'other-timeline';
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
    })).rejects.toThrow(/wrong clip type|visual|does not exist|scope changed/);
    expect(testHarness.calls).toEqual([]);
    expect(testHarness.patch.current).toBeUndefined();
  });

  it('rejects an already stale version before reading or ingesting objects', async () => {
    const { testHarness, original, capture } = await importedScene();
    testHarness.control.version += 1;
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
    })).rejects.toThrow('stale');
    expect(testHarness.calls).toEqual([]);
    expect(testHarness.patch.current).toBeUndefined();
  });

  it.each(['version', 'source', 'entry-cache', 'package-cache', 'object', 'type', 'track', 'removed', 'project', 'timeline'] as const)(
    'rejects a competing %s change during async ingestion without applying either placement', async (field) => {
      const { testHarness, original, capture } = await importedScene();
      const ingestStarted = deferred();
      const ingestReleased = deferred();
      testHarness.control.onIngest = async () => { ingestStarted.resolve(); await ingestReleased.promise; };
      const publication = publishLiveSceneEdit(testHarness.ctx, {
        placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
      });
      const rejection = expect(publication).rejects.toThrow(/stale|old source|old package|wrong clip type|visual|does not exist|scope changed/);
      await ingestStarted.promise;
      const clip = testHarness.clips[1];
      const scene = sceneOf(clip);
      if (field === 'version') testHarness.control.version += 1;
      if (field === 'source') clip.app = { ...clip.app, liveScene: { ...scene, revision: 'sha256:' + '1'.repeat(64) } };
      if (field === 'entry-cache') clip.app = { ...clip.app, liveScene: { ...scene, html: 'competing entry' } };
      if (field === 'package-cache') clip.app = { ...clip.app, liveScene: { ...scene, packageBody: scene.packageBody + ' ' } };
      if (field === 'object') clip.app = { ...clip.app, liveScene: { ...scene, source: { ...scene.source, objectId: 'competing-package-object' } } };
      if (field === 'type') clip.clipType = 'hold';
      if (field === 'track') testHarness.tracks[0].kind = 'audio';
      if (field === 'removed') testHarness.clips.splice(1, 1);
      if (field === 'project') testHarness.control.projectId = 'project-other';
      if (field === 'timeline') testHarness.control.timelineId = 'timeline-other';
      const newerState = structuredClone(testHarness.clips);
      ingestReleased.resolve();
      await rejection;
      expect(testHarness.patch.current).toBeUndefined();
      expect(testHarness.calls).not.toContain('apply');
      expect(testHarness.clips).toEqual(newerState);
      expect(sceneOf(testHarness.clips[0]).revision).toBe(capture.packageRevision);
    },
  );

  it('rechecks scope after asynchronous old-object reads', async () => {
    const { testHarness, original, capture } = await importedScene();
    testHarness.control.onRead = () => { testHarness.control.timelineId = 'timeline-other'; };
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
    })).rejects.toThrow('scope changed');
    expect(testHarness.patch.current).toBeUndefined();
  });

  it('keeps the explicit placements, old capture and candidate bytes fixed across async reads', async () => {
    const { testHarness, original, capture } = await importedScene();
    const candidate = changedEntry(original);
    const expectedEntryBytes = new Uint8Array(candidate.entry.bytes);
    const placementIds = PLACEMENTS.map(({ id }) => id);
    testHarness.control.onRead = () => {
      placementIds.splice(0, placementIds.length, 'redirected-placement');
      Object.assign(capture, { packageRevision: 'sha256:' + '0'.repeat(64), capturedTimelineVersion: 0 });
      candidate.entry.bytes.fill(0);
    };
    const result = await publishLiveSceneEdit(testHarness.ctx, { placementIds, package: candidate, capture });
    expect(result.affectedPlacements).toEqual(PLACEMENTS.map(({ id }) => id));
    expect(result.entry.digest).toBe(await digest(expectedEntryBytes));
    expect(result.capture.capturedTimelineVersion).toBe(8);
    expect(testHarness.clips.map((clip) => sceneOf(clip).revision)).toEqual([result.revision, result.revision]);
  });

  it('rejects corrupt candidate object readback before applying placements', async () => {
    const { testHarness, original, capture } = await importedScene();
    testHarness.control.onIngest = () => {
      const object = testHarness.ingested[testHarness.ingested.length - 1];
      testHarness.objects.set(object.id, bytes('corrupted ingestion'));
    };
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original), capture,
    })).rejects.toThrow('readback mismatch');
    expect(testHarness.patch.current).toBeUndefined();
    expect(testHarness.clips.every((clip) => sceneOf(clip).revision === capture.packageRevision)).toBe(true);
  });

  it.each([
    { placementIds: [] }, { placementIds: [PLACEMENTS[0].id, PLACEMENTS[0].id] },
    { placementIds: [''] }, { placementIds: [' '] },
  ])('rejects invalid explicit placement IDs $placementIds', async ({ placementIds }) => {
    const { testHarness, original, capture } = await importedScene();
    await expect(publishLiveSceneEdit(testHarness.ctx, { placementIds, package: changedEntry(original), capture })).rejects.toThrow('placement IDs');
    expect(testHarness.calls).toEqual([]);
    expect(testHarness.patch.current).toBeUndefined();
  });

  it('rejects version zero rather than using the public patch wildcard', async () => {
    const { testHarness, original, capture } = await importedScene();
    await expect(publishLiveSceneEdit(testHarness.ctx, {
      placementIds: PLACEMENTS.map(({ id }) => id), package: changedEntry(original),
      capture: { ...capture, capturedTimelineVersion: 0 },
    })).rejects.toThrow('capture is incomplete or invalid');
    expect(testHarness.calls).toEqual([]);
    expect(testHarness.patch.current).toBeUndefined();
  });
});
