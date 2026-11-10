import type {CSSProperties, ReactElement} from 'react';
import {useCurrentFrame} from 'remotion';
import {COLOR, FAMILY, finiteNumber, narrowParams, type ElementComponentProps} from '../../_shared/am';

// am-terminal: a terminal window. Command lines type in on single frames after
// a prompt; output lines land whole; a progress line counts on two-frame
// steps. Use REAL commands and real output only (it is shown as evidence).
// Optional badge chips stamp in under the window (brand stamps, outside it).
// look 'brand' (default): the ruled pixel-modernist window. look 'real': a
// realistic macOS-style dark terminal (traffic lights, JetBrains Mono, soft
// shadow, no ink borders), for films where real screens must read as real.

type Progress = {from?: number; to?: number; frames?: number; etaFrom?: number; etaTo?: number};
type LineSpec = {at?: number; text?: string; kind?: string; typeStep?: number; charsPerFrame?: number; progress?: Progress};
type Badge = {at?: number; text?: string; color?: string; ink?: string};
type Params = {
  title?: string;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  fontSize?: number;
  lines?: LineSpec[];
  badges?: Badge[];
  prompt?: string;
  /** Hide the window chrome and draw only the text (for an overlay on a plate). */
  bare?: boolean;
  /** 'brand' (default, ruled pixel window) or 'real' (a realistic dark terminal). */
  look?: 'brand' | 'real';
};

const MONO = `'${FAMILY.label}', monospace`;
const BG = '#1A1917';
const TEXT = '#E9E4D8';
const DIM = '#8E897E';
const OK = '#8FCB72';
// look 'real': a realistic dark terminal.
const REAL = {
  bg: '#1E1E1E', bar: '#2B2B2B', barText: '#9D9D9D', text: '#E3E3E3', dim: '#8C8C8C', ok: '#7FD47F',
  prompt: '#7FD47F', progress: '#E5C07B', ring: 'rgba(255, 255, 255, 0.10)',
  shadow: '0 30px 70px rgba(0, 0, 0, 0.35), 0 3px 10px rgba(0, 0, 0, 0.25)',
  lights: ['#FF5F57', '#FEBC2E', '#28C840'],
};
const REAL_MONO = "'JetBrains Mono', monospace";
const REAL_UI = "Inter, -apple-system, sans-serif";

