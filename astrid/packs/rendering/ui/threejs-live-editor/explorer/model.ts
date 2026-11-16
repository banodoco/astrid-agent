// Pure world-explorer model: worlds, uses, library, stretches, keys, travel clock.
import type { TimelineSnapshot } from '@astrid/editor-sdk';
import { LIVE_SCENE_CLIP_TYPE_ID } from '../identity';

/** Something that happens in the world, in world seconds; `end` is absent for a point event. */
export type WorldEvent = { readonly name: string; readonly start: number; readonly end?: number };
/** An authored shot: `start`/`end` in source seconds, `worldStart` the world time shown at `start` (default start). */
export type ShotMove = { readonly name: string; readonly start: number; readonly end: number; readonly worldStart?: number };
export type Stretch = { readonly from: number; readonly to: number; readonly speed: number };
export type WorldUse = { clipId: string; track: string; at: number; from: number; to: number; speed: number };
export type LibraryEntry = { objectId: string; revision: string; name: string; duration: number; authoredFps: number; events: WorldEvent[]; shots: ShotMove[]; sample?: true };
/** Names a timeline world can be given, best first: the clip's label, a name its package declares, a name from its entry file. */
export type NameHints = { label?: string; declared?: string; file?: string };
export type TimelineWorld = { objectId: string; revision: string; duration: number; authoredFps: number; uses: WorldUse[]; liveScene: Record<string, unknown>; nameHints: NameHints };

export const worldKey = (w: { objectId: string; revision: string }) => `${w.objectId}@${w.revision}`;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v);
const isPositive = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v) && v > 0;

function manifestNameHints(manifest: Record<string, unknown>): NameHints {
  const hints: NameHints = {};
  const declared = [manifest.name, manifest.title].find((v): v is string => typeof v === 'string' && v.trim().length > 0);
  if (declared) hints.declared = declared.trim();
  if (typeof manifest.entry === 'string') {
    const base = manifest.entry.split('/').pop()?.replace(/\.html?$/i, '') ?? '';
    if (base && !/^(index|scene|world|main|entry)$/i.test(base)) hints.file = worldTitleFromFilename(base);
  }
  return hints;
}

/** Worlds placed on the timeline, grouped by objectId@revision, ordered by first use. */
export function worldsFromSnapshot(snapshot: TimelineSnapshot): TimelineWorld[] {
  const byKey = new Map<string, TimelineWorld>();
  const uses: { world: TimelineWorld; use: WorldUse }[] = [];
  for (const clip of snapshot.clips) {
    if (clip.clipType !== LIVE_SCENE_CLIP_TYPE_ID) continue;
    const ls = clip.app?.liveScene;
    if (!isRecord(ls) || !isRecord(ls.source)) continue;
    const { objectId, revision } = ls.source;
    if (typeof objectId !== 'string' || typeof revision !== 'string' || typeof ls.packageBody !== 'string') continue;
    const key = `${objectId}@${revision}`;
    let world = byKey.get(key);
    if (!world) {
      let body: unknown;
      try { body = JSON.parse(ls.packageBody); } catch { continue; }
      const manifest = isRecord(body) ? body.manifest : undefined;
      if (!isRecord(manifest) || !isPositive(manifest.duration) || !isPositive(manifest.authoredFps)) continue;
      world = { objectId, revision, duration: manifest.duration, authoredFps: manifest.authoredFps, uses: [], liveScene: ls, nameHints: manifestNameHints(manifest) };
      byKey.set(key, world);
    }
    // The clip label reads "World" or "World · Shot"; the world is the part before the dot.
    const clipLabel = (clip as { label?: unknown }).label;
    const worldLabel = typeof clipLabel === 'string' ? clipLabel.split(' · ')[0].trim() : '';
    if (worldLabel && !PLACEHOLDER_LABEL.test(worldLabel) && !world.nameHints.label) world.nameHints.label = worldLabel;
    const rate = clip.rate ?? 1;
    const from = clip.sourceOffset ?? 0;
    const to = clip.sourceEnd ?? from + clip.duration * rate;
    uses.push({ world, use: { clipId: clip.id, track: clip.track, at: clip.at, from, to, speed: rate } });
  }
  uses.sort((a, b) => a.use.at - b.use.at || (a.use.clipId < b.use.clipId ? -1 : a.use.clipId > b.use.clipId ? 1 : 0));
  const ordered: TimelineWorld[] = [];
  for (const { world, use } of uses) {
    if (world.uses.length === 0) ordered.push(world);
    world.uses.push(use);
  }
  return ordered;
}

