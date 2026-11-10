import type {CSSProperties, ReactElement, ReactNode} from 'react';
import {useCurrentFrame} from 'remotion';
import {COLOR, FAMILY, finiteNumber, hashUnit, integerIn, narrowParams, type ElementComponentProps} from '../../_shared/am';

// am-ui-sketch: a small app window whose interface, behaviour and data layers
// swap while it is in use. Shapes only (no images). A change flickers the
// changed region in 6 px blocks for two frames and throws a few orange sparks.

type Layer = 'A' | 'B' | 'C';
type SketchState = {at: number; interface?: Layer; behaviour?: Layer; data?: Layer};
type Prompt = {text: string; at: number};
type Params = {
  width?: number;
  height?: number;
  title?: string;
  states?: SketchState[];
  prompt?: Prompt | null;
  allAt?: number | null;
  seed?: number;
  /** Group position in frame px. Default: centred in width x height. */
  x?: number;
  y?: number;
  /** Integer scale of the group, 1 or 2. Default: whichever is closest to 70% of the frame height. */
  scale?: 1 | 2;
  /** Pixel arrow cursor. Path points are window px (border included) at absolute frames. */
  cursor?: CursorSpec | null;
};

type Region = 'interface' | 'behaviour' | 'data';
type Values = Record<Region, Layer>;

const DEFAULT_STATES: SketchState[] = [
  {at: 0, interface: 'A', behaviour: 'A', data: 'A'},
  {at: 48, interface: 'B'},
  {at: 96, behaviour: 'B'},
  {at: 144, data: 'B'},
  {at: 204, interface: 'C'},
  {at: 240, behaviour: 'C'},
  {at: 288, data: 'C'},
];

const DEFAULT_PROMPT_TEXT = 'make the sidebar go right, the button a slider, and the table into cards';

// ---- Geometry (window-local px; the body sits 2 px in and 50 px down) -------
const W = 960;
const H = 636;
const BODY_X = 2;
const BODY_Y = 50;
const BODY_W = W - 4;
const BODY_H = H - 52;
const BLOCK = 6;
const SIDEBAR = 192;
const PAD = 24;
// The group is the window plus the prompt bar below it. At scale 1 it is 756 px,
// 70% of a 1080 px frame.
const BUBBLE_GAP = 24;
const BUBBLE_H = 96;
const GROUP_H = H + BUBBLE_GAP + BUBBLE_H;
const FRAME_H = 1080;
const snap6 = (value: number): number => Math.round(value / BLOCK) * BLOCK;

const INK = COLOR.ink;
const PANEL = COLOR.panel;
const SAND = '#E5E1DA';
const SIDE = '#EFEBE3';
const MUTED = COLOR.muted;
const ORANGE = COLOR.orange;
const MONO = `'${FAMILY.label}', monospace`;

type Box = {x: number; y: number; w: number; h: number};

// Layout of the main area for the interface layer.
const layout = (iface: Layer): {sidebar: 'left' | 'right' | null; main: Box; data: Box; behaviour: Box; control: Box; tabs: boolean} => {
  if (iface === 'A') {
    const main: Box = {x: SIDEBAR, y: 0, w: BODY_W - SIDEBAR, h: BODY_H};
    return {
      sidebar: 'left',
      main,
      data: {x: SIDEBAR + PAD, y: PAD, w: main.w - PAD * 2, h: 234},
      behaviour: {x: SIDEBAR + PAD, y: 288, w: main.w - PAD * 2, h: 132},
      control: {x: SIDEBAR + PAD, y: 450, w: main.w - PAD * 2, h: 96},
      tabs: false,
    };
  }
  if (iface === 'B') {
    const main: Box = {x: 0, y: 0, w: BODY_W - SIDEBAR, h: BODY_H};
    return {
      sidebar: 'right',
      main,
      data: {x: PAD, y: PAD, w: main.w - PAD * 2, h: 234},
      behaviour: {x: PAD, y: 288, w: main.w - PAD * 2, h: 132},
      control: {x: PAD, y: 450, w: main.w - PAD * 2, h: 96},
      tabs: false,
    };
  }
  const main: Box = {x: 0, y: 0, w: BODY_W, h: BODY_H};
  return {
    sidebar: null,
    main,
    data: {x: PAD, y: 72, w: main.w - PAD * 2, h: 234},
    behaviour: {x: PAD, y: 330, w: main.w - PAD * 2, h: 132},
    control: {x: PAD, y: 486, w: main.w - PAD * 2, h: 56},
    tabs: true,
  };
};

