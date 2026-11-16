import type {
  ExtensionContext,
  ProjectObjectMetadata,
  ProjectObjectStorage,
  TimelinePatch,
  TimelinePatchOperation,
  TimelineReader,
  TimelineSnapshot,
  LiveSceneRequest,
  LiveSceneExecution,
  LiveSceneResult,
  LiveSceneCapture,
} from '@astrid/editor-sdk';
import type { SceneManifest } from './runtime';
import { LIVE_SCENE_CLIP_TYPE_ID, THREEJS_LIVE_EDITOR_EXTENSION_ID } from './identity';

const SHA256_DIGEST = /^sha256:[0-9a-f]{64}$/;
const ENTRY_NAME = /^[\w./-]+\.html$/;
const PACKAGE_FILENAME = 'scene.package.json';
const PACKAGE_MEDIA_TYPE = 'application/json';

export { THREEJS_LIVE_EDITOR_EXTENSION_ID, LIVE_SCENE_CLIP_TYPE_ID };

export type LiveSceneObjectInput = {
  readonly bytes: Uint8Array;
  readonly mediaType: string;
  readonly filename?: string;
};

export type LiveScenePackageInput = {
  readonly manifest: SceneManifest;
  readonly entry: LiveSceneObjectInput;
  readonly assets: readonly LiveSceneObjectInput[];
};

export type LiveScenePublicationCapture = {
  readonly projectId: string;
  readonly timelineId: string;
  readonly capturedTimelineVersion: number;
  readonly packageRevision: string;
  readonly entryRevision: string;
};

export type PublishLiveSceneEditInput = {
  readonly placementIds: readonly string[];
  readonly package: LiveScenePackageInput;
  readonly capture: LiveScenePublicationCapture;
};

export type PreparedScenePlacement = {
  readonly id: string;
  readonly track: string;
  readonly at: number;
  readonly from: number;
  readonly to: number;
  readonly rate: number;
  /** The clip's visible name; left unset when absent. */
  readonly label?: string;
};

/** Compatibility type for the existing Maple acceptance caller. */
export type PreparedMaplePlacement = PreparedScenePlacement;

export const MAPLE_ACCEPTANCE_PLACEMENTS: readonly PreparedScenePlacement[] = Object.freeze([
  Object.freeze({ id: 'maple-acceptance-position-2', track: 'V1', at: 2, from: 55, to: 75, rate: 1 }),
  Object.freeze({ id: 'maple-acceptance-position-22', track: 'V1', at: 22, from: 20, to: 30, rate: 2 }),
]);
export const DEFAULT_PREPARED_SCENE_TRACK = 'V1';
export const DEFAULT_PREPARED_SCENE_PLACEMENT_ID = 'live-scene-import';

/**
 * The ordinary one-clip insertion used by the public prepared-scene command.
 * The optional IDs are a read-only public-reader projection used to keep
 * repeated command invocations collision-free without changing patch APIs.
 */
export function defaultPreparedScenePlacement(
  manifest: SceneManifest,
  existingClipIds: readonly string[] = [],
): PreparedScenePlacement {
  const duration = validateManifest(manifest).duration;
  const occupied = new Set(existingClipIds);
  let id = DEFAULT_PREPARED_SCENE_PLACEMENT_ID;
  let suffix = 2;
  while (occupied.has(id)) id = `${DEFAULT_PREPARED_SCENE_PLACEMENT_ID}-${suffix++}`;
  return Object.freeze({
    id,
    track: DEFAULT_PREPARED_SCENE_TRACK,
    at: 0,
    from: 0,
    to: duration,
    rate: 1,
  });
}
export const MAPLE_CAMERA_EDIT_SOURCE_TIME = 55;
export const MAPLE_CAMERA_EDIT_FOV = 49;

export type LiveScenePublicationResult = {
  readonly revision: string;
  readonly package: ProjectObjectMetadata;
  readonly entry: ProjectObjectMetadata;
  readonly assets: readonly ProjectObjectMetadata[];
  readonly affectedPlacements: readonly string[];
  readonly flushReceipt: { readonly version: number };
  readonly acknowledgedTimelineVersion: number;
  readonly capture: LiveScenePublicationCapture;
};

type AuthoringContext = Pick<ExtensionContext, 'creative'>;
type PreparedSceneImportContext = Pick<ExtensionContext, 'creative' | 'services' | 'chrome'>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isByteArray(value: unknown): value is Uint8Array {
  // ArrayBuffer.isView keeps this boundary valid when a browser/DOM realm
  // supplies the Uint8Array (where instanceof Uint8Array is false).
  return ArrayBuffer.isView(value) && Object.prototype.toString.call(value) === '[object Uint8Array]';
}

function copyBytes(value: Uint8Array): Uint8Array {
  return new Uint8Array(value);
}

function bytesEqual(left: Uint8Array, right: Uint8Array): boolean {
  return left.byteLength === right.byteLength && left.every((value, index) => value === right[index]);
}

async function digestBytes(value: Uint8Array): Promise<string> {
  const hash = await globalThis.crypto.subtle.digest('SHA-256', value as unknown as BufferSource);
  return `sha256:${Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')}`;
}

function fail(message: string): never {
  throw new Error(message);
}

function assertNotAborted(signal: AbortSignal): void {
  if (!signal.aborted) return;
  const error = new Error('Prepared scene selection cancelled');
  error.name = 'AbortError';
  throw error;
}

