import type {CSSProperties, ReactElement} from 'react';
import {useRef} from 'react';
import {AbsoluteFill, Img, OffthreadVideo, useCurrentFrame, useVideoConfig} from 'remotion';
import {
  COLOR,
  FAMILY,
  clamp,
  clipFrames,
  elementSource,
  finiteNumber,
  narrowParams,
  type ElementComponentProps,
} from '../../_shared/am';
import {footageVolume} from './volume';
import {MosaicImage, mosaicBlockAt, paintMosaic, type MosaicRamp, type Rect} from '../../_shared/mosaic';

// am-footage: a REAL-FOOTAGE SLOT. It shows a filmed shot (video or still on
// clip.asset) cover-fitted to the frame, with an optional integer-free crop
// (zoom about a focus) and a slow push, and the pixelate transition (mosaicIn,
// mosaicOut) that carries real footage into the 320x180 pixel world and back.
//
// With no asset it draws the SLOT CARD instead: a deliberate, labelled frame
// that shows the shot spec (framing note, the declared face zone, the hand
// outline and palm mark), so the edit reads and the overlays can be placed
// before anything is filmed. Swapping the slot's media for the real take keeps
// every overlay landing, because overlays are placed against the declared zones.
//
// faceZone and handMark are declarations in canvas px (1920x1080). Lint reads
// them (no overlay may cover the face zone); the slot card draws them.
//
// inset places the picture in a framed rect on the `background` field instead of
// filling the frame: a real screenshot (the Discord post, a repo page, the app)
// shown as a document on the page. zoom, focus, push and the mosaic ramps work
// inside the rect; the label sits under it.

type Zone = {x: number; y: number; w: number; h: number};
type Inset = {x: number; y: number; w: number; h: number; radius?: number; shadow?: boolean | string; border?: string};
type Params = {
  src?: string;
  slot?: string;
  kind?: string;
  /** Small corner label. Default "PLACEHOLDER · REAL FOOTAGE · <slot>" when the media is a stand-in. */
  label?: string;
  /** True while the media is a stand-in (generated plate or slot card): shows the label. */
  placeholder?: boolean;
  /** Framing note shown on the slot card ("MEDIUM · chest up · face centre-left"). */
  spec?: string;
  /** Crop: zoom >= 1 about focus (fractions of the source, 0..1). */
  zoom?: number;
  focus?: {x: number; y: number};
  /** Slow push: zoom moves linearly to `to` over the clip (real footage may ease; pixels never do). */
  push?: {to: number} | null;
  mosaicIn?: MosaicRamp | null;
  mosaicOut?: MosaicRamp | null;
  faceZone?: Zone | null;
  handMark?: {x: number; y: number; size?: number} | null;
  /** Draw the face zone and hand mark over real media too (review aid). */
  guides?: boolean;
  /** Seconds into the media where this clip starts. */
  in?: number;
  volume?: number;
  /** Source pixel size when the registry has no resolution ("WxH"). */
  sourceSize?: string;
  background?: string;
  tint?: {color?: string; opacity?: number} | null;
  /** Framed rect (canvas px) for the picture on the background field. Omit to fill the frame. */
  inset?: Inset | null;
};

const SOFT_SHADOW = '0 28px 64px rgba(21, 19, 17, 0.18), 0 3px 8px rgba(21, 19, 17, 0.10)';

const insetOf = (value: unknown, width: number, height: number): (Required<Pick<Inset, 'x' | 'y' | 'w' | 'h' | 'radius'>> & {shadow: string; border: string | null}) | null => {
  if (!value || typeof value !== 'object') return null;
  const v = value as Inset;
  const w = Math.round(clamp(finiteNumber(v.w, 0), 0, width));
  const h = Math.round(clamp(finiteNumber(v.h, 0), 0, height));
  if (w < 2 || h < 2) return null;
  const shadow = v.shadow === false ? 'none' : typeof v.shadow === 'string' ? v.shadow : SOFT_SHADOW;
  return {
    x: Math.round(finiteNumber(v.x, (width - w) / 2)),
    y: Math.round(finiteNumber(v.y, (height - h) / 2)),
    w,
    h,
    radius: Math.max(0, finiteNumber(v.radius, 12)),
    shadow,
    border: typeof v.border === 'string' ? v.border : null,
  };
};