// Resolve the states into full values, carrying each layer forward.
const resolveStates = (states: SketchState[]): {at: number; values: Values}[] => {
  const sorted = [...states].sort((a, b) => a.at - b.at);
  let current: Values = {interface: 'A', behaviour: 'A', data: 'A'};
  return sorted.map((state) => {
    current = {
      interface: state.interface ?? current.interface,
      behaviour: state.behaviour ?? current.behaviour,
      data: state.data ?? current.data,
    };
    return {at: finiteNumber(state.at, 0), values: {...current}};
  });
};

// Value of a region at frame, plus when it last changed and from what.
const regionAt = (
  resolved: {at: number; values: Values}[],
  region: Region,
  frame: number,
): {value: Layer; changedAt: number | null; from: Layer | null} => {
  let value: Layer = resolved[0]?.values[region] ?? 'A';
  let changedAt: number | null = null;
  let from: Layer | null = null;
  for (let i = 0; i < resolved.length; i += 1) {
    const step = resolved[i];
    if (step.at > frame) break;
    if (i > 0 && resolved[i - 1].values[region] !== step.values[region]) {
      changedAt = step.at;
      from = resolved[i - 1].values[region];
    }
    value = step.values[region];
  }
  return {value, changedAt, from};
};

// ---- Primitives ------------------------------------------------------------

const Box = ({
  box,
  style,
  children,
}: {
  box: Box;
  style?: CSSProperties;
  children?: ReactNode;
}): ReactElement => (
  <div style={{position: 'absolute', left: box.x, top: box.y, width: box.w, height: box.h, ...style}}>{children}</div>
);

const Node = ({x, y, label, w = 120, h = 48}: {x: number; y: number; label: string; w?: number; h?: number}): ReactElement => (
  <div
    style={{
      position: 'absolute',
      left: x,
      top: y,
      width: w,
      height: h,
      boxSizing: 'border-box',
      border: `2px solid ${INK}`,
      background: PANEL,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      fontFamily: MONO,
      fontSize: 14,
      letterSpacing: '0.08em',
      color: INK,
    }}
  >
    {label}
  </div>
);

// Orthogonal pixel wire: 6 px thick segments through the given points.
const Wire = ({points}: {points: [number, number][]}): ReactElement => {
  const segs: ReactElement[] = [];
  for (let i = 0; i < points.length - 1; i += 1) {
    const [ax, ay] = points[i];
    const [bx, by] = points[i + 1];
    const left = Math.min(ax, bx);
    const top = Math.min(ay, by);
    const w = Math.max(BLOCK, Math.abs(bx - ax) + BLOCK);
    const h = Math.max(BLOCK, Math.abs(by - ay) + BLOCK);
    segs.push(
      <div key={i} style={{position: 'absolute', left: left - BLOCK / 2, top: top - BLOCK / 2, width: w, height: h, background: INK}} />,
    );
  }
  return <>{segs}</>;
};

// Layer 1: the interface (sidebar, control).
const SidebarBlock = ({side}: {side: 'left' | 'right'}): ReactElement => (
  <div
    style={{
      position: 'absolute',
      left: side === 'left' ? 0 : BODY_W - SIDEBAR,
      top: 0,
      width: SIDEBAR,
      height: BODY_H,
      background: SIDE,
      borderRight: side === 'left' ? `2px solid ${INK}` : 'none',
      borderLeft: side === 'right' ? `2px solid ${INK}` : 'none',
      boxSizing: 'border-box',
    }}
  >
    {[0, 1, 2, 3].map((i) => (
      <div key={i} style={{position: 'absolute', left: 24, top: 36 + i * 48, width: 12, height: 12, background: i === 0 ? ORANGE : INK}}>
        <div style={{position: 'absolute', left: 24, top: 0, width: 96 - i * 12, height: 12, background: INK}} />
      </div>
    ))}
    <div style={{position: 'absolute', left: 24, top: BODY_H - 72, width: 144, height: 12, background: INK}} />
  </div>
);

