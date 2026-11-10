import type {CSSProperties, ReactElement} from 'react';
import {useCurrentFrame} from 'remotion';
import {
  type ElementComponentProps,
  narrowParams,
} from '../../../../../rendering/shared/element_contracts';
import {
  COLOR,
  FAMILY,
  clamp,
  finiteNumber,
  integerIn,
  renderableFile,
} from '../../_shared/am';
import {
  type CameraStep,
  type Token,
  annotationShown,
  cameraAt,
  circleProgress,
  connectorProgress,
  dividerState,
  fitHeight,
  handCirclePath,
  hexAlpha,
  highlightProgress,
  reactionCount,
  revealed,
  stampScale,
  tokenize,
  totalChars,
  underlineProgress,
  wordKey,
} from './discord-core';

// am-discord: a pixel-styled RECONSTRUCTION of a Discord channel window. It is
// not a screenshot and carries no Discord logo or wordmark. The window is
// 1280 px wide. Its height is fixed, or (fit "content") hugs the visible stack.
// Messages stack from the bottom, joins and date dividers interleave in time
// order, and reactions, highlighter, underline, strike, circle and annotation
// decorations step on whole frames. A camera steps the whole window in integer
// zoom, so it can push in on one word.
// Discord palette: community values from the lore notes (04-lore.md B3), so
// they are [S]/[U] and should be checked against a real client before sign-off.

const PALETTE = {
  rail: '#1E1F22',
  side: '#2B2D31',
  chat: '#313338',
  input: '#383A40',
  selected: '#404249',
  rule: '#3F4147',
  text: '#F2F3F5',
  body: '#F2F3F5',
  muted: '#949BA4',
  blurple: '#5865F2',
  blurpleInk: '#FFFFFF',
  green: '#23A559',
  mention: 'rgba(88, 101, 242, 0.3)',
  mentionText: '#C9CDFB',
  rust: COLOR.rust,
  orange: COLOR.orange,
};

const PANEL = COLOR.panel;
const INK = COLOR.ink;
const MONO = `'${FAMILY.label}', monospace`;
const SANS = `'${FAMILY.body}', 'Noto Sans', sans-serif`;
const EMOJI = `'Apple Color Emoji', 'Noto Color Emoji', sans-serif`;
const BODY_FONT = `400 17px ${FAMILY.body}, sans-serif`;

// Layout constants (px). The 6 px rhythm: chrome 48, rail 72, sidebar 240.
const RAIL_W = 72;
const SIDEBAR_W = 240;
const BORDER = 2;
const HEADER_H = 49;
const INPUT_BLOCK_H = 78;
const MESSAGE_PAD = 12;
const LINE_H = 24;
const DIVIDER_H = 48;
const JOIN_H = 36;
const REACTION_ROW_H = 36;
const ANNOTATION_HEADROOM = 64;
const MESSAGE_TEXT_INSET = 90; // avatar 42 + gap 12 + padding 18 x 2 + 8 spare

type MessageSpec = {
  author: string;
  tag: string;
  avatarColor: string;
  time: string;
  lines: string[];
  appearAt: number;
  typeOn: boolean;
};
type DividerSpec = {label: string; at: number; jump: boolean | null};
type ReactionSpec = {
  emoji: string;
  countFrom: number;
  countTo: number;
  startAt: number;
  stepFrames: number;
  onMessage: number | null;
  mine: boolean;
};
type JoinSpec = {name: string; at: number; time: string};
type TargetSpec = {word: string; at: number; onMessage: number | null};
type MarkSpec = TargetSpec & {color: string};
type HighlightSpec = TargetSpec & {color: string; alpha: number};
type AnnotationSpec = TargetSpec & {text: string};
type FrameSpec = {width: number; height: number; radius: number; outline: string; shadow: number};

type Params = {
  server?: string;
  channel?: string;
  channels?: string[];
  sidebar?: boolean;
  fit?: string;
  minHeight?: number;
  messages?: unknown[];
  dateDividers?: unknown[];
  reactions?: unknown[];
  joins?: unknown[];
  underline?: unknown[];
  strike?: unknown[];
  highlight?: unknown[];
  circle?: unknown[];
  annotation?: unknown[];
  camera?: unknown[];
  badge?: string;
  frame?: Partial<FrameSpec>;
  jumpFrames?: number;
  typeStepFrames?: number;
  /**
   * Composer: message `message` types into the message box from `startAt`
   * (one char per `typeStepFrames`, default 1), Enter is pressed on the 3
   * frames before the message's appearAt (box flashes, a keycap stamps in at
   * `key` {x, y} canvas px), then the box clears as the post lands.
   */
  composer?: {message?: number; startAt?: number; typeStepFrames?: number; key?: {x?: number; y?: number} | null} | null;
};

const obj = (value: unknown): Record<string, unknown> =>
  value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};

const strOr = (value: unknown, fallback: string): string => (typeof value === 'string' ? value : fallback);

const nonEmptyStr = (value: unknown): string | null =>
  typeof value === 'string' && value.trim().length > 0 ? value : null;

