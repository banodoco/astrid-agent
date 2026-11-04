import type {CSSProperties, ReactElement} from 'react';
import {Img, useCurrentFrame} from 'remotion';
import type {ComponentCapabilityContext, PreparedComponentCapability} from '@banodoco/timeline-composition/evaluation';
import {
  LOGICAL_PX,
  elementSource,
  finiteNumber,
  integerIn,
  narrowParams,
  oneOf,
  pixelImage,
  stepStart,
  type ElementComponentProps,
} from '../../_shared/am';

// am-sprite: a transparent pixel cutout on the logical grid.
//
// Each art px is `scale` screen px (6 means one art px per logical px). The
// sprite's top-left sits at (x, y) logical px, so positions are multiples of
// LOGICAL_PX screen px. The outer box is scaled from its top-left corner, and
// the flip is applied inside it, so the sprite stays anchored on its corner.
// Keyframes, flips and slide steps update only on step boundaries.

type Keyframe = {frame: number; x: number; y: number; flipX?: boolean};
type SpriteFrames = {frameWidth: number; frameHeight: number; count: number; fps: number; start?: number; sequence?: number[]};
type Shadow = {groundY?: number; w?: number; h?: number; color?: string; opacity?: number; dx?: number; fade?: number};
type Params = {
  src?: string;
  scale?: number;
  x?: number;
  y?: number;
  flipX?: boolean;
  keyframes?: Keyframe[];
  stepFrames?: 2 | 3;
  /** Source clip-local frame for a piece cut from an existing sprite clip. */
  frameOffset?: number;
  /** Original output-frame clock for a chapter piece, before playback-rate flooring. */
  sourceClock?: {offsetFrames: number; speed: number};
  enter?: 'stamp' | 'slideIn' | 'cut';
  slideFrom?: 'left' | 'right' | 'top' | 'bottom';
  slideDistance?: number;
  frames?: SpriteFrames;
  blinkAt?: number | number[];
  blinkIndex?: number;
  /** Contact shadow on the ground line (logical px). It narrows as the sprite rises. */
  shadow?: Shadow | null;
  /** Draw the sprite as a flat ink silhouette until clip frame `until` (omit for the whole clip). */
  silhouette?: {until?: number} | boolean | null;
  /** Hard pixel outline in `color`, `px` art px wide; with `pulse` frames it blinks wide/narrow on steps. */
  outline?: {color?: string; px?: number; pulse?: number} | null;
};

const ENTERS = ['stamp', 'slideIn', 'cut'] as const;
const SIDES = ['left', 'right', 'top', 'bottom'] as const;
const SLIDE_STEPS = 4;

