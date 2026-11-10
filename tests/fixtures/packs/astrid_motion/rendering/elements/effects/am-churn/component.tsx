import type {CSSProperties, ReactElement} from 'react';
import {useCurrentFrame} from 'remotion';
import {COLOR, FAMILY, finiteNumber, integerIn, narrowParams, type ElementComponentProps} from '../../_shared/am';
import {FlapStrip, flapChars, flapStripWidth, snapPx} from '../am-flap/flap';
import {
  barCells,
  buildLedger,
  buildTimeline,
  counterState,
  dayPosition,
  formatCount,
  formatDate,
  heartBins,
  lastDayChange,
  lastStampChange,
  visibleBin,
  withDays,
  type ChurnFreeze,
  type ChurnHighlight,
  type ChurnKey,
  type ChurnRow,
  type ChurnTotals,
} from './ledger';

// am-churn: the ledger of obsession. Real git totals count up against a
// timeline, real commit subjects stamp into a column as their day is reached,
// and a heartbeat of added and deleted lines grows alongside. All data comes in
// through params; the pack has no project-history or filesystem authority.

type ChurnParams = {
  rows?: ChurnRow[];
  highlights?: ChurnHighlight[];
  totals?: ChurnTotals;
  startDate?: string;
  endDate?: string;
  timeline?: ChurnKey[];
  freezeAt?: ChurnFreeze | null;
  footnote?: string;
  variant?: 'paper' | 'dark';
  width?: number;
  height?: number;
  x?: number;
  y?: number;
  binDays?: number;
  scrollFrames?: number;
  layout?: 'full' | 'panel';
};

// ---- Grid (1920x1080, 12 columns, 96 px margins, 24 px gutters) -----------
const W = 1920;
const H = 1080;
const MARGIN = 96;
const COL_X = (col: number): number => MARGIN + (col - 1) * 146;
const COL3 = 414;
const CENTRE_X = COL_X(7);
const RIGHT_X = COL_X(10);
const LABEL_TOP = 372;
const LIST_TOP = 408;
const LIST_BOTTOM = 912;
const ROW_H = 84;
const CHART_MID = 660;
const CHART_CELLS = 40;
const BAR_PITCH = 12;
const BAR_W = 6;
const COUNTER_SIZE = 128;
const DATE_TILE_W = 84;
const DATE_TILE_H = 120;
const DATE_GAP = 6;

// Panel layout (logical px, scaled to the box): date at 0, then the two counters.
const PANEL_SIGN_OFFSET = snapPx(COUNTER_SIZE * 0.36) + 24;
// Departure Mono's advance is 7/11 em (measured from the font: 81.45 px at 128 px). The old 0.6 em
// estimate ran 42 px short on a 9-glyph total ("3,963,147"), and the last digit was cut off.
const PANEL_DIGIT_W = Math.ceil((COUNTER_SIZE * 7) / 11);
const PANEL_WRITTEN_LABEL_TOP = 156;
const PANEL_WRITTEN_TOP = 186;
const PANEL_DELETED_LABEL_TOP = 346;
const PANEL_DELETED_TOP = 376;
const PANEL_H = PANEL_DELETED_TOP + COUNTER_SIZE;

type Palette = {
  bg: string;
  ink: string;
  muted: string;
  rule: string;
  rust: string;
  orange: string;
  tile: string;
  split: string;
  glyph: string;
  invertBg: string;
  invertInk: string;
};

const PAPER: Palette = {
  bg: COLOR.paper,
  ink: COLOR.ink,
  muted: COLOR.muted,
  rule: '#D5D0C5',
  rust: COLOR.rust,
  orange: COLOR.orange,
  tile: COLOR.ink,
  split: '#0E0D0B',
  glyph: COLOR.paper,
  invertBg: COLOR.ink,
  invertInk: COLOR.paper,
};

const DARK: Palette = {
  bg: COLOR.ink,
  ink: COLOR.paper,
  muted: '#A8A394',
  rule: '#4A483F',
  rust: '#D0632A',
  orange: COLOR.orange,
  tile: '#1B1A16',
  split: '#0A0A08',
  glyph: COLOR.paper,
  invertBg: COLOR.paper,
  invertInk: COLOR.ink,
};