const hexOr = (value: unknown, fallback: string): string =>
  typeof value === 'string' && /^#[0-9a-fA-F]{6}$/.test(value.trim()) ? value.trim() : fallback;

const frameOf = (value: unknown, fallback = 0): number =>
  Math.max(0, Math.round(finiteNumber(value, fallback)));

const onMessageOf = (value: unknown): number | null =>
  typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;

const listOf = <T,>(items: unknown[] | undefined, map: (o: Record<string, unknown>) => T | null): T[] =>
  (items ?? []).flatMap((raw) => {
    const value = map(obj(raw));
    return value ? [value] : [];
  });

const targetOf = (o: Record<string, unknown>): TargetSpec | null => {
  const word = nonEmptyStr(o.word);
  return word ? {word, at: frameOf(o.at), onMessage: onMessageOf(o.onMessage)} : null;
};

// Adjacent messages from the same author, tag and time render as one block:
// one header, then the lines, as Discord groups them.
const parseMessages = (items: unknown[] | undefined): MessageSpec[] => {
  const raw = listOf<MessageSpec>(items, (m) => {
    const author = nonEmptyStr(m.author);
    const lines = Array.isArray(m.lines) ? m.lines.filter((l): l is string => typeof l === 'string') : [];
    if (!author || lines.length === 0) return null;
    return {
      author,
      tag: strOr(m.tag, ''),
      avatarColor: hexOr(m.avatarColor, '#5865F2'),
      time: strOr(m.time, ''),
      lines,
      appearAt: frameOf(m.appearAt),
      typeOn: m.typeOn === true,
    };
  });
  const grouped: MessageSpec[] = [];
  for (const m of raw) {
    const last = grouped[grouped.length - 1];
    if (last && last.author === m.author && last.tag === m.tag && last.time === m.time && last.avatarColor === m.avatarColor) {
      grouped[grouped.length - 1] = {...last, lines: [...last.lines, ...m.lines], typeOn: last.typeOn || m.typeOn};
    } else {
      grouped.push(m);
    }
  }
  return grouped;
};

const parseDividers = (items: unknown[] | undefined): DividerSpec[] =>
  listOf<DividerSpec>(items, (d) => {
    const label = nonEmptyStr(d.label);
    if (!label) return null;
    return {label, at: frameOf(d.at), jump: typeof d.jump === 'boolean' ? d.jump : null};
  });

const parseReactions = (items: unknown[] | undefined): ReactionSpec[] =>
  listOf<ReactionSpec>(items, (r) => {
    const emoji = nonEmptyStr(r.emoji);
    if (!emoji) return null;
    return {
      emoji,
      countFrom: Math.round(finiteNumber(r.countFrom, 0)),
      countTo: Math.round(finiteNumber(r.countTo, 0)),
      startAt: frameOf(r.startAt),
      stepFrames: integerIn(r.stepFrames, 1, 60, 3),
      onMessage: onMessageOf(r.onMessage),
      mine: r.mine === true,
    };
  });

const parseJoins = (items: unknown[] | undefined): JoinSpec[] =>
  listOf<JoinSpec>(items, (j) => {
    const name = nonEmptyStr(j.name);
    return name ? {name, at: frameOf(j.at), time: strOr(j.time, '')} : null;
  });

const parseMarks = (items: unknown[] | undefined): MarkSpec[] =>
  listOf<MarkSpec>(items, (u) => {
    const t = targetOf(u);
    return t ? {...t, color: hexOr(u.color, COLOR.orange)} : null;
  });

const parseHighlights = (items: unknown[] | undefined): HighlightSpec[] =>
  listOf<HighlightSpec>(items, (h) => {
    const t = targetOf(h);
    return t
      ? {...t, color: hexOr(h.color, COLOR.orange), alpha: clamp(finiteNumber(h.alpha, 0.45), 0, 1)}
      : null;
  });

const parseAnnotations = (items: unknown[] | undefined): AnnotationSpec[] =>
  listOf<AnnotationSpec>(items, (a) => {
    const t = targetOf(a);
    const text = nonEmptyStr(a.text);
    return t && text ? {...t, text} : null;
  });

const parseStrikes = (items: unknown[] | undefined): TargetSpec[] =>
  listOf<TargetSpec>(items, targetOf);

const parseCamera = (items: unknown[] | undefined): CameraStep[] =>
  listOf<CameraStep>(items, (c) => {
    const focus = obj(c.focus);
    return {
      at: frameOf(c.at),
      zoom: integerIn(c.zoom, 1, 3, 1),
      fx: finiteNumber(focus.x, Number.NaN),
      fy: finiteNumber(focus.y, Number.NaN),
    };
  });

const DEFAULT_FRAME: FrameSpec = {width: 1280, height: 720, radius: 12, outline: '#25241F', shadow: 12};

const parseFrame = (raw: Partial<FrameSpec> | undefined): FrameSpec => {
  const f = obj(raw);
  return {
    width: integerIn(f.width, 320, 1920, DEFAULT_FRAME.width),
    height: integerIn(f.height, 180, 1080, DEFAULT_FRAME.height),
    radius: integerIn(f.radius, 0, 48, DEFAULT_FRAME.radius),
    outline: hexOr(f.outline, DEFAULT_FRAME.outline),
    shadow: integerIn(f.shadow, 0, 48, DEFAULT_FRAME.shadow),
  };
};