const spriteAt = (raw: unknown, assetEntry: ElementComponentProps['assetEntry'], frame: number, fps: number) => {
  const params = narrowParams<Params>(raw);
  const scale = integerIn(params.scale, 1, 24, 6);
  const stepFrames = integerIn(params.stepFrames, 2, 3, 2);
  const frameOffset = integerIn(params.frameOffset, 0, 10000000, 0);
  const clock = params.sourceClock;
  const animationFrame = clock && Number.isFinite(clock.offsetFrames) && Number.isFinite(clock.speed) && clock.speed > 0
    ? Math.floor((frame + clock.offsetFrames) * clock.speed)
    : frame;
  const sourceFrame = animationFrame + frameOffset;
  const sf = stepStart(sourceFrame, stepFrames) - frameOffset;
  const keyframeOrigin = clock && Number.isFinite(clock.offsetFrames) && Number.isFinite(clock.speed) && clock.speed > 0
    ? Math.floor(clock.offsetFrames * clock.speed)
    : 0;
  const keyframeStep = sf - keyframeOrigin;

  // Active keyframe: the last one at or before this step. Keyframes apply
  // as held poses, so the sprite never interpolates between them.
  let x = finiteNumber(params.x, 0);
  let y = finiteNumber(params.y, 0);
  let flipX = params.flipX === true;
  const keyframes = [...(params.keyframes ?? [])].sort((a, b) => a.frame - b.frame);
  for (const key of keyframes) {
    if (key.frame <= keyframeStep) {
      x = finiteNumber(key.x, x);
      y = finiteNumber(key.y, y);
      flipX = key.flipX ?? flipX;
    }
  }

  let visible = true;
  let dx = 0;
  let dy = 0;
  const enter = oneOf(params.enter, ENTERS, 'cut');
  if (enter === 'stamp') {
    // 2-frame flicker (on, off) then land 1 px lower on frame 2.
    if (sourceFrame < 2) {
      visible = sourceFrame === 0;
      dy = -1;
    }
  } else if (enter === 'slideIn') {
    const side = oneOf(params.slideFrom, SIDES, 'left');
    const distance = Math.max(0, finiteNumber(params.slideDistance, 24));
    const k = Math.min(SLIDE_STEPS, Math.floor(sourceFrame / stepFrames));
    const remaining = Math.round((distance * (SLIDE_STEPS - k)) / SLIDE_STEPS);
    if (side === 'left') dx = -remaining;
    if (side === 'right') dx = remaining;
    if (side === 'top') dy = -remaining;
    if (side === 'bottom') dy = remaining;
  }

  const blinks: number[] = Array.isArray(params.blinkAt)
    ? params.blinkAt
    : params.blinkAt === undefined
      ? []
      : [params.blinkAt];
  const blinking = blinks.some((at) => animationFrame >= at && animationFrame < at + 2);

  const frames = params.frames;
  let index = 0;
  if (frames && frames.count >= 1 && frames.fps > 0) {
    const tick = Math.floor((sourceFrame * frames.fps) / fps);
    const seq = Array.isArray(frames.sequence) ? frames.sequence.filter((n) => Number.isFinite(n)) : [];
    index = seq.length > 0
      ? Math.round(seq[tick % seq.length])
      : Math.round(finiteNumber(frames.start, 0)) + (tick % Math.max(1, Math.round(frames.count)));
  }
  if (blinking) {
    if (frames && typeof params.blinkIndex === 'number') {
      index = params.blinkIndex;
    } else {
      visible = false;
    }
  }

  // Sprite size in art px: the strip frame, else the registry resolution.
  const res = /^(\d+)x(\d+)$/.exec(String((assetEntry as {resolution?: string} | undefined)?.resolution ?? ''));
  const artW = frames ? Math.max(1, Math.round(frames.frameWidth)) : res ? Number(res[1]) : 0;
  const artH = frames ? Math.max(1, Math.round(frames.frameHeight)) : res ? Number(res[2]) : 0;
  const rect = {x: (x + dx) * LOGICAL_PX, y: (y + dy) * LOGICAL_PX,
    width: artW * scale, height: artH * scale};
  return {scale, x, y, dx, dy, flipX, visible, frames, index, artW, artH, rect, animationFrame};
};

const prepareSpriteScene = ({clip, assetEntry, fps}: ComponentCapabilityContext): PreparedComponentCapability => ({
  identity: JSON.stringify([clip.params, assetEntry, fps]),
  frame: frame => {
    const p = narrowParams<Params>(clip.params);
    const url = elementSource(assetEntry, p.src);
    if (!url) return {presence: 'absent', opaqueSupport: 'none',
      bounds: {status: 'unknown', reason: 'Sprite has no renderable source'}};
    const {visible, frames, artW, artH, rect} = spriteAt(p, assetEntry, frame, fps);
    // A layout rectangle says nothing about PNG alpha or successful decoding.
    const known = (frames || elementSource(assetEntry, undefined)) && artW > 0 && artH > 0 && Number.isFinite(artW) && Number.isFinite(artH)
      && !p.outline && !p.shadow;
    return {
      presence: !visible ? 'absent' : 'unknown',
      opaqueSupport: !visible ? 'none' : 'unknown',
      bounds: known
        ? {status: 'known', rect}
        : {status: 'unknown', reason: 'Sprite source dimensions or decorated bounds are not declared'},
    };
  },
});

