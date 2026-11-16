// @vitest-environment jsdom
import * as React from 'react';
import { createElement } from 'react';
import { act, cleanup, createEvent, fireEvent, render, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WorldExplorerPanel } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/WorldExplorerPanel';
import { fakeDocuments, storedWorlds, type FakeDocuments } from './world_explorer_documents';
import { quatRotate, type ViewPose } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/camera';
import { KEY_MAP } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/model';
import { ingestPreparedPackage, parsePreparedScenePackage } from '../../../astrid/packs/rendering/ui/threejs-live-editor/authoring';
import { SAMPLE_WORLD } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/worlds/sample';
import { LIVE_SCENE_CLIP_TYPE_ID } from '../../../astrid/packs/rendering/ui/threejs-live-editor/identity';

// The mock records every render the panel makes (the props it hands the scene), the latest callbacks,
// and how often the scene was mounted or unmounted.
const scene = vi.hoisted(() => ({
  renders: [] as Array<{
    sourceTime: number;
    worldTime?: number;
    view?: { position: [number, number, number]; quaternion: [number, number, number, number]; fov: number };
    width: number;
    height: number;
  }>,
  onStatus: null as null | ((status: unknown) => void),
  onReady: null as null | ((info: unknown) => void),
  mounts: 0,
  unmounts: 0,
  live: false,
}));

// The hidden still renderer draws through its own SceneSurface; keep it out of the viewer's recorded renders.
vi.mock('../../../astrid/packs/rendering/ui/threejs-live-editor/shotStillRenderer', () => ({ ShotStillRenderer: () => null }));

vi.mock('../../../astrid/packs/rendering/ui/threejs-live-editor/SceneSurface', () => ({
  SceneSurface: (props: {
    sourceTime: number;
    worldTime?: number;
    view?: { position: [number, number, number]; quaternion: [number, number, number, number]; fov: number };
    width: number;
    height: number;
    onStatus: (status: unknown) => void;
    onReady: (info: unknown) => void;
  }) => {
    React.useEffect(() => {
      scene.mounts += 1;
      scene.live = true;
      return () => {
        scene.unmounts += 1;
        scene.live = false;
      };
    }, []);
    scene.renders.push({
      sourceTime: props.sourceTime,
      worldTime: props.worldTime,
      view: props.view,
      width: props.width,
      height: props.height,
    });
    scene.onStatus = props.onStatus;
    scene.onReady = props.onReady;
    return null;
  },
}));

// Deterministic animation frames: requestAnimationFrame queues, performance.now is a fake clock.
let pending = new Map<number, FrameRequestCallback>();
let nextFrame = 1;
let nowMs = 1000;

/** Runs `count` animation frames, each `stepMs` after the last, flushing React updates after each. */
function advance(count: number, stepMs = 16) {
  for (let i = 0; i < count; i += 1) {
    nowMs += stepMs;
    const due = [...pending.values()];
    pending.clear();
    act(() => {
      for (const callback of due) callback(nowMs);
    });
  }
}

beforeEach(() => {
  pending = new Map();
  nextFrame = 1;
  nowMs = 1000;
  scene.renders.length = 0;
  scene.onStatus = null;
  scene.onReady = null;
  scene.mounts = 0;
  scene.unmounts = 0;
  scene.live = false;
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    const id = nextFrame++;
    pending.set(id, callback);
    return id;
  });
  vi.stubGlobal('cancelAnimationFrame', (id: number) => {
    pending.delete(id);
  });
  vi.spyOn(performance, 'now').mockImplementation(() => nowMs);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

