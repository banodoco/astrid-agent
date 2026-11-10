import type {ReactElement} from 'react';
import {useCurrentFrame, useVideoConfig} from 'remotion';
import {
  BlockWipe,
  COLOR,
  finiteNumber,
  narrowParams,
  oneOf,
  type ElementComponentProps,
  type WipePattern,
} from '../../_shared/am';

// am-pixel-wipe: a full-frame 6 px block wipe. Progress advances once per
// frame over `frames`, then holds at its end state. Block order is set by
// pattern, and the random pattern is a pure hash of (seed, block index).

type Params = {
  mode?: 'cover' | 'reveal';
  frames?: number;
  color?: string;
  pattern?: WipePattern;
  seed?: number;
};

const MODES = ['cover', 'reveal'] as const;
const PATTERNS: readonly WipePattern[] = ['diagonal', 'random', 'scan'];

export default function AmPixelWipe(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const {width, height} = useVideoConfig();
  const params = narrowParams<Params>(props.params);

  const mode = oneOf(params.mode, MODES, 'cover');
  const frames = Math.max(1, Math.round(finiteNumber(params.frames, 12)));
  const color = params.color ?? COLOR.charcoal;
  const pattern = oneOf<WipePattern>(params.pattern, PATTERNS, 'diagonal');
  const seed = Math.trunc(finiteNumber(params.seed, 1));

  return (
    <BlockWipe
      width={width}
      height={height}
      paint={{mode, progress: (frame + 1) / frames, color, pattern, seed}}
    />
  );
}