function validEvent(v: unknown): v is WorldEvent {
  return isRecord(v) && typeof v.name === 'string' && typeof v.start === 'number' && Number.isFinite(v.start)
    && (v.end === undefined || (typeof v.end === 'number' && Number.isFinite(v.end)));
}

function validShot(v: unknown): v is ShotMove {
  return isRecord(v) && typeof v.name === 'string'
    && typeof v.start === 'number' && Number.isFinite(v.start)
    && typeof v.end === 'number' && Number.isFinite(v.end) && v.end > v.start
    && (v.worldStart === undefined || (typeof v.worldStart === 'number' && Number.isFinite(v.worldStart)));
}

function validEntry(v: unknown): boolean {
  return isRecord(v)
    && typeof v.objectId === 'string'
    && typeof v.revision === 'string'
    && typeof v.name === 'string'
    && isPositive(v.duration)
    && isPositive(v.authoredFps);
}

/** Library entries from a stored document body; invalid entries are dropped, and so are invalid events and shots
 * (an entry saved with the old `stops` simply loses them). Never throws. */
export function parseLibrary(content: unknown): LibraryEntry[] {
  const raw = isRecord(content) ? content.worlds : undefined;
  if (!Array.isArray(raw)) return [];
  return raw.filter(validEntry).map((entry): LibraryEntry => {
    const e = entry as Record<string, unknown>;
    return {
      objectId: e.objectId as string,
      revision: e.revision as string,
      name: e.name as string,
      duration: e.duration as number,
      authoredFps: e.authoredFps as number,
      events: Array.isArray(e.events) ? e.events.filter(validEvent) : [],
      shots: Array.isArray(e.shots) ? e.shots.filter(validShot) : [],
      ...(e.sample === true ? { sample: true as const } : {}),
    };
  });
}

/** The stored document body for a library. */
export const libraryContent = (list: readonly LibraryEntry[]) => ({ version: 1, worlds: list });

/** Upserts by worldKey: replaces in place, or appends. */
export function upsertEntry(list: readonly LibraryEntry[], entry: LibraryEntry): LibraryEntry[] {
  const key = worldKey(entry);
  const i = list.findIndex((e) => worldKey(e) === key);
  return i >= 0 ? list.map((e, j) => (j === i ? entry : e)) : [...list, entry];
}

export const removeEntry = (list: readonly LibraryEntry[], key: string): LibraryEntry[] => list.filter((e) => worldKey(e) !== key);

/** Renames one entry; a blank name is ignored. */
export function renameEntry(list: readonly LibraryEntry[], key: string, name: string): LibraryEntry[] {
  const trimmed = name.trim();
  if (!trimmed) return [...list];
  return list.map((e) => (worldKey(e) === key ? { ...e, name: trimmed } : e));
}

const SCENE_SUFFIX = /(\.scene\.json|\.scene|\.json)$/i;

/** A world's name from its file name: scene and JSON extensions dropped, separators read as spaces, first letter capitalised. */
export function worldTitleFromFilename(filename: string): string {
  let stem = filename;
  while (SCENE_SUFFIX.test(stem)) stem = stem.replace(SCENE_SUFFIX, '');
  const words = stem.replace(/[-_.]+/g, ' ').replace(/\s+/g, ' ').trim();
  if (!words) return 'Imported world';
  return words[0].toUpperCase() + words.slice(1).toLowerCase();
}

/** The name of a timeline clip showing a world: the world, then the shot when one is shown. */
export function clipName(world: string, shot?: string): string {
  return shot ? `${world} · ${shot}` : world;
}