const TabStrip = ({labels}: {labels: string[]}): ReactElement => (
  <div style={{position: 'absolute', left: 0, top: 0, width: BODY_W, height: 48, background: SIDE, borderBottom: `2px solid ${INK}`, boxSizing: 'border-box'}}>
    {labels.map((label, i) => (
      <div
        key={label}
        style={{
          position: 'absolute',
          left: 24 + i * 144,
          top: 12,
          width: 132,
          height: 24,
          fontFamily: MONO,
          fontSize: 14,
          letterSpacing: '0.08em',
          color: INK,
          lineHeight: '24px',
          borderBottom: i === 0 ? `6px solid ${ORANGE}` : 'none',
          boxSizing: 'border-box',
          paddingLeft: 0,
        }}
      >
        {label}
      </div>
    ))}
  </div>
);

const Control = ({layer, box}: {layer: Layer; box: Box}): ReactElement => {
  if (layer === 'A') {
    // Big button, with a hard 6 px shadow.
    return (
      <Box box={box}>
        <div style={{position: 'absolute', left: 6, top: 6, width: 264, height: 72, background: INK}} />
        <div
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            width: 264,
            height: 72,
            background: ORANGE,
            border: `2px solid ${INK}`,
            boxSizing: 'border-box',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            fontFamily: MONO,
            fontSize: 24,
            letterSpacing: '0.12em',
            color: INK,
          }}
        >
          RUN
        </div>
      </Box>
    );
  }
  if (layer === 'B') {
    // Slider: track, filled part, knob.
    const knob = 360;
    return (
      <Box box={box}>
        <div style={{position: 'absolute', left: 0, top: 36, width: box.w, height: 12, background: INK}} />
        <div style={{position: 'absolute', left: 0, top: 36, width: knob, height: 12, background: ORANGE}} />
        <div
          style={{
            position: 'absolute',
            left: knob - 12,
            top: 18,
            width: 24,
            height: 48,
            background: PANEL,
            border: `2px solid ${INK}`,
            boxSizing: 'border-box',
          }}
        />
        <div style={{position: 'absolute', left: 0, top: 66, fontFamily: MONO, fontSize: 14, color: MUTED}}>0</div>
        <div style={{position: 'absolute', right: 0, top: 66, fontFamily: MONO, fontSize: 14, color: MUTED}}>100</div>
      </Box>
    );
  }
  // Two switches: the first is on.
  return (
    <Box box={box}>
      {[0, 1].map((i) => (
        <div key={i} style={{position: 'absolute', left: i * 180, top: 12, width: 96, height: 36, border: `2px solid ${INK}`, boxSizing: 'border-box', background: i === 0 ? ORANGE : PANEL}}>
          <div style={{position: 'absolute', left: i === 0 ? 48 : 6, top: 4, width: 36, height: 24, background: INK}} />
        </div>
      ))}
    </Box>
  );
};

const ROWS = [
  ['Inbox', 'list', 128, 'ok'],
  ['Drafts', 'list', 37, 'wip'],
  ['Archive', 'log', 904, 'ok'],
] as const;

