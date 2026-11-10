// am-churn data model. Pure functions only: the same frame and params always
// give the same picture. Dates are ISO strings in the rows; every position is a
// day index into rows (0 = startDate).

export type ChurnRow = {date: string; commits: number; added: number; deleted: number};
export type ChurnHighlight = {
  date: string;
  /** Day index, filled in once per render (see withDays). */
  day?: number;
  repo: string;
  sha: string;
  subject: string;
  added: number;
  deleted: number;
};
export type ChurnKey = {atFrame: number; date: string};
export type ChurnFreeze = {
  /** Frame (relative to the clip) where the timeline stops on the spotlight day. */
  atFrame: number;
  /** sha (7 or more chars) of the highlight to spotlight. Its date is used unless `date` is set. */
  highlight?: string;
  date?: string;
  /** Frames the timeline holds before it resumes. */
  holdFrames?: number;
  /** Frames after atFrame before the spotlight card draws in. */
  cardDelay?: number;
  label?: string;
};
export type ChurnTotals = {commits: number; added: number; deleted: number};

export type Ledger = {
  days: string[];
  /** cumAdded[i] = lines added through day i inclusive. Last entry equals totals. */
  cumAdded: number[];
  cumDeleted: number[];
  cumCommits: number[];
  totals: ChurnTotals;
};

const DAY_MS = 86400000;

export const dayOffset = (iso: string, startIso: string): number =>
  Math.round((Date.parse(`${iso}T00:00:00Z`) - Date.parse(`${startIso}T00:00:00Z`)) / DAY_MS);

const finite = (value: unknown, fallback: number): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

export const buildLedger = (rows: ChurnRow[], totals: ChurnTotals): Ledger => {
  const days: string[] = [];
  const cumAdded: number[] = [];
  const cumDeleted: number[] = [];
  const cumCommits: number[] = [];
  let a = 0;
  let d = 0;
  let c = 0;
  rows.forEach((row) => {
    days.push(row.date);
    a += finite(row.added, 0);
    d += finite(row.deleted, 0);
    c += finite(row.commits, 0);
    cumAdded.push(a);
    cumDeleted.push(d);
    cumCommits.push(c);
  });
  const last = rows.length - 1;
  if (last >= 0) {
    // The endDate row must land exactly on the provided totals.
    cumAdded[last] = totals.added;
    cumDeleted[last] = totals.deleted;
    cumCommits[last] = totals.commits;
  }
  return {days, cumAdded, cumDeleted, cumCommits, totals};
};

/** Day index of a date, clamped to the ledger. */
export const dayIndexOf = (ledger: Ledger, iso: string): number => {
  const start = ledger.days[0] ?? iso;
  const index = dayOffset(iso, start);
  return Math.max(0, Math.min(ledger.days.length - 1, index));
};

export const formatDate = (iso: string): string => {
  // 2026-05-08 -> 08.05.26
  const [y, m, d] = iso.split('-');
  return `${d}.${m}.${y.slice(2)}`;
};

export const formatCount = (value: number): string => {
  const n = Math.max(0, Math.round(value));
  return n.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ',');
};

export type TimelineSpec = {keys: {atFrame: number; day: number}[]; freeze: null | {at: number; hold: number; day: number | null}};

export const buildTimeline = (
  ledger: Ledger,
  timeline: ChurnKey[],
  freeze: ChurnFreeze | null,
  highlights: ChurnHighlight[],
): TimelineSpec => {
  const keys = timeline
    .map((key) => ({atFrame: finite(key.atFrame, 0), day: dayIndexOf(ledger, key.date)}))
    .sort((x, y) => x.atFrame - y.atFrame);
  if (keys.length === 0) {
    keys.push({atFrame: 0, day: 0}, {atFrame: 300, day: Math.max(0, ledger.days.length - 1)});
  }
  let frozen: TimelineSpec['freeze'] = null;
  if (freeze) {
    const wanted = freeze.highlight ?? '';
    const match = wanted ? highlights.find((h) => h.sha.startsWith(wanted) || wanted.startsWith(h.sha)) : undefined;
    const target = freeze.date ?? match?.date ?? null;
    frozen = {
      at: finite(freeze.atFrame, 0),
      hold: Math.max(0, Math.round(finite(freeze.holdFrames, 90))),
      day: target ? dayIndexOf(ledger, target) : null,
    };
  }
  return {keys, freeze: frozen};
};

// Raw position on the keyframes (fractional day index), linear between keys.
const rawPosition = (keys: TimelineSpec['keys'], frame: number): number => {
  if (frame <= keys[0].atFrame) return keys[0].day;
  const last = keys[keys.length - 1];
  if (frame >= last.atFrame) return last.day;
  for (let i = 0; i < keys.length - 1; i += 1) {
    const a = keys[i];
    const b = keys[i + 1];
    if (frame >= a.atFrame && frame <= b.atFrame) {
      const span = b.atFrame - a.atFrame;
      if (span <= 0) return b.day;
      return a.day + ((b.day - a.day) * (frame - a.atFrame)) / span;
    }
  }
  return last.day;
};