// Per-token decoration, keyed by "message:line:token".
type Decor = {
  underline?: {at: number; color: string};
  strike?: {at: number};
  highlight?: {at: number; color: string; alpha: number};
  circle?: {at: number; d: string};
  annotation?: {at: number; text: string};
};

const findToken = (tokenLines: Token[][][], word: string, onMessage: number | null): string | null => {
  const key = wordKey(word);
  const messageIndexes = onMessage !== null ? [onMessage] : tokenLines.map((_, i) => i);
  for (const mi of messageIndexes) {
    const lines = tokenLines[mi];
    if (!lines) continue;
    for (let li = 0; li < lines.length; li += 1) {
      const tokens = lines[li];
      for (let ti = 0; ti < tokens.length; ti += 1) {
        const token = tokens[ti];
        if (token.kind !== 'mention' && wordKey(token.text) === key) return `${mi}:${li}:${ti}`;
      }
    }
  }
  return null;
};

// Text width measured on a canvas with the same font as the DOM body text, so
// the fit height follows the real wrapping. Falls back to an average advance.
let measureCtx: CanvasRenderingContext2D | null | undefined;
const measure = (text: string): number => {
  if (measureCtx === undefined) {
    try {
      measureCtx = document.createElement('canvas').getContext('2d');
    } catch {
      measureCtx = null;
    }
  }
  if (!measureCtx) return text.length * 8.5;
  measureCtx.font = BODY_FONT;
  return measureCtx.measureText(text).width;
};

const wrapCount = (text: string, width: number): number => {
  let lines = 1;
  let current = '';
  for (const word of text.split(' ')) {
    const candidate = current ? `${current} ${word}` : word;
    if (current && measure(candidate) > width) {
      lines += 1;
      current = word;
    } else {
      current = candidate;
    }
  }
  return lines;
};

// Marker bar outline in a 20 px box: body rows 1 to 19, ragged left end (top
// edge 1 px down over 2 px) and ragged right end (bottom edge 1 px up over 2 px).
const MARKER_ENDS = 'polygon(0px 2px, 2px 2px, 2px 1px, 100% 1px, 100% 18px, calc(100% - 2px) 18px, calc(100% - 2px) 19px, 0px 19px)';

const PixelArrow = ({color}: {color: string}): ReactElement => (
  <svg width={16} height={12} viewBox="0 0 16 12" shapeRendering="crispEdges" style={{display: 'block'}}>
    <rect x={0} y={5} width={10} height={2} fill={color} />
    <rect x={10} y={3} width={2} height={6} fill={color} />
    <rect x={12} y={4} width={2} height={4} fill={color} />
    <rect x={14} y={5} width={2} height={2} fill={color} />
  </svg>
);

// Stepped corners (6 px steps) keep the avatars on the pixel grid.
const STEPPED = 'polygon(6px 0, calc(100% - 6px) 0, 100% 6px, 100% calc(100% - 6px), calc(100% - 6px) 100%, 6px 100%, 0 calc(100% - 6px), 0 6px)';

const Avatar = ({name, color}: {name: string; color: string}): ReactElement => (
  <div
    style={{
      width: 42,
      height: 42,
      clipPath: STEPPED,
      background: color,
      color: '#FFFEFA',
      fontFamily: SANS,
      fontWeight: 700,
      fontSize: 18,
      lineHeight: '42px',
      textAlign: 'center',
      flexShrink: 0,
    }}
  >
    {name.charAt(0).toUpperCase()}
  </div>
);

const Emoji = ({value}: {value: string}): ReactElement => {
  const src = /^(https?:|data:|blob:|\/)|\.(png|svg|webp|gif)$/i.test(value) ? renderableFile(value) : null;
  if (src) {
    return <img src={src} width={18} height={18} style={{display: 'block', imageRendering: 'pixelated'}} />;
  }
  return <span style={{fontFamily: EMOJI, fontSize: 16, lineHeight: '18px'}}>{value}</span>;
};

// Split-flap date. The top half shows this tick's date and the bottom half the
// previous tick's, so each step reads as a flap turning over.
const DayText = ({text, half}: {text: string; half: 'top' | 'bottom'}): ReactElement => (
  <div
    style={{
      position: 'absolute',
      inset: 0,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      clipPath: half === 'top' ? 'inset(0 0 50% 0)' : 'inset(50% 0 0 0)',
      fontFamily: MONO,
      fontSize: 14,
      lineHeight: '24px',
      color: PALETTE.muted,
      whiteSpace: 'nowrap',
    }}
  >
    {text}
  </div>
);