export default function AmTerminal(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const p = narrowParams<Params>(props.params);
  const real = p.look === 'real';
  const ink = real ? {text: REAL.text, dim: REAL.dim, ok: REAL.ok, prompt: REAL.prompt, progress: REAL.progress, mono: REAL_MONO}
    : {text: TEXT, dim: DIM, ok: OK, prompt: COLOR.orange, progress: COLOR.orange, mono: MONO};
  const barH = real ? 44 : 54;
  const width = finiteNumber(p.width, 1440);
  const height = finiteNumber(p.height, 720);
  const x = finiteNumber(p.x, (1920 - width) / 2);
  const y = finiteNumber(p.y, 120);
  const fontSize = finiteNumber(p.fontSize, 40);
  const lineH = Math.round(fontSize * 1.4);
  const prompt = typeof p.prompt === 'string' ? p.prompt : '$ ';
  const lines = (Array.isArray(p.lines) ? p.lines : []).filter((l) => l && typeof l === 'object');
  const visible = lines.filter((l) => frame >= finiteNumber(l.at, 0));
  const caretOn = Math.floor(frame / 8) % 2 === 0;

  let typingIndex = -1;
  const rendered = visible.map((l, i) => {
    const at = finiteNumber(l.at, 0);
    const kind = typeof l.kind === 'string' ? l.kind : 'out';
    let text = typeof l.text === 'string' ? l.text : '';
    if (kind === 'cmd') {
      const step = Math.max(1, Math.round(finiteNumber(l.typeStep, 1)));
      const cpf = Math.max(1, Math.round(finiteNumber(l.charsPerFrame, 1)));
      const shown = Math.min(text.length, (Math.floor((frame - at) / step) + 1) * cpf);
      if (shown < text.length) typingIndex = i;
      text = text.slice(0, shown);
    }
    if (kind === 'progress' && l.progress) {
      const pr = l.progress;
      const frames = Math.max(1, Math.round(finiteNumber(pr.frames, 60)));
      const t = Math.min(1, Math.floor((frame - at) / 2) * 2 / frames);
      const n = Math.round(finiteNumber(pr.from, 0) + (finiteNumber(pr.to, 0) - finiteNumber(pr.from, 0)) * t);
      const eta = Math.max(0, Math.round(finiteNumber(pr.etaFrom, 0) + (finiteNumber(pr.etaTo, 0) - finiteNumber(pr.etaFrom, 0)) * t));
      text = text.replace('{n}', n.toLocaleString('en-US').replace(/,/g, '')).replace('{eta}', String(eta));
    }
    const color = kind === 'cmd' ? ink.text : kind === 'dim' ? ink.dim : kind === 'ok' ? ink.ok : kind === 'progress' ? ink.progress : ink.text;
    return {kind, text, color, key: i};
  });
  const idle = typingIndex < 0;
  // Top-anchored until the text overflows, then it scrolls (bottom-anchored).
  const charW = fontSize * 0.54;
  const perLine = Math.max(1, Math.floor((width - 56) / charW));
  const usedLines = rendered.reduce((n, r) => n + Math.max(1, Math.ceil((r.text.length + (r.kind === 'cmd' ? prompt.length : 0) + 1) / perLine)), 0) + (idle ? 1 : 0);
  const avail = height - (p.bare ? 0 : barH + 18) - 22;
  const overflow = usedLines * lineH > avail;

  const windowStyle: CSSProperties = p.bare
    ? {position: 'absolute', left: x, top: y, width, height}
    : real
    ? {position: 'absolute', left: x, top: y, width, height, boxSizing: 'border-box', background: REAL.bg, borderRadius: 12,
       boxShadow: `0 0 0 1px ${REAL.ring}, ${REAL.shadow}`, overflow: 'hidden'}
    : {
        position: 'absolute',
        left: x,
        top: y,
        width,
        height,
        boxSizing: 'border-box',
        background: BG,
        border: `3px solid ${COLOR.ink}`,
        borderRadius: 12,
        boxShadow: `12px 12px 0 0 ${COLOR.ink}`,
        overflow: 'hidden',
      };

  const badges = (Array.isArray(p.badges) ? p.badges : []).filter((b) => b && b.text && frame >= finiteNumber(b.at, 0));

  return (
    <div style={{position: 'absolute', inset: 0, pointerEvents: 'none'}}>
      <div style={windowStyle}>
        {p.bare ? null : real ? (
          <div style={{height: barH, position: 'relative', display: 'flex', alignItems: 'center', gap: 9, padding: '0 16px', background: REAL.bar,
                       borderBottom: '1px solid rgba(0, 0, 0, 0.45)'}}>
            {REAL.lights.map((c) => (
              <div key={c} style={{width: 14, height: 14, borderRadius: 7, background: c}} />
            ))}
            <div style={{position: 'absolute', left: 0, right: 0, textAlign: 'center', fontFamily: REAL_UI, fontSize: 17, color: REAL.barText, whiteSpace: 'nowrap'}}>
              {typeof p.title === 'string' ? p.title : 'zsh'}
            </div>
          </div>
        ) : (
          <div style={{height: 54, display: 'flex', alignItems: 'center', gap: 12, padding: '0 20px', background: '#E5E1DA', borderBottom: `3px solid ${COLOR.ink}`}}>
            {[COLOR.rust, COLOR.orange, '#C9C3B6'].map((c) => (
              <div key={c} style={{width: 18, height: 18, background: c, border: `2px solid ${COLOR.ink}`}} />
            ))}
            <div style={{marginLeft: 18, fontFamily: MONO, fontSize: 22, letterSpacing: '0.08em', color: COLOR.ink, whiteSpace: 'nowrap'}}>
              {typeof p.title === 'string' ? p.title : 'zsh'}
            </div>
          </div>
        )}
        <div
          style={{
            position: 'absolute',
            left: 28,
            right: 28,
            top: p.bare ? 0 : barH + 18,
            bottom: 22,
            display: 'flex',
            flexDirection: 'column',
            justifyContent: overflow ? 'flex-end' : 'flex-start',
            overflow: 'hidden',
            fontFamily: ink.mono,
            fontSize,
            lineHeight: `${lineH}px`,
            wordBreak: 'break-all',
            whiteSpace: 'pre-wrap',
          }}
        >
          {rendered.map((r, i) => (
            <div key={r.key} style={{color: r.color}}>
              {r.kind === 'cmd' ? <span style={{color: ink.prompt}}>{prompt}</span> : null}
              {r.text}
              {i === typingIndex ? <span style={{display: 'inline-block', width: fontSize * 0.6, height: fontSize, verticalAlign: 'text-bottom', background: ink.text}} /> : null}
            </div>
          ))}
          {idle ? (
            <div style={{color: ink.text}}>
              <span style={{color: ink.prompt}}>{prompt}</span>
              <span style={{display: 'inline-block', width: fontSize * 0.6, height: fontSize, verticalAlign: 'text-bottom', background: caretOn ? ink.text : 'transparent'}} />
            </div>
          ) : null}
        </div>
      </div>
      {badges.length > 0 ? (
        <div style={{position: 'absolute', left: x, top: y + height + 36, display: 'flex', gap: 18}}>
          {badges.map((b, i) => {
            const age = frame - finiteNumber(b.at, 0);
            const sc = age === 0 ? 1.25 : age === 1 ? 1.06 : 1;
            return (
              <div
                key={i}
                style={{
                  fontFamily: MONO,
                  fontSize: 34,
                  lineHeight: '34px',
                  letterSpacing: '0.12em',
                  padding: '14px 20px',
                  background: b.color ?? COLOR.orange,
                  color: b.ink ?? COLOR.panel,
                  border: `3px solid ${COLOR.ink}`,
                  boxShadow: `6px 6px 0 0 ${COLOR.ink}`,
                  transform: `scale(${sc})`,
                  transformOrigin: 'left center',
                  whiteSpace: 'nowrap',
                }}
              >
                {b.text}
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