/** Run the post-picker command work from the lazy pack-owned authoring module. */
export async function importPreparedSceneFile(
  ctx: PreparedSceneImportContext,
  bytes: Uint8Array,
  signal: AbortSignal,
): Promise<void> {
  try {
    assertNotAborted(signal);
    const parsed = await parsePreparedScenePackage(bytes);
    assertNotAborted(signal);
    const existingClipIds = ctx.creative.reader.snapshot().clips.map(({ id }) => id);
    const placement = defaultPreparedScenePlacement(parsed.manifest, existingClipIds);
    // Keep this fence immediately before the durable publication operation.
    assertNotAborted(signal);
    const result = await importPreparedScene(ctx, parsed, [placement]);
    ctx.services.diagnostics.report({
      severity: 'info',
      code: 'threejs-live-editor/authoring-published',
      message: `Imported ${result.revision} to ${result.affectedPlacements.join(', ')}; timeline version ${result.acknowledgedTimelineVersion}.`,
      detail: result,
    });
    ctx.chrome.toast(
      `Prepared scene imported (${result.affectedPlacements.length} placements, timeline ${result.acknowledgedTimelineVersion}).`,
      'info',
    );
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      ctx.chrome.toast('Prepared scene import cancelled.', 'info');
      return;
    }
    const message = error instanceof Error ? error.message : String(error);
    ctx.services.diagnostics.report({
      severity: 'error',
      code: 'threejs-live-editor/authoring-failed',
      message: `Import prepared scene failed: ${message}`,
      detail: { stage: 'import-prepared-scene', error: message },
    });
    ctx.chrome.toast(`Prepared scene import failed: ${message}`, 'error');
    throw error;
  }
}

function validateManifest(value: unknown): SceneManifest {
  if (!isRecord(value)
    || value.formatVersion !== 1
    || typeof value.entry !== 'string'
    || !ENTRY_NAME.test(value.entry)
    || value.entry.split('/').includes('..')
    || typeof value.duration !== 'number'
    || !Number.isFinite(value.duration)
    || value.duration <= 0
    || typeof value.authoredFps !== 'number'
    || !Number.isFinite(value.authoredFps)
    || value.authoredFps <= 0) {
    return fail('Invalid prepared scene manifest or timing');
  }
  const keys = Object.keys(value).sort();
  if (keys.join(',') !== 'authoredFps,duration,entry,formatVersion') {
    return fail('Prepared scene manifest has unsupported fields');
  }
  return Object.freeze({
    formatVersion: 1,
    entry: value.entry,
    duration: value.duration,
    authoredFps: value.authoredFps,
  });
}

function metadata(value: unknown, label: string): Omit<ProjectObjectMetadata, 'object_id'> {
  if (!isRecord(value)
    || typeof value.digest !== 'string'
    || !SHA256_DIGEST.test(value.digest)
    || typeof value.media_type !== 'string'
    || typeof value.size !== 'number'
    || !Number.isSafeInteger(value.size)
    || value.size < 0
    || typeof value.filename !== 'string'
    || !value.filename) {
    return fail(`Invalid ${label} prepared metadata`);
  }
  const keys = Object.keys(value).sort();
  if (keys.join(',') !== 'digest,filename,media_type,size') {
    return fail(`Prepared ${label} metadata has unsupported fields`);
  }
  if (Object.prototype.hasOwnProperty.call(value, 'object_id')) {
    return fail(`Prepared ${label} must not contain persisted object identity`);
  }
  return Object.freeze({
    digest: value.digest,
    media_type: value.media_type,
    size: value.size,
    filename: value.filename,
  });
}

function persistedMetadata(value: unknown, label: string): ProjectObjectMetadata {
  if (!isRecord(value)
    || typeof value.object_id !== 'string'
    || !value.object_id
    || typeof value.digest !== 'string'
    || !SHA256_DIGEST.test(value.digest)
    || typeof value.media_type !== 'string'
    || typeof value.size !== 'number'
    || !Number.isSafeInteger(value.size)
    || value.size < 0
    || typeof value.filename !== 'string'
    || !value.filename) {
    return fail(`Invalid ${label} persisted metadata`);
  }
  return Object.freeze({
    object_id: value.object_id,
    digest: value.digest,
    media_type: value.media_type,
    size: value.size,
    filename: value.filename,
  });
}

/** Parse the supported self-contained prepared HTML package shape. */
export async function parsePreparedScenePackage(bytes: Uint8Array): Promise<LiveScenePackageInput> {
  let text: string;
  try {
    text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    return fail('Prepared scene package is not valid UTF-8');
  }

  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return fail('Prepared scene package is not valid JSON');
  }
  if (!isRecord(value)) return fail('Prepared scene package must be an object');
  const allowed = new Set(['manifest', 'entry', 'assets', 'html']);
  const unexpected = Object.keys(value).filter((key) => !allowed.has(key));
  if (unexpected.length > 0) return fail(`Prepared scene package has unsupported fields: ${unexpected.join(', ')}`);
  if (Array.isArray(value.assets) === false || value.assets.length !== 0) {
    return fail('Prepared scene package must contain assets: []');
  }
  const manifest = validateManifest(value.manifest);
  if (typeof value.html !== 'string' || !value.html) return fail('Prepared scene package HTML entry is empty');
  const entryMetadata = metadata(value.entry, 'entry');
  if (entryMetadata.media_type !== 'text/html' || entryMetadata.filename !== manifest.entry) {
    return fail('Prepared scene entry metadata does not match the manifest');
  }
  const entryBytes = new TextEncoder().encode(value.html);
  const entryDigest = await digestBytes(entryBytes);
  if (entryMetadata.size !== entryBytes.byteLength) return fail('Prepared scene entry size mismatch');
  if (entryMetadata.digest !== entryDigest) return fail('Prepared scene entry digest mismatch');
  return Object.freeze({
    manifest,
    entry: Object.freeze({ bytes: entryBytes, mediaType: 'text/html', filename: manifest.entry }),
    assets: Object.freeze([]),
  });
}

// Compatibility aliases retained for existing Maple acceptance callers.
export const parsePreparedMaplePackage = parsePreparedScenePackage;
export const parsePreparedMaple = parsePreparedScenePackage;

