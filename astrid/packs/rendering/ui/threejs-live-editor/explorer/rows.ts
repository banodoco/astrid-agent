// The one world list: worlds in the video, then library worlds.
import type { TimelineSnapshot } from '@astrid/editor-sdk';
import { PLACEHOLDER_LABEL, UNTITLED_WORLD, worldKey, worldsFromSnapshot, type ShotMove, type TimelineWorld, type LibraryEntry, type WorldEvent, type WorldUse } from './model';

/** One world the explorer can open: from the timeline, or from the project library. */
export type Row = {
  readonly key: string;
  readonly name: string;
  readonly duration: number;
  readonly authoredFps: number;
  readonly objectId: string;
  readonly revision: string;
  readonly uses: readonly WorldUse[];
  readonly events: readonly WorldEvent[];
  readonly shots: readonly ShotMove[];
  /** Present for timeline worlds, whose package already rides on their clips. */
  readonly liveScene?: Record<string, unknown>;
  /** Set only when another row has the same name, to tell them apart: "library" for the library copy, else the tracks the clips sit on ("on V3, V6"), else a short revision. Never "in video". */
  readonly qualifier?: string;
};

export function rowFromEntry(entry: LibraryEntry): Row {
  return {
    key: worldKey(entry),
    name: entry.name,
    duration: entry.duration,
    authoredFps: entry.authoredFps,
    objectId: entry.objectId,
    revision: entry.revision,
    uses: [],
    events: entry.events,
    shots: entry.shots,
  };
}

/** A timeline world's name: its saved name, else the clip's label, a name the package declares, a library world with the same
 * object or revision, a title from its entry file; "Untitled world" only when none exists. */
export function timelineWorldName(world: TimelineWorld, entry: LibraryEntry | undefined, library: readonly LibraryEntry[], index = 0): string {
  if (entry && !UNTITLED_WORLD.test(entry.name)) return entry.name;
  const sibling = library.find((e) => (e.objectId === world.objectId || e.revision === world.revision) && !UNTITLED_WORLD.test(e.name));
  const adopt = (name: string) => library.find((e) => e.name.toLowerCase() === name.toLowerCase())?.name ?? name;
  const label = world.nameHints.label && !PLACEHOLDER_LABEL.test(world.nameHints.label) ? adopt(world.nameHints.label) : undefined;
  const file = world.nameHints.file ? adopt(world.nameHints.file) : undefined;
  return label ?? world.nameHints.declared ?? sibling?.name ?? file
    ?? entry?.name ?? (index === 0 ? 'Untitled world' : `Untitled world ${index + 1}`);
}

/** Timeline worlds first (named from the library when saved, else "Untitled world"), then library-only worlds. */
export function buildRows(snapshot: TimelineSnapshot | undefined, library: readonly LibraryEntry[]): { timeline: Row[]; library: Row[] } {
  const worlds = snapshot ? worldsFromSnapshot(snapshot) : [];
  const saved = new Map(library.map((entry) => [worldKey(entry), entry] as const));
  const onTimeline = new Set(worlds.map(worldKey));
  const timeline = worlds.map((world, index): Row => {
    const entry = saved.get(worldKey(world));
    return {
      key: worldKey(world),
      name: timelineWorldName(world, entry, library, index),
      duration: world.duration,
      authoredFps: world.authoredFps,
      objectId: world.objectId,
      revision: world.revision,
      uses: world.uses,
      events: entry?.events ?? [],
      shots: entry?.shots ?? [],
      liveScene: world.liveScene,
    };
  });
  const libraryRows = library.filter((entry) => !onTimeline.has(worldKey(entry))).map(rowFromEntry);
  // One spelling per name: a library or saved spelling first, then a declared one, else the one with more capitals.
  const spellings = new Map<string, { name: string; rank: number; caps: number }>();
  const caps = (name: string) => (name.match(/\p{Lu}/gu) ?? []).length;
  const offer = (name: string, rank: number) => {
    const key = name.toLowerCase();
    const best = spellings.get(key);
    if (!best || rank > best.rank || (rank === best.rank && caps(name) > best.caps)) spellings.set(key, { name, rank, caps: caps(name) });
  };
  for (const entry of library) offer(entry.name, 2);
  worlds.forEach((world, index) => { if (world.nameHints.declared && timeline[index].name === world.nameHints.declared) offer(timeline[index].name, 1); });
  for (const row of timeline) offer(row.name, 0);
  const respell = (row: Row): Row => ({ ...row, name: spellings.get(row.name.toLowerCase())?.name ?? row.name });
  const timelineRows = timeline.map(respell);
  const all = [...timelineRows, ...libraryRows.map(respell)];
  const group = (row: Row) => all.filter((other) => other.name.toLowerCase() === row.name.toLowerCase());
  const tracks = (row: Row) => `on ${[...new Set(row.uses.map((use) => use.track))].sort().join(', ')}`;
  const shortRevision = (row: Row) => `rev ${row.revision.replace(/^sha256:/, '').slice(0, 6)}`;
  const qualifyTimeline = (row: Row): Row => {
    const rivals = group(row).filter((other) => other.uses.length > 0);
    if (rivals.length < 2) return row;
    const label = row.uses.length ? tracks(row) : shortRevision(row);
    return { ...row, qualifier: rivals.filter((other) => tracks(other) === label).length > 1 ? shortRevision(row) : label };
  };
  const qualifyLibrary = (row: Row): Row => (group(row).length > 1 ? { ...row, qualifier: 'library' } : row);
  return { timeline: timelineRows.map(qualifyTimeline), library: libraryRows.map(respell).map(qualifyLibrary) };
}