// Layer 3: the data. A table (two column orders) or three cards.
const DataBlock = ({layer, box}: {layer: Layer; box: Box}): ReactElement => {
  if (layer === 'C') {
    const cardW = Math.floor((box.w - 48) / 3 / BLOCK) * BLOCK;
    return (
      <Box box={box}>
        {ROWS.map((row, i) => {
          const [name, kind, lines, status] = row;
          const bar = Math.round((lines / 904) * 15) * BLOCK;
          return (
            <div
              key={name}
              style={{
                position: 'absolute',
                left: i * (cardW + 24),
                top: 0,
                width: cardW,
                height: box.h,
                boxSizing: 'border-box',
                border: `2px solid ${INK}`,
                background: PANEL,
                padding: 18,
                fontFamily: MONO,
                color: INK,
              }}
            >
              <div style={{fontSize: 20}}>{name}</div>
              <div style={{fontSize: 14, color: MUTED, marginTop: 6}}>{kind}</div>
              <div style={{fontSize: 36, marginTop: 36}}>{lines}</div>
              <div style={{position: 'absolute', left: 18, bottom: 48, width: 180, height: 12, background: SAND}}>
                <div style={{position: 'absolute', left: 0, top: 0, width: Math.max(6, bar), height: 12, background: INK}} />
              </div>
              <div style={{position: 'absolute', left: 18, bottom: 18, width: 12, height: 12, background: status === 'ok' ? INK : ORANGE}} />
              <div style={{position: 'absolute', left: 36, bottom: 16, fontSize: 14}}>{status}</div>
            </div>
          );
        })}
      </Box>
    );
  }
  // Table. A: name, kind, lines, status. B: the same data, columns reshuffled.
  const order = layer === 'A' ? [0, 1, 2, 3] : [1, 3, 0, 2];
  const widths = layer === 'A' ? [264, 120, 144, 96] : [120, 96, 264, 144];
  const headers = ['NAME', 'KIND', 'LINES', 'STATUS'];
  const xs = widths.reduce<number[]>((acc, _w, i) => [...acc, i === 0 ? 0 : acc[i - 1] + widths[i - 1]], []);
  return (
    <Box box={box} style={{background: PANEL, border: `2px solid ${INK}`, boxSizing: 'border-box'}}>
      <div style={{position: 'absolute', left: 0, top: 0, width: box.w - 4, height: 42, background: SAND, borderBottom: `2px solid ${INK}`, boxSizing: 'border-box'}} />
      {order.map((col, c) => (
        <div key={`h${col}`} style={{position: 'absolute', left: xs[c] + 12, top: 12, fontFamily: MONO, fontSize: 14, color: MUTED, letterSpacing: '0.08em'}}>
          {headers[col]}
        </div>
      ))}
      {ROWS.map((row, r) => (
        <div key={String(row[0])}>
          {order.map((col, c) => (
            <div
              key={`${r}-${col}`}
              style={{
                position: 'absolute',
                left: xs[c] + 12,
                top: 54 + r * 48,
                fontFamily: MONO,
                fontSize: 18,
                color: INK,
              }}
            >
              {String((row as readonly (string | number)[])[col])}
            </div>
          ))}
          <div style={{position: 'absolute', left: 0, top: 96 + r * 48, width: box.w - 4, height: 2, background: '#D5D0C5'}} />
        </div>
      ))}
    </Box>
  );
};

// Layer 2: the behaviour. A node graph whose wires rewire.
const BehaviourBlock = ({layer, box}: {layer: Layer; box: Box}): ReactElement => {
  if (layer === 'A') {
    return (
      <Box box={box}>
        <Node x={24} y={42} label="IN" />
        <Node x={288} y={42} label="MODEL" />
        <Node x={552} y={42} label="OUT" />
        <Wire points={[[144, 66], [288, 66]]} />
        <Wire points={[[408, 66], [552, 66]]} />
      </Box>
    );
  }
  if (layer === 'B') {
    return (
      <Box box={box}>
        <Node x={24} y={42} label="IN" />
        <Node x={192} y={42} label="ROUTER" w={132} />
        <Node x={384} y={0} label="PATH A" />
        <Node x={384} y={84} label="PATH B" />
        <Node x={552} y={42} label="OUT" />
        <Wire points={[[144, 66], [192, 66]]} />
        <Wire points={[[324, 66], [354, 66], [354, 24], [384, 24]]} />
        <Wire points={[[324, 66], [354, 66], [354, 108], [384, 108]]} />
        <Wire points={[[504, 24], [528, 24], [528, 66], [552, 66]]} />
        <Wire points={[[504, 108], [528, 108], [528, 66]]} />
      </Box>
    );
  }
  return (
    <Box box={box}>
      <Node x={24} y={42} label="IN" />
      <Node x={192} y={42} label="MODEL" />
      <Node x={360} y={42} label="CHECK" />
      <Node x={528} y={42} label="OUT" />
      <Wire points={[[144, 66], [192, 66]]} />
      <Wire points={[[312, 66], [360, 66]]} />
      <Wire points={[[480, 66], [528, 66]]} />
      <Wire points={[[420, 90], [420, 120], [252, 120], [252, 90]]} />
    </Box>
  );
};