function validatePackageInput(input: LiveScenePackageInput): void {
  validateManifest(input.manifest);
  if (!input.entry || !isByteArray(input.entry.bytes)
    || input.entry.mediaType !== 'text/html' || !input.entry.filename) {
    fail('Live-scene package requires an HTML entry');
  }
  if (input.entry.filename !== input.manifest.entry) fail('Live-scene entry filename does not match manifest');
  if (!Array.isArray(input.assets)) fail('Live-scene package assets must be an array');
  for (const asset of input.assets) {
    if (!asset || !isByteArray(asset.bytes) || !asset.mediaType || !asset.filename) {
      fail('Live-scene asset is incomplete');
    }
  }
}

function validateCapture(capture: LiveScenePublicationCapture): void {
  if (!capture || !capture.projectId || !capture.timelineId
    || !Number.isSafeInteger(capture.capturedTimelineVersion)
    || capture.capturedTimelineVersion <= 0
    || !SHA256_DIGEST.test(capture.packageRevision)
    || !SHA256_DIGEST.test(capture.entryRevision)) {
    fail('Live-scene publication capture is incomplete or invalid');
  }
}

function assertCaptureMatchesSnapshot(
  capture: LiveScenePublicationCapture,
  snapshot: TimelineSnapshot,
): void {
  if (snapshot.projectId !== capture.projectId || snapshot.timelineId !== capture.timelineId) {
    fail('Live-scene publication scope changed');
  }
  if (snapshot.baseVersion !== capture.capturedTimelineVersion) {
    fail('Live-scene publication captured timeline version is stale');
  }
}

function currentApp(clip: TimelineSnapshot['clips'][number]): Record<string, unknown> {
  return clip.app ? { ...clip.app } : {};
}

async function ingestVerified(
  storage: ProjectObjectStorage,
  input: Uint8Array,
  mediaType: string,
  filename: string,
): Promise<ProjectObjectMetadata> {
  const expected = await digestBytes(input);
  const result = await storage.ingest(copyBytes(input), mediaType, filename);
  if (result.digest !== expected || result.size !== input.byteLength || result.media_type !== mediaType) {
    fail(`Project object ingestion metadata mismatch for ${filename}`);
  }
  const readback = await storage.read(result.object_id);
  if (!bytesEqual(readback, input)) fail(`Project object readback mismatch for ${filename}`);
  return result;
}

async function readVerified(
  storage: ProjectObjectStorage,
  object: ProjectObjectMetadata,
): Promise<Uint8Array> {
  const value = await storage.read(object.object_id);
  if (await digestBytes(value) !== object.digest || value.byteLength !== object.size) {
    fail(`Existing project object ${object.object_id} failed integrity verification`);
  }
  return value;
}

async function ingestPackage(
  storage: ProjectObjectStorage,
  input: LiveScenePackageInput,
): Promise<{
  readonly entry: ProjectObjectMetadata;
  readonly assets: readonly ProjectObjectMetadata[];
  readonly package: ProjectObjectMetadata;
  readonly liveScene: Record<string, unknown>;
}> {
  validatePackageInput(input);
  const entry = await ingestVerified(storage, input.entry.bytes, input.entry.mediaType, input.entry.filename!);
  const assets: ProjectObjectMetadata[] = [];
  for (const asset of input.assets) {
    assets.push(await ingestVerified(storage, asset.bytes, asset.mediaType, asset.filename!));
  }
  const body = JSON.stringify({
    manifest: input.manifest,
    entry,
    assets,
  });
  const packageObject = await ingestVerified(storage, new TextEncoder().encode(body), PACKAGE_MEDIA_TYPE, PACKAGE_FILENAME);
  return {
    entry,
    assets: Object.freeze(assets),
    package: packageObject,
    liveScene: {
      revision: packageObject.digest,
      source: { objectId: packageObject.object_id, revision: packageObject.digest },
      packageBody: body,
      html: new TextDecoder().decode(input.entry.bytes),
    },
  };
}

/** Ingest a prepared package and return the scene envelope without placing it. */
export async function ingestPreparedPackage(
  storage: ProjectObjectStorage,
  input: LiveScenePackageInput,
): Promise<{ package: ProjectObjectMetadata; liveScene: Record<string, unknown> }> {
  const ingested = await ingestPackage(storage, input);
  return { package: ingested.package, liveScene: ingested.liveScene };
}

/** Load and verify an already-ingested canonical package and its HTML entry. */
export async function loadLiveSceneSource(
  storage: ProjectObjectStorage,
  objectId: string,
  revision: string,
): Promise<{ liveScene: Record<string, unknown>; manifest: SceneManifest }> {
  const bytes = await storage.read(objectId);
  if (await digestBytes(bytes) !== revision) fail('Stored world failed integrity verification');
  let packageBody: string;
  try {
    packageBody = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    return fail('Stored world package is not valid UTF-8');
  }
  let body: unknown;
  try { body = JSON.parse(packageBody); }
  catch { return fail('Stored world package is not valid JSON'); }
  if (!isRecord(body)) fail('Stored world package must be an object');
  const manifest = validateManifest(body.manifest);
  const entry = persistedMetadata(body.entry, 'entry');
  if (entry.media_type !== 'text/html' || entry.filename !== manifest.entry) {
    fail('Stored world entry does not match the manifest');
  }
  const html = new TextDecoder('utf-8', { fatal: true }).decode(await readVerified(storage, entry));
  return {
    liveScene: {
      revision,
      source: { objectId, revision },
      packageBody,
      html,
    },
    manifest,
  };
}

type CapturedSceneSource = {
  readonly objectId: string;
  readonly packageBody: string;
  readonly html: string;
};

/** Check the public reader and inline cache agree on the OLD source identity.
 * Persisted bytes are verified separately before any candidate is ingested. */