const DividerRow = ({label, flip}: {label: string; flip: {top: string; bottom: string} | null}): ReactElement => (
  <div style={{display: 'flex', alignItems: 'center', gap: 12, padding: '12px 18px'}}>
    <div style={{flex: 1, height: 1, background: PALETTE.rule}} />
    {flip ? (
      <div style={{position: 'relative', height: 24, minWidth: 210, flexShrink: 0}}>
        <DayText text={flip.top} half="top" />
        <DayText text={flip.bottom} half="bottom" />
        <div style={{position: 'absolute', left: 0, right: 0, top: 11, height: 2, background: PALETTE.chat}} />
      </div>
    ) : (
      <div style={{fontFamily: MONO, fontSize: 14, lineHeight: '24px', color: PALETTE.muted, whiteSpace: 'nowrap'}}>
        {label}
      </div>
    )}
    <div style={{flex: 1, height: 1, background: PALETTE.rule}} />
  </div>
);

const TokenNode = ({
  token, shown, decor, frame,
}: {token: Token; shown: number; decor: Decor | undefined; frame: number}): ReactElement => {
  const visible = token.text.slice(0, shown);
  const hidden = token.text.slice(shown);
  let body: ReactElement;
  if (token.kind === 'mention') {
    body = shown > 0 ? (
      <span style={{background: PALETTE.mention, color: PALETTE.mentionText, borderRadius: 4, padding: '0 4px', fontWeight: 600}}>
        {visible}
      </span>
    ) : (
      <span />
    );
  } else if (token.kind === 'u') {
    body = (
      <span style={{textDecoration: 'underline', textDecorationThickness: 2, textUnderlineOffset: 4}}>{visible}</span>
    );
  } else {
    body = <span>{visible}</span>;
  }
  const hiddenNode = hidden.length > 0 ? <span style={{color: 'transparent'}}>{hidden}</span> : null;
  if (!decor) {
    return (
      <>
        {body}
        {hiddenNode}
      </>
    );
  }

  // Stacking: highlighter (0) sits behind the text (1); the marks, circle and
  // annotation draw over it (2 to 4).
  // Marker swipe: a bar the height of cap height plus about 3 px either side,
  // behind the glyphs. Both ends are ragged in 2 px steps, with a 1 px vertical
  // offset per end (the left end sits 1 px low, the right end 1 px high).
  const hl = decor.highlight;
  const highlight = hl && frame >= hl.at ? (
    <span
      key="hl"
      style={{
        position: 'absolute', left: -3, top: 2, height: 20, zIndex: 0,
        width: `calc((100% + 6px) * ${highlightProgress(frame, hl.at)})`,
        background: hexAlpha(hl.color, hl.alpha),
        clipPath: MARKER_ENDS,
      }}
    />
  ) : null;

  const marks: ReactElement[] = [];
  const u = decor.underline;
  if (u && frame >= u.at) {
    marks.push(
      <span
        key="u"
        style={{
          position: 'absolute', left: 0, bottom: -5, height: 3, zIndex: 2,
          width: `${underlineProgress(frame, u.at) * 100}%`, background: u.color,
        }}
      />,
    );
  }
  const s = decor.strike;
  if (s && frame >= s.at) {
    marks.push(
      <span key="s-rust" style={{position: 'absolute', left: 0, right: 0, top: 11, height: 3, zIndex: 2, background: PALETTE.rust}} />,
    );
  }
  if (s && frame >= s.at + 1) {
    marks.push(
      <span key="s-orange" style={{position: 'absolute', left: 3, right: -3, top: 13, height: 3, zIndex: 2, background: PALETTE.orange}} />,
    );
  }

  const c = decor.circle;
  if (c && frame >= c.at) {
    const prog = circleProgress(frame, c.at);
    marks.push(
      <svg
        key="circle"
        viewBox="0 0 100 100"
        preserveAspectRatio="none"
        style={{
          position: 'absolute', left: -18, top: -9, width: 'calc(100% + 36px)', height: 'calc(100% + 18px)',
          overflow: 'visible', pointerEvents: 'none', zIndex: 3,
        }}
      >
        <path
          d={c.d}
          pathLength={1}
          strokeDasharray={`${prog} 1`}
          fill="none"
          stroke={PALETTE.rust}
          strokeWidth={3}
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />
      </svg>,
    );
  }

  const a = decor.annotation;
  if (a && frame >= a.at) {
    const grow = connectorProgress(frame, a.at);
    marks.push(
      <span
        key="ann-line"
        style={{position: 'absolute', left: 10, bottom: '100%', width: 2, height: 26 * grow, zIndex: 3, background: PALETTE.orange}}
      />,
    );
    if (grow >= 1) {
      marks.push(
        <span
          key="ann-dot"
          style={{position: 'absolute', left: 7, top: -3, width: 8, height: 8, borderRadius: 4, zIndex: 3, background: PALETTE.orange}}
        />,
      );
    }
    if (annotationShown(frame, a.at)) {
      marks.push(
        <span
          key="ann-card"
          style={{
            position: 'absolute', left: 0, bottom: 'calc(100% + 26px)', zIndex: 4, pointerEvents: 'none',
            fontFamily: MONO, fontSize: 15, lineHeight: '20px', color: INK, whiteSpace: 'nowrap',
            background: PANEL, border: `1px solid ${COLOR.rule}`, borderRadius: 4, padding: '6px 10px',
            boxShadow: '0 12px 30px rgba(31, 31, 31, 0.14)',
          }}
        >
          {a.text}
        </span>,
      );
    }
  }

  return (
    <span style={{position: 'relative', display: 'inline-block', isolation: 'isolate'}}>
      {highlight}
      <span style={{position: 'relative', zIndex: 1}}>
        {body}
        {hiddenNode}
      </span>
      {marks}
    </span>
  );
};