// Pixel-wipe flicker: hashed 6 px blocks over a region. Painted synchronously.
const Flicker = ({box, density, color, seed}: {box: Box; density: number; color: string; seed: number}): ReactElement => (
  <canvas
    ref={(node) => {
      if (!node) return;
      const ctx = node.getContext('2d');
      if (!ctx) return;
      ctx.clearRect(0, 0, box.w, box.h);
      ctx.fillStyle = color;
      const cols = Math.ceil(box.w / BLOCK);
      const rows = Math.ceil(box.h / BLOCK);
      for (let r = 0; r < rows; r += 1) {
        for (let c = 0; c < cols; c += 1) {
          if (hashUnit(seed, r * cols + c) < density) ctx.fillRect(c * BLOCK, r * BLOCK, BLOCK, BLOCK);
        }
      }
    }}
    width={box.w}
    height={box.h}
    style={{position: 'absolute', left: box.x, top: box.y, width: box.w, height: box.h}}
  />
);

// Spark burst: orange 6 px squares that step outward for three steps.
const Sparks = ({cx, cy, step, seed}: {cx: number; cy: number; step: number; seed: number}): ReactElement => {
  const dirs: [number, number][] = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [-1, -1], [1, -1], [-1, 1]];
  return (
    <>
      {dirs.map(([dx, dy], i) => {
        const jitter = hashUnit(seed, i) > 0.5 ? 0 : 1;
        const r = (step + jitter) * 12;
        const x = Math.round((cx + dx * r) / BLOCK) * BLOCK;
        const y = Math.round((cy + dy * r) / BLOCK) * BLOCK;
        return <div key={i} style={{position: 'absolute', left: x, top: y, width: BLOCK, height: BLOCK, background: ORANGE}} />;
      })}
      <div style={{position: 'absolute', left: Math.round(cx / BLOCK) * BLOCK, top: Math.round(cy / BLOCK) * BLOCK, width: BLOCK, height: BLOCK, background: ORANGE}} />
    </>
  );
};

const boxCentre = (box: Box): [number, number] => [box.x + box.w / 2, box.y + box.h / 2];

// Frame -> the region's changing state for the flicker and spark steps.
const flickerPlan = (frame: number, changedAt: number | null): {phase: 0 | 1 | null; spark: number | null} => {
  if (changedAt === null) return {phase: null, spark: null};
  const age = frame - changedAt;
  if (age < 0) return {phase: null, spark: null};
  const phase = age === 0 ? 0 : age === 1 ? 1 : null;
  const spark = age <= 3 ? Math.min(3, age + 1) : null;
  return {phase, spark};
};

// ---- Cursor ----------------------------------------------------------------
type CursorPoint = {x: number; y: number; frame: number};
type CursorSpec = {at?: number; path?: CursorPoint[]; holdFrames?: number; stepFrames?: number};

// Chunky pixel arrow: '#' outline, 'p' fill, '.' empty. One cell is one BLOCK.
const ARROW = [
  '#.........',
  '##........',
  '#p#.......',
  '#pp#......',
  '#ppp#.....',
  '#pppp#....',
  '#ppppp#...',
  '#pppppp#..',
  '#ppppppp#.',
  '#pppp#####',
  '#pp#pp#...',
  '#p#.#pp#..',
  '##..#pp#..',
  '#....#pp#.',
  '.....#pp#.',
  '......##..',
];

// Cursor position at `frame`. It holds on each point and steps toward the next
// one every stepFrames, snapped to the 6 px grid. It hides holdFrames after the
// last point.
const cursorPosition = (cursor: CursorSpec | null, frame: number): {x: number; y: number} | null => {
  if (!cursor || !Array.isArray(cursor.path) || cursor.path.length === 0) return null;
  const pts = [...cursor.path].sort((a, b) => a.frame - b.frame);
  const first = pts[0];
  const last = pts[pts.length - 1];
  const hold = integerIn(cursor.holdFrames, 0, 600, 18);
  if (frame < finiteNumber(cursor.at, first.frame)) return null;
  if (frame >= last.frame + hold) return null;
  if (frame <= first.frame) return {x: snap6(first.x), y: snap6(first.y)};
  if (frame >= last.frame) return {x: snap6(last.x), y: snap6(last.y)};
  const step = integerIn(cursor.stepFrames, 1, 12, 2);
  for (let i = 0; i < pts.length - 1; i += 1) {
    const a = pts[i];
    const b = pts[i + 1];
    if (frame >= a.frame && frame < b.frame) {
      const span = Math.max(1, b.frame - a.frame);
      const t = Math.min(1, (Math.floor((frame - a.frame) / step) * step) / span);
      return {x: snap6(a.x + (b.x - a.x) * t), y: snap6(a.y + (b.y - a.y) * t)};
    }
  }
  return {x: snap6(last.x), y: snap6(last.y)};
};