const MONO = `'${FAMILY.label}', monospace`;
const CARD_BG = '#1F1F1F';
const GUIDE = '#FFFEFA';

const parseSize = (value: unknown): {w: number; h: number} | null => {
  const m = /^(\d+)x(\d+)$/.exec(String(value ?? ''));
  return m ? {w: Number(m[1]), h: Number(m[2])} : null;
};

// Cover-fit the source into width x height, then zoom about the focus (source
// fractions). The view is clamped so the frame never shows past the source edge.
const coverRect = (
  src: {w: number; h: number},
  width: number,
  height: number,
  zoom: number,
  focus: {x: number; y: number},
): Rect => {
  const base = Math.max(width / src.w, height / src.h);
  const s = base * Math.max(1, zoom);
  const w = src.w * s;
  const h = src.h * s;
  const x = clamp(width / 2 - focus.x * w, width - w, 0);
  const y = clamp(height / 2 - focus.y * h, height - h, 0);
  return {x, y, w, h};
};

const isVideo = (type: unknown, url: string): boolean =>
  (typeof type === 'string' && type.startsWith('video')) || /\.(mp4|mov|m4v|webm|mkv)(\?|$)/i.test(url);

// An open hand, palm up, drawn as a dashed storyboard outline. Unit: palm width.
// The palm centre is (0, 0); fingers point up and to the left, thumb out right.
const HandOutline = ({cx, cy, size}: {cx: number; cy: number; size: number}): ReactElement => {
  const u = size;
  const finger = (x: number, len: number, w: number, tilt: number, key: string): ReactElement => (
    <rect
      key={key}
      x={x - w / 2}
      y={-0.42 * u - len}
      width={w}
      height={len + 0.12 * u}
      rx={w / 2}
      transform={`rotate(${tilt} ${x} ${-0.42 * u})`}
    />
  );
  return (
    <g transform={`translate(${cx} ${cy})`} fill="none" stroke={GUIDE} strokeWidth={4} strokeDasharray="14 10" opacity={0.85}>
      <rect x={-0.5 * u} y={-0.5 * u} width={u} height={1.05 * u} rx={0.28 * u} />
      {finger(-0.36 * u, 0.62 * u, 0.2 * u, -8, 'f1')}
      {finger(-0.12 * u, 0.74 * u, 0.21 * u, -3, 'f2')}
      {finger(0.12 * u, 0.7 * u, 0.21 * u, 2, 'f3')}
      {finger(0.34 * u, 0.55 * u, 0.19 * u, 8, 'f4')}
      <rect x={0.42 * u} y={-0.05 * u} width={0.62 * u} height={0.22 * u} rx={0.11 * u} transform={`rotate(-28 ${0.42 * u} ${0.06 * u})`} />
      <rect x={-0.3 * u} y={0.55 * u} width={0.6 * u} height={0.5 * u} rx={0.1 * u} strokeDasharray="6 10" />
    </g>
  );
};

const Guides = ({face, hand}: {face: Zone | null; hand: Params['handMark'] | null}): ReactElement => (
  <svg width={1920} height={1080} style={{position: 'absolute', left: 0, top: 0, overflow: 'visible', pointerEvents: 'none'}}>
    {face ? (
      <g>
        <rect x={face.x} y={face.y} width={face.w} height={face.h} fill="none" stroke={GUIDE} strokeWidth={3} strokeDasharray="18 12" opacity={0.8} />
        <ellipse cx={face.x + face.w / 2} cy={face.y + face.h * 0.42} rx={face.w * 0.3} ry={face.h * 0.36} fill="none" stroke={GUIDE} strokeWidth={3} strokeDasharray="10 10" opacity={0.6} />
        <text x={face.x + 12} y={face.y + 34} fill={GUIDE} fontFamily={MONO} fontSize={24} letterSpacing="0.12em" opacity={0.9}>FACE ZONE · KEEP CLEAR</text>
      </g>
    ) : null}
    {hand ? (
      <g>
        <HandOutline cx={hand.x} cy={hand.y} size={finiteNumber(hand.size, 300)} />
        <line x1={hand.x - 36} y1={hand.y} x2={hand.x + 36} y2={hand.y} stroke={COLOR.orange} strokeWidth={4} />
        <line x1={hand.x} y1={hand.y - 36} x2={hand.x} y2={hand.y + 36} stroke={COLOR.orange} strokeWidth={4} />
        <text x={hand.x + 48} y={hand.y + 60} fill={GUIDE} fontFamily={MONO} fontSize={22} letterSpacing="0.12em">PALM MARK</text>
      </g>
    ) : null}
  </svg>
);