type LineProps = {
  tokens: Token[];
  visible: number[];
  decor: (ti: number) => Decor | undefined;
  frame: number;
  caretAt: number | null;
};

const Line = ({tokens, visible, decor, frame, caretAt}: LineProps): ReactElement => {
  const nodes: ReactElement[] = [];
  const caret = (key: string): ReactElement => (
    <span key={key} style={{display: 'inline-block', width: 2, height: 18, background: PALETTE.text, verticalAlign: 'middle'}} />
  );
  tokens.forEach((token, ti) => {
    if (caretAt === ti) nodes.push(caret(`caret-${ti}`));
    nodes.push(
      <TokenNode key={`t-${ti}`} token={token} shown={visible[ti] ?? token.text.length} decor={decor(ti)} frame={frame} />,
    );
  });
  if (caretAt === tokens.length) nodes.push(caret('caret-end'));
  return (
    <div style={{minHeight: LINE_H, lineHeight: `${LINE_H}px`, whiteSpace: 'pre-wrap', color: PALETTE.body, fontFamily: SANS, fontSize: 17}}>
      {nodes}
    </div>
  );
};

const seedOf = (key: string): number => Array.from(key).reduce((sum, ch) => sum + ch.charCodeAt(0), 0);

export default function AmDiscord(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const p = narrowParams<Params>(props.params);

  const server = strOr(p.server, 'OSAI');
  const channel = strOr(p.channel, '# announcements');
  const channels = Array.isArray(p.channels) && p.channels.length > 0
    ? p.channels.filter((c): c is string => typeof c === 'string')
    : ['# announcements', '# general', '# testing'];
  const sidebarOn = p.sidebar !== false;
  const fitContent = p.fit === 'content';
  const minHeight = integerIn(p.minHeight, 180, 1080, 360);
  const messages = parseMessages(p.messages);
  const dividers = parseDividers(p.dateDividers).sort((a, b) => a.at - b.at);
  const reactions = parseReactions(p.reactions);
  const joins = parseJoins(p.joins);
  const underlines = parseMarks(p.underline);
  const strikes = parseStrikes(p.strike);
  const highlights = parseHighlights(p.highlight);
  const circles = listOf<TargetSpec>(p.circle, targetOf);
  const annotations = parseAnnotations(p.annotation);
  const cameras = parseCamera(p.camera).sort((a, b) => a.at - b.at);
  const jumpFrames = integerIn(p.jumpFrames, 1, 60, 12);
  const typeStep = integerIn(p.typeStepFrames, 1, 12, 2);
  const badge = strOr(p.badge, 'RECONSTRUCTION · REACTIONS & JOINS ILLUSTRATIVE');
  const win = parseFrame(p.frame);

  // Tokens per message, line and token. Built once per render.
  const tokenLines: Token[][][] = messages.map((m) => m.lines.map((line) => tokenize(line)));

  // Decorations address a token. The first match wins unless onMessage is given.
  const decorMap = new Map<string, Decor>();
  const put = (key: string, patch: Decor): void => {
    decorMap.set(key, {...decorMap.get(key), ...patch});
  };
  for (const u of underlines) {
    const key = findToken(tokenLines, u.word, u.onMessage);
    if (key) put(key, {underline: {at: u.at, color: u.color}});
  }
  for (const s of strikes) {
    const key = findToken(tokenLines, s.word, s.onMessage);
    if (key) put(key, {strike: {at: s.at}});
  }
  for (const h of highlights) {
    const key = findToken(tokenLines, h.word, h.onMessage);
    if (key) put(key, {highlight: {at: h.at, color: h.color, alpha: h.alpha}});
  }
  for (const c of circles) {
    const key = findToken(tokenLines, c.word, c.onMessage);
    if (key) put(key, {circle: {at: c.at, d: handCirclePath(seedOf(key))}});
  }
  for (const a of annotations) {
    const key = findToken(tokenLines, a.word, a.onMessage);
    if (key) put(key, {annotation: {at: a.at, text: a.text}});
  }

  // Reactions attach to a message: explicit onMessage, else the latest message posted by startAt.
  const reactionTarget = (r: ReactionSpec): number => {
    if (r.onMessage !== null && r.onMessage < messages.length) return r.onMessage;
    let best = 0;
    messages.forEach((m, i) => {
      if (m.appearAt <= r.startAt) best = i;
    });
    return best;
  };
  const reactsFor = (index: number): ReactionSpec[] =>
    reactions.filter((r) => reactionTarget(r) === index && frame >= r.startAt);

  // Chat stack: messages, joins and dividers in time order, newest at the bottom.
  type Item =
    | {kind: 'message'; at: number; index: number}
    | {kind: 'join'; at: number; index: number}
    | {kind: 'divider'; at: number; index: number};
  const rank = (item: Item): number => (item.kind === 'divider' ? 0 : item.kind === 'message' ? 1 : 2);
  const items: Item[] = [
    ...messages.map((m, index) => ({kind: 'message' as const, at: m.appearAt, index})),
    ...joins.map((j, index) => ({kind: 'join' as const, at: j.at, index})),
    ...dividers.map((d, index) => ({kind: 'divider' as const, at: d.at, index})),
  ].sort((a, b) => a.at - b.at || rank(a) - rank(b));
  const visibleItems = items.filter((item) => item.at <= frame);

  // Window height. Fit "content" hugs the visible stack; otherwise the frame height.
  const sidebarW = sidebarOn ? SIDEBAR_W : 0;
  const textWidth = win.width - BORDER * 2 - RAIL_W - sidebarW - MESSAGE_TEXT_INSET;
  const headroom = annotations.length > 0 ? ANNOTATION_HEADROOM : 0;
  const itemHeight = (item: Item): number => {
    if (item.kind === 'join') return JOIN_H;
    if (item.kind === 'divider') return DIVIDER_H;
    const lines = tokenLines[item.index];
    const text = lines.reduce((sum, tokens) => sum + wrapCount(tokens.map((t) => t.text).join(''), textWidth) * LINE_H, 0);
    return MESSAGE_PAD + LINE_H + text + (reactsFor(item.index).length > 0 ? REACTION_ROW_H : 0);
  };
  const stack = visibleItems.reduce((sum, item) => sum + itemHeight(item), 0);
  const winH = fitContent ? fitHeight(stack, headroom, minHeight, win.height) : win.height;
  const winLeft = (1920 - win.width) / 2;
  const winTop = (1080 - winH) / 2;

  // Camera: the whole window steps to zoom about its focus, then sits at the frame centre.
  const cam = cameraAt(cameras, frame);
  const camFx = cam && Number.isFinite(cam.fx) ? cam.fx : win.width / 2;
  const camFy = cam && Number.isFinite(cam.fy) ? cam.fy : winH / 2;
  const cameraStyle: CSSProperties = cameras.length > 0 && cam
    ? {
        transform: `translate(${960 - (winLeft + camFx)}px, ${540 - (winTop + camFy)}px) scale(${cam.zoom})`,
        transformOrigin: `${camFx}px ${camFy}px`,
      }
    : {};

  // Composer state: the post types into the message box before it lands.
  const comp = p.composer && typeof p.composer === 'object' ? p.composer : null;
  const compIndex = comp ? Math.round(finiteNumber(comp.message, 0)) : -1;
  const compMsg = comp && compIndex >= 0 && compIndex < messages.length ? messages[compIndex] : null;
  const compStart = comp ? Math.round(finiteNumber(comp.startAt, 0)) : 0;
  const compStep = comp ? integerIn(comp.typeStepFrames, 1, 12, 1) : 1;
  const composing = compMsg !== null && frame >= compStart && frame < compMsg.appearAt;
  const pressing = composing && compMsg !== null && frame >= compMsg.appearAt - 3;
  const compFull = compMsg ? compMsg.lines.join(' ').replace(/\{u:([^}]*)\}/g, '$1') : '';
  const composeText = composing ? compFull.slice(0, Math.min(compFull.length, Math.floor((frame - compStart) / compStep) + 1)) : '';
  const caretOn = Math.floor(frame / 8) % 2 === 0;
  const keyAt = compMsg ? compMsg.appearAt - 3 : -1;
  const keyAge = frame - keyAt;
  const keyShown = compMsg !== null && comp?.key !== null && keyAge >= 0 && keyAge < 14;
  const keyX = finiteNumber(comp?.key?.x, 1400);
  const keyY = finiteNumber(comp?.key?.y, 800);

  const renderDivider = (index: number): ReactElement | null => {
    const d = dividers[index];
    const from = index > 0 ? dividers[index - 1].label : d.label;
    const jump = d.jump !== null ? d.jump : index > 0;
    const state = dividerState(d.label, d.at, frame, from, jump, jumpFrames);
    if (state.phase === 'hidden') return null;
    if (state.phase === 'landed') return <DividerRow key={`d${index}`} label={state.text} flip={null} />;
    return <DividerRow key={`d${index}`} label="" flip={{top: state.top, bottom: state.bottom}} />;
  };

  const renderMessage = (index: number): ReactElement => {
    const m = messages[index];
    const age = frame - m.appearAt;
    const total = totalChars(m.lines);
    const shown = m.typeOn ? clamp(Math.floor(age / typeStep) + 1, 0, total) : total;
    const typing = m.typeOn && shown < total;
    // Caret sits at the first token that is not fully typed, on the line that holds it.
    let consumed = 0;
    let caretLine = -1;
    let caretToken = -1;
    if (typing) {
      for (let li = 0; li < tokenLines[index].length; li += 1) {
        const tokens = tokenLines[index][li];
        const lineChars = tokens.reduce((s, t) => s + t.text.length, 0);
        if (shown <= consumed + lineChars) {
          caretLine = li;
          let within = shown - consumed;
          caretToken = tokens.length;
          for (let ti = 0; ti < tokens.length; ti += 1) {
            if (within < tokens[ti].text.length) {
              caretToken = ti;
              break;
            }
            within -= tokens[ti].text.length;
          }
          break;
        }
        consumed += lineChars;
      }
    }
    // Per-line visible counts, derived from the global shown count.
    let left = shown;
    const lineVisible = tokenLines[index].map((tokens) => {
      const counts = revealed(tokens, left);
      left -= tokens.reduce((s, t) => s + t.text.length, 0);
      return counts;
    });
    const reacts = reactsFor(index);

    return (
      <div
        key={`m${index}`}
        style={{
          display: 'flex',
          gap: 12,
          padding: `${MESSAGE_PAD}px 18px 0`,
          transform: age === 0 ? 'translateY(6px)' : undefined,
        }}
      >
        <Avatar name={m.author} color={m.avatarColor} />
        <div style={{minWidth: 0, flex: 1}}>
          <div style={{display: 'flex', alignItems: 'center', gap: 10, lineHeight: `${LINE_H}px`, whiteSpace: 'nowrap'}}>
            <span style={{fontFamily: SANS, fontWeight: 700, fontSize: 16, color: PALETTE.text}}>{m.author}</span>
            {m.tag ? (
              <span
                style={{
                  fontFamily: SANS, fontWeight: 700, fontSize: 11, lineHeight: '18px', letterSpacing: '0.04em',
                  background: PALETTE.blurple, color: PALETTE.blurpleInk, borderRadius: 4, padding: '0 6px',
                }}
              >
                {m.tag}
              </span>
            ) : null}
            {m.time ? <span style={{fontFamily: MONO, fontSize: 13, color: PALETTE.muted}}>{m.time}</span> : null}
          </div>
          {m.lines.map((_, li) => (
            <Line
              key={li}
              tokens={tokenLines[index][li]}
              visible={lineVisible[li]}
              decor={(ti) => decorMap.get(`${index}:${li}:${ti}`)}
              frame={frame}
              caretAt={typing && caretLine === li ? caretToken : null}
            />
          ))}
          {reacts.length > 0 ? (
            <div style={{display: 'flex', flexWrap: 'wrap', gap: 6, paddingTop: 6}}>
              {reacts.map((r, ri) => {
                const count = reactionCount(r.countFrom, r.countTo, r.startAt, r.stepFrames, frame);
                const scale = stampScale(frame, r.startAt);
                return (
                  <div
                    key={ri}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6, height: 30, padding: '0 10px', borderRadius: 8,
                      background: r.mine ? 'rgba(88, 101, 242, 0.15)' : PALETTE.side,
                      border: `1px solid ${r.mine ? PALETTE.blurple : PALETTE.rule}`,
                      transform: `scale(${scale})`, transformOrigin: 'left center',
                      fontFamily: SANS, fontSize: 14, fontWeight: 600, color: PALETTE.text,
                    }}
                  >
                    <Emoji value={r.emoji} />
                    <span>{count}</span>
                  </div>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>
    );
  };

  const renderJoin = (index: number): ReactElement => {
    const j = joins[index];
    const age = frame - j.at;
    return (
      <div
        key={`j${index}`}
        style={{
          display: 'flex', alignItems: 'center', gap: 12, padding: '12px 18px 0 18px',
          fontFamily: MONO, fontSize: 15, lineHeight: `${LINE_H}px`, color: PALETTE.muted,
          transform: age === 0 ? 'translateX(-6px)' : undefined,
        }}
      >
        <div style={{width: 42, display: 'flex', justifyContent: 'center', flexShrink: 0}}>
          <PixelArrow color={PALETTE.green} />
        </div>
        <span style={{color: PALETTE.text}}>{j.name}</span>
        <span>joined the server.</span>
        {j.time ? <span style={{fontSize: 13, marginLeft: 6}}>{j.time}</span> : null}
      </div>
    );
  };

  return (
    <div style={{position: 'absolute', inset: 0, pointerEvents: 'none', overflow: 'hidden'}}>
      <div
        style={{
          position: 'absolute',
          left: winLeft,
          top: winTop,
          width: win.width,
          height: winH,
          ...cameraStyle,
        }}
      >
        <div
          style={{
            position: 'absolute',
            inset: 0,
            borderRadius: win.radius,
            border: `${BORDER}px solid ${win.outline}`,
            boxShadow: `${win.shadow}px ${win.shadow}px 0 0 ${win.outline}`,
            background: PALETTE.chat,
            overflow: 'hidden',
            display: 'flex',
            boxSizing: 'border-box',
          }}
        >
          {/* Server rail */}
          <div style={{width: RAIL_W, background: PALETTE.rail, display: 'flex', flexDirection: 'column', alignItems: 'center', paddingTop: 12, gap: 12, flexShrink: 0}}>
            <div style={{width: 48, height: 48, clipPath: STEPPED, background: PALETTE.blurple, color: PALETTE.blurpleInk, fontFamily: SANS, fontWeight: 700, fontSize: 14, lineHeight: '48px', textAlign: 'center'}}>
              {server.slice(0, 2).toUpperCase()}
            </div>
            <div style={{width: 48, height: 48, clipPath: STEPPED, background: PALETTE.chat}} />
            <div style={{width: 48, height: 48, clipPath: STEPPED, background: PALETTE.chat}} />
          </div>

          {/* Channel sidebar (optional) */}
          {sidebarOn ? (
            <div style={{width: SIDEBAR_W, background: PALETTE.side, flexShrink: 0, display: 'flex', flexDirection: 'column'}}>
              <div style={{height: 48, lineHeight: '48px', padding: '0 18px', fontFamily: SANS, fontWeight: 700, fontSize: 16, color: PALETTE.text, borderBottom: `1px solid ${PALETTE.rail}`, whiteSpace: 'nowrap', overflow: 'hidden'}}>
                {server}
              </div>
              <div style={{padding: '18px 12px 0 12px'}}>
                <div style={{fontFamily: MONO, fontSize: 12, lineHeight: `${LINE_H}px`, color: PALETTE.muted, padding: '0 6px', letterSpacing: '0.08em', textTransform: 'uppercase'}}>
                  Text channels
                </div>
                {channels.map((name) => {
                  const active = name === channel;
                  return (
                    <div
                      key={name}
                      style={{
                        height: 36, lineHeight: '36px', padding: '0 10px', margin: '2px 0', borderRadius: 4,
                        background: active ? PALETTE.selected : 'transparent',
                        color: active ? PALETTE.text : PALETTE.muted,
                        fontFamily: SANS, fontWeight: 500, fontSize: 16, whiteSpace: 'nowrap', overflow: 'hidden',
                      }}
                    >
                      {name}
                    </div>
                  );
                })}
              </div>
            </div>
          ) : null}

          {/* Main */}
          <div style={{flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column'}}>
            <div style={{height: 48, lineHeight: '48px', padding: '0 18px', fontFamily: SANS, fontWeight: 700, fontSize: 16, color: PALETTE.text, borderBottom: `1px solid ${PALETTE.rail}`, flexShrink: 0, whiteSpace: 'nowrap'}}>
              {channel}
            </div>
            <div
              style={{
                flex: 1,
                minHeight: 0,
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'flex-end',
                overflow: 'hidden',
                paddingTop: headroom,
                paddingBottom: 12,
                ...(fitContent
                  ? {}
                  : {
                      WebkitMaskImage: 'linear-gradient(to bottom, transparent 0, #000 60px)',
                      maskImage: 'linear-gradient(to bottom, transparent 0, #000 60px)',
                    }),
              }}
            >
              {visibleItems.map((item) => {
                if (item.kind === 'message') return renderMessage(item.index);
                if (item.kind === 'join') return renderJoin(item.index);
                return renderDivider(item.index);
              })}
            </div>
            <div style={{padding: '12px 18px 18px', flexShrink: 0}}>
              <div
                style={{
                  height: 48, boxSizing: 'border-box', borderRadius: 8, background: PALETTE.input,
                  padding: '0 16px', lineHeight: '48px', fontFamily: SANS, fontSize: 15,
                  color: composing ? PALETTE.text : PALETTE.muted,
                  whiteSpace: 'nowrap', overflow: 'hidden',
                  display: 'flex', justifyContent: composing ? 'flex-start' : undefined,
                  outline: pressing ? `3px solid ${PALETTE.orange}` : 'none',
                  outlineOffset: -3,
                }}
              >
                {composing ? (
                  <>
                    <span style={{fontSize: 17}}>{composeText}</span>
                    {!pressing ? (
                      <span style={{display: 'inline-block', width: 3, height: 22, marginTop: 13, marginLeft: 1, background: caretOn ? PALETTE.text : 'transparent'}} />
                    ) : null}
                  </>
                ) : (
                  `Message ${channel}`
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {keyShown ? (
        <div
          style={{
            position: 'absolute', left: keyX, top: keyY + (keyAge >= 1 && keyAge < 4 ? 8 : 0),
            transform: `scale(${keyAge === 0 ? 1.25 : 1})`, transformOrigin: 'center',
            fontFamily: MONO, fontSize: 44, lineHeight: '44px', letterSpacing: '0.12em', color: INK,
            background: PANEL, border: `4px solid ${INK}`, borderRadius: 10, padding: '22px 34px',
            boxShadow: `0 ${keyAge >= 1 && keyAge < 4 ? 2 : 10}px 0 0 ${INK}`,
          }}
        >
          ENTER <span style={{color: PALETTE.orange}}>⏎</span>
        </div>
      ) : null}

      {badge ? (
        <div
          style={{
            position: 'absolute',
            right: 104,
            bottom: 44,
            fontFamily: MONO,
            fontSize: 14,
            lineHeight: '20px',
            letterSpacing: '0.08em',
            whiteSpace: 'nowrap',
            color: INK,
            background: PANEL,
            border: `1px solid ${INK}`,
            padding: '6px 12px',
          }}
        >
          {badge}
        </div>
      ) : null}
    </div>
  );
}