function capturedPlacement(
  snapshot: TimelineSnapshot,
  id: string,
  capture: LiveScenePublicationCapture,
): { clip: TimelineSnapshot['clips'][number]; source: CapturedSceneSource } {
  const clip = snapshot.clips.find((candidate) => candidate.id === id);
  if (!clip) fail(`Live-scene placement ${id} does not exist`);
  if (clip.clipType !== LIVE_SCENE_CLIP_TYPE_ID) fail(`Live-scene placement ${id} has the wrong clip type`);
  const track = snapshot.tracks.find((candidate) => candidate.id === clip.track);
  if (!track || track.kind !== 'visual') fail(`Live-scene placement ${id} is not on a visual track`);
  const sources = clip.sourceRefs?.filter((candidate) => candidate.extensionId === THREEJS_LIVE_EDITOR_EXTENSION_ID) ?? [];
  const source = sources[0];
  const liveScene = clip.app?.liveScene;
  if (sources.length !== 1 || !source || source.clipId !== id || !source.sourceObjectId
    || !isRecord(liveScene) || !isRecord(liveScene.source)
    || typeof liveScene.packageBody !== 'string' || typeof liveScene.html !== 'string') {
    fail(`Clip ${id} has no immutable scene package source`);
  }
  if (source.sourceRevision !== capture.packageRevision || source.packageRevision !== capture.packageRevision
    || liveScene.revision !== capture.packageRevision || liveScene.source.revision !== capture.packageRevision
    || liveScene.source.objectId !== source.sourceObjectId) {
    fail(`Live-scene placement ${id} does not match the captured old package identity`);
  }
  return { clip, source: { objectId: source.sourceObjectId, packageBody: liveScene.packageBody, html: liveScene.html } };
}

async function validateExistingSource(
  storage: ProjectObjectStorage,
  source: CapturedSceneSource,
  capture: LiveScenePublicationCapture,
): Promise<void> {
  const packageBytes = await storage.read(source.objectId);
  if (await digestBytes(packageBytes) !== capture.packageRevision
    || !bytesEqual(packageBytes, new TextEncoder().encode(source.packageBody))) {
    fail('Existing live-scene canonical package does not match the captured old package identity');
  }
  let body: unknown;
  try {
    body = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(packageBytes));
  } catch {
    fail('Existing live-scene canonical package is not valid UTF-8 JSON');
  }
  if (!isRecord(body) || !Array.isArray(body.assets)) fail('Existing live-scene canonical package is incomplete');
  const manifest = validateManifest(body.manifest);
  const entry = persistedMetadata(body.entry, 'entry');
  if (entry.media_type !== 'text/html' || entry.filename !== manifest.entry
    || entry.digest !== capture.entryRevision) {
    fail('Existing live-scene canonical entry does not match the captured old entry identity');
  }
  const entryBytes = await readVerified(storage, entry);
  if (!bytesEqual(entryBytes, new TextEncoder().encode(source.html))) {
    fail('Existing live-scene HTML cache does not match the canonical entry');
  }
}

type MapleJourneyScript = {
  readonly payloadStart: number;
  readonly payloadEnd: number;
  readonly payload: string;
};

type MapleKnotSpan = {
  readonly start: number;
  readonly end: number;
  readonly value: Record<string, unknown>;
};

