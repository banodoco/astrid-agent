// @vitest-environment jsdom
import { createElement } from 'react';
import { act, cleanup, fireEvent, render, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WorldExplorerPanel } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/WorldExplorerPanel';
import { libraryDocumentId } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/library';
import { fakeDocuments, storedWorlds, type FakeDocuments } from './world_explorer_documents';
import { ingestPreparedPackage, parsePreparedScenePackage } from '../../../astrid/packs/rendering/ui/threejs-live-editor/authoring';
import { SAMPLE_WORLD } from '../../../astrid/packs/rendering/ui/threejs-live-editor/explorer/worlds/sample';
import { LIVE_SCENE_CLIP_TYPE_ID } from '../../../astrid/packs/rendering/ui/threejs-live-editor/identity';

// Module-level handle on the scene surface: the mock records every render (all the props the panel passes) and keeps the latest callbacks.
const scene = vi.hoisted(() => ({
  renders: [] as Array<{ sourceTime: number; worldTime?: number; view?: { position: [number, number, number] }; width: number; height: number }>,
  onStatus: null as null | ((status: unknown) => void),
  onReady: null as null | ((info: unknown) => void),
}));

// The hidden still renderer draws through its own SceneSurface; keep it out of the viewer's recorded renders.
vi.mock('../../../astrid/packs/rendering/ui/threejs-live-editor/shotStillRenderer', () => ({ ShotStillRenderer: () => null }));

vi.mock('../../../astrid/packs/rendering/ui/threejs-live-editor/SceneSurface', () => ({
  SceneSurface: (props: {
    sourceTime: number; worldTime?: number; view?: { position: [number, number, number] }; width: number; height: number;
    onStatus: (status: unknown) => void; onReady: (info: unknown) => void;
  }) => {
    scene.renders.push({ sourceTime: props.sourceTime, worldTime: props.worldTime, view: props.view, width: props.width, height: props.height });
    scene.onStatus = props.onStatus;
    scene.onReady = props.onReady;
    return null;
  },
}));

// Deterministic animation frames: requestAnimationFrame queues, performance.now is a fake clock.
let pending = new Map<number, FrameRequestCallback>();
let nextFrame = 1;
let nowMs = 1000;

