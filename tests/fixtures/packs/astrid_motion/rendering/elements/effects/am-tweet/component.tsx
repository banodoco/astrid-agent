import type {CSSProperties, ReactElement} from 'react';
import {useCurrentFrame} from 'remotion';
import {COLOR, FAMILY, finiteNumber, narrowParams, type ElementComponentProps} from '../../_shared/am';

// am-tweet: a RECONSTRUCTED post card with a visible slot for the real text.
// It never invents a real person's words and never draws a likeness: the
// avatar is a generic pixel silhouette, the body defaults to a slot marker, and
// a RECONSTRUCTION chip is always on the card. Fill body/date/link only with
// the verbatim post.

type Params = {
  name?: string;
  handle?: string;
  date?: string;
  body?: string;
  link?: string;
  badge?: string;
  note?: string;
  x?: number;
  y?: number;
  width?: number;
  enter?: 'stamp' | 'cut';
  /** Frame where a marker swipe runs across the body (optional). */
  highlightAt?: number;
};

const MONO = `'${FAMILY.label}', monospace`;
const SANS = `'${FAMILY.body}', sans-serif`;
const INK = COLOR.ink;

// 8x8 generic head-and-shoulders silhouette (1 = ink-muted pixel).
const SILHOUETTE = [
  '00011000',
  '00111100',
  '00111100',
  '00011000',
  '00111100',
  '01111110',
  '11111111',
  '11111111',
];
// 7x6 pixel icons for the action row.
const ICONS: Record<string, string[]> = {
  reply: ['0111110', '1000001', '1000001', '0111110', '0010000', '0100000'],
  repost: ['0011100', '0100010', '1110010', '0100111', '0100010', '0011100'],
  like: ['0110110', '1111111', '1111111', '0111110', '0011100', '0001000'],
};

const Bitmap = ({rows, cell, color}: {rows: string[]; cell: number; color: string}): ReactElement => (
  <div style={{position: 'relative', width: rows[0].length * cell, height: rows.length * cell, flexShrink: 0}}>
    {rows.flatMap((row, y) =>
      Array.from(row).map((ch, x) =>
        ch === '1' ? (
          <div key={`${x}-${y}`} style={{position: 'absolute', left: x * cell, top: y * cell, width: cell, height: cell, background: color}} />
        ) : null,
      ),
    )}
  </div>
);

export default function AmTweet(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const p = narrowParams<Params>(props.params);
  const width = Math.max(600, finiteNumber(p.width, 1200));
  const x = finiteNumber(p.x, (1920 - width) / 2);
  const y = finiteNumber(p.y, 210);
  const name = typeof p.name === 'string' ? p.name : '[NAME]';
  const handle = typeof p.handle === 'string' ? p.handle : '@handle';
  const date = typeof p.date === 'string' ? p.date : '[DATE NEEDED]';
  const body = typeof p.body === 'string' && p.body ? p.body : '[REAL TWEET TEXT + LINK NEEDED]';
  const isSlot = !(typeof p.body === 'string' && p.body && !p.body.startsWith('['));
  const link = typeof p.link === 'string' ? p.link : '[LINK NEEDED]';
  const badge = typeof p.badge === 'string' ? p.badge : 'RECONSTRUCTION';
  const note = typeof p.note === 'string' ? p.note : 'SLOT · PASTE THE REAL POST VERBATIM';

  let scale = 1;
  let visible = true;
  if (p.enter !== 'cut') {
    if (frame === 0) scale = 1.12;
    else if (frame === 1) visible = false;
    else if (frame === 2) scale = 1.03;
  }
  if (!visible) return null;
  const caretOn = Math.floor(frame / 15) % 2 === 0;
  const hlAt = typeof p.highlightAt === 'number' ? p.highlightAt : null;
  const hl = hlAt === null ? 0 : Math.max(0, Math.min(4, frame - hlAt + 1)) / 4;

  const card: CSSProperties = {
    position: 'absolute',
    left: x,
    top: y,
    width,
    boxSizing: 'border-box',
    padding: '36px 42px 30px',
    background: COLOR.panel,
    border: `3px solid ${INK}`,
    borderRadius: 18,
    boxShadow: `12px 12px 0 0 ${INK}`,
    transform: `scale(${scale})`,
    transformOrigin: '50% 50%',
    color: INK,
  };

  return (
    <div style={{position: 'absolute', inset: 0, pointerEvents: 'none'}}>
      <div style={card}>
        <div style={{display: 'flex', alignItems: 'center', gap: 24}}>
          <div style={{width: 108, height: 108, borderRadius: 54, background: '#E5E1DA', border: `3px solid ${INK}`, display: 'flex', alignItems: 'flex-end', justifyContent: 'center', overflow: 'hidden', flexShrink: 0}}>
            <Bitmap rows={SILHOUETTE} cell={12} color="#8E897E" />
          </div>
          <div style={{minWidth: 0, flex: 1}}>
            <div style={{fontFamily: SANS, fontWeight: 700, fontSize: 44, lineHeight: '52px', whiteSpace: 'nowrap'}}>{name}</div>
            <div style={{fontFamily: SANS, fontSize: 32, lineHeight: '40px', color: COLOR.muted, whiteSpace: 'nowrap'}}>
              {handle} · <span style={{fontFamily: MONO, fontSize: 26}}>{date}</span>
            </div>
          </div>
          <div style={{alignSelf: 'flex-start', fontFamily: MONO, fontSize: 22, lineHeight: '22px', letterSpacing: '0.12em', background: COLOR.orange, color: COLOR.panel, padding: '10px 14px', whiteSpace: 'nowrap'}}>
            {badge}
          </div>
        </div>
        <div
          style={{
            position: 'relative',
            marginTop: 30,
            padding: isSlot ? '30px 30px' : '6px 0',
            border: isSlot ? `3px dashed ${COLOR.rust}` : 'none',
            fontFamily: isSlot ? MONO : SANS,
            fontSize: isSlot ? 46 : 46,
            lineHeight: 1.3,
            letterSpacing: isSlot ? '0.04em' : '0',
            color: isSlot ? COLOR.rust : INK,
          }}
        >
          {hl > 0 ? (
            <div style={{position: 'absolute', left: isSlot ? 24 : 0, top: isSlot ? 30 : 8, height: 58, width: `calc(${hl * 100}% - ${isSlot ? 48 : 0}px)`, background: 'rgba(237, 107, 35, 0.35)'}} />
          ) : null}
          <span style={{position: 'relative'}}>{body}</span>
          {isSlot ? <span style={{display: 'inline-block', width: 26, height: 46, marginLeft: 10, verticalAlign: 'middle', background: caretOn ? COLOR.rust : 'transparent'}} /> : null}
        </div>
        <div style={{marginTop: 22, fontFamily: MONO, fontSize: 24, letterSpacing: '0.06em', color: COLOR.muted}}>{link}</div>
        <div style={{marginTop: 24, paddingTop: 22, borderTop: `2px solid ${COLOR.rule}`, display: 'flex', gap: 120}}>
          {Object.keys(ICONS).map((k) => (
            <Bitmap key={k} rows={ICONS[k]} cell={6} color={COLOR.muted} />
          ))}
        </div>
      </div>
      {note ? (
        <div style={{position: 'absolute', left: x, top: y + 18, transform: 'translateY(-100%)', marginTop: -30, fontFamily: MONO, fontSize: 20, letterSpacing: '0.12em', color: INK, background: COLOR.panel, border: `1px solid ${INK}`, padding: '6px 10px', whiteSpace: 'nowrap'}}>
          {note}
        </div>
      ) : null}
    </div>
  );
}
