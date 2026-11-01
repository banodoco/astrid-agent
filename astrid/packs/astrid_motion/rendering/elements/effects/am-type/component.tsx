import type {CSSProperties, ReactElement} from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {
  COLOR,
  FAMILY,
  clamp,
  easeOutCubic,
  finiteNumber,
  narrowParams,
  oneOf,
  type ElementComponentProps,
} from '../../_shared/am';
import {interpolateKeys, type PositionKey} from './position-keys';

// am-type: editorial type. Type may ease (masked slide-up uses ease-out
// cubic); the typewriter reveal and the underline step on frames.

type Params = {
  text?: string;
  italicWord?: string;
  /** Colour of the italic word (default rust #A94714; use orange on charcoal, where rust is too dark). */
  italicColor?: string;
  font?: 'display' | 'label';
  size?: number;
  color?: string;
  align?: 'left' | 'center' | 'right';
  x?: number;
  y?: number;
  width?: number;
  reveal?: 'slideUp' | 'type' | 'none';
  tracking?: number;
  lineHeight?: number;
  underlineWord?: string;
  underlineAt?: number;
  xKeyframes?: PositionKey[];
  yKeyframes?: PositionKey[];
};

const SLIDE_FRAMES = 8;
const TYPE_FRAMES_PER_CHAR = 2;
const UNDERLINE_FRAMES = 6;
const UNDERLINE_PX = 6;
const FONTS = ['display', 'label'] as const;
const REVEALS = ['slideUp', 'type', 'none'] as const;
const ALIGNS = ['left', 'center', 'right'] as const;

type Run = {text: string; italic: boolean; underline: boolean; space: boolean};

// Split into word and whitespace runs. The first word matching italicWord
// (and the first matching underlineWord) is styled. Punctuation is ignored
// on BOTH sides when matching, so "tomorrow." matches "tomorrow" and an
// italicWord of "year." still matches "year".
const buildRuns = (text: string, italicWordRaw: string, underlineWordRaw: string): Run[] => {
  const cleaned = (token: string): string => token.replace(/[^\p{L}\p{N}']/gu, '').toLowerCase();
  const italicWord = cleaned(italicWordRaw);
  const underlineWord = cleaned(underlineWordRaw);
  let italicUsed = false;
  let underlineUsed = false;
  return text.split(/(\s+)/).filter((part) => part.length > 0).map((part) => {
    if (/^\s+$/.test(part)) {
      return {text: part, italic: false, underline: false, space: true};
    }
    const key = cleaned(part);
    const italic = !italicUsed && italicWord !== '' && key === italicWord;
    if (italic) italicUsed = true;
    const underline = !underlineUsed && underlineWord !== '' && key === underlineWord;
    if (underline) underlineUsed = true;
    return {text: part, italic, underline, space: false};
  });
};

export default function AmType(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<Params>(props.params);
  const text = typeof params.text === 'string' ? params.text : '';
  if (!text.trim()) {
    return null;
  }

  const font = oneOf(params.font, FONTS, 'display');
  const isLabel = font === 'label';
  const reveal = oneOf(params.reveal, REVEALS, 'slideUp');
  const align = oneOf(params.align, ALIGNS, 'left');
  const size = Math.max(1, finiteNumber(params.size, isLabel ? 28 : 96));
  const tracking = finiteNumber(params.tracking, isLabel ? 0.12 : -0.01);
  const lineHeight = Math.max(0.5, finiteNumber(params.lineHeight, isLabel ? 1.3 : 0.95));
  const color = params.color ?? COLOR.ink;
  const x = interpolateKeys(params.xKeyframes, 'x', frame) ?? finiteNumber(params.x, 96);
  const y = interpolateKeys(params.yKeyframes, 'y', frame) ?? finiteNumber(params.y, 96);
  const width = Math.max(1, finiteNumber(params.width, 1200));

  const italicWord = typeof params.italicWord === 'string' ? params.italicWord.trim() : '';
  const underlineWord = typeof params.underlineWord === 'string' ? params.underlineWord.trim() : '';
  const runs = buildRuns(text, italicWord, underlineWord);

  // Underline draw-in: 6 frames, eased, from underlineAt. Before it starts the
  // background size is 0 and nothing shows.
  const underlineStart = finiteNumber(params.underlineAt, 0);
  const underlineP = easeOutCubic((frame - underlineStart) / UNDERLINE_FRAMES);

  // Typewriter reveal: one character every 2 frames, first character on frame 0.
  const visibleChars = Math.floor(frame / TYPE_FRAMES_PER_CHAR) + 1;
  let charIndex = 0;

  const renderChars = (value: string, key: string): ReactElement[] => {
    const chars = Array.from(value);
    return chars.map((ch, i) => {
      const index = charIndex;
      charIndex += 1;
      const shown = reveal !== 'type' || index < visibleChars;
      return (
        <span key={`${key}-${i}`} style={{opacity: shown ? 1 : 0}}>
          {ch}
        </span>
      );
    });
  };

  const underlineFraction = clamp(underlineP, 0, 1);
  const body = runs.map((run, index) => {
    if (run.space) {
      return <span key={`s-${index}`}>{run.text}</span>;
    }
    // A run can be both the italic word and the underlined word, so the two
    // style sets are merged. The underline is an absolutely positioned bar
    // whose width steps with the eased progress. A background-size gradient on
    // an inline span did not paint in the Remotion renderer.
    const runStyle: CSSProperties = {};
    if (run.italic) {
      Object.assign(runStyle, {fontFamily: FAMILY.display, fontStyle: 'italic', color: params.italicColor ?? COLOR.rust, textTransform: 'none'});
    }
    if (run.underline) {
      runStyle.position = 'relative';
      runStyle.display = 'inline-block';
    }
    return (
      <span key={`w-${index}`} style={runStyle}>
        {renderChars(run.text, `w-${index}`)}
        {run.underline ? (
          <span
            aria-hidden
            style={{
              position: 'absolute',
              left: 0,
              bottom: '-0.1em',
              height: UNDERLINE_PX,
              width: `${underlineFraction * 100}%`,
              backgroundColor: COLOR.orange,
            }}
          />
        ) : null}
      </span>
    );
  });

  const slideProgress = easeOutCubic(frame / SLIDE_FRAMES);
  const slideTransform = reveal === 'slideUp' && frame < SLIDE_FRAMES
    ? `translateY(${(1 - slideProgress) * 100}%)`
    : undefined;

  // The mask gets padding so descenders and italic overhang are not clipped,
  // and the matching negative margin keeps the layout unchanged.
  const mask: CSSProperties = {
    position: 'absolute',
    left: x,
    top: y,
    width,
    // Clip only while the slide is moving. A permanent clip would also cut the
    // underline, which sits below the line box.
    overflow: reveal === 'slideUp' && frame < SLIDE_FRAMES ? 'hidden' : 'visible',
    paddingTop: '0.15em',
    paddingBottom: '0.25em',
    marginTop: '-0.15em',
    marginBottom: '-0.25em',
    boxSizing: 'border-box',
  };
  const line: CSSProperties = {
    color,
    fontFamily: isLabel ? FAMILY.label : FAMILY.display,
    fontSize: size,
    fontWeight: 400,
    lineHeight,
    letterSpacing: `${tracking}em`,
    textAlign: align,
    textTransform: isLabel ? 'uppercase' : 'none',
    whiteSpace: 'pre-wrap',
    transform: slideTransform,
  };

  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <div style={mask}>
        <div style={line}>{body}</div>
      </div>
    </AbsoluteFill>
  );
}