async function sha256(bytes: Uint8Array): Promise<string> {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', bytes as unknown as BufferSource);
  return `sha256:${Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

/** In-memory project object store with real digests, mirroring test_live_scene_authoring. */
function makeStorage(objects = new Map<string, Uint8Array>()) {
  return {
    objects,
    async ingest(input: Uint8Array, mediaType: string, filename?: string) {
      const bytes = new Uint8Array(input);
      const digest = await sha256(bytes);
      objects.set(digest, bytes);
      return { object_id: digest, digest, media_type: mediaType, size: bytes.byteLength, filename };
    },
    async read(objectId: string) {
      const value = objects.get(objectId);
      if (!value) throw new Error(`unknown object ${objectId}`);
      return new Uint8Array(value);
    },
  };
}

function makeSettings(store = new Map<string, unknown>()) {
  return {
    store,
    get: (key: string) => store.get(key),
    set: (key: string, value: unknown) => { store.set(key, value); },
    delete: (key: string) => store.delete(key),
    keys: () => [...store.keys()],
    subscribe: () => ({ dispose() {} }),
  };
}

/** The sample package as a real live scene envelope, the same shape a timeline clip carries. */
async function sampleLiveScene() {
  const parsed = await parsePreparedScenePackage(new TextEncoder().encode(SAMPLE_WORLD.packageJson));
  const storage = makeStorage();
  const ingested = await ingestPreparedPackage(storage, parsed);
  // The persisted reference carries no HTML: keep the ingested objects so every harness's storage can serve them.
  return { liveScene: ingested.liveScene, duration: parsed.manifest.duration, objects: storage.objects };
}
const SAMPLE = await sampleLiveScene();

function liveClip(id = 'c1') {
  return {
    id,
    track: 'V1',
    at: 2,
    duration: 3,
    clipType: LIVE_SCENE_CLIP_TYPE_ID,
    sourceOffset: 1,
    sourceEnd: 4,
    rate: 1,
    managed: false,
    app: { liveScene: SAMPLE.liveScene },
  };
}

/** The sample package with its HTML entry changed, so it is a different world with its own digest. */
async function variantPackage(suffix: string): Promise<Uint8Array> {
  const body = JSON.parse(SAMPLE_WORLD.packageJson) as { html: string; entry: Record<string, unknown> };
  body.html = `${body.html}<!-- ${suffix} -->`;
  const html = new TextEncoder().encode(body.html);
  body.entry = { ...body.entry, size: html.byteLength, digest: await sha256(html) };
  return new TextEncoder().encode(JSON.stringify(body));
}

/** A library entry backed by a real stored package. */
async function libraryEntry(storage: ReturnType<typeof makeStorage>, suffix: string, name: string) {
  const parsed = await parsePreparedScenePackage(await variantPackage(suffix));
  const ingested = await ingestPreparedPackage(storage, parsed);
  return {
    objectId: ingested.package.object_id,
    revision: ingested.package.digest,
    name,
    duration: parsed.manifest.duration,
    authoredFps: parsed.manifest.authoredFps,
    events: [] as Array<{ name: string; start: number; end?: number }>,
    shots: [] as Array<{ name: string; start: number; end: number; worldStart?: number }>,
  };
}

type HarnessOptions = {
  projectId?: string | null;
  clips?: unknown[];
  settings?: ReturnType<typeof makeSettings>;
  storage?: ReturnType<typeof makeStorage>;
  /** Seeds the project's library document. */
  library?: unknown[];
  documents?: FakeDocuments;
};

function makeHarness(options: HarnessOptions = {}) {
  const projectId = options.projectId === undefined ? 'p1' : options.projectId;
  const settings = options.settings ?? makeSettings();
  const storage = options.storage ?? makeStorage();
  // A clip stores a reference only, so the sample's package and entry must be readable from this storage.
  for (const [id, bytes] of SAMPLE.objects) if (!storage.objects.has(id)) storage.objects.set(id, bytes);
  const documents = options.documents ?? fakeDocuments({ projectId: projectId ?? 'none', library: options.library });
  const snapshot = {
    projectId,
    timelineId: 't1',
    baseVersion: 1,
    tracks: [{ id: 'V1', kind: 'visual', label: 'V1', muted: false }],
    clips: options.clips ?? [],
    outputMetadata: { resolution: '1280x720' },
  };
  const readerListeners = new Set<() => void>();
  const timeline = {
    validate: vi.fn(() => ({ valid: true, diagnostics: [] })),
    apply: vi.fn((patch: { operations: Array<{ target: string }> }) => ({
      affectedObjectIds: patch.operations.map((operation) => operation.target),
    })),
    flush: vi.fn(async () => ({ version: 2 })),
  };
  const toast = vi.fn();
  const report = vi.fn();
  const ctx = {
    creative: {
      documents,
      reader: {
        snapshot: () => snapshot,
        subscribe: (listener: () => void) => {
          readerListeners.add(listener);
          return () => { readerListeners.delete(listener); };
        },
      },
      timeline,
      projectObjects: storage,
    },
    services: { settings, diagnostics: { report } },
    chrome: { toast },
  };

  let viewSnapshot: { playhead: { time: number }; selection: { selectedClipIds: Set<string> } } = {
    playhead: { time: 4 },
    selection: { selectedClipIds: new Set<string>() },
  };
  const viewListeners = new Set<() => void>();
  const seek = vi.fn();
  const video = {
    view: {
      getSnapshot: () => viewSnapshot,
      subscribe: (listener: () => void) => {
        viewListeners.add(listener);
        return { dispose: () => { viewListeners.delete(listener); } };
      },
    },
    seek,
  };

  return {
    ctx: ctx as unknown as Parameters<typeof WorldExplorerPanel>[0]['context'],
    raw: ctx,
    settings,
    storage,
    documents,
    timeline,
    toast,
    report,
    seek,
    video,
    selectClip(ids: string[]) {
      viewSnapshot = { ...viewSnapshot, selection: { selectedClipIds: new Set(ids) } };
      act(() => viewListeners.forEach((listener) => listener()));
    },
  };
}

function mount(
  harness: ReturnType<typeof makeHarness>,
  options: { visible?: boolean; fullscreen?: { active: boolean; enter: () => void; exit: () => void } } = {},
) {
  const element = (visible: boolean) => createElement(WorldExplorerPanel, {
    context: harness.ctx,
    instanceId: 'explorer:worlds',
    visible,
    video: harness.video,
    fullscreen: options.fullscreen,
  } as never);
  const view = render(element(options.visible ?? true));
  return { ...view, setVisible: (value: boolean) => view.rerender(element(value)) };
}

type Mounted = ReturnType<typeof mount>;

const latest = () => scene.renders[scene.renders.length - 1]!;
const viewerOf = (view: Mounted) => view.getByRole('application', { name: 'World viewer' });

/** Acknowledges the newest frame the panel sent, which lets it send the next one. */
function ack() {
  const last = latest();
  act(() => scene.onStatus!({ phase: 'ready', revision: 'rev', sourceTime: last.sourceTime, view: last.view }));
}

/** One frame, then an acknowledgement, so every frame is sent and the scene is never behind. */
function stepAck(count: number, stepMs = 16) {
  for (let i = 0; i < count; i += 1) {
    advance(1, stepMs);
    ack();
  }
}

/** Runs `totalMs` of frames at `stepMs` each, acknowledging every one. */
function runFor(totalMs: number, stepMs: number) {
  for (let elapsed = 0; elapsed < totalMs; elapsed += stepMs) stepAck(1, stepMs);
}

/** Distinct free-view frames the panel has sent so far (each send makes a new view object). */
function sentViews() {
  return new Set(scene.renders.map((render) => render.view).filter((view) => view !== undefined)).size;
}

/** Waits for the scene to be mounted, hands it a declared world, then acknowledges the view frame so the world reveals. */
async function openReady(
  explore: unknown,
  info: { events?: unknown[]; shots?: unknown[] } = {},
) {
  await waitFor(() => expect(scene.live).toBe(true));
  act(() => scene.onReady!({ events: info.events ?? [], shots: info.shots ?? [], explore }));
  act(() => scene.onStatus!({ phase: 'ready', revision: 'rev', sourceTime: latest().sourceTime }));
  ack();
}

function press(element: HTMLElement, key: string, init: KeyboardEventInit = {}) {
  const event = createEvent.keyDown(element, { key, ...init });
  fireEvent(element, event);
  return event;
}

function release(element: HTMLElement, key: string, init: KeyboardEventInit = {}) {
  fireEvent.keyUp(element, { key, ...init });
}

/** A mouse-style pointer event: jsdom has no PointerEvent, so pointerId and pointerType are defined by hand. */
function pointer(element: HTMLElement, type: 'pointerdown' | 'pointermove' | 'pointerup', x: number, y: number, id = 1) {
  const event = new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0 });
  Object.defineProperty(event, 'pointerId', { value: id });
  Object.defineProperty(event, 'pointerType', { value: 'mouse' });
  act(() => {
    element.dispatchEvent(event);
  });
}

const HINT_TEXT = 'Drag to look · WASD to move · Scroll for speed';
const START_POSITION = [1, 1.5, 2] as const;
const EXPLORE = { start: { position: START_POSITION, target: [1, 1.5, -8] as const }, speed: 2 };
const CAMERAS = [
  { name: 'A', start: 0, end: 3 },
  { name: 'B', start: 3, end: 6 },
];
const EVENTS = [
  { name: 'Dawn', start: 1 },
  { name: 'Lanterns', start: 4, end: 5 },
];

describe('World Explorer free roam', () => {
  it('keeps one frame in flight and sends the newest time and view on the next acknowledgement', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const before = sentViews();

    advance(6);
    const afterSix = sentViews();
    expect(afterSix - before).toBeLessThanOrEqual(1);

    // Without an acknowledgement the scene is not sent anything new, however long the frames run.
    advance(10);
    expect(sentViews()).toBe(afterSix);
    const sentTime = latest().sourceTime;

    ack();
    expect(latest().sourceTime).toBeGreaterThan(sentTime);
    expect(latest().sourceTime).toBe(latest().worldTime);
    expect(sentViews()).toBe(afterSix + 1);

    for (const render of scene.renders) {
      if (render.view) expect(render.sourceTime).toBe(render.worldTime);
    }
    expect(view.getByRole('button', { name: 'Pause' })).toBeTruthy();
  });

  it('consumes the movement, reset, pause and zoom keys', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());

    const keys: Array<[string, KeyboardEventInit?]> = [
      ['w'], ['a'], ['s'], ['d'], ['e'], ['q'], ['r'], [' '],
      ['ArrowUp'], ['ArrowDown'], ['ArrowLeft'], ['ArrowRight'],
      ['W', { shiftKey: true }],
    ];
    for (const [key, init] of keys) {
      const event = press(viewer, key, init);
      expect(event.defaultPrevented, `key ${key}`).toBe(true);
      release(viewer, key, init);
    }
  });

  it('leaves Escape, Tab, Shift, plain x and modifier chords to the page', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    const start = [...latest().view!.position];

    const unhandled: Array<[string, KeyboardEventInit?]> = [
      ['Escape'], ['Tab'], ['Shift', { shiftKey: true }], ['x'], ['w', { ctrlKey: true }], ['w', { metaKey: true }],
    ];
    for (const [key, init] of unhandled) {
      const event = press(viewer, key, init);
      expect(event.defaultPrevented, `key ${key}`).toBe(false);
      release(viewer, key, init);
    }
    stepAck(5);
    expect([...latest().view!.position]).toEqual(start);
  });

  it('walks along the look direction while held, stops on release, and Shift is three times faster', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    const start = [...latest().view!.position];

    press(viewer, 'w');
    stepAck(10);
    // Speed eases in, so the camera is still short of its top speed after 160 ms.
    const earlyDistance = start[2] - latest().view!.position[2];
    expect(earlyDistance).toBeGreaterThan(0);
    expect(earlyDistance).toBeLessThan(EXPLORE.speed * 0.16);
    release(viewer, 'w');
    // ...and it glides to rest after release instead of stopping dead.
    stepAck(2);
    const gliding = latest().view!.position[2];
    expect(gliding).toBeLessThan(start[2] - earlyDistance);
    runFor(1500, 16);
    const walked = [...latest().view!.position];
    expect(walked[2]).toBeLessThan(start[2] - earlyDistance);
    expect(walked[0]).toBeCloseTo(start[0], 9);
    expect(walked[1]).toBeCloseTo(start[1], 9);
    stepAck(5);
    expect(latest().view!.position[2]).toBeCloseTo(walked[2], 9);
    const walkDistance = start[2] - walked[2];

    // From rest again, the same hold with Shift travels three times as far.
    press(viewer, 'W', { shiftKey: true });
    stepAck(10);
    release(viewer, 'W', { shiftKey: true });
    runFor(1500, 16);
    const runDistance = walked[2] - latest().view!.position[2];
    expect(runDistance / walkDistance).toBeCloseTo(3, 5);
  });

  it('moves the same distance in the same time whatever the frame length', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    const start = latest().view!.position[2];

    press(viewer, 'w');
    runFor(480, 16);
    release(viewer, 'w');
    runFor(1504, 16);
    const shortSteps = start - latest().view!.position[2];
    expect(shortSteps).toBeGreaterThan(0.5);

    press(viewer, 'r');
    runFor(500, 16);
    expect(latest().view!.position[2]).toBeCloseTo(start, 9);

    press(viewer, 'w');
    runFor(480, 32);
    release(viewer, 'w');
    runFor(1504, 32);
    const longSteps = start - latest().view!.position[2];
    expect(longSteps).toBeCloseTo(shortSteps, 6);
  });

  it('ignores movement keys unless the viewer has focus', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const start = [...latest().view!.position];

    fireEvent.keyDown(document.body, { key: 'w' });
    fireEvent.keyDown(view.getByRole('button', { name: 'Reset view' }), { key: 'w' });
    stepAck(10);
    expect([...latest().view!.position]).toEqual(start);
  });

  it('Space toggles the world pause and stops world time; R returns to the start view', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());

    press(viewer, ' ');
    expect(view.getByRole('button', { name: 'Play' })).toBeTruthy();
    const frozen = latest().worldTime;
    stepAck(5);
    expect(latest().worldTime).toBe(frozen);

    press(viewer, ' ');
    expect(view.getByRole('button', { name: 'Pause' })).toBeTruthy();
    stepAck(3);
    expect(latest().worldTime).toBeGreaterThan(frozen!);

    press(viewer, 'w');
    stepAck(10);
    release(viewer, 'w');
    expect(latest().view!.position[2]).toBeLessThan(START_POSITION[2]);
    press(viewer, 'r');
    runFor(500, 16);
    latest().view!.position.forEach((value, axis) => expect(value).toBeCloseTo(START_POSITION[axis], 9));
  });

  it('never leaves the explore box, however long a key is held', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    const BOX = { min: [-0.5, -0.5, -0.5] as const, max: [0.5, 0.5, 0.5] as const };
    await openReady({ start: { position: [0, 0, 0], target: [0, 0, -10] }, bounds: BOX, speed: 2 });
    const viewer = viewerOf(view);
    act(() => viewer.focus());

    press(viewer, 'w');
    stepAck(300);
    release(viewer, 'w');
    expect(latest().view!.position[2]).toBeCloseTo(BOX.min[2], 6);

    press(viewer, 'e');
    stepAck(300);
    release(viewer, 'e');
    expect(latest().view!.position[1]).toBeCloseTo(BOX.max[1], 6);

    press(viewer, 'a');
    stepAck(300);
    release(viewer, 'a');
    expect(latest().view!.position[0]).toBeCloseTo(BOX.min[0], 6);

    for (const render of scene.renders) {
      if (!render.view) continue;
      render.view.position.forEach((value, axis) => {
        expect(value).toBeGreaterThanOrEqual(BOX.min[axis] - 1e-9);
        expect(value).toBeLessThanOrEqual(BOX.max[axis] + 1e-9);
      });
    }
  });

  it('a drag looks around, and pitch stops at 89 degrees', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    const startQuaternion = [...latest().view!.quaternion];

    pointer(viewer, 'pointerdown', 100, 100);
    pointer(viewer, 'pointermove', 160, 100);
    pointer(viewer, 'pointerup', 160, 100);
    runFor(400, 16);
    expect([...latest().view!.quaternion]).not.toEqual(startQuaternion);
    expect(quatRotate(latest().view!.quaternion, [0, 0, -1])[0]).toBeGreaterThan(0.1);

    const limit = Math.sin((89 * Math.PI) / 180);
    pointer(viewer, 'pointerdown', 100, 100);
    for (let y = 200; y <= 4000; y += 200) pointer(viewer, 'pointermove', 100, y);
    pointer(viewer, 'pointerup', 100, 4000);
    runFor(400, 16);
    const down = quatRotate(latest().view!.quaternion, [0, 0, -1]);
    expect(Math.abs(down[1])).toBeLessThanOrEqual(limit + 1e-9);
    expect(down[1]).toBeCloseTo(-limit, 4);

    pointer(viewer, 'pointerdown', 100, 100);
    for (let y = 100; y >= -4000; y -= 200) pointer(viewer, 'pointermove', 100, y);
    pointer(viewer, 'pointerup', 100, -4000);
    runFor(400, 16);
    const up = quatRotate(latest().view!.quaternion, [0, 0, -1]);
    expect(up[1]).toBeCloseTo(limit, 4);
  });

  it('uses a 50 degree field of view and a crosshair that hides while dragging', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    expect(latest().view!.fov).toBe(50);
    expect(viewer.style.cursor).toBe('crosshair');
    pointer(viewer, 'pointerdown', 100, 100);
    expect(viewer.style.cursor).toBe('none');
    pointer(viewer, 'pointerup', 100, 100);
    expect(viewer.style.cursor).toBe('crosshair');
  });

  it('the wheel changes the speed chip, scales walking, and is consumed by the viewer', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    expect(view.getByLabelText('Camera speed')).toHaveTextContent('1×');

    const wheel = createEvent.wheel(viewer, { deltaY: -100 });
    fireEvent(viewer, wheel);
    expect(wheel.defaultPrevented).toBe(true);
    expect(view.getByLabelText('Camera speed')).toHaveTextContent('1.5×');

    press(viewer, 'w');
    stepAck(10);
    const fast = EXPLORE.start.position[2] - latest().view!.position[2];
    release(viewer, 'w');
    press(viewer, 'r');
    ack();
    fireEvent.wheel(viewer, { deltaY: 100 });
    expect(view.getByLabelText('Camera speed')).toHaveTextContent(/^1×$/);
    expect(fast).toBeGreaterThan(0);
  });

  it('shows the hint once the world is ready, hides it on dismiss and remembers that', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    expect(view.queryByRole('note')).toBeNull();
    await openReady(EXPLORE);
    expect(view.getByRole('note')).toHaveTextContent(HINT_TEXT);

    fireEvent.click(view.getByRole('button', { name: 'Dismiss hint' }));
    expect(view.queryByRole('note')).toBeNull();
    expect(harness.settings.store.get('exploreHintDismissed')).toBe(true);
    view.unmount();

    const again = mount(makeHarness({
      clips: [liveClip()],
      settings: makeSettings(new Map<string, unknown>([['exploreHintDismissed', true]])),
    }));
    await openReady(EXPLORE);
    expect(again.queryByRole('note')).toBeNull();
    expect(again.getByLabelText('Camera speed')).toBeTruthy();
  });

  it('Reset view restores the start pose after moving', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());

    press(viewer, 'w');
    stepAck(10);
    release(viewer, 'w');
    expect(latest().view!.position[2]).toBeLessThan(START_POSITION[2]);

    fireEvent.click(view.getByRole('button', { name: 'Reset view' }));
    ack();
    // It glides back rather than snapping.
    advance(1, 16);
    ack();
    expect([...latest().view!.position]).not.toEqual([...START_POSITION]);
    runFor(500, 16);
    latest().view!.position.forEach((value, axis) => expect(value).toBeCloseTo(START_POSITION[axis], 9));
  });

  it('Shots mode sends no view or world time, and Explore sends both again', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { shots: CAMERAS });
    expect(latest().view).toBeDefined();

    const start = scene.renders.length;
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy();
    ack();
    advance(5);
    ack();
    advance(5);
    expect(scene.renders.slice(start).length).toBeGreaterThan(0);
    for (const render of scene.renders.slice(start)) {
      expect(render.view).toBeUndefined();
      expect(render.worldTime).toBeUndefined();
    }

    fireEvent.click(view.getByRole('button', { name: 'Explore' }));
    ack();
    expect(latest().view).toBeDefined();
    expect(latest().worldTime).toBeDefined();
    expect(view.getByRole('button', { name: 'Reset view' })).toBeTruthy();
  });

  it('a world that is not explorable disables Explore, says so, and never sends a view', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(null);

    expect(view.getByRole('button', { name: 'Explore' })).toBeDisabled();
    expect(view.getByText(/has no free view yet/)).toBeTruthy();
    expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy();
    advance(5);
    ack();
    advance(5);
    expect(scene.renders.every((render) => render.view === undefined)).toBe(true);
  });

  it('a shot card scopes Shots travel to one shot and its stretch', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { shots: CAMERAS });
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));

    const cards = within(view.getByRole('list', { name: 'Shots' }));
    expect(cards.getAllByRole('button').map((card) => card.getAttribute('aria-label'))).toEqual([
      'A, 0:00 – 0:03',
      'B, 0:03 – 0:06',
    ]);
    const slider = view.getByRole('slider', { name: 'Travel position' });
    expect(slider).toHaveAttribute('aria-valuemin', '0');
    expect(slider).toHaveAttribute('aria-valuemax', '3');

    fireEvent.click(cards.getByRole('button', { name: 'B, 0:03 – 0:06' }));
    expect(slider).toHaveAttribute('aria-valuemin', '3');
    expect(slider).toHaveAttribute('aria-valuemax', '6');
    expect(view.queryByText(/^Using /)).toBeNull();
  });

  it('a world with no shots gets one implicit shot covering the whole world', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));

    expect(within(view.getByRole('list', { name: 'Shots' })).getAllByRole('button')).toHaveLength(1);
    const slider = view.getByRole('slider', { name: 'Travel position' });
    expect(slider).toHaveAttribute('aria-valuemin', '0');
    expect(slider).toHaveAttribute('aria-valuemax', String(SAMPLE.duration));
  });

  it('events covered by a shot appear as cue labels on the time bar', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { events: EVENTS, shots: CAMERAS });
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));

    const chips = () => Array.from(view.container.querySelectorAll('[data-event-marker]')).map((b) => b.getAttribute('aria-label'));
    expect(view.queryByRole('group', { name: 'Shot events' })).toBeNull();
    expect(chips()).toEqual(['Dawn, 0:01.0']);
    fireEvent.click(within(view.getByRole('list', { name: 'Shots' })).getByRole('button', { name: /^B,/ }));
    expect(chips()).toEqual(['Lanterns, 0:04.0']);
  });

  it('selecting a live-scene clip opens its world in Shots mode on the shot holding its start, with its stretch', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { shots: CAMERAS });

    // The world is already open, so following the clip does not reload the scene.
    harness.selectClip(['c1']);

    await waitFor(() => expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy());
    expect(within(view.getByRole('list', { name: 'Shots' })).getByRole('button', { name: /^A,/ })).toHaveAttribute('aria-current', 'true');
    expect(view.getByText(/0:01–0:04/)).toBeTruthy();
    expect(view.getByText(/Editing V1/)).toBeTruthy();
  });

  it('the header shows the position among all worlds and Next steps through them', async () => {
    const storage = makeStorage();
    const lantern = await libraryEntry(storage, 'lantern', 'Lantern Valley');
    const spare = await libraryEntry(storage, 'spare', 'Spare');
    const documents = fakeDocuments({ library: [lantern, spare] });
    const harness = makeHarness({ clips: [liveClip()], storage, documents });
    const view = mount(harness);
    await openReady(EXPLORE);
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('1 / 3');

    fireEvent.click(view.getByRole('button', { name: 'Next world' }));
    await waitFor(() => expect(scene.mounts).toBe(2));
    await openReady(EXPLORE);
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('2 / 3');
    expect(scene.unmounts).toBeGreaterThanOrEqual(1);
  });

  it('Choose world opens one list, and a row switches to that world', async () => {
    const storage = makeStorage();
    const lantern = await libraryEntry(storage, 'lantern', 'Lantern Valley');
    const spare = await libraryEntry(storage, 'spare', 'Spare');
    const documents = fakeDocuments({ library: [lantern, spare] });
    const harness = makeHarness({ clips: [liveClip()], storage, documents });
    const view = mount(harness);
    await openReady(EXPLORE);

    fireEvent.click(view.getByRole('button', { name: 'Choose world' }));
    expect(view.getAllByRole('list', { name: 'In this video' })).toHaveLength(1);
    expect(view.getAllByRole('list', { name: 'Library' })).toHaveLength(1);

    fireEvent.click(within(view.getByRole('list', { name: 'Library' })).getByRole('button', { name: /^Spare/ }));
    await waitFor(() => expect(view.queryByRole('list', { name: 'Library' })).toBeNull());
    await openReady(EXPLORE);
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Spare');
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('3 / 3');
  });

  it('the default world is the last one viewed in this project when it is still listed', async () => {
    const storage = makeStorage();
    const lantern = await libraryEntry(storage, 'lantern', 'Lantern Valley');
    const spare = await libraryEntry(storage, 'spare', 'Spare');
    const documents = fakeDocuments({ library: [lantern, spare] });
    const settings = makeSettings(new Map<string, unknown>([
      ['lastWorld:p1', `${lantern.objectId}@${lantern.revision}`],
    ]));
    const view = mount(makeHarness({ clips: [liveClip()], storage, settings, documents }));
    await openReady(EXPLORE);
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
  });

  it('a stale last world falls back to the first world in the video', async () => {
    const storage = makeStorage();
    const lantern = await libraryEntry(storage, 'lantern', 'Lantern Valley');
    const documents = fakeDocuments({ library: [lantern] });
    const settings = makeSettings(new Map<string, unknown>([
      ['lastWorld:p1', 'sha256:gone@sha256:gone'],
    ]));
    const view = mount(makeHarness({ clips: [liveClip()], storage, settings, documents }));
    await openReady(EXPLORE);
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('1 / 2');
  });

  it('with no worlds in the video, the first library world opens', async () => {
    const storage = makeStorage();
    const lantern = await libraryEntry(storage, 'lantern', 'Lantern Valley');
    const spare = await libraryEntry(storage, 'spare', 'Spare');
    const documents = fakeDocuments({ library: [lantern, spare] });
    const view = mount(makeHarness({ clips: [], storage, documents }));
    await openReady(EXPLORE);
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('1 / 2');
  });

  it('switching worlds stores the choice and starts the new world at its start pose', async () => {
    const storage = makeStorage();
    const lantern = await libraryEntry(storage, 'lantern', 'Lantern Valley');
    const spare = await libraryEntry(storage, 'spare', 'Spare');
    const documents = fakeDocuments({ library: [lantern, spare] });
    const harness = makeHarness({ clips: [liveClip()], storage, documents });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    press(viewer, 'w');
    stepAck(5);
    release(viewer, 'w');

    fireEvent.click(view.getByRole('button', { name: 'Next world' }));
    await openReady({ start: { position: [5, 1, 5], target: [5, 1, -5] }, speed: 2 });

    expect(harness.settings.store.get('lastWorld:p1')).toBe(`${lantern.objectId}@${lantern.revision}`);
    expect([...latest().view!.position]).toEqual([5, 1, 5]);
  });

  it('removes a library world through the single list and keeps the current selection', async () => {
    const storage = makeStorage();
    const lantern = await libraryEntry(storage, 'lantern', 'Lantern Valley');
    const spare = await libraryEntry(storage, 'spare', 'Spare');
    const documents = fakeDocuments({ library: [lantern, spare] });
    const view = mount(makeHarness({ clips: [], storage, documents }));
    await openReady(EXPLORE);
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');

    fireEvent.click(view.getByRole('button', { name: 'Choose world' }));
    fireEvent.click(view.getByRole('button', { name: 'Remove Spare from library' }));

    await waitFor(() => expect(storedWorlds(documents)).toEqual([lantern]));
    expect(view.queryByRole('button', { name: /^Spare/ })).toBeNull();
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
  });

  it('in Shots mode, Space toggles the travel and is prevented from reaching the page', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { shots: CAMERAS });
    harness.selectClip(['c1']);
    await waitFor(() => expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy());
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    expect(view.getByRole('button', { name: 'Play' })).toBeTruthy();

    const event = press(viewer, ' ');
    expect(event.defaultPrevented).toBe(true);
    expect(view.getByRole('button', { name: 'Pause' })).toBeTruthy();

    press(viewer, ' ');
    expect(view.getByRole('button', { name: 'Play' })).toBeTruthy();
  });

  it('in Shots mode, ArrowRight on the viewer moves the travel position by one second', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { shots: CAMERAS });
    harness.selectClip(['c1']);
    await waitFor(() => expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy());
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    const travelSlider = view.getByRole('slider', { name: 'Travel position' });
    // The travel opens at the stretch start (1 s).
    expect(travelSlider).toHaveAttribute('aria-valuenow', '1');

    const event = press(viewer, 'ArrowRight');
    expect(event.defaultPrevented).toBe(true);
    expect(travelSlider).toHaveAttribute('aria-valuenow', '2');
  });

  it('in Shots mode, o ends the stretch at the travel position', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { shots: CAMERAS });
    harness.selectClip(['c1']);
    await waitFor(() => expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy());
    const viewer = viewerOf(view);
    act(() => viewer.focus());

    press(viewer, 'ArrowRight');
    press(viewer, 'o');
    expect(view.getByText(/0:01–0:02/)).toBeTruthy();
  });

  it('in Shots mode, i starts the stretch at the travel position', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { shots: CAMERAS });
    harness.selectClip(['c1']);
    await waitFor(() => expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy());
    const viewer = viewerOf(view);
    act(() => viewer.focus());

    press(viewer, 'ArrowRight');
    press(viewer, 'i');
    expect(view.getByText(/0:02–0:04/)).toBeTruthy();
  });

  it('the hint survives a single drag of more than 12 px, and only fades once the user has also pressed W', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    expect(view.getByRole('note')).toHaveTextContent(HINT_TEXT);

    pointer(viewer, 'pointerdown', 100, 100);
    pointer(viewer, 'pointermove', 120, 100);
    pointer(viewer, 'pointerup', 120, 100);
    stepAck(5);
    expect(view.getByRole('note')).toHaveTextContent(HINT_TEXT);

    press(viewer, 'w');
    release(viewer, 'w');
    await waitFor(() => expect(view.queryByRole('note')).toBeNull(), { timeout: 3000 });
  });

  it('the hint does not fade on W alone; a later drag starts the fade', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());

    press(viewer, 'w');
    release(viewer, 'w');
    stepAck(5);
    expect(view.getByRole('note')).toHaveTextContent(HINT_TEXT);

    pointer(viewer, 'pointerdown', 100, 100);
    pointer(viewer, 'pointermove', 130, 100);
    pointer(viewer, 'pointerup', 130, 100);
    // The drag completes the pair, so the fade starts now; the note leaves only when its timer runs out.
    await waitFor(() => expect(view.queryByRole('note')).toBeNull(), { timeout: 3000 });
  });

  it('? on the focused viewer shows the Key map with every key, and ? again hides it', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE);
    const viewer = viewerOf(view);
    act(() => viewer.focus());
    expect(view.queryByLabelText('Key map')).toBeNull();

    press(viewer, '?');
    const keyMap = view.getByLabelText('Key map');
    const keys = [...keyMap.querySelectorAll('dt')].map((term) => term.textContent);
    expect(keys).toEqual(KEY_MAP.map((row) => row.keys));
    expect(keys.join(' ')).toMatch(/Space/);

    press(viewer, '?');
    expect(view.queryByLabelText('Key map')).toBeNull();
  });

  it('clicking an event chip in Explore moves focus to the viewer', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { events: EVENTS });
    const viewer = viewerOf(view);
    const chip = within(view.getByRole('group', { name: 'Events' })).getByRole('button', { name: /Dawn/ });
    act(() => (chip as HTMLElement).focus());
    expect(document.activeElement).not.toBe(viewer);

    fireEvent.click(chip);
    expect(document.activeElement).toBe(viewer);
  });

  it('under the Explore bar there is only the events, no pick-a-shot row', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await openReady(EXPLORE, { events: EVENTS, shots: CAMERAS });
    expect(view.queryByRole('button', { name: 'pick a shot →' })).toBeNull();
    expect(view.getByRole('group', { name: 'Events' })).toBeTruthy();
  });

});

describe('World Explorer layout', () => {
  const layout = { rootWidth: 900, stageWidth: 600, stageHeight: 300 };
  const observers: Array<{ trigger(): void }> = [];
  let originalResizeObserver: typeof ResizeObserver;

  class FakeResizeObserver {
    private readonly callback: () => void;
    constructor(callback: () => void) {
      this.callback = callback;
      observers.push({ trigger: () => this.callback() });
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  }

  /** The panel measures its root section (its width) and its stage div (both dimensions). */
  function resize() {
    act(() => {
      for (const observer of [...observers]) observer.trigger();
    });
  }

  beforeEach(() => {
    observers.length = 0;
    layout.rootWidth = 900;
    layout.stageWidth = 600;
    layout.stageHeight = 300;
    originalResizeObserver = globalThis.ResizeObserver;
    globalThis.ResizeObserver = FakeResizeObserver as unknown as typeof ResizeObserver;
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
      configurable: true,
      get(this: HTMLElement) { return this.tagName === 'SECTION' ? layout.rootWidth : layout.stageWidth; },
    });
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
      configurable: true,
      get(this: HTMLElement) { return this.tagName === 'SECTION' || this.hasAttribute('data-stage-spacer') ? 0 : layout.stageHeight; },
    });
  });

  afterEach(() => {
    globalThis.ResizeObserver = originalResizeObserver;
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
    delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientHeight;
  });

  it('a wide panel uses the side column and fits the viewer to the stage in both dimensions', async () => {
    const view = mount(makeHarness({ clips: [liveClip()] }));
    await openReady(EXPLORE, { shots: CAMERAS });

    const region = view.getByRole('region', { name: 'World Explorer' });
    expect(region).toHaveAttribute('data-layout', 'side');
    const aside = view.getByRole('complementary', { name: 'Worlds and shots' });
    expect(within(aside).getByRole('group', { name: 'Worlds' })).toBeTruthy();
    expect(within(aside).getByRole('group', { name: 'Mode' })).toBeTruthy();
    expect(view.queryByRole('button', { name: 'Choose world' })).toBeNull();

    // 600x300 stage at 16:9 (1280x720): the widest box that fits is 533 wide, 299 high.
    expect(latest().width).toBe(533);
    expect(latest().height).toBe(299);

    layout.stageHeight = 500;
    resize();
    expect(latest().width).toBe(600);
    expect(latest().height).toBe(337);
  });

  it('a 400 px panel is docked, with the header switcher and no side column', async () => {
    layout.rootWidth = 400;
    const view = mount(makeHarness({ clips: [liveClip()] }));
    await openReady(EXPLORE);
    expect(view.getByRole('region', { name: 'World Explorer' })).toHaveAttribute('data-layout', 'docked');
    expect(view.queryByRole('complementary', { name: 'Worlds and shots' })).toBeNull();
    expect(view.getByRole('button', { name: 'Choose world' })).toBeTruthy();
  });

  it('switching layout keeps the same scene mounted', async () => {
    const view = mount(makeHarness({ clips: [liveClip()] }));
    await openReady(EXPLORE);
    const mounts = scene.mounts;
    const unmounts = scene.unmounts;

    layout.rootWidth = 400;
    resize();
    expect(view.getByRole('region', { name: 'World Explorer' })).toHaveAttribute('data-layout', 'docked');
    layout.rootWidth = 900;
    resize();
    expect(view.getByRole('region', { name: 'World Explorer' })).toHaveAttribute('data-layout', 'side');

    expect(scene.mounts).toBe(mounts);
    expect(scene.unmounts).toBe(unmounts);
  });

  it('the full-screen icon appears only with a fullscreen prop and not while active, and the viewer asks for focus', async () => {
    const enter = vi.fn();
    const view = mount(makeHarness({ clips: [liveClip()] }), { fullscreen: { active: false, enter, exit: vi.fn() } });
    await openReady(EXPLORE);
    expect(viewerOf(view)).toHaveAttribute('data-autofocus');
    fireEvent.click(view.getByRole('button', { name: 'Full screen World Explorer' }));
    expect(enter).toHaveBeenCalledTimes(1);

    view.unmount();
    const active = mount(makeHarness({ clips: [liveClip()] }), { fullscreen: { active: true, enter, exit: vi.fn() } });
    await openReady(EXPLORE);
    expect(active.queryByRole('button', { name: 'Full screen World Explorer' })).toBeNull();

    active.unmount();
    const plain = mount(makeHarness({ clips: [liveClip()] }));
    await openReady(EXPLORE);
    expect(plain.queryByRole('button', { name: 'Full screen World Explorer' })).toBeNull();
  });
});