const MONO = `'${FAMILY.label}', monospace`;
const LABEL: CSSProperties = {
  fontFamily: MONO,
  fontSize: 18,
  lineHeight: 1,
  letterSpacing: '0.12em',
  textTransform: 'uppercase',
  whiteSpace: 'nowrap',
  position: 'absolute',
};

// Counter: a pixel sign, then the digits. Digits that changed since the
// previous frame tick orange.
const Counter = ({
  text,
  prev,
  sign,
  color,
  accent,
  top,
  left,
}: {
  text: string;
  prev: string;
  sign: '+' | '-';
  color: string;
  accent: string;
  top: number;
  left: number;
}): ReactElement => {
  const size = COUNTER_SIZE;
  const signBox = snapPx(size * 0.36);
  const bar = 12;
  const width = Math.max(text.length, prev.length);
  const now = text.padStart(width, ' ');
  const before = prev.padStart(width, ' ');
  // Only the three least significant cells tick orange, so fast frames stay calm.
  const tickFrom = now.length - 3;
  const signTop = Math.round(size * 0.5 - signBox / 2);
  return (
    <div style={{position: 'absolute', left, top, height: size, display: 'flex', alignItems: 'flex-start'}}>
      <div style={{position: 'relative', width: signBox, height: size, flex: `0 0 ${signBox}px`, marginRight: 24}}>
        <div style={{position: 'absolute', left: 0, top: signTop + (signBox - bar) / 2, width: signBox, height: bar, background: color}} />
        {sign === '+' ? (
          <div style={{position: 'absolute', left: (signBox - bar) / 2, top: signTop, width: bar, height: signBox, background: color}} />
        ) : null}
      </div>
      <div style={{fontFamily: MONO, fontSize: size, lineHeight: 1, color, display: 'flex', whiteSpace: 'pre'}}>
        {now.split('').map((char, index) => (
          <span key={index} style={{color: index >= tickFrom && char !== before[index] && char !== ' ' ? accent : color}}>
            {char === ' ' ? ' ' : char}
          </span>
        ))}
      </div>
    </div>
  );
};

// Stamp row: date, real subject, then repo, sha and the line counts.
const StampRow = ({
  h,
  top,
  visible,
  inverted,
  palette,
}: {
  h: ChurnHighlight;
  top: number;
  visible: boolean;
  inverted: boolean;
  palette: Palette;
}): ReactElement => {
  const ink = inverted ? palette.invertInk : palette.ink;
  const muted = inverted ? palette.invertInk : palette.muted;
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        top,
        width: COL3,
        height: ROW_H,
        boxSizing: 'border-box',
        padding: '12px 12px 0 12px',
        background: inverted ? palette.invertBg : 'transparent',
        borderBottom: inverted ? 'none' : `1px solid ${palette.rule}`,
        visibility: visible ? 'visible' : 'hidden',
        fontFamily: MONO,
      }}
    >
      <div style={{fontSize: 14, lineHeight: 1, color: muted, letterSpacing: '0.08em', whiteSpace: 'nowrap'}}>
        {formatDate(h.date)}
      </div>
      <div
        style={{
          fontSize: 20,
          lineHeight: '24px',
          marginTop: 8,
          color: ink,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}
      >
        {h.subject}
      </div>
      <div style={{fontSize: 14, lineHeight: 1, marginTop: 8, color: muted, whiteSpace: 'nowrap'}}>
        {h.repo} · {h.sha} ·{' '}
        <span style={{color: inverted ? palette.invertInk : palette.ink}}>+{formatCount(h.added)}</span>{' '}
        <span style={{color: inverted ? palette.invertInk : palette.rust}}>−{formatCount(h.deleted)}</span>
      </div>
    </div>
  );
};