/** JSON with object keys sorted, so equal content compares equal whatever order its keys were written in. */
export function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (typeof value === 'object' && value !== null) {
    return `{${Object.entries(value).filter(([, v]) => v !== undefined).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([k, v]) => `${JSON.stringify(k)}:${stableJson(v)}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

const sameJson = (a: unknown, b: unknown) => stableJson(a) === stableJson(b);

export const UNTITLED_WORLD = /^Untitled world( \d+)?$/;

/** Labels the app generates ("world-1", "Untitled world", "Scene 2"): never a name for a world. */
export const PLACEHOLDER_LABEL = /^(world[-_ ]?\d+|untitled world( \d+)?|scene[-_ ]?\d+)$/i;

/** Keeps the events and shots a library world's scene reported; the same list when the entry is missing or already has them. */
export function withInfo(
  list: readonly LibraryEntry[],
  key: string,
  info: { events: readonly WorldEvent[]; shots: readonly ShotMove[] },
): LibraryEntry[] {
  const entry = list.find((e) => worldKey(e) === key);
  if (!entry || (sameJson(entry.events, info.events) && sameJson(entry.shots, info.shots))) return [...list];
  return upsertEntry(list, { ...entry, events: [...info.events], shots: [...info.shots] });
}

export const stretchDuration = (s: Stretch) => (s.to - s.from) / s.speed;

export const MIN_STRETCH = 0.1;

/** Keeps from/to inside [0, duration] with at least MIN_STRETCH between them; bad speed becomes 1. */
export function clampStretch(s: Stretch, duration: number): Stretch {
  const speed = Number.isFinite(s.speed) && s.speed > 0 ? s.speed : 1;
  const from = Math.min(Math.max(s.from, 0), Math.max(0, duration - MIN_STRETCH));
  const to = Math.min(duration, Math.max(s.to, from + MIN_STRETCH));
  return { from, to, speed };
}

/** Moving the start never passes the end: it stops MIN_STRETCH short of it. */
export function setStretchStart(s: Stretch, t: number, duration: number): Stretch {
  return clampStretch({ ...s, from: Math.min(t, s.to - MIN_STRETCH) }, duration);
}

export function setStretchEnd(s: Stretch, t: number, duration: number): Stretch {
  return clampStretch({ ...s, to: Math.max(t, s.from + MIN_STRETCH) }, duration);
}

/** The stretch equals a clip's range to within half an output frame (a clip's length is stored in whole frames, so its source range drifts by up to that, scaled by speed). */
export function stretchMatches(a: Stretch, b: Stretch, fps: number): boolean {
  const frame = Number.isFinite(fps) && fps > 0 ? fps : 30;
  const tolerance = (0.5 / frame) * Math.max(a.speed, b.speed, 1);
  return Math.abs(a.from - b.from) <= tolerance
    && Math.abs(a.to - b.to) <= tolerance
    && Math.abs(a.speed - b.speed) <= 1e-3 * Math.max(a.speed, b.speed);
}

export type BarRange = { readonly from: number; readonly to: number };

/** Moves one handle of an in/out range to time `t`: it stays inside [start, end] and MIN_STRETCH away from the other handle. */
export function moveRangeHandle(range: BarRange, which: 'from' | 'to', t: number, bounds: { start: number; end: number }): BarRange {
  // Handles snap to 0.1 s, so what the bar shows and the length the pill adds always agree.
  const time = Number.isFinite(t) ? Math.round(t * 10) / 10 : which === 'from' ? range.from : range.to;
  if (which === 'from') return { from: Math.min(Math.max(time, bounds.start), Math.max(bounds.start, range.to - MIN_STRETCH)), to: range.to };
  return { from: range.from, to: Math.max(Math.min(time, bounds.end), Math.min(bounds.end, range.from + MIN_STRETCH)) };
}

/** The time a slider key moves to, or undefined for other keys. ←/→ step (Shift ×5), Home/End the bounds. */
export function sliderKeyTime(e: { key: string; shiftKey: boolean }, ctx: { time: number; start: number; end: number; step: number }): number | undefined {
  const clamp = (t: number) => Math.min(Math.max(t, ctx.start), ctx.end);
  switch (e.key) {
    case 'ArrowLeft': case 'ArrowDown': return clamp(ctx.time - ctx.step * (e.shiftKey ? 5 : 1));
    case 'ArrowRight': case 'ArrowUp': return clamp(ctx.time + ctx.step * (e.shiftKey ? 5 : 1));
    case 'Home': return ctx.start;
    case 'End': return ctx.end;
    default: return undefined;
  }
}

export type KeyAction =
  | { type: 'toggle' }
  | { type: 'seek'; time: number }
  | { type: 'setStart' }
  | { type: 'setEnd' };

export function keyAction(e: { key: string; shiftKey: boolean }, ctx: { time: number; duration: number }): KeyAction | undefined {
  switch (e.key) {
    case ' ':
    case 'Space':
      return { type: 'toggle' };
    case 'ArrowLeft':
    case 'ArrowRight': {
      const step = (e.shiftKey ? 5 : 1) * (e.key === 'ArrowLeft' ? -1 : 1);
      return { type: 'seek', time: Math.min(Math.max(ctx.time + step, 0), ctx.duration) };
    }
    case 'i':
    case 'I':
      return { type: 'setStart' };
    case 'o':
    case 'O':
      return { type: 'setEnd' };
    default:
      return undefined;
  }
}

/** First output visual track with no clip or shot overlapping [at, at+duration); else a new V-id. */
export function freeVisualTrack(snapshot: TimelineSnapshot, at: number, duration: number): { track: string; isNew: boolean } {
  const end = at + duration;
  const busy = (track: string) =>
    snapshot.clips.some((c) => c.track === track && c.at < end && c.at + c.duration > at)
    || (snapshot.shots ?? []).some((s) => s.track === track && s.at < end && s.at + s.duration > at);
  for (const t of snapshot.tracks) {
    if (t.kind === 'visual' && t.role !== 'source' && !busy(t.id)) return { track: t.id, isNew: false };
  }
  const ids = new Set(snapshot.tracks.map((t) => t.id));
  let n = 1;
  while (ids.has(`V${n}`)) n++;
  return { track: `V${n}`, isNew: true };
}

export function newClipId(snapshot: TimelineSnapshot, base = 'world'): string {
  const ids = new Set(snapshot.clips.map((c) => c.id));
  let n = 1;
  while (ids.has(`${base}-${n}`)) n++;
  return `${base}-${n}`;
}

const MAX_TICK_GAP_MS = 250;

/** Travel position for one world. next()/ack() give at most one render in flight. */
export class TravelClock {
  private _duration: number;
  private _start = 0;
  private _end: number;
  private _loop: boolean;
  private _speed = 1;
  private _time = 0;
  private _playing = false;
  private _lastTickMs: number | undefined;
  private _inFlight: number | undefined;
  private _shown: number | undefined;

  constructor(opts: { duration: number; loop?: boolean }) {
    this._duration = Math.max(0, opts.duration);
    this._end = this._duration;
    this._loop = opts.loop ?? false;
  }

  get time() { return this._time; }
  get playing() { return this._playing; }
  get speed() { return this._speed; }
  get inFlight() { return this._inFlight; }
  get shown() { return this._shown; }

  get windowStart() { return this._start; }
  get windowEnd() { return this._end; }

  setDuration(d: number) {
    this._duration = Math.max(0, d);
    this._start = Math.min(this._start, this._duration);
    this._end = Math.min(this._end, this._duration);
    this._time = Math.min(this._time, this._end);
  }

  /** Confines travel (seek, play, loop) to [start, end]; the time is pulled into it. */
  setWindow(start: number, end: number) {
    this._start = Math.min(Math.max(start, 0), this._duration);
    this._end = Math.min(Math.max(end, this._start), this._duration);
    this._time = Math.min(Math.max(this._time, this._start), this._end);
  }

  setLoop(b: boolean) { this._loop = b; }

  setSpeed(s: number) { this._speed = Number.isFinite(s) && s >= 0 ? s : 1; }

  play() {
    if (this._time >= this._end && !this._loop) this._time = this._start;
    this._playing = true;
    this._lastTickMs = undefined;
  }

  pause() {
    this._playing = false;
    this._lastTickMs = undefined;
  }

  seek(t: number) {
    this._time = Math.min(Math.max(t, this._start), this._end);
  }

  tick(nowMs: number) {
    if (!this._playing) return;
    if (this._lastTickMs === undefined) { this._lastTickMs = nowMs; return; }
    const gap = Math.min(Math.max(nowMs - this._lastTickMs, 0), MAX_TICK_GAP_MS);
    this._lastTickMs = nowMs;
    this._time += (gap / 1000) * this._speed;
    if (this._time >= this._end) {
      const length = this._end - this._start;
      if (this._loop) this._time = length > 0 ? this._start + ((this._time - this._start) % length) : this._start;
      else { this._time = this._end; this._playing = false; this._lastTickMs = undefined; }
    }
  }

  next(): number | undefined {
    if (this._inFlight !== undefined) return undefined;
    if (this._shown === undefined || this._time !== this._shown) {
      this._inFlight = this._time;
      return this._time;
    }
    return undefined;
  }

  ack(sourceTime: number) {
    this._shown = sourceTime;
    this._inFlight = undefined;
  }

  fail() {
    this._inFlight = undefined;
    this.pause();
  }

  reset() {
    this._time = this._start;
    this._shown = undefined;
    this._inFlight = undefined;
    this.pause();
  }
}

// ---- Shots: authored moves through the world, in source time.

export const WHOLE_WORLD_SHOT = 'Whole world';

/** The world's shots; a world that declares none gets one implicit shot covering all of it. */
export function shotsOrImplicit(shots: readonly ShotMove[], duration: number): ShotMove[] {
  return shots.length ? [...shots] : [{ name: WHOLE_WORLD_SHOT, start: 0, end: Math.max(duration, 0) }];
}

/** World time shown at a source time inside a shot: worldStart + (sourceTime - start). */
export const worldTimeIn = (shot: ShotMove, sourceTime: number) => (shot.worldStart ?? shot.start) + (sourceTime - shot.start);

/** Index of the shot whose source range holds `time` (end exclusive, the last one inclusive); else the nearest earlier one; else 0. */
export function shotIndexAt(shots: readonly ShotMove[], time: number): number {
  let found = -1;
  for (let i = 0; i < shots.length; i++) {
    if (time >= shots[i].start && (time < shots[i].end || (i === shots.length - 1 && time <= shots[i].end))) return i;
    if (shots[i].start <= time) found = i;
  }
  return Math.max(found, 0);
}

/** An event as seen from one shot: its overlap with the shot's world window, mapped to source time. */
export type ShotEvent = { readonly event: WorldEvent; readonly from: number; readonly to: number };

/** Events whose world time overlaps the shot's world-time window (touching counts for a point), clipped and mapped to source time. */
export function eventsInShot(events: readonly WorldEvent[], shot: ShotMove): ShotEvent[] {
  const w0 = shot.worldStart ?? shot.start;
  const w1 = w0 + (shot.end - shot.start);
  const out: ShotEvent[] = [];
  for (const event of events) {
    const e0 = event.start;
    const e1 = event.end ?? event.start;
    if (e1 < w0 || e0 > w1) continue;
    out.push({ event, from: shot.start + (Math.max(e0, w0) - w0), to: shot.start + (Math.min(e1, w1) - w0) });
  }
  return out;
}

// ---- Free roam keys, view fitting, default world.

export type ExploreKey =
  | { type: 'move'; axis: 'forward' | 'right' | 'up'; sign: 1 | -1 }
  | { type: 'reset' }
  | { type: 'pause' };

/** The keys the free viewer handles. Modifier chords (Cmd/Ctrl/Alt) and Esc are never handled. */
export function exploreKey(e: { key: string; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean }): ExploreKey | undefined {
  if (e.ctrlKey || e.metaKey || e.altKey) return undefined;
  switch (e.key) {
    case 'w': case 'W': case 'ArrowUp': return { type: 'move', axis: 'forward', sign: 1 };
    case 's': case 'S': case 'ArrowDown': return { type: 'move', axis: 'forward', sign: -1 };
    case 'd': case 'D': case 'ArrowRight': return { type: 'move', axis: 'right', sign: 1 };
    case 'a': case 'A': case 'ArrowLeft': return { type: 'move', axis: 'right', sign: -1 };
    case 'e': case 'E': return { type: 'move', axis: 'up', sign: 1 };
    case 'q': case 'Q': return { type: 'move', axis: 'up', sign: -1 };
    case 'r': case 'R': return { type: 'reset' };
    case ' ': case 'Space': return { type: 'pause' };
    default: return undefined;
  }
}

/** The wheel steps through these free-view speeds. */
export const SPEED_LADDER = [0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10] as const;
/** The free viewer's key map, as shown by "?". */
export const KEY_MAP: readonly { readonly keys: string; readonly does: string }[] = [
  { keys: 'W A S D / arrows', does: 'move' },
  { keys: 'E  Q', does: 'up / down' },
  { keys: 'Shift', does: 'faster' },
  { keys: 'Scroll', does: 'speed' },
  { keys: 'R', does: 'reset view' },
  { keys: 'Space', does: 'pause' },
  { keys: '?', does: 'this list' },
];

/** The shot viewer's key map, as shown by "?". I and O set the two ends of the part that Add to video uses. */
export const SHOT_KEY_MAP: readonly { readonly keys: string; readonly does: string }[] = [
  { keys: 'Space', does: 'play / pause' },
  { keys: '←  →', does: '1 s back / forward' },
  { keys: 'Shift  ← →', does: '5 s' },
  { keys: 'I', does: 'start the part to add here' },
  { keys: 'O', does: 'end the part to add here' },
  { keys: '?', does: 'this list' },
];

export const SPEED_SCALE_MIN = SPEED_LADDER[0];
export const SPEED_SCALE_MAX = SPEED_LADDER[SPEED_LADDER.length - 1];
export const SHIFT_BOOST = 3;

/** One wheel notch is one rung of the ladder; deltaY > 0 (scroll down) slows down. A scale between rungs first lands on the next rung in that direction. */
export function wheelSpeedScale(scale: number, deltaY: number): number {
  if (!Number.isFinite(deltaY) || deltaY === 0) return scale;
  if (deltaY < 0) return SPEED_LADDER.find((rung) => rung > scale + 1e-9) ?? SPEED_SCALE_MAX;
  return [...SPEED_LADDER].reverse().find((rung) => rung < scale - 1e-9) ?? SPEED_SCALE_MIN;
}

/** "1×", "1.25×", "0.5×". */
export const speedScaleLabel = (scale: number) => `${Number(scale.toFixed(2))}×`;

/** Largest `aspect` box that fits inside boxWidth x boxHeight, whole pixels. */
export function fitToBox(boxWidth: number, boxHeight: number, aspect: number): { width: number; height: number } {
  if (!(boxWidth > 0) || !(boxHeight > 0) || !(aspect > 0)) return { width: 0, height: 0 };
  const width = Math.floor(Math.min(boxWidth, boxHeight * aspect));
  return { width, height: Math.floor(width / aspect) };
}

/** The last world viewed in this project if it is still listed; else the first world in the video; else the first library world. */
export function chooseDefaultWorld<T extends { key: string }>(timeline: readonly T[], library: readonly T[], lastKey: string | undefined): T | undefined {
  return [...timeline, ...library].find((row) => row.key === lastKey) ?? timeline[0] ?? library[0];
}

export const lastWorldKey = (projectId: string | null | undefined) => `lastWorld:${projectId ? projectId : 'none'}`;
export const HINT_DISMISSED_KEY = 'exploreHintDismissed';