const CursorArrow = ({x, y}: {x: number; y: number}): ReactElement => {
  const cells: ReactElement[] = [];
  ARROW.forEach((row, r) => {
    row.split('').forEach((ch, c) => {
      if (ch === '.') return;
      cells.push(
        <div
          key={`${r}-${c}`}
          style={{position: 'absolute', left: x + c * BLOCK, top: y + r * BLOCK, width: BLOCK, height: BLOCK, background: ch === '#' ? INK : PANEL}}
        />,
      );
    });
  });
  return <>{cells}</>;
};

// Integer scale: the requested one, else whichever of 1 or 2 is closest to 70% of the frame.
const pickScale = (requested: unknown, frameH: number): 1 | 2 => {
  if (requested === 1 || requested === 2) return requested;
  const target = 0.7 * frameH;
  return Math.abs(GROUP_H * 2 - target) < Math.abs(GROUP_H - target) ? 2 : 1;
};

export default function AmUiSketch(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<Params>(props.params);
  const width = finiteNumber(params.width, 1920);
  const height = finiteNumber(params.height, FRAME_H);
  const seed = integerIn(params.seed, 0, 2 ** 30, 1);
  const scale = pickScale(params.scale, height);
  const groupW = W * scale;
  const groupH = GROUP_H * scale;
  const gx = typeof params.x === 'number' && Number.isFinite(params.x) ? snap6(params.x) : snap6((width - groupW) / 2);
  const gy = typeof params.y === 'number' && Number.isFinite(params.y) ? snap6(params.y) : snap6((height - groupH) / 2);
  const cursorPos = cursorPosition(params.cursor ?? null, frame);
  const states = Array.isArray(params.states) && params.states.length > 0 ? params.states : DEFAULT_STATES;
  const resolved = resolveStates(states);
  const allAt = typeof params.allAt === 'number' ? params.allAt : null;
  const inAll = allAt !== null && frame >= allAt && frame < allAt + 12;
  const allStep = inAll ? Math.floor((frame - (allAt as number)) / 2) : 0;

  const region = (name: Region) => regionAt(resolved, name, frame);
  const values: Values = {
    interface: region('interface').value,
    behaviour: region('behaviour').value,
    data: region('data').value,
  };
  if (inAll) {
    const regionsOrder: Region[] = ['interface', 'behaviour', 'data'];
    regionsOrder.forEach((name, i) => {
      values[name] = (['A', 'B', 'C'] as Layer[])[(allStep + i) % 3];
    });
  }

  const geo = layout(values.interface);
  const boxFor: Record<Region, Box> = {interface: geo.control, behaviour: geo.behaviour, data: geo.data};
  const changes = {
    interface: region('interface'),
    behaviour: region('behaviour'),
    data: region('data'),
  };

  // Which region flickers: the one whose change is in its two-frame window.
  const flickers: {region: Region; phase: 0 | 1; seed: number; box: Box}[] = [];
  const sparks: {cx: number; cy: number; step: number; seed: number}[] = [];
  (['interface', 'behaviour', 'data'] as Region[]).forEach((name, i) => {
    const plan = flickerPlan(frame, changes[name].changedAt);
    const box = name === 'interface' ? {x: 0, y: 0, w: BODY_W, h: BODY_H} : boxFor[name];
    if (plan.phase !== null) flickers.push({region: name, phase: plan.phase, seed: seed + i * 97, box});
    if (plan.spark !== null) {
      const [cx, cy] = boxCentre(box);
      sparks.push({cx, cy, step: plan.spark, seed: seed + i * 31});
    }
  });
  if (inAll && allStep >= 0) {
    const [cx, cy] = boxCentre({x: 0, y: 0, w: BODY_W, h: BODY_H});
    sparks.push({cx, cy, step: 1 + (allStep % 3), seed: seed + allStep});
  }

  // Prompt bubble: types in on frames.
  const prompt = params.prompt && typeof params.prompt === 'object' ? params.prompt : null;
  const promptText = prompt ? (typeof prompt.text === 'string' ? prompt.text : DEFAULT_PROMPT_TEXT) : '';
  const promptAt = prompt ? finiteNumber(prompt.at, 0) : 0;
  const typed = prompt ? Math.max(0, Math.min(promptText.length, Math.floor((frame - promptAt) / 2))) : 0;
  const bubbleOn = prompt !== null && frame >= promptAt - 6;
  const chipOn = prompt !== null && frame >= promptAt + promptText.length * 2 + 6;
  const caretOn = prompt !== null && Math.floor(frame / 3) % 2 === 0;

  return (
    <div style={{position: 'absolute', left: 0, top: 0, width, height, overflow: 'hidden'}}>
      <div style={{position: 'absolute', left: gx, top: gy, width: groupW, height: groupH}}>
      <div style={{position: 'absolute', left: 0, top: 0, width: W, height: GROUP_H, transform: `scale(${scale})`, transformOrigin: '0 0'}}>
      {/* Window */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: 0,
          width: W,
          height: H,
          boxSizing: 'border-box',
          border: `2px solid ${INK}`,
          background: PANEL,
          overflow: 'hidden',
        }}
      >
        <div style={{position: 'absolute', left: 0, top: 0, width: W, height: 48, background: SAND, borderBottom: `2px solid ${INK}`, boxSizing: 'border-box'}}>
          {[0, 1, 2].map((i) => (
            <div key={i} style={{position: 'absolute', left: 18 + i * 24, top: 18, width: 12, height: 12, background: INK}} />
          ))}
          <div style={{position: 'absolute', left: 120, top: 12, fontFamily: MONO, fontSize: 16, color: INK, letterSpacing: '0.08em'}}>
            {typeof params.title === 'string' ? params.title : 'untitled app'}
          </div>
        </div>
        <div style={{position: 'absolute', left: BODY_X - 2, top: BODY_Y - 2, width: BODY_W, height: BODY_H, overflow: 'hidden'}}>
          {/* Body contents in body-local coordinates */}
          <div style={{position: 'absolute', left: 0, top: 0, width: BODY_W, height: BODY_H}}>
            {geo.sidebar ? <SidebarBlock side={geo.sidebar} /> : null}
            {geo.tabs ? <TabStrip labels={['INBOX', 'DRAFTS', 'ARCHIVE']} /> : null}
            <BehaviourBlock layer={values.behaviour} box={geo.behaviour} />
            <DataBlock layer={values.data} box={geo.data} />
            <Control layer={values.interface} box={geo.control} />
            {flickers.map((f) => (
              <Flicker key={f.region} box={f.box} density={f.phase === 0 ? 0.35 : 0.2} color={f.phase === 0 ? INK : PANEL} seed={f.seed} />
            ))}
            {sparks.map((s, i) => (
              <Sparks key={i} cx={s.cx} cy={s.cy} step={s.step} seed={s.seed} />
            ))}
          </div>
        </div>
      {cursorPos ? <CursorArrow x={cursorPos.x} y={cursorPos.y} /> : null}
      </div>

      {/* Prompt bubble: "ask the LLM to change it" */}
      {bubbleOn ? (
        <div
          style={{
            position: 'absolute',
            left: 0,
            top: H + 24,
            width: W,
            height: 96,
            boxSizing: 'border-box',
            border: `2px solid ${INK}`,
            background: PANEL,
            padding: '18px 24px',
            fontFamily: MONO,
            fontSize: 18,
            lineHeight: '28px',
            color: INK,
          }}
        >
          <span style={{color: MUTED, fontSize: 14, letterSpacing: '0.12em'}}>YOU&nbsp;&nbsp;</span>
          <span>{promptText.slice(0, typed)}</span>
          <span style={{display: 'inline-block', width: 10, height: 18, background: caretOn ? INK : 'transparent', verticalAlign: 'middle', marginLeft: 2}} />
          {chipOn ? (
            <span style={{display: 'inline-block', marginLeft: 12, padding: '2px 10px', background: INK, color: PANEL, fontSize: 14, letterSpacing: '0.12em', verticalAlign: 'middle'}}>
              LLM
            </span>
          ) : null}
        </div>
      ) : null}
      </div>
      </div>
    </div>
  );
}