function mapleJourneyScript(html: string): MapleJourneyScript {
  const match = html.match(/(<script[^>]+id=["']mapleJourneyData["'][^>]*>)([\s\S]*?)(<\/script>)/i);
  if (!match || match.index === undefined) fail('Existing Maple entry has no journey data');
  const payloadStart = match.index + match[1].length;
  return { payloadStart, payloadEnd: payloadStart + match[2].length, payload: match[2] };
}

function mapleKnotSpans(payload: string): readonly MapleKnotSpan[] {
  const marker = /"knots"\s*:\s*\[/g;
  const match = marker.exec(payload);
  if (!match) fail('Existing Maple journey data has no camera knots');
  const arrayStart = match.index + match[0].lastIndexOf('[');
  const spans: MapleKnotSpan[] = [];
  let objectStart = -1;
  let objectDepth = 0;
  let inString = false;
  let escaped = false;
  for (let index = arrayStart + 1; index < payload.length; index += 1) {
    const character = payload[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === '"') inString = false;
      continue;
    }
    if (character === '"') {
      inString = true;
      continue;
    }
    if (character === '{') {
      if (objectDepth === 0) objectStart = index;
      objectDepth += 1;
      continue;
    }
    if (character === '}' && objectDepth > 0) {
      objectDepth -= 1;
      if (objectDepth === 0 && objectStart >= 0) {
        const end = index + 1;
        let value: unknown;
        try { value = JSON.parse(payload.slice(objectStart, end)); }
        catch { fail('Existing Maple camera knot is not valid JSON'); }
        if (!isRecord(value)) fail('Existing Maple camera knot must be an object');
        spans.push({ start: objectStart, end, value });
        objectStart = -1;
      }
      continue;
    }
    if (character === ']' && objectDepth === 0) break;
  }
  return spans;
}

function prepareMapleCameraEdit(input: LiveScenePackageInput): LiveScenePackageInput {
  validatePackageInput(input);
  const html = new TextDecoder('utf-8', { fatal: true }).decode(input.entry.bytes);
  const script = mapleJourneyScript(html);
  let journey: unknown;
  try { journey = JSON.parse(script.payload); }
  catch { fail('Existing Maple journey data is not valid JSON'); }
  if (!isRecord(journey) || !isRecord(journey.camera) || !Array.isArray(journey.camera.knots)) {
    fail('Existing Maple journey data has no camera knots');
  }
  const knots = mapleKnotSpans(script.payload).filter((knot) => knot.value.t === MAPLE_CAMERA_EDIT_SOURCE_TIME);
  if (knots.length !== 1) fail(`Existing Maple journey data must have exactly one camera knot at source ${MAPLE_CAMERA_EDIT_SOURCE_TIME}`);
  const knot = knots[0];
  const knotText = script.payload.slice(knot.start, knot.end);
  const fovMatches = [...knotText.matchAll(/("fov"\s*:\s*)(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)/g)];
  if (fovMatches.length !== 1) fail(`Existing Maple camera knot at source ${MAPLE_CAMERA_EDIT_SOURCE_TIME} must have exactly one fov value`);
  const fov = Number(fovMatches[0][2]);
  if (!Number.isFinite(fov)) fail('Existing Maple camera FOV is not finite');
  if (fov === MAPLE_CAMERA_EDIT_FOV) fail(`Existing Maple camera knot at source ${MAPLE_CAMERA_EDIT_SOURCE_TIME} already has the acceptance FOV`);
  const fovStart = knot.start + fovMatches[0].index! + fovMatches[0][1].length;
  const fovEnd = fovStart + fovMatches[0][2].length;
  const editedPayload = `${script.payload.slice(0, fovStart)}${MAPLE_CAMERA_EDIT_FOV}${script.payload.slice(fovEnd)}`;
  const editedHtml = `${html.slice(0, script.payloadStart)}${editedPayload}${html.slice(script.payloadEnd)}`;
  return {
    manifest: input.manifest,
    entry: { bytes: new TextEncoder().encode(editedHtml), mediaType: 'text/html', filename: input.manifest.entry },
    assets: input.assets,
  };
}

function ensurePublicContext(ctx: AuthoringContext): { storage: ProjectObjectStorage; timeline: ExtensionContext['creative']['timeline']; reader: TimelineReader } {
  const storage = ctx.creative.projectObjects;
  if (!storage) fail('project object capability unavailable');
  return { storage, timeline: ctx.creative.timeline, reader: ctx.creative.reader };
}

function validatePlacements(
  snapshot: TimelineSnapshot,
  placements: readonly PreparedScenePlacement[],
  duration: number,
): void {
  if (!Array.isArray(placements) || placements.length === 0) {
    fail('Prepared scene import requires at least one placement');
  }
  const ids = new Set<string>();
  for (const placement of placements) {
    if (!placement || typeof placement !== 'object') fail('Invalid prepared scene placement');
    if (ids.has(placement.id)) fail(`Prepared scene placement ID collision: ${placement.id}`);
    ids.add(placement.id);
    if (!placement.id || !placement.track || !Number.isFinite(placement.at) || placement.at < 0
      || !Number.isFinite(placement.from) || !Number.isFinite(placement.to)
      || placement.from < 0 || placement.to <= placement.from || placement.to > duration
      || !Number.isFinite(placement.rate) || placement.rate <= 0) {
      fail(`Invalid prepared scene placement ${placement.id}`);
    }
    if (snapshot.clips.some((clip) => clip.id === placement.id)) {
      fail(`Prepared scene placement ID collision: ${placement.id}`);
    }
    const track = snapshot.tracks.find((candidate) => candidate.id === placement.track);
    if (track && track.kind !== 'visual') fail(`Prepared scene placement track ${placement.track} is not visual`);
  }
}

function liveScenePatch(
  version: number,
  operations: readonly TimelinePatchOperation[],
  source = THREEJS_LIVE_EDITOR_EXTENSION_ID,
): TimelinePatch {
  return { version, operations, source };
}

export function placementOperations(
  snapshot: TimelineSnapshot,
  placements: readonly PreparedScenePlacement[],
  liveScene: Record<string, unknown>,
): TimelinePatchOperation[] {
  const operations: TimelinePatchOperation[] = [];
  let order = 0;
  const tracks = new Set(snapshot.tracks.map((track) => track.id));
  for (const placement of placements) {
    if (!tracks.has(placement.track)) {
      operations.push({ op: 'track.add', target: placement.track, payload: { kind: 'visual', label: placement.track }, order: order++ });
      tracks.add(placement.track);
    }
    operations.push({
      op: 'clip.add',
      target: placement.id,
      payload: { track: placement.track, at: placement.at, clipType: LIVE_SCENE_CLIP_TYPE_ID },
      order: order++,
    });
    operations.push({
      op: 'clip.update',
      target: placement.id,
      payload: {
        mode: 'merge', from: placement.from, to: placement.to, speed: placement.rate, app: { liveScene },
        ...(placement.label ? { label: placement.label } : {}),
      },
      order: order++,
    });
  }
  return operations;
}

export async function importPreparedScene(
  ctx: AuthoringContext,
  prepared: LiveScenePackageInput,
  placements: readonly PreparedScenePlacement[],
): Promise<LiveScenePublicationResult> {
  const { storage, timeline, reader } = ensurePublicContext(ctx);
  const initial = reader.snapshot();
  if (!initial.projectId || !initial.timelineId) fail('Live-scene timeline scope unavailable');
  const manifest = validateManifest(prepared.manifest);
  if (!Array.isArray(prepared.assets) || prepared.assets.length !== 0) {
    fail('Prepared scene import must contain assets: []');
  }
  validatePlacements(initial, placements, manifest.duration);
  const ingested = await ingestPackage(storage, prepared);
  const capture: LiveScenePublicationCapture = {
    projectId: initial.projectId,
    timelineId: initial.timelineId,
    capturedTimelineVersion: initial.baseVersion,
    packageRevision: ingested.package.digest,
    entryRevision: ingested.entry.digest,
  };
  const current = reader.snapshot();
  assertCaptureMatchesSnapshot(capture, current);
  validatePlacements(current, placements, manifest.duration);
  const operations = placementOperations(current, placements, ingested.liveScene);
  const patch = liveScenePatch(capture.capturedTimelineVersion, operations);
  const validation = timeline.validate(patch);
  if (!validation.valid) fail(`Prepared scene timeline patch rejected: ${validation.diagnostics.map((item) => item.message).join('; ')}`);
  const diff = timeline.apply(patch);
  const affectedPlacements = placements.map((placement) => placement.id);
  if (!affectedPlacements.every((id) => diff.affectedObjectIds.includes(id))) {
    fail('Prepared scene timeline patch did not affect every placement');
  }
  let flushReceipt: { readonly version: number };
  try {
    flushReceipt = await timeline.flush();
  } catch (error) {
    throw new Error(`durable timeline persistence unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
  return {
    revision: ingested.package.digest,
    package: ingested.package,
    entry: ingested.entry,
    assets: ingested.assets,
    affectedPlacements,
    flushReceipt,
    acknowledgedTimelineVersion: flushReceipt.version,
    capture,
  };
}

/**
 * Compatibility wrapper for the Maple acceptance fixture. The reusable import
 * operation above has no Maple-specific placement count or source ranges.
 */
export async function importPreparedMapleScene(
  ctx: AuthoringContext,
  prepared: LiveScenePackageInput,
  placements: readonly PreparedMaplePlacement[],
): Promise<LiveScenePublicationResult> {
  if (placements.length !== MAPLE_ACCEPTANCE_PLACEMENTS.length) {
    fail('Prepared Maple acceptance requires exactly two placements');
  }
  return importPreparedScene(ctx, prepared, placements);
}

/** Place an already-ingested live scene as new clips on the timeline. */
export async function placeLiveScene(
  ctx: AuthoringContext,
  liveScene: Record<string, unknown>,
  duration: number,
  placements: readonly PreparedScenePlacement[],
): Promise<{ affectedPlacements: string[]; acknowledgedTimelineVersion: number }> {
  const { timeline, reader } = ensurePublicContext(ctx);
  const snapshot = reader.snapshot();
  if (!snapshot.projectId || !snapshot.timelineId) fail('Live-scene timeline scope unavailable');
  validatePlacements(snapshot, placements, duration);
  const operations = placementOperations(snapshot, placements, liveScene);
  const patch = liveScenePatch(snapshot.baseVersion, operations);
  const validation = timeline.validate(patch);
  if (!validation.valid) fail(`Prepared scene timeline patch rejected: ${validation.diagnostics.map((item) => item.message).join('; ')}`);
  const diff = timeline.apply(patch);
  const affectedPlacements = placements.map((placement) => placement.id);
  if (!affectedPlacements.every((id) => diff.affectedObjectIds.includes(id))) {
    fail('Prepared scene timeline patch did not affect every placement');
  }
  let flushReceipt: { readonly version: number };
  try {
    flushReceipt = await timeline.flush();
  } catch (error) {
    throw new Error(`durable timeline persistence unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
  return { affectedPlacements, acknowledgedTimelineVersion: flushReceipt.version };
}

/** Change the from/to source range and speed of one existing live-scene clip. */
export async function updateLiveSceneClip(
  ctx: AuthoringContext,
  clipId: string,
  stretch: { from: number; to: number; speed: number },
): Promise<{ acknowledgedTimelineVersion: number }> {
  const { timeline, reader } = ensurePublicContext(ctx);
  const snapshot = reader.snapshot();
  if (!snapshot.projectId || !snapshot.timelineId) fail('Live-scene timeline scope unavailable');
  const clip = snapshot.clips.find((candidate) => candidate.id === clipId);
  if (!clip) fail(`Live-scene clip ${clipId} does not exist`);
  if (clip.clipType !== LIVE_SCENE_CLIP_TYPE_ID) fail(`Clip ${clipId} is not a live-scene clip`);
  const { from, to, speed } = stretch;
  if (!Number.isFinite(from) || !Number.isFinite(to) || !Number.isFinite(speed)
    || from < 0 || to <= from || speed <= 0) {
    fail(`Invalid live-scene stretch for ${clipId}`);
  }
  const patch = liveScenePatch(snapshot.baseVersion, [{
    op: 'clip.update',
    target: clipId,
    payload: { mode: 'merge', from, to, speed },
    order: 0,
  }]);
  const validation = timeline.validate(patch);
  if (!validation.valid) fail(`Live-scene clip update rejected: ${validation.diagnostics.map((item) => item.message).join('; ')}`);
  timeline.apply(patch);
  let flushReceipt: { readonly version: number };
  try {
    flushReceipt = await timeline.flush();
  } catch (error) {
    throw new Error(`durable timeline persistence unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
  return { acknowledgedTimelineVersion: flushReceipt.version };
}

export async function publishLiveSceneEdit(
  ctx: AuthoringContext,
  input: PublishLiveSceneEditInput,
  assertActive: () => void = () => {},
): Promise<LiveScenePublicationResult> {
  assertActive();
  const { storage, timeline, reader } = ensurePublicContext(ctx);
  validateCapture(input.capture);
  validatePackageInput(input.package);
  if (!Array.isArray(input.placementIds) || input.placementIds.length === 0
    || input.placementIds.some((id) => typeof id !== 'string' || !id.trim())
    || new Set(input.placementIds).size !== input.placementIds.length) {
    fail('Live-scene publication requires nonempty, duplicate-free placement IDs');
  }
  // Retain the caller's captured values across asynchronous object operations.
  const capture = { ...input.capture };
  const placementIds = [...input.placementIds];
  const candidate: LiveScenePackageInput = {
    manifest: validateManifest(input.package.manifest),
    entry: { ...input.package.entry, bytes: copyBytes(input.package.entry.bytes) },
    assets: input.package.assets.map((asset) => ({ ...asset, bytes: copyBytes(asset.bytes) })),
  };
  const initial = reader.snapshot();
  assertCaptureMatchesSnapshot(capture, initial);
  const captured = placementIds.map((id) => capturedPlacement(initial, id, capture));
  for (const placement of captured) await validateExistingSource(storage, placement.source, capture);
  assertActive();
  const ingested = await ingestPackage(storage, candidate);
  assertActive();
  const current = reader.snapshot();
  assertCaptureMatchesSnapshot(capture, current);
  // No await between this fresh source check and the atomic versioned apply.
  const clips = placementIds.map((id, index) => {
    const placement = capturedPlacement(current, id, capture);
    const oldSource = captured[index].source;
    if (placement.source.objectId !== oldSource.objectId || placement.source.packageBody !== oldSource.packageBody
      || placement.source.html !== oldSource.html) {
      fail(`Live-scene placement ${id} changed its captured old source during publication`);
    }
    return placement.clip;
  });
  const operations: TimelinePatchOperation[] = clips.map((clip, index) => ({
    op: 'clip.update',
    target: clip.id,
    payload: { mode: 'merge', app: { ...currentApp(clip), liveScene: ingested.liveScene } },
    order: index,
  }));
  const patch = liveScenePatch(capture.capturedTimelineVersion, operations);
  const validation = timeline.validate(patch);
  if (!validation.valid) fail(`Live-scene timeline patch rejected: ${validation.diagnostics.map((item) => item.message).join('; ')}`);
  assertActive();
  const diff = timeline.apply(patch);
  if (!placementIds.every((id) => diff.affectedObjectIds.includes(id))) fail('Live-scene patch did not affect every placement');
  let flushReceipt: { readonly version: number };
  try {
    flushReceipt = await timeline.flush();
  } catch (error) {
    throw new Error(`durable timeline persistence unavailable: ${error instanceof Error ? error.message : String(error)}`);
  }
  return {
    revision: ingested.package.digest,
    package: ingested.package,
    entry: ingested.entry,
    assets: ingested.assets,
    affectedPlacements: placementIds,
    flushReceipt,
    acknowledgedTimelineVersion: flushReceipt.version,
    capture,
  };
}

/** Ordinary scene source authoring, kept in the rendering pack. ACP sees excerpts only. */
export async function executeLiveSceneAuthoring(
  ctx: AuthoringContext,
  request: LiveSceneRequest,
  execution: LiveSceneExecution,
): Promise<LiveSceneResult> {
  execution.assertActive();
  const { storage, reader } = ensurePublicContext(ctx);
  const snapshot = reader.snapshot();
  const first = snapshot.clips.find((clip) => clip.id === request.placementIds[0]);
  const liveScene = first?.app?.liveScene;
  if (!isRecord(liveScene) || !isRecord(liveScene.source) || typeof liveScene.packageBody !== 'string') fail('Existing scene package unavailable');
  if (liveScene.packageBody.length > 65_536) fail('Scene package metadata exceeds authoring limit');
  const body: unknown = JSON.parse(liveScene.packageBody);
  if (!isRecord(body) || !Array.isArray(body.assets) || body.assets.length > 16) fail('Invalid scene package assets');
  const manifest = validateManifest(body.manifest);
  const entry = persistedMetadata(body.entry, 'entry');
  if (entry.size > 8_388_608) fail('Scene source exceeds authoring limit');
  const capture: LiveSceneCapture = {
    projectId: request.scope.projectId,
    timelineId: request.scope.timelineId,
    capturedTimelineVersion: request.scope.capturedTimelineVersion,
    packageRevision: String(liveScene.revision),
    entryRevision: entry.digest,
    packageObjectId: String(liveScene.source.objectId),
    entryObjectId: entry.object_id,
  };
  validateCapture(capture);
  assertCaptureMatchesSnapshot(capture, snapshot);
  const placements = request.placementIds.map((id) => capturedPlacement(snapshot, id, capture));
  for (const placement of placements) {
    if (placement.source.objectId !== capture.packageObjectId) fail('Placements must share the same canonical package');
    await validateExistingSource(storage, placement.source, capture);
  }
  execution.assertActive();
  const source = new TextDecoder('utf-8', { fatal: true }).decode(await readVerified(storage, entry));
  execution.assertActive();
  if (request.action === 'read') {
    let offset = request.offset;
    if (request.find !== undefined) {
      const found = source.indexOf(request.find);
      if (found < 0 || source.indexOf(request.find, found + 1) >= 0) fail('Source search must match exactly once');
      offset = Math.max(0, found - request.offset);
    }
    if (offset >= source.length) fail('Source excerpt offset is outside entry');
    return {
      kind: 'read', capture, placementIds: [...request.placementIds],
      timing: placements.map(({ clip }) => ({ id: clip.id, at: clip.at, duration: clip.duration,
        sourceOffset: clip.sourceOffset, sourceEnd: clip.sourceEnd, rate: clip.rate })),
      source: { offset, text: source.slice(offset, offset + request.length), totalLength: source.length },
    };
  }
  if (Object.entries(capture).some(([key, expected]) => (request.capture as unknown as Record<string, unknown>)[key] !== expected)) fail('Source capture identity changed');
  const spans = request.replacements.map(({ before, after }) => {
    const start = source.indexOf(before);
    if (start < 0 || source.indexOf(before, start + 1) >= 0) fail('Exact source replacement is missing or ambiguous');
    return { start, end: start + before.length, after };
  }).sort((left, right) => left.start - right.start);
  if (spans.some((span, index) => index > 0 && spans[index - 1].end > span.start)) fail('Exact source replacements overlap');
  let edited = source;
  for (const span of [...spans].reverse()) edited = edited.slice(0, span.start) + span.after + edited.slice(span.end);
  if (new TextEncoder().encode(edited).byteLength > 8_388_608) fail('Edited source exceeds authoring limit');
  const assets: LiveSceneObjectInput[] = [];
  let assetBytes = 0;
  for (const value of body.assets) {
    const asset = persistedMetadata(value, 'asset');
    assetBytes += asset.size;
    if (assetBytes > 16_777_216) fail('Scene assets exceed authoring limit');
    assets.push({ bytes: await readVerified(storage, asset), mediaType: asset.media_type, filename: asset.filename });
    execution.assertActive();
  }
  const published = await publishLiveSceneEdit(ctx, {
    capture, placementIds: request.placementIds,
    package: { manifest, entry: { bytes: new TextEncoder().encode(edited), mediaType: 'text/html', filename: manifest.entry }, assets },
  }, execution.assertActive);
  return {
    kind: 'published', projectId: capture.projectId, timelineId: capture.timelineId,
    revision: published.revision, entryRevision: published.entry.digest,
    affectedPlacements: [...published.affectedPlacements],
    acknowledgedTimelineVersion: published.acknowledgedTimelineVersion, flushReceipt: published.flushReceipt,
  };
}

/** Prepare the bounded Maple camera edit from the canonical entry object. */
export async function prepareBoundedLiveSceneEdit(
  ctx: AuthoringContext,
  placementId: string,
): Promise<LiveScenePackageInput> {
  const { storage, reader } = ensurePublicContext(ctx);
  const clip = reader.snapshot().clips.find((candidate) => candidate.id === placementId);
  if (!clip?.app?.liveScene || !isRecord(clip.app.liveScene)) fail(`Live-scene placement ${placementId} does not exist`);
  const liveScene = clip.app.liveScene;
  if (typeof liveScene.packageBody !== 'string' || !isRecord(liveScene.source)) fail('Existing live-scene package is unavailable');
  const body = JSON.parse(liveScene.packageBody) as Record<string, unknown>;
  const manifest = validateManifest(body.manifest);
  const entry = persistedMetadata(body.entry, 'entry');
  if (entry.media_type !== 'text/html' || entry.filename !== manifest.entry) {
    fail('Existing live-scene canonical entry does not match the manifest');
  }
  const entryObjectId = entry.object_id;
  if (typeof entryObjectId !== 'string' || typeof liveScene.source.revision !== 'string') fail('Existing scene source is incomplete');
  const oldEntryBytes = await readVerified(storage, { object_id: entryObjectId, digest: entry.digest, media_type: entry.media_type, size: entry.size, filename: entry.filename });
  const assets: LiveSceneObjectInput[] = [];
  for (const asset of Array.isArray(body.assets) ? body.assets : []) {
    const assetMeta = persistedMetadata(asset, 'asset');
    assets.push({ bytes: await readVerified(storage, assetMeta), mediaType: assetMeta.media_type, filename: assetMeta.filename });
  }
  return prepareMapleCameraEdit({
    manifest,
    entry: { bytes: oldEntryBytes, mediaType: 'text/html', filename: manifest.entry },
    assets,
  });
}

/** Capture canonical old identities before preparing and publishing the acceptance edit. */
async function captureMaplePublication(
  ctx: AuthoringContext,
  placementIds: readonly string[],
): Promise<LiveScenePublicationCapture> {
  const { storage, reader } = ensurePublicContext(ctx);
  const snapshot = reader.snapshot();
  if (!snapshot.projectId || !snapshot.timelineId) fail('Live-scene timeline scope unavailable');
  if (placementIds.length !== 2 || new Set(placementIds).size !== placementIds.length) {
    fail('Maple camera acceptance requires exactly two placements');
  }
  const first = snapshot.clips.find((clip) => clip.id === placementIds[0]);
  if (!first || !isRecord(first.app?.liveScene) || !isRecord(first.app.liveScene.source)) {
    fail(`Live-scene placement ${placementIds[0]} has no immutable scene package source`);
  }
  const liveScene = first.app!.liveScene as Record<string, unknown>;
  const source = liveScene.source as Record<string, unknown>;
  if (typeof liveScene.revision !== 'string' || typeof source.objectId !== 'string' || typeof source.revision !== 'string') {
    fail('Existing live-scene package identity is incomplete');
  }
  const packageRevision = liveScene.revision;
  if (!SHA256_DIGEST.test(packageRevision) || source.revision !== packageRevision) {
    fail('Existing live-scene package identity is invalid');
  }
  const tentativeCapture: LiveScenePublicationCapture = {
    projectId: snapshot.projectId,
    timelineId: snapshot.timelineId,
    capturedTimelineVersion: snapshot.baseVersion,
    packageRevision,
    entryRevision: `sha256:${'0'.repeat(64)}`,
  };
  const firstCaptured = capturedPlacement(snapshot, placementIds[0], tentativeCapture);
  const packageBytes = await storage.read(firstCaptured.source.objectId);
  const packageBody = new TextDecoder('utf-8', { fatal: true }).decode(packageBytes);
  if (await digestBytes(packageBytes) !== packageRevision || packageBody !== firstCaptured.source.packageBody) {
    fail('Existing live-scene canonical package does not match the public reader');
  }
  let body: unknown;
  try { body = JSON.parse(packageBody); }
  catch { fail('Existing live-scene canonical package is not valid UTF-8 JSON'); }
  if (!isRecord(body) || !Array.isArray(body.assets)) fail('Existing live-scene canonical package is incomplete');
  const manifest = validateManifest(body.manifest);
  const entry = persistedMetadata(body.entry, 'entry');
  if (entry.media_type !== 'text/html' || entry.filename !== manifest.entry) {
    fail('Existing live-scene canonical entry does not match the manifest');
  }
  const entryBytes = await readVerified(storage, entry);
  if (!bytesEqual(entryBytes, new TextEncoder().encode(firstCaptured.source.html))) {
    fail('Existing live-scene HTML cache does not match the canonical entry');
  }
  const capture: LiveScenePublicationCapture = {
    ...tentativeCapture,
    entryRevision: entry.digest,
  };
  for (const placementId of placementIds) capturedPlacement(snapshot, placementId, capture);
  return capture;
}

/** Apply the one pack-owned Maple camera edit to exactly the two imported placements. */
export async function publishBoundedMapleEdit(
  ctx: AuthoringContext,
  placementIds: readonly string[] = MAPLE_ACCEPTANCE_PLACEMENTS.map(({ id }) => id),
): Promise<LiveScenePublicationResult> {
  if (placementIds.length !== 2) fail('Maple camera acceptance requires exactly two placements');
  const capture = await captureMaplePublication(ctx, placementIds);
  const candidate = await prepareBoundedLiveSceneEdit(ctx, placementIds[0]);
  return publishLiveSceneEdit(ctx, { placementIds: [...placementIds], package: candidate, capture });
}
