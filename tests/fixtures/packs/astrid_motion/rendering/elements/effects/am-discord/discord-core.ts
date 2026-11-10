// Pure am-discord logic (no React, no DOM). Markup, typing reveal, split-flap
// date schedule, reaction counts and decoration targets. Kept separate so the
// schedule can be checked without rendering.
import {clamp, hashUnit} from '../../_shared/am';

export type Token = {kind: 'text' | 'u' | 'mention'; text: string};

// `{u:word}` marks a word for underline. `@everyone` renders as a mention pill.
// Everything else is plain text, split at word and space boundaries so that
// underline and strike targets can address one word.
export const tokenize = (line: string): Token[] => {
  const out: Token[] = [];
  const pattern = /\{u:([^}]+)\}|(@everyone)/g;
  let last = 0;
  let match: RegExpExecArray | null = pattern.exec(line);
  while (match !== null) {
    if (match.index > last) out.push(...plainTokens(line.slice(last, match.index)));
    if (match[1] !== undefined) out.push({kind: 'u', text: match[1]});
    else out.push({kind: 'mention', text: match[2] ?? '@everyone'});
    last = match.index + match[0].length;
    match = pattern.exec(line);
  }
  if (last < line.length) out.push(...plainTokens(line.slice(last)));
  return out;
};

const plainTokens = (text: string): Token[] =>
  text.split(/(\s+)/).filter((part) => part.length > 0).map((part) => ({kind: 'text' as const, text: part}));