function frames(count: number) {
  for (let i = 0; i < count; i += 1) {
    nowMs += 16;
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
/** The sample's length, and how the panel prints it: m:ss, and m:ss.t for the travel readout. */
const SAMPLE_END = SAMPLE.duration;
const mmss = (s: number) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
const tenths = (s: number) => `${mmss(Math.floor(s))}.${Math.round((s % 1) * 10)}`;
const SCENE_USE = new RegExp(`^Lantern valley ${mmss(SAMPLE_END)}\\s*· 1 clip$`);

/** What a scene declares when it supports free roam: a start view the viewer can move from. */
const EXPLORE = { start: { position: [0, 1, 5] as [number, number, number], target: [0, 1, 0] as [number, number, number] } };

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

type HarnessOptions = {
  projectId?: string | null;
  timelineId?: string;
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
  let snapshot: Record<string, unknown> = {
    projectId,
    timelineId: options.timelineId ?? 't1',
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
    select: vi.fn(),
    dragSource: vi.fn((spec: unknown) => ({ draggable: true as const, onDragStart: vi.fn(), onDragEnd: vi.fn(), spec })),
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
    setClips(clips: unknown[]) {
      snapshot = { ...snapshot, clips };
      act(() => readerListeners.forEach((listener) => listener()));
    },
    setPlayhead(time: number) {
      viewSnapshot = { ...viewSnapshot, playhead: { time } };
    },
    selectClip(ids: string[]) {
      viewSnapshot = { ...viewSnapshot, selection: { selectedClipIds: new Set(ids) } };
      act(() => viewListeners.forEach((listener) => listener()));
    },
  };
}

function mount(harness: ReturnType<typeof makeHarness>, visible = true, fullscreen?: { active: boolean; enter: () => void; exit: () => void }) {
  const element = (value: boolean) => createElement(WorldExplorerPanel, {
    context: harness.ctx,
    instanceId: 'explorer:worlds',
    visible: value,
    video: harness.video,
    fullscreen,
  } as never);
  const view = render(element(visible));
  return { ...view, setVisible: (value: boolean) => view.rerender(element(value)) };
}

function sceneStatus(status: { phase: string; revision?: string; sourceTime?: number; error?: string; view?: unknown }) {
  expect(scene.onStatus).toBeTruthy();
  act(() => scene.onStatus!({ revision: 'rev', ...status }));
}

/** Acknowledges the frame in flight with the time and view it showed, as the scene does. The panel then sends the newest one. */
function ackLastFrame() {
  const last = scene.renders.at(-1)!;
  sceneStatus({ phase: 'ready', sourceTime: last.sourceTime, view: last.view });
}

/** The view the scene was last handed. */
function lastView() {
  const view = scene.renders.at(-1)?.view;
  if (!view) throw new Error('the last frame carried no view');
  return view;
}

/**
 * Makes the open world ready the way the scene does: it declares what it can do (free roam or not), then reports its frames.
 * Free roam reveals only on a frame that carries the view, so the first ready frame is followed by the panel's view frame.
 */
async function readyWorld(explore = true, shots: readonly unknown[] = []) {
  await waitFor(() => expect(scene.renders.length).toBeGreaterThan(0));
  act(() => scene.onReady!({ events: [], shots, explore: explore ? EXPLORE : null }));
  const first = scene.renders.at(-1)!;
  sceneStatus({ phase: 'ready', sourceTime: first.sourceTime });
  ackLastFrame();
}

/** Adds the sample from the empty state and waits until the scene surface has been handed the world. */
async function addSampleWorld(view: ReturnType<typeof mount>) {
  fireEvent.click(await view.findByRole('button', { name: /^Add Lantern Valley/ }));
  await waitFor(() => expect(scene.renders.length).toBeGreaterThan(0));
}

/** A scene package file as the import control receives it. */
function importedFile() {
  return new File([new TextEncoder().encode(SAMPLE_WORLD.packageJson)], 'harbour-lights.json', { type: 'application/json' });
}

/** Opens the full world list from the docked header and picks one of its rows. */
function pickWorld(view: ReturnType<typeof mount>, name: string) {
  fireEvent.click(view.getByRole('button', { name: 'Choose world' }));
  const worlds = view.getByRole('group', { name: 'Worlds' });
  fireEvent.click(within(worlds).getByRole('button', { name }));
}

describe('World Explorer panel', () => {
  it('shows the empty state, adds the sample into the project library and opens it', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    expect(await view.findByText(/A world is a live 3D place/)).toBeTruthy();
    expect(view.getByRole('button', { name: 'Import world…' })).toBeTruthy();
    expect(view.queryByRole('button', { name: 'Choose world' })).toBeNull();
    // Only the welcome card: no mode toggle, no disabled transport, no empty viewer.
    expect(view.queryByRole('group', { name: 'Mode' })).toBeNull();
    expect(view.queryByRole('button', { name: 'Pause' })).toBeNull();
    expect(view.queryByRole('application', { name: 'World viewer' })).toBeNull();
    expect(view.queryByText(/Choose a world to explore/)).toBeNull();

    await addSampleWorld(view);

    await waitFor(() => expect(storedWorlds(harness.documents)).toHaveLength(1));
    expect(harness.documents.docs.has(libraryDocumentId('none'))).toBe(false);
    expect(storedWorlds(harness.documents)).toEqual([
      expect.objectContaining({
        name: 'Lantern Valley',
        duration: SAMPLE.duration,
        events: SAMPLE_WORLD.events,
        shots: SAMPLE_WORLD.shots,
      }),
    ]);
    expect(harness.toast).toHaveBeenCalledWith('Added Lantern Valley to the library', 'info');
    // The sample is opened straight away: the header names it, the empty state is gone.
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
    expect(view.queryByRole('button', { name: /^Add Lantern Valley/ })).toBeNull();
  });

  it('lists a world from the timeline under In this video, shows its whole stretch and a band for its use', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    // The first world in the video opens by itself.
    await waitFor(() => expect(scene.renders.length).toBeGreaterThan(0));
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern valley');

    fireEvent.click(view.getByRole('button', { name: 'Choose world' }));
    const worlds = view.getByRole('group', { name: 'Worlds' });
    const inVideo = within(worlds).getByRole('list', { name: 'In this video' });
    // The accessible name drops the space before the separator, so the match allows any spacing there.
    expect(within(inVideo).getByRole('button', { name: SCENE_USE })).toBeTruthy();
    expect(within(worlds).queryByRole('list', { name: 'Library' })).toBeNull();
    expect(within(worlds).queryByRole('button', { name: /^Lantern Valley/ })).toBeNull();

    fireEvent.click(within(inVideo).getByRole('button', { name: SCENE_USE }));
    expect(view.queryByRole('group', { name: 'Worlds' })).toBeNull();

    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    expect(view.queryByText(/^Using /)).toBeNull();
    expect(view.getByTitle('On V1 at 0:02')).toBeTruthy();
  });

  it('never queues more than one frame ahead of the scene and sends exactly one newer time per ack', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await readyWorld();
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    ackLastFrame();

    fireEvent.click(view.getByRole('button', { name: 'Play' }));
    const start = scene.renders.length;
    frames(6);
    const inFlight = [...new Set(scene.renders.slice(start).map((render) => render.sourceTime))];
    expect(inFlight).toHaveLength(1);
    const sent = inFlight[0];
    expect(sent).toBeGreaterThan(0);

    const beforeAck = scene.renders.length;
    sceneStatus({ phase: 'ready', sourceTime: sent });
    frames(6);
    const afterAck = [...new Set(scene.renders.slice(beforeAck).map((render) => render.sourceTime))];
    expect(afterAck).toHaveLength(1);
    expect(afterAck[0]).toBeGreaterThan(sent);
  });

  it('handles travel keys on the Shot travel group, and ignores keys from focused controls', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    await readyWorld();
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    const travel = view.getByRole('group', { name: 'Shot travel' });

    expect(view.getByLabelText('Time')).toHaveTextContent(`${mmss(0)} / ${mmss(SAMPLE_END)}`);
    fireEvent.keyDown(travel, { key: 'ArrowRight' });
    expect(view.getByLabelText('Time')).toHaveTextContent(`${mmss(1)} / ${mmss(SAMPLE_END)}`);
    fireEvent.keyDown(travel, { key: 'ArrowRight', shiftKey: true });
    expect(view.getByLabelText('Time')).toHaveTextContent(`${mmss(6)} / ${mmss(SAMPLE_END)}`);
    fireEvent.keyDown(travel, { key: 'ArrowLeft', shiftKey: true });
    expect(view.getByLabelText('Time')).toHaveTextContent(`${mmss(1)} / ${mmss(SAMPLE_END)}`);

    fireEvent.keyDown(travel, { key: 'i' });
    expect(view.getByText(new RegExp(`0:01–${mmss(SAMPLE_END)}`))).toBeTruthy();
    fireEvent.keyDown(travel, { key: 'ArrowRight' });
    fireEvent.keyDown(travel, { key: 'ArrowRight' });
    fireEvent.keyDown(travel, { key: 'o' });
    expect(view.getByText(/0:01–0:03/)).toBeTruthy();

    // Space toggles play and pause.
    fireEvent.keyDown(travel, { key: ' ' });
    expect(view.getByRole('button', { name: 'Pause' })).toBeTruthy();
    fireEvent.keyDown(travel, { key: ' ' });
    expect(view.getByRole('button', { name: 'Play' })).toBeTruthy();

    // Keys from a focused button or select belong to that control.
    fireEvent.keyDown(view.getByRole('button', { name: 'Play' }), { key: ' ' });
    expect(view.getByRole('button', { name: 'Play' })).toBeTruthy();
    expect(view.queryByRole('button', { name: 'Pause' })).toBeNull();
    fireEvent.keyDown(view.getByRole('button', { name: /^Speed/ }), { key: 'ArrowRight' });
    expect(view.getByLabelText('Time')).toHaveTextContent(`${mmss(3)} / ${mmss(SAMPLE_END)}`);
  });

  it('moves the free camera with keys on the World viewer in Explore, and Space pauses the world', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    await readyWorld();
    const viewer = view.getByRole('application', { name: 'World viewer' });
    expect(view.getByRole('button', { name: 'Explore' })).toHaveAttribute('aria-pressed', 'true');

    const before = [...lastView().position];
    fireEvent.keyDown(viewer, { key: 'ArrowRight' });
    frames(5);
    fireEvent.keyUp(viewer, { key: 'ArrowRight' });
    expect(lastView().position).not.toEqual(before);

    // The world plays until Space pauses it.
    expect(view.getByRole('button', { name: 'Pause' })).toBeTruthy();
    fireEvent.keyDown(viewer, { key: ' ' });
    expect(view.getByRole('button', { name: 'Play' })).toBeTruthy();
  });

  it('opens a world that declares no free roam in Shots, with Explore disabled and a note', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    await readyWorld(false);

    expect(view.getByRole('button', { name: 'Shots' })).toHaveAttribute('aria-pressed', 'true');
    expect(view.getByRole('button', { name: 'Explore' })).toBeDisabled();
    expect(view.getByText(/This world has no free view yet/)).toBeTruthy();
    expect(view.getByRole('group', { name: 'Shot travel' })).toBeTruthy();
  });

  it('opens the last world viewed in this project, else the first in the video', async () => {
    const lantern = { objectId: 'sha256:lantern', revision: 'sha256:lantern', name: 'Lantern Valley', duration: 6, authoredFps: 30, events: [], shots: [] };
    const spare = { ...lantern, objectId: 'sha256:spare', revision: 'sha256:spare', name: 'Spare' };
    const documents = fakeDocuments({ library: [spare, lantern] });
    const settings = makeSettings(new Map<string, unknown>([
      ['lastWorld:p1', `${lantern.objectId}@${lantern.revision}`],
    ]));
    const harness = makeHarness({ settings, documents });
    const view = mount(harness);
    // Lantern Valley is the second library world, but it was the last one viewed, so it opens.
    await waitFor(() => expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley'));
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('2 / 2');
  });

  it('the docked picker shows the name and n / m only; the qualifier stays in the list', async () => {
    const a = { objectId: 'sha256:a', revision: 'sha256:a', name: 'Lantern Valley', duration: 6, authoredFps: 30, events: [], shots: [] };
    const b = { ...a, objectId: 'sha256:b', revision: 'sha256:b' };
    const settings = makeSettings(new Map<string, unknown>([['lastWorld:p1', 'sha256:a@sha256:a']]));
    const view = mount(makeHarness({ settings, documents: fakeDocuments({ library: [a, b] }) }));
    await waitFor(() => expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley'));
    const trigger = view.getByRole('button', { name: 'Choose world' });
    expect(trigger.textContent).not.toContain('library');
    expect(trigger.textContent).toMatch(/1 \/ 2/);
    expect(trigger.querySelectorAll('.truncate')).toHaveLength(1);
    fireEvent.click(trigger);
    expect(view.getAllByText(/· library/).length).toBe(2);
  });

  it('pauses playback when the panel is hidden', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    await readyWorld();
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    fireEvent.click(view.getByRole('button', { name: 'Play' }));
    frames(2);
    expect(view.getByRole('button', { name: 'Pause' })).toBeTruthy();

    view.setVisible(false);
    expect(view.getByRole('button', { name: 'Play' })).toBeTruthy();
    expect(pending.size).toBe(0);
    const renders = scene.renders.length;
    frames(4);
    expect(scene.renders.length).toBe(renders);
  });

  it('adds the stretch at the playhead on a free track, then seeks there and reports success', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    await waitFor(() => expect(view.getByRole('button', { name: 'Add to video' })).toBeEnabled());
    harness.setPlayhead(4);

    fireEvent.click(view.getByRole('button', { name: 'Add to video' }));

    await waitFor(() => expect(harness.toast).toHaveBeenCalledWith('Added to V1 at 0:04', 'info'));
    expect(harness.timeline.apply).toHaveBeenCalledTimes(1);
    const patch = harness.timeline.apply.mock.calls[0][0] as { operations: unknown[] };
    expect(patch.operations).toEqual([
      { op: 'clip.add', target: 'world-1', payload: { track: 'V1', at: 4, clipType: LIVE_SCENE_CLIP_TYPE_ID }, order: 0 },
      {
        op: 'clip.update',
        target: 'world-1',
        // The stretch opens on the sample's first camera (Valley flight, 0-25), not the whole world.
        payload: { mode: 'merge', from: 0, to: SAMPLE_WORLD.shots[0].end, speed: 1, app: { liveScene: expect.any(Object) }, label: `${SAMPLE_WORLD.name} · ${SAMPLE_WORLD.shots[0].name}` },
        order: 1,
      },
    ]);
    expect(harness.seek).toHaveBeenCalledWith(4);
    expect(harness.seek).toHaveBeenCalledTimes(1);
  });

  it('reports a failed add with an error toast and an inline message, and does not seek', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    await waitFor(() => expect(view.getByRole('button', { name: 'Add to video' })).toBeEnabled());
    harness.timeline.apply.mockImplementationOnce(() => {
      throw new Error('Track locked');
    });

    fireEvent.click(view.getByRole('button', { name: 'Add to video' }));

    await waitFor(() => expect(harness.toast).toHaveBeenCalledWith('Add to video failed: Track locked', 'error'));
    expect(view.getByRole('status')).toHaveTextContent('Could not add to the video: Track locked');
    expect(harness.seek).not.toHaveBeenCalled();
    expect(harness.report).toHaveBeenCalledWith(expect.objectContaining({ severity: 'error', code: 'world-explorer/add-failed' }));
  });

  it('shows an import error in plain words, with the reason under a closed Details and a dismiss button', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    const bytes = new TextEncoder().encode('{"not":"a world"}');
    const file = { name: 'bad.scene.json', arrayBuffer: async () => bytes.buffer };

    fireEvent.change(view.getByLabelText('Import world file'), { target: { files: [file] } });

    await waitFor(() => expect(view.getByRole('status')).toHaveTextContent("This file isn't an Astrid world. Choose a .scene.json file."));
    const details = view.getByText('Details').closest('details');
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute('open');
    fireEvent.click(view.getByRole('button', { name: 'Dismiss' }));
    expect(view.queryByRole('status')).toBeNull();
  });

  it('shows a broken world as a calm overlay, stops its world clock, and Try again reopens it', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    await readyWorld();
    const before = view.getByRole('slider', { name: 'World time' }).getAttribute('aria-valuetext');
    frames(5);
    expect(view.getByRole('slider', { name: 'World time' }).getAttribute('aria-valuetext')).not.toBe(before);

    sceneStatus({ phase: 'error', error: 'Shader failed to compile' });
    expect(view.getByText("This world couldn't start")).toBeTruthy();
    expect(view.getByText('Shader failed to compile')).toBeTruthy();
    const frozen = view.getByRole('slider', { name: 'World time' }).getAttribute('aria-valuetext');
    frames(10);
    expect(view.getByRole('slider', { name: 'World time' }).getAttribute('aria-valuetext')).toBe(frozen);

    const renders = scene.renders.length;
    fireEvent.click(view.getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(scene.renders.length).toBeGreaterThan(renders));
    expect(view.queryByText("This world couldn't start")).toBeNull();
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
  });

  it('disables Add to video with the reason when the timeline has no project id', () => {
    const harness = makeHarness({ projectId: null, clips: [liveClip()] });
    const view = mount(harness);
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    const add = view.getByRole('button', { name: 'Add to video' });
    expect(add).toBeDisabled();
    expect(add).toHaveAttribute('title', 'Open a timeline to add worlds to a video.');
  });

  it('follows the single selected live-scene clip in Shots, and updates it with a single clip.update', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    harness.selectClip(['c1']);

    await waitFor(() => expect(view.getByText(/0:01–0:04/)).toBeTruthy());
    expect(view.getByRole('button', { name: 'Shots' })).toHaveAttribute('aria-pressed', 'true');
    expect(view.getByText(/Editing V1/)).toBeTruthy();
    expect(view.queryByRole('button', { name: 'Update clip' })).toBeNull(); // matches: a status, not a button


    await waitFor(() => expect(scene.renders.length).toBeGreaterThan(0));
    // The travel clock opens at the stretch start (1 s); two steps forward, then the end moves to 3.
    const travel = view.getByRole('group', { name: 'Shot travel' });
    fireEvent.keyDown(travel, { key: 'ArrowRight' });
    fireEvent.keyDown(travel, { key: 'ArrowRight' });
    fireEvent.keyDown(travel, { key: 'o' });
    expect(view.getByText(/0:01–0:03/)).toBeTruthy();
    expect(view.getByRole('button', { name: 'Update clip' })).toBeEnabled();

    fireEvent.click(view.getByRole('button', { name: 'Update clip' }));

    await waitFor(() => expect(harness.toast).toHaveBeenCalledWith('Clip updated', 'info'));
    expect(harness.timeline.apply).toHaveBeenCalledTimes(1);
    const patch = harness.timeline.apply.mock.calls[0][0] as { operations: unknown[] };
    expect(patch.operations).toEqual([
      { op: 'clip.update', target: 'c1', payload: { mode: 'merge', from: 1, to: 3, speed: 1 }, order: 0 },
    ]);
  });

  it('keeps each project library separate when two projects share one document store', async () => {
    const shared = fakeDocuments({ projectId: 'p1' });
    const first = makeHarness({ projectId: 'p1', documents: shared });
    const viewA = mount(first);
    await addSampleWorld(viewA);
    await waitFor(() => expect(storedWorlds(shared, 'p1')).toHaveLength(1));
    viewA.unmount();

    const second = makeHarness({ projectId: 'p2', documents: shared });
    const viewB = mount(second);
    expect(await viewB.findByText(/A world is a live 3D place/)).toBeTruthy();
    expect(viewB.queryByRole('button', { name: 'Choose world' })).toBeNull();
    expect(shared.docs.has(libraryDocumentId('p2'))).toBe(false);

    fireEvent.click(viewB.getByRole('button', { name: /^Add Lantern Valley/ }));
    await waitFor(() => expect(storedWorlds(shared, 'p2')).toHaveLength(1));
    expect(storedWorlds(shared, 'p1')).toHaveLength(1);
  });

  it('cancels the animation frame loop on unmount, so no scene renders follow', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    await readyWorld();
    // Explore runs its world clock and free camera on every frame while the panel is visible.
    frames(2);
    expect(pending.size).toBe(1);

    view.unmount();
    expect(pending.size).toBe(0);
    const renders = scene.renders.length;
    frames(4);
    expect(scene.renders.length).toBe(renders);
  });

  it('saves the events and shots a library scene reports, only when they differ, and shows them on the next visit', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    await waitFor(() => expect(storedWorlds(harness.documents)).toHaveLength(1));
    const update = vi.spyOn(harness.documents, 'update');

    act(() => scene.onReady!({ events: SAMPLE_WORLD.events, shots: SAMPLE_WORLD.shots, explore: EXPLORE }));
    expect(update).not.toHaveBeenCalled();

    act(() => scene.onReady!({ events: [{ name: 'Bridge', start: 1 }], shots: SAMPLE_WORLD.shots, explore: EXPLORE }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(storedWorlds(harness.documents)).toEqual([
      expect.objectContaining({ events: [{ name: 'Bridge', start: 1 }], shots: SAMPLE_WORLD.shots }),
    ]));
    expect(view.getByRole('button', { name: 'Bridge 0:01' })).toBeTruthy();
    expect(view.queryByRole('button', { name: `${SAMPLE_WORLD.events[0].name} 0:00` })).toBeNull();

    act(() => scene.onReady!({ events: [{ name: 'Bridge', start: 1 }], shots: SAMPLE_WORLD.shots, explore: EXPLORE }));
    expect(update).toHaveBeenCalledTimes(1);

    view.unmount();
    // The library's last copy stands in until the scene reports again, and the default world is the one last viewed.
    const again = mount(harness);
    expect(await again.findByRole('button', { name: 'Bridge 0:01' })).toBeTruthy();
    expect(again.queryByRole('button', { name: `${SAMPLE_WORLD.events[0].name} 0:00` })).toBeNull();
  });

  it('importing a world writes it into the project document, and a reload lists it by name', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    fireEvent.change(view.getByLabelText('Import world file'), { target: { files: [importedFile()] } });
    await waitFor(() => expect(storedWorlds(harness.documents)).toHaveLength(1));
    expect(storedWorlds(harness.documents)).toEqual([expect.objectContaining({ name: 'Harbour lights', duration: SAMPLE.duration })]);

    view.unmount();
    const again = mount(harness);
    fireEvent.click(await again.findByRole('button', { name: 'Choose world' }));
    expect(within(again.getByRole('group', { name: 'Worlds' })).getByRole('list', { name: 'Library' })).toHaveTextContent('Harbour lights');
  });

  it('a new mount on another timeline of the same project shows the same worlds', async () => {
    const first = makeHarness({ timelineId: 't1' });
    const viewA = mount(first);
    fireEvent.change(viewA.getByLabelText('Import world file'), { target: { files: [importedFile()] } });
    await waitFor(() => expect(storedWorlds(first.documents)).toHaveLength(1));
    viewA.unmount();

    const second = makeHarness({ timelineId: 't2', documents: first.documents });
    const viewB = mount(second);
    fireEvent.click(await viewB.findByRole('button', { name: 'Choose world' }));
    expect(within(viewB.getByRole('group', { name: 'Worlds' })).getByRole('list', { name: 'Library' })).toHaveTextContent('Harbour lights');
    expect(storedWorlds(second.documents)).toEqual(storedWorlds(first.documents));
  });

  it('a timeline world missing from the project document is registered into it', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    mount(harness);
    await waitFor(() => expect(storedWorlds(harness.documents)).toHaveLength(1));
    expect(storedWorlds(harness.documents)).toEqual([
      expect.objectContaining({ objectId: SAMPLE.liveScene.source.objectId, revision: SAMPLE.liveScene.source.revision }),
    ]);
  });

  it('a timeline world is named from its clip label, and the name is registered into the library', async () => {
    const harness = makeHarness({ clips: [{ ...liveClip(), label: 'Harbour Dusk · Pier walk' }] });
    mount(harness);
    await waitFor(() => expect(storedWorlds(harness.documents)).toEqual([expect.objectContaining({ name: 'Harbour Dusk' })]));
  });

  it('opening the panel twice on unchanged worlds writes nothing the second time', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const first = mount(harness);
    await waitFor(() => expect(storedWorlds(harness.documents)).toHaveLength(1));
    await waitFor(() => expect(scene.renders.length).toBeGreaterThan(0));
    act(() => scene.onReady!({ events: SAMPLE_WORLD.events, shots: SAMPLE_WORLD.shots, explore: EXPLORE }));
    await waitFor(() => expect(storedWorlds(harness.documents)).toEqual([expect.objectContaining({ events: SAMPLE_WORLD.events })]));
    first.unmount();
    const create = vi.spyOn(harness.documents, 'create');
    const update = vi.spyOn(harness.documents, 'update');
    const second = mount(harness);
    await waitFor(() => expect(scene.renders.length).toBeGreaterThan(0));
    act(() => scene.onReady!({ events: SAMPLE_WORLD.events, shots: SAMPLE_WORLD.shots, explore: EXPLORE }));
    await new Promise((resolve) => setTimeout(resolve, 700));
    second.unmount();
    expect(create).not.toHaveBeenCalled();
    expect(update).not.toHaveBeenCalled();
  });

  /** Two library worlds: the sample (real package) and a spare entry that has no package behind it. */
  async function seededLibrary() {
    const storage = makeStorage();
    const parsed = await parsePreparedScenePackage(new TextEncoder().encode(SAMPLE_WORLD.packageJson));
    const ingested = await ingestPreparedPackage(storage, parsed);
    const lantern = {
      objectId: ingested.package.object_id,
      revision: ingested.package.digest,
      name: 'Lantern Valley',
      duration: parsed.manifest.duration,
      authoredFps: parsed.manifest.authoredFps,
      events: [{ name: 'Dawn', start: 0 }],
      shots: [],
    };
    const spare = { ...lantern, objectId: 'sha256:spare', revision: 'sha256:spare', name: 'Spare' };
    const documents = fakeDocuments({ library: [lantern, spare] });
    return { storage, documents, lantern, spare };
  }

  it('removes a library world without changing the selected one', async () => {
    const { storage, documents, lantern } = await seededLibrary();
    const harness = makeHarness({ documents, storage });
    const view = mount(harness);
    // The first library world opens by itself.
    expect(await view.findByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');

    fireEvent.click(view.getByRole('button', { name: 'Choose world' }));
    fireEvent.click(view.getByRole('button', { name: 'Remove Spare from library' }));

    await waitFor(() => expect(storedWorlds(documents)).toEqual([lantern]));
    expect(view.queryByRole('button', { name: 'Remove Spare from library' })).toBeNull();
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Lantern Valley');
    expect(view.queryByRole('button', { name: /^Spare/ })).toBeNull();
  });

  it('selects the next library world when the selected one is removed, then shows the empty state', async () => {
    const { storage, documents, lantern, spare } = await seededLibrary();
    const harness = makeHarness({ documents, storage });
    const view = mount(harness);

    fireEvent.click(await view.findByRole('button', { name: 'Choose world' }));
    fireEvent.click(view.getByRole('button', { name: 'Remove Lantern Valley from library' }));

    await waitFor(() => expect(storedWorlds(documents)).toEqual([spare]));
    expect(view.getByRole('button', { name: 'Choose world' })).toHaveTextContent('Spare');

    fireEvent.click(view.getByRole('button', { name: 'Choose world' }));
    fireEvent.click(view.getByRole('button', { name: 'Remove Spare from library' }));

    await waitFor(() => expect(storedWorlds(documents)).toEqual([]));
    expect(view.getByText(/A world is a live 3D place/)).toBeTruthy();
    expect(view.queryByRole('button', { name: 'Choose world' })).toBeNull();
    expect(storage.objects.has(lantern.objectId)).toBe(true);
  });

  it('disables Update clip once the clip matches the stretch, and never shows the word Stretch', async () => {
    const harness = makeHarness({ clips: [liveClip()] });
    const view = mount(harness);
    harness.selectClip(['c1']);
    await waitFor(() => expect(view.getByText(/Editing V1/)).toBeTruthy());
    await waitFor(() => expect(scene.renders.length).toBeGreaterThan(0));

    const travel = view.getByRole('group', { name: 'Shot travel' });
    fireEvent.keyDown(travel, { key: 'ArrowRight' });
    fireEvent.keyDown(travel, { key: 'ArrowRight' });
    fireEvent.keyDown(travel, { key: 'o' });
    expect(view.getByRole('button', { name: 'Update clip' })).toBeEnabled();
    fireEvent.click(view.getByRole('button', { name: 'Update clip' }));
    await waitFor(() => expect(harness.toast).toHaveBeenCalledWith('Clip updated', 'info'));

    // The harness timeline does not write back, so the snapshot takes the clip as the timeline would store it: 1 s to 3 s.
    harness.setClips([{ ...liveClip(), sourceOffset: 1, sourceEnd: 3 }]);
    expect(view.queryByRole('button', { name: 'Update clip' })).toBeNull();
    expect(view.getByText(/Editing V1/)).toBeTruthy();
    expect(view.getByText(/0:01–0:03/)).toBeTruthy();
    expect(view.container.textContent).not.toMatch(/stretch/i);
  });

  it('selects the new clip after Add to video, with the host video select', async () => {
    const harness = makeHarness();
    const view = mount(harness);
    await addSampleWorld(view);
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    await waitFor(() => expect(view.getByRole('button', { name: 'Add to video' })).toBeEnabled());
    harness.setPlayhead(4);

    fireEvent.click(view.getByRole('button', { name: 'Add to video' }));

    await waitFor(() => expect(harness.toast).toHaveBeenCalledWith('Added to V1 at 0:04', 'info'));
    const patch = harness.timeline.apply.mock.calls[0][0] as { operations: Array<{ op: string; target: string }> };
    const newClipId = patch.operations.find((operation) => operation.op === 'clip.add')!.target;
    expect(harness.video.select).toHaveBeenCalledTimes(1);
    expect(harness.video.select).toHaveBeenCalledWith([newClipId]);
  });

  it('adds and seeks without throwing when the host video has no select', async () => {
    const harness = makeHarness();
    delete (harness.video as { select?: unknown }).select;
    const view = mount(harness);
    await addSampleWorld(view);
    fireEvent.click(view.getByRole('button', { name: 'Shots' }));
    await waitFor(() => expect(view.getByRole('button', { name: 'Add to video' })).toBeEnabled());
    harness.setPlayhead(4);

    fireEvent.click(view.getByRole('button', { name: 'Add to video' }));

    await waitFor(() => expect(harness.toast).toHaveBeenCalledWith('Added to V1 at 0:04', 'info'));
    expect(harness.seek).toHaveBeenCalledWith(4);
  });

  describe('Shots gallery', () => {
    it('docked: shows one gallery of cards, each and the viewer with a drag handle carrying the clip spec', async () => {
      const harness = makeHarness();
      const view = mount(harness);
      await addSampleWorld(view);
      await readyWorld(true, SAMPLE_WORLD.shots);
      fireEvent.click(view.getByRole('button', { name: 'Shots' }));
      await waitFor(() => expect(view.getAllByTestId('shot-drag-handle')).toHaveLength(SAMPLE_WORLD.shots.length));
      expect(view.getAllByRole('searchbox')).toHaveLength(1);
      expect(view.queryByRole('combobox', { name: /Shot|Camera/ })).toBeNull();
      expect(view.queryByRole('button', { name: /^Add .* to video$/ })).toBeNull();

      const second = SAMPLE_WORLD.shots[1];
      expect(harness.video.dragSource).toHaveBeenCalledWith(expect.objectContaining({
        clipType: LIVE_SCENE_CLIP_TYPE_ID, from: second.start, to: second.end, speed: 1,
        label: `${SAMPLE_WORLD.name} · ${second.name}`, app: { liveScene: expect.any(Object) },
      }));
      await waitFor(() => expect(view.getByTestId('viewer-drag-handle')).toBeTruthy());
    });

    it('clicking a card shows that shot and plays it', async () => {
      const harness = makeHarness();
      const view = mount(harness);
      await addSampleWorld(view);
      await readyWorld(true, SAMPLE_WORLD.shots);
      fireEvent.click(view.getByRole('button', { name: 'Shots' }));
      const second = SAMPLE_WORLD.shots[1];
      const card = await view.findByRole('button', { name: new RegExp(`^${second.name},`) });
      fireEvent.click(card);
      expect(card).toHaveAttribute('aria-current', 'true');
      const slider = view.getByRole('slider', { name: 'Travel position' });
      expect(slider).toHaveAttribute('aria-valuemin', String(second.start));
      expect(slider).toHaveAttribute('aria-valuemax', String(second.end));
    });

    it('clicking card B after card A moves the viewer to B\'s start, and stops following a clip', async () => {
      const harness = makeHarness({ clips: [liveClip()] });
      const view = mount(harness);
      harness.selectClip(['c1']);
      await waitFor(() => expect(view.getByText(/Editing V1/)).toBeTruthy());
      await readyWorld(true, SAMPLE_WORLD.shots);
      ackLastFrame();
      const [first, second] = SAMPLE_WORLD.shots;
      fireEvent.click(await view.findByRole('button', { name: new RegExp(`^${first.name},`) }));
      ackLastFrame();
      expect(scene.renders.at(-1)!.sourceTime).toBe(first.start);
      expect(view.queryByText(/Editing V1|Update clip on/)).toBeNull();

      fireEvent.click(view.getByRole('button', { name: new RegExp(`^${second.name},`) }));
      ackLastFrame();
      expect(scene.renders.at(-1)!.sourceTime).toBeGreaterThanOrEqual(second.start);
      expect(scene.renders.at(-1)!.sourceTime).toBeLessThan(second.end);
      expect(view.getByRole('button', { name: new RegExp(`^${second.name},`) })).toHaveAttribute('aria-current', 'true');
      expect(view.getByRole('button', { name: new RegExp(`^${first.name},`) })).not.toHaveAttribute('aria-current', 'true');
    });

    it('full screen: cards and the viewer say Add to video instead of offering a handle, and a card adds its shot', async () => {
      const harness = makeHarness();
      const view = mount(harness, true, { active: true, enter: vi.fn(), exit: vi.fn() });
      await addSampleWorld(view);
      await readyWorld(true, SAMPLE_WORLD.shots);
      fireEvent.click(view.getByRole('button', { name: 'Shots' }));
      await waitFor(() => expect(view.getAllByRole('button', { name: /^Add .* to video$/ }).length).toBeGreaterThan(SAMPLE_WORLD.shots.length - 1));
      expect(view.queryByTestId('shot-drag-handle')).toBeNull();
      expect(view.queryByTestId('viewer-drag-handle')).toBeNull();
      expect(view.getByRole('button', { name: 'Add to video' })).toBeTruthy();

      const second = SAMPLE_WORLD.shots[1];
      await waitFor(() => expect(view.getByRole('button', { name: `Add ${second.name} to video` })).toBeEnabled());
      harness.setPlayhead(4);
      fireEvent.click(view.getByRole('button', { name: `Add ${second.name} to video` }));
      await waitFor(() => expect(harness.timeline.apply).toHaveBeenCalledTimes(1));
      const patch = harness.timeline.apply.mock.calls[0][0] as { operations: Array<{ payload: Record<string, unknown> }> };
      expect(patch.operations[1].payload).toMatchObject({ from: second.start, to: second.end, speed: 1, label: `${SAMPLE_WORLD.name} · ${second.name}` });
    });
  });
});
