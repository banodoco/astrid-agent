// am-pixel-shape geometry: which cells of a w x h grid are inked for a shape.
// Pure (no imports), so tests can run it directly under node. Cells are [col, row].

export type PixelShape = 'ring' | 'dot' | 'underline' | 'arrow' | 'rect';
export const PIXEL_SHAPES: readonly PixelShape[] = ['ring', 'dot', 'underline', 'arrow', 'rect'];

export type Cell = [number, number];

// rect: a filled w x h box with square or pixel-stepped rounded corners. Inside
// test is a cell-centre test against the corner circle (clamp-to-corner-centre),
// so every radius steps on the cell grid. `radius` is clamped to half the short side.
const inRoundedBox = (
  col: number,
  row: number,
  x0: number,
  y0: number,
  x1: number,
  y1: number,
  radius: number,
): boolean => {
  if (col < x0 || row < y0 || col >= x1 || row >= y1) return false;
  if (radius <= 0) return true;
  const px = col + 0.5;
  const py = row + 0.5;
  const nx = Math.min(Math.max(px, x0 + radius), x1 - radius);
  const ny = Math.min(Math.max(py, y0 + radius), y1 - radius);
  const dx = px - nx;
  const dy = py - ny;
  return dx * dx + dy * dy <= radius * radius;
};

// rectCells: `fill` is the whole silhouette (draw it in the fill colour); `outline`
// is the border ring of `stroke` cells inside it (draw it on top in the outline
// colour). The inner corner radius is radius - stroke, as for a stroked rounded box.
export const rectCells = (
  w: number,
  h: number,
  stroke: number,
  radius: number,
): {fill: Cell[]; outline: Cell[]} => {
  const r = Math.max(0, Math.min(Math.floor(radius), Math.floor(Math.min(w, h) / 2)));
  const s = Math.max(1, Math.min(Math.floor(stroke), Math.floor(Math.min(w, h) / 2) || 1));
  const fill: Cell[] = [];
  const outline: Cell[] = [];
  for (let row = 0; row < h; row += 1) {
    for (let col = 0; col < w; col += 1) {
      if (!inRoundedBox(col, row, 0, 0, w, h, r)) continue;
      fill.push([col, row]);
      if (!inRoundedBox(col, row, s, s, w - s, h - s, Math.max(0, r - s))) outline.push([col, row]);
    }
  }
  return {fill, outline};
};

export const shapeCells = (shape: PixelShape, w: number, h: number, stroke: number, radius = 0): Cell[] => {
  const cells: [number, number][] = [];
  const cx = w / 2;
  const cy = h / 2;
  if (shape === 'rect') {
    return rectCells(w, h, stroke, radius).fill;
  }
  if (shape === 'dot' || shape === 'ring') {
    const rx = w / 2;
    const ry = h / 2;
    const irx = rx - stroke;
    const iry = ry - stroke;
    for (let row = 0; row < h; row += 1) {
      for (let col = 0; col < w; col += 1) {
        const dx = (col + 0.5 - cx) / rx;
        const dy = (row + 0.5 - cy) / ry;
        if (dx * dx + dy * dy > 1) continue;
        if (shape === 'ring' && irx > 0 && iry > 0) {
          const ix = (col + 0.5 - cx) / irx;
          const iy = (row + 0.5 - cy) / iry;
          if (ix * ix + iy * iy <= 1) continue;
        }
        cells.push([col, row]);
      }
    }
  } else if (shape === 'underline') {
    for (let row = Math.max(0, h - stroke); row < h; row += 1) {
      for (let col = 0; col < w; col += 1) cells.push([col, row]);
    }
  } else {
    // arrow: a shaft of `stroke` rows, then a head whose base is the full height
    // and whose tip is at the right edge.
    const head = Math.min(w - 1, Math.max(2, Math.round(h / 2)));
    for (let row = 0; row < h; row += 1) {
      const off = Math.abs(row + 0.5 - cy);
      if (off <= stroke / 2) {
        for (let col = 0; col < w - head; col += 1) cells.push([col, row]);
      }
    }
    for (let k = 0; k < head; k += 1) {
      const half = (h / 2) * (1 - k / head);
      const col = w - head + k;
      for (let row = 0; row < h; row += 1) {
        if (Math.abs(row + 0.5 - cy) <= half) cells.push([col, row]);
      }
    }
  }
  return cells;
};

// Blink rule, the same as am-sprite's blinkAt: the shape hides for two frames
// starting at each listed clip frame. A number or an array of numbers.
export const isBlinking = (frame: number, blinkAt: number | number[] | null | undefined): boolean => {
  const blinks: number[] = Array.isArray(blinkAt) ? blinkAt : typeof blinkAt === 'number' ? [blinkAt] : [];
  return blinks.some((at) => frame >= at && frame < at + 2);
};
