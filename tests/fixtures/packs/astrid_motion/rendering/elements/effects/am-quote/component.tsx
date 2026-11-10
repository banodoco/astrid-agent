import type {CSSProperties, ReactElement} from 'react';
import {useCurrentFrame, useVideoConfig} from 'remotion';
import {COLOR, FAMILY, clamp, finiteNumber, narrowParams, spanList, type ElementComponentProps} from '../../_shared/am';

// am-quote: a quotation as an editorial document, then overruled.
// The quote can reveal word by word on the VO's word onsets (`words`, the
// builder's @words helper), a strike sweeps through it word by word on single
// frames, and a rubber stamp slams on top. The attribution line can mark one
// word (for example the year) in orange when the stamp lands.

type Params = {
  text?: string;
  attribution?: string;
  x?: number;
  y?: number;
  width?: number;
  size?: number;
  /** [start_s, end_s] per quote word (clip-relative). Word i shows from its onset. */
  words?: [number, number][];
  /** Frame where the strike starts; it crosses one word per `strikeStep` frames. */
  strikeAt?: number;
  strikeStep?: number;
  stamp?: {text?: string; at?: number; x?: number; y?: number; rotate?: number; size?: number} | null;
  /** Orange marker on one attribution word from `at`. */
  mark?: {word?: string; at?: number} | null;
  card?: boolean;
};

const MONO = `'${FAMILY.label}', monospace`;

export default function AmQuote(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const {fps: compFps} = useVideoConfig();
  const fps = finiteNumber(props.fps, compFps);
  const p = narrowParams<Params>(props.params);
  const text = typeof p.text === 'string' ? p.text : '';
  if (!text) return null;
  const x = finiteNumber(p.x, 192);
  const y = finiteNumber(p.y, 240);
  const width = finiteNumber(p.width, 1536);
  const size = finiteNumber(p.size, 88);
  const words = text.split(/\s+/).filter(Boolean);
  const spans = spanList(p.words);
  const onset = (i: number): number => (spans.length > 0 ? Math.round((spans[Math.min(i, spans.length - 1)][0]) * fps) : 0);
  const strikeAt = typeof p.strikeAt === 'number' ? p.strikeAt : null;
  const strikeStep = Math.max(1, Math.round(finiteNumber(p.strikeStep, 1)));
  const stamp = p.stamp && typeof p.stamp === 'object' && p.stamp.text ? p.stamp : null;
  const mark = p.mark && typeof p.mark === 'object' && p.mark.word ? p.mark : null;
  const attribution = typeof p.attribution === 'string' ? p.attribution : '';

  const wordStyle = (i: number): CSSProperties => ({
    position: 'relative',
    display: 'inline-block',
    marginRight: '0.24em',
    visibility: spans.length === 0 || i >= spans.length || frame >= onset(i) ? 'visible' : 'hidden',
  });

  const struck = (i: number): number => {
    if (strikeAt === null) return 0;
    const k = frame - (strikeAt + i * strikeStep);
    return k < 0 ? 0 : k === 0 ? 1 : 2; // 1: rust bar, 2: rust + orange offset bar
  };

  let stampNode: ReactElement | null = null;
  if (stamp) {
    const at = finiteNumber(stamp.at, 0);
    const age = frame - at;
    if (age >= 0) {
      const sc = age === 0 ? 1.6 : age === 1 ? 1.18 : 1;
      const jitter = age === 1 ? 6 : 0;
      const ssize = finiteNumber(stamp.size, 78);
      stampNode = (
        <div
          style={{
            position: 'absolute',
            left: finiteNumber(stamp.x, x + width * 0.42),
            top: finiteNumber(stamp.y, y + size * 1.6),
            transform: `translate(${jitter}px, ${-jitter}px) rotate(${finiteNumber(stamp.rotate, -4)}deg) scale(${sc})`,
            transformOrigin: '50% 50%',
            fontFamily: MONO,
            fontSize: ssize,
            lineHeight: 1,
            letterSpacing: '0.1em',
            color: COLOR.rust,
            border: `8px solid ${COLOR.rust}`,
            outline: `3px solid ${COLOR.rust}`,
            outlineOffset: 6,
            padding: '18px 28px 14px',
            whiteSpace: 'nowrap',
            background: 'rgba(247, 244, 237, 0.82)',
            boxShadow: `8px 8px 0 0 rgba(37, 36, 31, 0.9)`,
          }}
        >
          {stamp.text}
        </div>
      );
    }
  }

  const attrTokens = attribution.split(/(\s+)/);
  const markOn = mark !== null && frame >= finiteNumber(mark.at, 0);

  return (
    <div style={{position: 'absolute', inset: 0, pointerEvents: 'none'}}>
      <div style={{position: 'absolute', left: x, top: y, width}}>
        <div
          style={{
            fontFamily: FAMILY.display,
            fontSize: size,
            lineHeight: 1.08,
            letterSpacing: '-0.01em',
            color: COLOR.ink,
          }}
        >
          <span style={{position: 'absolute', left: -size * 0.55, top: -size * 0.12, color: COLOR.orange}}>“</span>
          {words.map((w, i) => {
            const s = struck(i);
            return (
              <span key={i} style={wordStyle(i)}>
                {w}
                {i === words.length - 1 ? <span style={{color: COLOR.orange}}>”</span> : null}
                {s > 0 ? (
                  <span style={{position: 'absolute', left: -size * 0.12, right: -size * 0.24, top: '52%', height: Math.round(size * 0.09), background: COLOR.rust}} />
                ) : null}
                {s > 1 ? (
                  <span style={{position: 'absolute', left: -size * 0.12, right: -size * 0.24, top: `calc(52% + ${Math.round(size * 0.09)}px)`, height: Math.round(size * 0.05), background: COLOR.orange}} />
                ) : null}
              </span>
            );
          })}
        </div>
        {attribution ? (
          <div style={{marginTop: size * 0.5, fontFamily: MONO, fontSize: clamp(size * 0.34, 22, 36), letterSpacing: '0.12em', textTransform: 'uppercase', color: COLOR.muted}}>
            —{' '}
            {attrTokens.map((t, i) =>
              markOn && mark && t === mark.word ? (
                <span key={i} style={{background: COLOR.orange, color: COLOR.panel, padding: '0 6px'}}>{t}</span>
              ) : (
                <span key={i}>{t}</span>
              ),
            )}
          </div>
        ) : null}
      </div>
      {stampNode}
    </div>
  );
}
