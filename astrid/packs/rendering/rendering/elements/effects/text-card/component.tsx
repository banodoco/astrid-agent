import type {ReactElement} from 'react';
import {AbsoluteFill, interpolate, useCurrentFrame} from 'remotion';
import {narrowParams} from '../../../../shared/element_contracts';
import type {ElementComponentProps} from '../../../../shared/element_contracts';

// Minimal default text card. Themes are expected to override this with richer
// typography, but the builtin renders visible markup so a stripped theme still
// produces a readable caption instead of an empty frame. The shape mirrors the
// element.yaml manifest: `content` (required) and `align` (left/center/right).
type TextCardParams = {
  content?: string;
  align?: 'left' | 'center' | 'right';
  bold?: boolean;
  color?: string;
  fontFamily?: string;
  fontSize?: number;
};

const numberOrUndefined = (value: unknown): number | undefined => {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
};

const clamp01 = (value: number): number => Math.max(0, Math.min(1, value));

const clipDurationFrames = (
  clip: {hold?: unknown; from?: unknown; to?: unknown; speed?: unknown},
  fps: number,
): number => {
  if (typeof clip.hold === 'number' && Number.isFinite(clip.hold)) {
    return Math.max(1, Math.round(clip.hold * fps));
  }
  const from = numberOrUndefined(clip.from) ?? 0;
  const to = numberOrUndefined(clip.to) ?? from;
  const speed = numberOrUndefined(clip.speed) ?? 1;
  return Math.max(1, Math.round(((to - from) / speed) * fps));
};

export default function TextCard(
  props: ElementComponentProps,
): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<TextCardParams>(props.params);
  const content = typeof params.content === 'string' ? params.content : '';
  if (!content) {
    return null;
  }
  const align: 'left' | 'center' | 'right' = params.align ?? 'center';
  const clip = props.clip as {
    x?: unknown;
    y?: unknown;
    width?: unknown;
    height?: unknown;
    hold?: unknown;
    from?: unknown;
    to?: unknown;
    speed?: unknown;
  };
  const x = numberOrUndefined(clip.x);
  const y = numberOrUndefined(clip.y);
  const width = numberOrUndefined(clip.width);
  const height = numberOrUndefined(clip.height);
  const hasBounds =
    x !== undefined || y !== undefined || width !== undefined || height !== undefined;
  const fontSize = numberOrUndefined(params.fontSize) ?? (hasBounds ? 32 : 64);
  const themeColor = props.theme?.visual?.color ?? {};
  const themeType = props.theme?.visual?.type ?? {};
  const themeMotion = props.theme?.visual?.motion ?? {};
  const fg = typeof themeColor.fg === 'string' ? themeColor.fg : '#ffffff';
  const bg = typeof themeColor.bg === 'string' ? themeColor.bg : '#232329';
  const accent =
    typeof themeColor.accent === 'string' ? themeColor.accent : '#B79CE4';
  const color = typeof params.color === 'string' ? params.color : fg;
  const fontFamily =
    typeof params.fontFamily === 'string'
      ? params.fontFamily
      : typeof themeType.families?.body === 'string'
        ? themeType.families.body
        : 'Chillax, Inter, Arial, sans-serif';
  const durationFrames = clipDurationFrames(clip, props.fps);
  const fadeFrames = Math.max(
    8,
    Math.round(((numberOrUndefined(themeMotion.fadeMs) ?? 450) / 1000) * props.fps),
  );
  const intro = clamp01(frame / fadeFrames);
  const outro = clamp01((durationFrames - frame - 1) / fadeFrames);
  const motion = Math.min(intro, outro);
  const eased = 1 - Math.pow(1 - motion, 3);
  const translateX = interpolate(eased, [0, 1], [-42, 0]);
  const translateY = interpolate(eased, [0, 1], [16, 0]);
  const scaleX = interpolate(eased, [0, 1], [0.08, 1]);

  if (hasBounds) {
    const lines = content.split('\n').map((line) => line.trim()).filter(Boolean);
    const name = lines[0] ?? content;
    const title = lines[1] ?? '';
    const handle = lines[2] ?? '';
    return (
      <div
        style={{
          position: 'absolute',
          left: x ?? 0,
          top: y ?? 0,
          width: width ?? 'auto',
          height: height ?? 'auto',
          display: 'block',
          overflow: 'visible',
          color,
          fontFamily,
          opacity: eased,
          textAlign: align,
          transform: `translate(${translateX}px, ${translateY}px)`,
        }}
      >
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'stretch',
            maxWidth: '100%',
            background: `linear-gradient(90deg, ${bg}F2 0%, ${bg}D8 70%, ${bg}00 100%)`,
            borderLeft: `6px solid ${accent}`,
            borderTop: `1px solid ${accent}66`,
            padding: '16px 26px 15px 20px',
            boxShadow: '0 18px 44px rgba(0, 0, 0, 0.42)',
            transformOrigin: 'left center',
          }}
        >
          <div style={{minWidth: 0}}>
            <div
              style={{
                color,
                fontFamily,
                fontSize,
                fontWeight: params.bold ? 700 : 600,
                lineHeight: 1.02,
                letterSpacing: 0,
                whiteSpace: 'nowrap',
              }}
            >
              {name}
            </div>
            {title ? (
              <div
                style={{
                  marginTop: 8,
                  color,
                  fontFamily,
                  fontSize: Math.max(14, fontSize * 0.43),
                  fontWeight: 500,
                  letterSpacing: '0.18em',
                  lineHeight: 1.15,
                  opacity: 0.9,
                  textTransform: 'uppercase',
                  whiteSpace: 'nowrap',
                }}
              >
                {title}
              </div>
            ) : null}
            {handle ? (
              <div
                style={{
                  marginTop: 7,
                  color: accent,
                  fontFamily,
                  fontSize: Math.max(13, fontSize * 0.4),
                  fontWeight: 600,
                  letterSpacing: '0.12em',
                  lineHeight: 1.05,
                  textTransform: 'uppercase',
                  whiteSpace: 'nowrap',
                }}
              >
                {handle}
              </div>
            ) : null}
          </div>
        </div>
        <div
          style={{
            marginTop: 8,
            width: 184,
            height: 3,
            backgroundColor: accent,
            transform: `scaleX(${scaleX})`,
            transformOrigin: 'left center',
            opacity: eased * 0.92,
          }}
        />
      </div>
    );
  }

  return (
    <AbsoluteFill
      style={{
        left: x ?? 120,
        top: y ?? 120,
        width: width ?? 640,
        height: height ?? 180,
        position: 'absolute',
        justifyContent: 'center',
        color,
        fontFamily,
        fontSize,
        fontWeight: params.bold ? 700 : 400,
        fontStyle: 'normal',
        textAlign: align,
        whiteSpace: 'pre-wrap',
        lineHeight: 1.1,
        textShadow: '0 2px 4px rgba(0, 0, 0, 0.95), 0 2px 18px rgba(0, 0, 0, 0.8)',
        opacity: props.clip.opacity ?? 1,
      }}
    >
      {content}
    </AbsoluteFill>
  );
}
