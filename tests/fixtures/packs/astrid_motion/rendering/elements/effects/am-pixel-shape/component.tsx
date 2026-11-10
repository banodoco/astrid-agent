import type {ReactElement} from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {COLOR, LOGICAL_PX, finiteNumber, integerIn, narrowParams, oneOf, type ElementComponentProps} from '../../_shared/am';
import {PIXEL_SHAPES, isBlinking, rectCells, shapeCells, type Cell, type PixelShape} from './shape-cells';

// am-pixel-shape: a procedural pixel mark on the sticker grid. No asset, no
// animation except blinkAt, which hides it for two frames as am-sprite does.
// Each cell is px_scale screen px; the shape's top-left sits at (x, y) logical
// px, the same placement as am-sprite. rect takes fill and outline colours
// (either optional; an absent colour is not drawn), and radius in cells.

type Params = {
  shape?: PixelShape;
  color?: string;
  px_scale?: number;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  stroke?: number;
  radius?: number;
  fill?: string;
  outline?: string;
  blinkAt?: number | number[];
};

export default function AmPixelShape(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<Params>(props.params);
  const shape = oneOf<PixelShape>(params.shape, PIXEL_SHAPES, 'ring');
  const px = integerIn(params.px_scale, 1, 24, LOGICAL_PX);
  const w = integerIn(params.w, 1, 200, 24);
  const h = integerIn(params.h, 1, 200, 24);
  // stroke defaults to 1 cell for rect and 2 for the other shapes.
  const stroke = integerIn(params.stroke, 1, Math.min(w, h), shape === 'rect' ? 1 : 2);
  const radius = integerIn(params.radius, 0, 100, 0);
  const color = typeof params.color === 'string' && params.color !== '' ? params.color : COLOR.charcoal;
  const fill = typeof params.fill === 'string' && params.fill !== '' ? params.fill : undefined;
  const outline = typeof params.outline === 'string' && params.outline !== '' ? params.outline : undefined;
  const x = finiteNumber(params.x, 0);
  const y = finiteNumber(params.y, 0);
  if (isBlinking(frame, params.blinkAt)) {
    return null;
  }

  // One path per colour, one unit square per cell: hard edges, no anti-aliasing seams.
  const toPath = (cells: Cell[]): string => cells.map(([col, row]) => `M${col} ${row}h1v1h-1z`).join('');
  const paths: {d: string; fill: string}[] = [];
  if (shape === 'rect') {
    const rect = rectCells(w, h, stroke, radius);
    if (fill !== undefined) paths.push({d: toPath(rect.fill), fill});
    if (outline !== undefined) paths.push({d: toPath(rect.outline), fill: outline});
  } else {
    paths.push({d: toPath(shapeCells(shape, w, h, stroke)), fill: color});
  }

  return (
    <AbsoluteFill style={{overflow: 'hidden', pointerEvents: 'none'}}>
      <svg
        style={{position: 'absolute', left: x * LOGICAL_PX, top: y * LOGICAL_PX, overflow: 'visible'}}
        width={w * px}
        height={h * px}
        viewBox={`0 0 ${w} ${h}`}
        shapeRendering="crispEdges"
      >
        {paths.map((p, i) => (
          <path key={i} d={p.d} fill={p.fill} />
        ))}
      </svg>
    </AbsoluteFill>
  );
}