// Spotlight: a floating card with an orange connector and dot ends, joined to
// the spotlit row. The connector draws in on 6 px steps.
const SpotCard = ({
  spot,
  rowMidY,
  drawn,
  label,
  palette,
}: {
  spot: ChurnHighlight;
  rowMidY: number;
  drawn: number;
  label: string;
  palette: Palette;
}): ReactElement => {
  const cardW = 480;
  const cardH = 132;
  const cardX = CENTRE_X - 60 - cardW;
  const cardY = snapPx(rowMidY - cardH / 2);
  const dotY = snapPx(rowMidY - 3);
  const linkLen = Math.max(6, Math.min(60, drawn));
  return (
    <>
      <div style={{position: 'absolute', left: CENTRE_X - linkLen, top: dotY, width: linkLen, height: 6, background: palette.orange}} />
      <div style={{position: 'absolute', left: cardX + cardW, top: dotY, width: 6, height: 6, background: palette.orange}} />
      <div style={{position: 'absolute', left: CENTRE_X - 6, top: dotY, width: 6, height: 6, background: palette.orange}} />
      <div
        style={{
          position: 'absolute',
          left: cardX,
          top: cardY,
          width: cardW,
          height: cardH,
          boxSizing: 'border-box',
          background: COLOR.panel,
          border: `2px solid ${COLOR.ink}`,
          padding: '18px 24px',
          fontFamily: MONO,
          color: COLOR.ink,
        }}
      >
        <div style={{fontSize: 16, lineHeight: 1, letterSpacing: '0.12em', textTransform: 'uppercase', color: COLOR.orange}}>
          {label}
        </div>
        <div style={{fontSize: 22, lineHeight: '26px', marginTop: 12, overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis'}}>
          {spot.subject}
        </div>
        <div style={{fontSize: 14, lineHeight: 1, marginTop: 12, color: COLOR.muted, whiteSpace: 'nowrap'}}>
          {formatDate(spot.date)} · {spot.repo} · {spot.sha} · +{formatCount(spot.added)} −{formatCount(spot.deleted)}
        </div>
      </div>
    </>
  );
};

const TRACK_X0 = 1000;
const TRACK_X1 = 1824;
const TRACK_W = TRACK_X1 - TRACK_X0;
const TRACK_Y = 240;
const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];

export default function AmChurn(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<ChurnParams>(props.params);
  const rows = Array.isArray(params.rows) && params.rows.length > 0 ? params.rows : [];
  if (rows.length === 0) return null;
  const totals: ChurnTotals = {
    commits: finiteNumber(params.totals?.commits, 0),
    added: finiteNumber(params.totals?.added, 0),
    deleted: finiteNumber(params.totals?.deleted, 0),
  };
  const palette = params.variant === 'dark' ? DARK : PAPER;
  const ledger = buildLedger(rows, totals);
  const highlights = withDays(Array.isArray(params.highlights) ? params.highlights : [], ledger);
  const lastDate = ledger.days[ledger.days.length - 1];
  const timeline =
    Array.isArray(params.timeline) && params.timeline.length > 0
      ? params.timeline
      : [
          {atFrame: 0, date: ledger.days[0]},
          {atFrame: 300, date: lastDate},
        ];
  const freeze = params.freezeAt ?? null;
  const spec = buildTimeline(ledger, timeline, freeze, highlights);
  const maxDay = ledger.days.length - 1;
  const position = (f: number): number => dayPosition(spec, f, maxDay);

  const xOfDay = (d: number): number => TRACK_X0 + Math.round(((d / Math.max(1, maxDay)) * TRACK_W) / 6) * 6;
  const monthTicks = ledger.days
    .map((iso, i) => ({i, label: MONTHS[Number(iso.slice(5, 7)) - 1], first: iso.endsWith('-01') || i === 0}))
    .filter((t) => t.first);
  const freezeDay = spec.freeze ? spec.freeze.day : null;
  const pos = position(frame);
  const state = counterState(ledger, pos);
  const before = counterState(ledger, position(Math.max(0, frame - 1)));
  const day = state.day;

  // Date readout: the am-flap drum, flipping to each new day.
  const change = lastDayChange(position, frame, 40);
  const nowText = formatDate(ledger.days[day]);
  const dateChars = change
    ? flapChars(
        [
          {text: formatDate(ledger.days[change.prevDay]), at: 0},
          {text: nowText, at: change.changeFrame},
        ],
        frame,
        {width: 8, stagger: 2, step: 2},
      )
    : nowText.split('');
  const dateStripW = flapStripWidth(8, DATE_TILE_W, DATE_GAP);

  // Stamps: the list scrolls one row per arrival, in 6 px steps.
  const scrollWindow = integerIn(params.scrollFrames, 2, 40, 14);
  const scroll = lastStampChange(highlights, position, frame, scrollWindow);
  const scrollOffset = snapPx((scroll.delta * ROW_H * (1 - (scroll.age + 1) / scrollWindow)));
  const maxRows = Math.floor((LIST_BOTTOM - LIST_TOP) / ROW_H) + 1;
  const arrived = highlights
    .map((h, index) => ({h, index}))
    .filter(({h}) => (h.day ?? 0) <= day);
  const shown = arrived.slice(-maxRows);
  const newest = arrived.length - 1;
  const newestIsFresh = scroll.delta > 0 && scroll.age < 2;

  // Spotlight: the freeze card shows while the timeline holds.
  const spotIndex = freeze
    ? highlights.findIndex((h) => {
        const wanted = freeze.highlight ?? '';
        return wanted !== '' && (h.sha.startsWith(wanted) || wanted.startsWith(h.sha));
      })
    : -1;
  const freezeAt = freeze ? finiteNumber(freeze.atFrame, 0) : 0;
  const holdFrames = freeze ? integerIn(freeze.holdFrames, 0, 2000, 90) : 0;
  const cardDelay = freeze ? integerIn(freeze.cardDelay, 0, 120, 8) : 0;
  const spotSlot = spotIndex >= 0 ? newest - arrived.findIndex(({index}) => index === spotIndex) : -1;
  const spotVisible = spotIndex >= 0 && spotSlot >= 0 && spotSlot < maxRows && arrived.some(({index}) => index === spotIndex);
  const cardOn = spotVisible && frame >= freezeAt + cardDelay && frame < freezeAt + holdFrames;
  const spotRowTop = LIST_BOTTOM - (spotSlot + 1) * ROW_H + scrollOffset;
  const spotMidY = spotVisible ? spotRowTop + ROW_H / 2 : 0;

  // Heartbeat: bins across column 10, one common sqrt scale in 6 px cells.
  const binDays = integerIn(params.binDays, 1, 60, Math.ceil(ledger.days.length / 34));
  const bins = heartBins(ledger, binDays);
  const maxBin = bins.reduce((m, bin) => Math.max(m, bin.added, bin.deleted), 0);
  const currentBin = Math.floor(day / binDays);

  const addedText = formatCount(state.added);
  const deletedText = formatCount(state.deleted);
  const beforeAdded = formatCount(before.added);
  const beforeDeleted = formatCount(before.deleted);
  const width = finiteNumber(params.width, W);
  const height = finiteNumber(params.height, H);
  const x = finiteNumber(params.x, 0);
  const y = finiteNumber(params.y, 0);

  if (params.layout === 'panel') {
    // Panel: date readout and the two counters only, scaled whole into the box
    // on a transparent ground. Natural size is fixed from the totals so the
    // scale does not change from frame to frame.
    // CONTAIN: the natural box is as wide as the widest total really is (plus its sign), and the panel
    // scales by the smaller of the two fits, so neither a 7-digit total nor the date is ever cropped.
    const digits = Math.max(formatCount(totals.added).length, formatCount(totals.deleted).length);
    const natW = Math.max(dateStripW, PANEL_SIGN_OFFSET + digits * PANEL_DIGIT_W);
    const scale = Math.min(width / natW, height / PANEL_H);
    const left = (width - natW * scale) / 2;
    const top = (height - PANEL_H * scale) / 2;
    return (
      <div style={{position: 'absolute', left: x, top: y, width, height, overflow: 'hidden', color: palette.ink, fontFamily: MONO}}>
        <div style={{position: 'absolute', left, top, width: natW, height: PANEL_H, transform: `scale(${scale})`, transformOrigin: '0 0'}}>
          <div style={{position: 'absolute', left: 0, top: 0, width: dateStripW}}>
            <FlapStrip
              chars={dateChars}
              tileW={DATE_TILE_W}
              tileH={DATE_TILE_H}
              gap={DATE_GAP}
              color={palette.glyph}
              tileColor={palette.tile}
              accentIndex={[]}
              accentColor={palette.orange}
              splitColor={palette.split}
              fontSize={72}
            />
          </div>
          <div style={{...LABEL, left: 0, top: PANEL_WRITTEN_LABEL_TOP, color: palette.muted}}>LINES WRITTEN</div>
          <Counter text={addedText} prev={beforeAdded} sign="+" color={palette.ink} accent={palette.orange} top={PANEL_WRITTEN_TOP} left={0} />
          <div style={{...LABEL, left: 0, top: PANEL_DELETED_LABEL_TOP, color: palette.muted}}>LINES DELETED</div>
          <Counter text={deletedText} prev={beforeDeleted} sign="-" color={palette.rust} accent={palette.orange} top={PANEL_DELETED_TOP} left={0} />
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        position: 'absolute',
        left: x,
        top: y,
        width,
        height,
        overflow: 'hidden',
        background: palette.bg,
        color: palette.ink,
        fontFamily: MONO,
      }}
    >
      {/* Header */}
      <div style={{...LABEL, left: MARGIN, top: MARGIN, color: palette.ink}}>
        <span style={{color: palette.orange}}>01.</span>&nbsp;&nbsp;THE LEDGER
      </div>
      <div style={{...LABEL, right: MARGIN, top: MARGIN, color: palette.muted}}>
        {ledger.days.length} DAYS · {formatCount(totals.commits)} COMMITS
      </div>
      <div style={{position: 'absolute', left: MARGIN, right: MARGIN, top: 132, height: 2, background: palette.ink}} />

      {/* Date readout */}
      <div style={{...LABEL, left: MARGIN, top: 168, color: palette.muted}}>ENTRY DATE</div>
      <div style={{position: 'absolute', left: MARGIN, top: 204, width: dateStripW}}>
        <FlapStrip
          chars={dateChars}
          tileW={DATE_TILE_W}
          tileH={DATE_TILE_H}
          gap={DATE_GAP}
          color={palette.glyph}
          tileColor={palette.tile}
          accentIndex={[]}
          accentColor={palette.orange}
          splitColor={palette.split}
          fontSize={72}
        />
      </div>
      <div style={{...LABEL, left: MARGIN + dateStripW + 36, top: 204, color: palette.muted, lineHeight: '28px'}}>
        DAY {String(day + 1).padStart(3, '0')}
        <br />
        OF {ledger.days.length}
      </div>

      {/* Timeline track: the whole window, month ticks, the day reached and the freeze. */}
      <div style={{position: 'absolute', left: TRACK_X0, top: TRACK_Y, width: TRACK_W, height: 6, background: palette.rule}} />
      <div style={{position: 'absolute', left: TRACK_X0, top: TRACK_Y, width: Math.max(0, xOfDay(day) - TRACK_X0), height: 6, background: palette.ink}} />
      {monthTicks.map(({i, label}) => (
        <div key={i}>
          <div style={{position: 'absolute', left: xOfDay(i), top: TRACK_Y - 6, width: 2, height: 18, background: palette.muted}} />
          <div style={{...LABEL, left: xOfDay(i), top: TRACK_Y - 36, color: palette.muted, fontSize: 14}}>{label}</div>
        </div>
      ))}
      {freezeDay !== null ? (
        <div style={{position: 'absolute', left: xOfDay(freezeDay) - 2, top: TRACK_Y - 12, width: 6, height: 30, background: palette.orange}} />
      ) : null}
      <div style={{position: 'absolute', left: xOfDay(day) - 6, top: TRACK_Y - 6, width: 12, height: 18, background: palette.ink}} />

      {/* Counters, columns 1-7 */}
      <div style={{...LABEL, left: MARGIN, top: LABEL_TOP, color: palette.muted}}>LINES WRITTEN</div>
      <Counter text={addedText} prev={beforeAdded} sign="+" color={palette.ink} accent={palette.orange} top={408} left={MARGIN} />
      <div style={{...LABEL, left: MARGIN, top: 588, color: palette.muted}}>LINES DELETED</div>
      <Counter text={deletedText} prev={beforeDeleted} sign="-" color={palette.rust} accent={palette.orange} top={624} left={MARGIN} />
      <div style={{...LABEL, left: MARGIN, top: 804, color: palette.muted}}>{formatCount(state.commits)} COMMITS</div>

      {/* Stamp column, columns 7-9 */}
      <div style={{...LABEL, left: CENTRE_X, top: LABEL_TOP, color: palette.muted}}>REAL COMMITS, IN ORDER</div>
      <div style={{position: 'absolute', left: CENTRE_X, top: LIST_TOP, width: COL3, height: LIST_BOTTOM - LIST_TOP, overflow: 'hidden'}}>
        {shown.map(({h, index}, j) => {
          const slot = shown.length - 1 - j;
          const top = LIST_BOTTOM - LIST_TOP - (slot + 1) * ROW_H + scrollOffset;
          const isFresh = slot === 0 && newestIsFresh;
          return (
            <StampRow
              key={`${h.sha}-${index}`}
              h={h}
              top={top}
              visible={!isFresh || scroll.age >= 2}
              inverted={index === spotIndex}
              palette={palette}
            />
          );
        })}
      </div>
      {cardOn ? (
        <SpotCard
          spot={highlights[spotIndex]}
          rowMidY={spotMidY}
          drawn={6 * (1 + Math.floor((frame - freezeAt - cardDelay) / 2))}
          label={freeze?.label ?? 'THE RENAME'}
          palette={palette}
        />
      ) : null}

      {/* Heartbeat, columns 10-12 */}
      <div style={{...LABEL, left: RIGHT_X, top: LABEL_TOP, color: palette.muted}}>HEARTBEAT · {binDays} DAYS A BAR</div>
      {bins.map((bin, i) => {
        if (day < bin.start) return null;
        const v = visibleBin(ledger, bin, day);
        const up = v.added > 0 ? barCells(v.added, maxBin, CHART_CELLS) : 0;
        const down = v.deleted > 0 ? barCells(v.deleted, maxBin, CHART_CELLS) : 0;
        const left = RIGHT_X + i * BAR_PITCH;
        return (
          <div key={i}>
            {up > 0 ? (
              <div style={{position: 'absolute', left, top: CHART_MID - 6 - up * 6, width: BAR_W, height: up * 6, background: palette.ink}} />
            ) : null}
            {down > 0 ? (
              <div style={{position: 'absolute', left, top: CHART_MID + 6, width: BAR_W, height: down * 6, background: palette.rust}} />
            ) : null}
          </div>
        );
      })}
      <div style={{position: 'absolute', left: RIGHT_X, top: CHART_MID, width: COL3, height: 2, background: palette.ink}} />
      {currentBin < bins.length ? (
        <div style={{position: 'absolute', left: RIGHT_X + currentBin * BAR_PITCH, top: CHART_MID - 2, width: BAR_W, height: 6, background: palette.orange}} />
      ) : null}
      <div style={{...LABEL, left: RIGHT_X, top: 920, color: palette.muted, fontSize: 14}}>{formatDate(ledger.days[0])}</div>
      <div style={{...LABEL, right: MARGIN, top: 920, color: palette.muted, fontSize: 14}}>{formatDate(lastDate)}</div>

      {/* Footer */}
      <div style={{position: 'absolute', left: MARGIN, right: MARGIN, top: 948, height: 2, background: palette.ink}} />
      <div style={{...LABEL, left: MARGIN, top: 972, color: palette.muted, fontSize: 16, letterSpacing: '0.08em'}}>
        {typeof params.footnote === 'string' ? params.footnote : ''}
      </div>
    </div>
  );
}