/** Fractional day index shown at `frame`, after the optional freeze hold. */
export const dayPosition = (spec: TimelineSpec, frame: number, maxDay: number): number => {
  const clamp = (value: number): number => Math.max(0, Math.min(maxDay, value));
  const freeze = spec.freeze;
  if (!freeze || frame < freeze.at) return clamp(rawPosition(spec.keys, frame));
  const held = freeze.day ?? rawPosition(spec.keys, freeze.at);
  if (frame < freeze.at + freeze.hold) return clamp(held);
  return clamp(rawPosition(spec.keys, frame - freeze.hold));
};

export type CounterState = {added: number; deleted: number; commits: number; day: number};

export const counterState = (ledger: Ledger, position: number): CounterState => {
  const last = ledger.days.length - 1;
  const day = Math.max(0, Math.min(last, Math.floor(position)));
  const frac = day >= last ? 0 : position - day;
  const lerp = (cum: number[]): number => {
    if (day >= last) return cum[last];
    return Math.round(cum[day] + (cum[day + 1] - cum[day]) * frac);
  };
  return {added: lerp(ledger.cumAdded), deleted: lerp(ledger.cumDeleted), commits: lerp(ledger.cumCommits), day};
};

/** Number of highlights whose day has been reached (stamped in). */
export const withDays = (highlights: ChurnHighlight[], ledger: Ledger): ChurnHighlight[] =>
  highlights.map((h) => ({...h, day: dayIndexOf(ledger, h.date)}));

export const stampedCount = (highlights: ChurnHighlight[], day: number): number => {
  let count = 0;
  for (const h of highlights) {
    if ((h.day ?? 0) <= day) count += 1;
  }
  return count;
};

// Heartbeat: bins of `binDays` days; shows sums up to the current day.
export type HeartBin = {start: number; end: number; added: number; deleted: number};

export const heartBins = (ledger: Ledger, binDays: number): HeartBin[] => {
  const bins: HeartBin[] = [];
  const n = ledger.days.length;
  const step = Math.max(1, Math.round(binDays));
  let prevA = 0;
  let prevD = 0;
  for (let start = 0; start < n; start += step) {
    const end = Math.min(n - 1, start + step - 1);
    const endA = ledger.cumAdded[end];
    const endD = ledger.cumDeleted[end];
    bins.push({start, end, added: endA - prevA, deleted: endD - prevD});
    prevA = endA;
    prevD = endD;
  }
  return bins;
};

/** Bin sums visible at `day` (partial bins count the days reached so far). */
export const visibleBin = (ledger: Ledger, bin: HeartBin, day: number): {added: number; deleted: number} => {
  if (day < bin.start) return {added: 0, deleted: 0};
  const upTo = Math.min(day, bin.end);
  const beforeA = bin.start > 0 ? ledger.cumAdded[bin.start - 1] : 0;
  const beforeD = bin.start > 0 ? ledger.cumDeleted[bin.start - 1] : 0;
  return {added: ledger.cumAdded[upTo] - beforeA, deleted: ledger.cumDeleted[upTo] - beforeD};
};

// Stepped sqrt scale to whole cells of `cell` px; the same max for both sides.
export const barCells = (value: number, maxValue: number, maxCells: number): number => {
  if (value <= 0 || maxValue <= 0) return 0;
  const cells = Math.round(Math.sqrt(Math.min(1, value / maxValue)) * maxCells);
  return Math.max(1, cells);
};

// Stamp list scroll: the most recent change in the stamped count, `age` frames ago.
export const lastStampChange = (
  highlights: ChurnHighlight[],
  position: (frame: number) => number,
  frame: number,
  span: number,
): {delta: number; age: number} => {
  const countAt = (f: number): number => stampedCount(highlights, Math.floor(position(f)));
  for (let age = 0; age < span && frame - age - 1 >= 0; age += 1) {
    const now = countAt(frame - age);
    const before = countAt(frame - age - 1);
    if (now !== before) return {delta: now - before, age};
  }
  return {delta: 0, age: span};
};

// Last frame at which the integer day changed, for the date flap.
export const lastDayChange = (
  position: (frame: number) => number,
  frame: number,
  span: number,
): {changeFrame: number; prevDay: number} | null => {
  for (let k = 0; k < span && frame - k - 1 >= 0; k += 1) {
    const now = Math.floor(position(frame - k));
    const before = Math.floor(position(frame - k - 1));
    if (now !== before) return {changeFrame: frame - k, prevDay: before};
  }
  return null;
};