// The comparable form of a token: letters, digits and apostrophes, lower case.
export const wordKey = (text: string): string =>
  text.replace(/[^\p{L}\p{N}']/gu, '').toLowerCase();

// Characters visible after `chars` have been typed, split per token. Hidden
// characters stay in the layout (transparent), so typing never reflows the line.
export const revealed = (tokens: Token[], chars: number): number[] => {
  let left = chars;
  return tokens.map((token) => {
    const shown = clamp(left, 0, token.text.length);
    left -= token.text.length;
    return shown;
  });
};

export const totalChars = (lines: string[]): number =>
  lines.reduce((sum, line) => sum + tokenize(line).reduce((s, t) => s + t.text.length, 0), 0);

// ---- Split-flap date jump --------------------------------------------------

const DAY_MS = 86400000;
const MONTHS = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

// "26 October 2025" -> UTC midnight ms, or null if it is not that shape.
export const parseDayLabel = (label: string): number | null => {
  const m = /^(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$/.exec(label.trim());
  if (!m) return null;
  const month = MONTHS.findIndex((name) => name.toLowerCase() === m[2].toLowerCase());
  if (month < 0) return null;
  return Date.UTC(Number(m[3]), month, Number(m[1]));
};

export const formatDayLabel = (ms: number): string => {
  const d = new Date(ms);
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
};

// Ease-out-quad day offsets for a jump of `total` days over `ticks` frames.
// Tick k (0-based) shows offset dayOffset(k). The last tick is the target.
export const dayOffset = (total: number, k: number, ticks: number): number => {
  const t = clamp((k + 1) / ticks, 0, 1);
  const eased = 1 - (1 - t) * (1 - t);
  return k >= ticks - 1 ? total : Math.round(total * eased);
};

export type DividerState =
  | {phase: 'hidden'}
  | {phase: 'landed'; text: string}
  | {phase: 'flip'; top: string; bottom: string; tick: number};

// State of one divider at clip frame `frame`. `from` is the previous divider's
// date (or the same date when this is the first). A non-jump divider lands at once.
export const dividerState = (
  label: string,
  at: number,
  frame: number,
  from: string,
  jump: boolean,
  ticks: number,
): DividerState => {
  if (frame < at) return {phase: 'hidden'};
  const to = parseDayLabel(label);
  const start = parseDayLabel(from);
  if (!jump || to === null || start === null || to === start || frame >= at + ticks) {
    return {phase: 'landed', text: label};
  }
  const total = Math.round((to - start) / DAY_MS);
  const k = frame - at;
  const topMs = start + dayOffset(total, k, ticks) * DAY_MS;
  const bottomMs = start + (k === 0 ? 0 : dayOffset(total, k - 1, ticks)) * DAY_MS;
  return {phase: 'flip', top: formatDayLabel(topMs), bottom: formatDayLabel(bottomMs), tick: k};
};

// ---- Decoration timing -----------------------------------------------------

// Underline draws in over 6 frames, one step per frame. Returns 0..1 in sixths.
export const underlineProgress = (frame: number, at: number): number =>
  clamp(frame - at + 1, 0, 6) / 6;

// ---- Reactions -------------------------------------------------------------

export const reactionCount = (
  countFrom: number,
  countTo: number,
  startAt: number,
  stepFrames: number,
  frame: number,
): number => {
  if (frame < startAt) return countFrom;
  const span = countTo - countFrom;
  const steps = Math.floor((frame - startAt) / Math.max(1, stepFrames));
  const magnitude = Math.min(Math.abs(span), steps);
  return countFrom + Math.sign(span) * magnitude;
};

// Stamp entrance: 0.6 -> 1.1 -> 1.0 over three frames, then held.
export const stampScale = (frame: number, startAt: number): number => {
  const k = frame - startAt;
  if (k < 0) return 0;
  if (k === 0) return 0.6;
  if (k === 1) return 1.1;
  return 1;
};

// ---- Camera (stepped push-ins) ---------------------------------------------

export type CameraStep = {at: number; zoom: number; fx: number; fy: number};

// The last camera step that has started by `frame`. Steps snap; nothing eases.
export const cameraAt = (steps: CameraStep[], frame: number): CameraStep | null => {
  let active: CameraStep | null = null;
  for (const step of steps) {
    if (step.at <= frame) active = step;
  }
  return active;
};

// ---- Highlighter, circle and annotation timing -----------------------------

// Vox-style swipe: the bar wipes across the word in 4 stepped frames.
export const highlightProgress = (frame: number, at: number): number => clamp(frame - at + 1, 0, 4) / 4;

// Hand-drawn circle: 6 stepped segments, one per frame.
export const circleProgress = (frame: number, at: number): number => clamp(frame - at + 1, 0, 6) / 6;

// Annotation: connector grows in 2 steps, the card lands on the frame after.
export const connectorProgress = (frame: number, at: number): number => clamp(frame - at + 1, 0, 2) / 2;
export const annotationShown = (frame: number, at: number): boolean => frame >= at + 2;

// A hand-drawn ellipse in a 0..100 box (container percent). About 1.15 turns,
// so the stroke overlaps itself, with a seeded radius wobble. Same seed, same stroke.
export const handCirclePath = (seed: number): string => {
  const steps = 72;
  const start = -1.95;
  const sweep = Math.PI * 2 * 1.15;
  const parts: string[] = [];
  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps;
    const angle = start + sweep * t;
    const wobble = 1 + (hashUnit(seed, i) - 0.5) * 0.04;
    const drift = t > 0.85 ? 1 + (t - 0.85) * 0.18 : 1;
    const rx = 48 * wobble * drift;
    const ry = 42 * wobble;
    const x = 50 + rx * Math.cos(angle);
    const y = 50 + ry * Math.sin(angle);
    parts.push(`${i === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`);
  }
  return parts.join(' ');
};

// ---- Fit to content --------------------------------------------------------

// Window height that hugs the visible stack. 143 px is the window chrome (border,
// header, input bar and the chat padding); `headroom` is room above the stack
// for annotation cards. Clamped to [minHeight, maxHeight].
export const fitHeight = (stack: number, headroom: number, minHeight: number, maxHeight: number): number =>
  clamp(143 + headroom + stack, minHeight, maxHeight);

export const hexAlpha = (hex: string, alpha: number): string => {
  const n = Number.parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  return `rgba(${r}, ${g}, ${b}, ${clamp(alpha, 0, 1)})`;
};