function AmSprite(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<Params>(props.params);
  const frameOffset = integerIn(params.frameOffset, 0, 10000000, 0);
  const url = elementSource(props.assetEntry, params.src);
  if (!url) return null;
  const {scale, x, y, dx, dy, flipX, visible, frames, index, artW, artH, rect, animationFrame} = spriteAt(params, props.assetEntry, frame, props.fps);

  // Silhouette and outline are CSS filters on the art (inside the scale), so
  // offsets are art px and land on the pixel grid. drop-shadow with 0 blur is a
  // hard 1-art-px ring; four of them make an outline.
  const sil = params.silhouette;
  const silOn = sil === true || (typeof sil === 'object' && sil !== null && (sil.until === undefined || animationFrame < sil.until));
  const ol = params.outline && typeof params.outline === 'object' ? params.outline : null;
  const filters: string[] = [];
  if (silOn) filters.push('brightness(0)');
  if (ol) {
    const pulse = Math.max(0, Math.round(finiteNumber(ol.pulse, 0)));
    const wide = pulse > 0 && Math.floor((animationFrame + frameOffset) / pulse) % 2 === 1;
    const w = Math.max(1, Math.round(finiteNumber(ol.px, 1))) + (wide ? 1 : 0);
    const c = typeof ol.color === 'string' ? ol.color : '#ED6B23';
    filters.push(`drop-shadow(${w}px 0 0 ${c})`, `drop-shadow(-${w}px 0 0 ${c})`, `drop-shadow(0 ${w}px 0 ${c})`, `drop-shadow(0 -${w}px 0 ${c})`);
  }
  const artFilter: CSSProperties = filters.length > 0 ? {filter: filters.join(' ')} : {};

  const outer: CSSProperties = {
    position: 'absolute',
    left: rect.x,
    top: rect.y,
    transform: `scale(${scale})`,
    transformOrigin: '0 0',
    lineHeight: 0,
    display: visible ? 'block' : 'none',
  };
  const flip: CSSProperties = {
    transform: flipX ? 'scaleX(-1)' : undefined,
    transformOrigin: 'center center',
  };

  const shadow = params.shadow && typeof params.shadow === 'object' ? params.shadow : null;
  let shadowNode: ReactElement | null = null;
  if (shadow && visible && artW > 0) {
    // Logical px: sprite width and its feet line.
    const spriteW = (artW * scale) / LOGICAL_PX;
    const feet = y + dy + (artH * scale) / LOGICAL_PX;
    const ground = finiteNumber(shadow.groundY, feet);
    const altitude = Math.max(0, ground - feet);
    const k = Math.max(0.35, 1 - altitude / Math.max(1, finiteNumber(shadow.fade, 40)));
    const w = Math.max(2, Math.round(finiteNumber(shadow.w, spriteW * 0.7) * k));
    const h = Math.max(1, Math.round(finiteNumber(shadow.h, 3)));
    const cx = x + dx + spriteW / 2 + finiteNumber(shadow.dx, 0);
    const rows: CSSProperties[] = [];
    for (let r = 0; r < h; r += 1) {
      const t = (r + 0.5) / h - 0.5;
      const rw = Math.max(1, Math.round(w * Math.sqrt(Math.max(0, 1 - 4 * t * t))));
      rows.push({
        position: 'absolute',
        left: Math.round(cx - rw / 2) * LOGICAL_PX,
        top: Math.round(ground - h / 2 + r) * LOGICAL_PX,
        width: rw * LOGICAL_PX,
        height: LOGICAL_PX,
        background: shadow.color ?? '#1F1F1F',
      });
    }
    shadowNode = (
      <div style={{position: 'absolute', inset: 0, opacity: Math.min(1, Math.max(0, finiteNumber(shadow.opacity, 0.35)))}}>
        {rows.map((st, i) => <div key={i} style={st} />)}
      </div>
    );
  }

  if (frames) {
    const fw = artW;
    const fh = artH;
    return (
      <>
      {shadowNode}
      <div style={outer}>
        <div style={{...flip, width: fw, height: fh}}>
          <div style={{position: 'relative', width: fw, height: fh, overflow: 'hidden'}}>
            <Img
              src={url}
              crossOrigin="anonymous"
              style={{position: 'absolute', left: -index * fw, top: 0, display: 'block', maxWidth: 'none', ...pixelImage, ...artFilter}}
            />
          </div>
        </div>
      </div>
      </>
    );
  }

  return (
    <>
      {shadowNode}
      <div style={outer}>
        <div style={flip}>
          <Img src={url} crossOrigin="anonymous" style={{display: 'block', maxWidth: 'none', ...pixelImage, ...artFilter}} />
        </div>
      </div>
    </>
  );
}

export default Object.assign(AmSprite, {prepareSceneCapability: prepareSpriteScene});