const labelStyle: CSSProperties = {
  position: 'absolute',
  left: 48,
  bottom: 36,
  fontFamily: MONO,
  fontSize: 16,
  lineHeight: '16px',
  letterSpacing: '0.1em',
  textTransform: 'uppercase',
  whiteSpace: 'nowrap',
  color: COLOR.ink,
  background: COLOR.panel,
  border: `1px solid ${COLOR.ink}`,
  padding: '6px 10px',
  opacity: 0.9,
};

export default function AmFootage(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const {width, height, fps: compositionFps} = useVideoConfig();
  const p = narrowParams<Params>(props.params);
  const fps = finiteNumber(props.fps, compositionFps);
  const total = clipFrames(props.clip, fps);
  const url = elementSource(props.assetEntry, p.src);
  const slot = typeof p.slot === 'string' ? p.slot : '';
  const face = p.faceZone && typeof p.faceZone === 'object' ? p.faceZone : null;
  const hand = p.handMark && typeof p.handMark === 'object' ? p.handMark : null;
  const placeholder = p.placeholder === true || !url;
  const label = typeof p.label === 'string' ? p.label : `PLACEHOLDER · REAL FOOTAGE${slot ? ` · ${slot}` : ''}`;
  const background = p.background ?? CARD_BG;
  const canvasRef = useRef<HTMLCanvasElement>(null);

  const block = mosaicBlockAt(frame, total, p.mosaicIn, p.mosaicOut);

  // ---- Slot card: no media yet.
  if (!url) {
    const spec = typeof p.spec === 'string' ? p.spec : '';
    const kind = typeof p.kind === 'string' ? p.kind.toUpperCase() : 'REAL FOOTAGE';
    const dots: ReactElement[] = [];
    for (let gx = 96; gx < 1920; gx += 96) {
      for (let gy = 96; gy < 1080; gy += 96) {
        dots.push(<div key={`${gx}-${gy}`} style={{position: 'absolute', left: gx - 3, top: gy - 3, width: 6, height: 6, background: '#34332F'}} />);
      }
    }
    return (
      <AbsoluteFill style={{background, overflow: 'hidden'}}>
        {dots}
        <Guides face={face} hand={hand} />
        <div style={{position: 'absolute', left: 96, top: 84, fontFamily: MONO, fontSize: 30, lineHeight: '36px', letterSpacing: '0.12em', color: GUIDE}}>
          <span style={{color: COLOR.orange}}>SLOT {slot || '—'}</span> · {kind} · TO FILM
        </div>
        {spec ? (
          <div style={{position: 'absolute', left: 96, top: 132, width: 1100, fontFamily: MONO, fontSize: 22, lineHeight: '30px', letterSpacing: '0.08em', color: '#B9B4A8', textTransform: 'uppercase'}}>
            {spec}
          </div>
        ) : null}
      </AbsoluteFill>
    );
  }

  // ---- Real media (or a stand-in still) on the slot.
  const inset = insetOf(p.inset, width, height);
  // The picture box: the inset rect, or the whole frame.
  const boxW = inset ? inset.w : width;
  const boxH = inset ? inset.h : height;
  const size = parseSize((props.assetEntry as {resolution?: string} | undefined)?.resolution) ?? parseSize(p.sourceSize) ?? {w: boxW, h: boxH};
  const zoom0 = Math.max(1, finiteNumber(p.zoom, 1));
  const zoomTo = p.push && typeof p.push === 'object' ? Math.max(1, finiteNumber(p.push.to, zoom0)) : zoom0;
  const zoom = zoom0 + (zoomTo - zoom0) * clamp(frame / Math.max(1, total - 1), 0, 1);
  const focus = {x: clamp(finiteNumber(p.focus?.x, 0.5), 0, 1), y: clamp(finiteNumber(p.focus?.y, 0.5), 0, 1)};
  const rect = coverRect(size, boxW, boxH, zoom, focus);
  const video = isVideo((props.assetEntry as {type?: string} | undefined)?.type, url);
  const trimBefore = Math.max(0, Math.round(finiteNumber(p.in, 0) * fps));
  const volume = footageVolume((props.clip as {volume?: unknown} | undefined)?.volume, p.volume);
  const tint = p.tint && typeof p.tint === 'object' ? p.tint : null;
  const mediaStyle: CSSProperties = {position: 'absolute', left: rect.x, top: rect.y, width: rect.w, height: rect.h, maxWidth: 'none', maxHeight: 'none'};

  let picture: ReactElement;
  if (video) {
    picture = (
      <>
        {/* no playbackRate: clip.speed is applied by the composition's time remap */}
        <OffthreadVideo
          src={url}
          trimBefore={trimBefore > 0 ? trimBefore : undefined}
          volume={volume}
          muted={volume <= 0}
          style={{...mediaStyle, opacity: block > 0 ? 0 : 1}}
          onVideoFrame={(source) => {
            const canvas = canvasRef.current;
            const ctx = canvas?.getContext('2d');
            if (ctx && block > 0) paintMosaic(ctx, source, rect, block, boxW, boxH, background);
          }}
        />
        {block > 0 ? (
          <canvas ref={canvasRef} width={boxW} height={boxH} style={{position: 'absolute', left: 0, top: 0, width: boxW, height: boxH}} />
        ) : null}
      </>
    );
  } else if (block > 0) {
    picture = <MosaicImage url={url} rect={rect} block={block} width={boxW} height={boxH} background={background} />;
  } else {
    picture = <Img src={url} crossOrigin="anonymous" style={mediaStyle} />;
  }

  const tintLayer = tint?.color ? (
    <AbsoluteFill style={{background: tint.color, opacity: clamp(finiteNumber(tint.opacity, 0.25), 0, 1)}} />
  ) : null;

  if (inset) {
    // The label hangs under the inset's left edge, or sits inside it when there is no room below.
    const below = inset.y + inset.h + 18 + 30 <= height;
    const insetLabel: CSSProperties = below
      ? {...labelStyle, left: inset.x, top: inset.y + inset.h + 18, bottom: undefined}
      : {...labelStyle, left: inset.x + 16, top: inset.y + inset.h - 46, bottom: undefined};
    const ring = inset.border ? `0 0 0 1px ${inset.border}` : null;
    return (
      <AbsoluteFill style={{background, overflow: 'hidden'}}>
        <div
          style={{
            position: 'absolute',
            left: inset.x,
            top: inset.y,
            width: inset.w,
            height: inset.h,
            overflow: 'hidden',
            borderRadius: inset.radius,
            boxShadow: [ring, inset.shadow === 'none' ? null : inset.shadow].filter(Boolean).join(', ') || 'none',
            background,
          }}
        >
          {picture}
          {tintLayer}
        </div>
        {p.guides ? <Guides face={face} hand={hand} /> : null}
        {placeholder && label ? <div style={insetLabel}>{label}</div> : null}
      </AbsoluteFill>
    );
  }

  return (
    <AbsoluteFill style={{background, overflow: 'hidden'}}>
      {picture}
      {tintLayer}
      {p.guides ? <Guides face={face} hand={hand} /> : null}
      {placeholder && label ? <div style={labelStyle}>{label}</div> : null}
    </AbsoluteFill>
  );
}
