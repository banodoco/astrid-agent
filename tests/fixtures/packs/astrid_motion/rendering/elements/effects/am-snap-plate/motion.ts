// am-snap-plate's motion as data. The component draws with plateView(), and
// `timelines visualize` evaluates motionAt() on the frames it charts (curves,
// bounds, lint), so the chart is this element's own maths, not a copy.
// Pure: no React, no DOM, no Math.random.
import {LOGICAL_H, LOGICAL_W, finiteNumber, frameList, integerIn, narrowParams, spanList} from '../../_shared/am';
import {
  type Point,
  type PresenterTiming,
  type PresenterView,
  presenterView,
} from '../am-presenter/presenter-core';

type MotionParams = {
  zoom?: number;
  focus?: Point;
  pan?: {dx: number; dy: number};
  stepFrames?: number;
  punchAt?: number[];
  push?: {to: number; frames: number; at?: number} | null;
  bob?: number;
  words?: unknown;
  seed?: number;
};

// The plate's view on a clip frame: am-presenter's presenterView() with the pan
// step folded into the focus first.
export const plateView = (raw: unknown, frame: number, fps: number): {timing: PresenterTiming; view: PresenterView} => {
  const params = narrowParams<MotionParams>(raw);
  const stepFrames = integerIn(params.stepFrames, 2, 3, 2);
  const steps = Math.floor(frame / stepFrames);
  const focusX0 = finiteNumber(params.focus?.x, LOGICAL_W / 2);
  const focusY0 = finiteNumber(params.focus?.y, LOGICAL_H / 2);
  const dx = finiteNumber(params.pan?.dx, 0);
  const dy = finiteNumber(params.pan?.dy, 0);

  const timing: PresenterTiming = {
    fps,
    words: spanList(params.words),
    seed: Math.trunc(finiteNumber(params.seed, 7)),
    blinkEvery: 0,
    noBlink: [],
    bob: integerIn(params.bob, 0, 1, 0),
  };
  const view = presenterView(
    {
      zoom: integerIn(params.zoom, 1, 3, 1),
      focus: {x: focusX0 + dx * steps, y: focusY0 + dy * steps},
      punchAt: frameList(params.punchAt),
      push: params.push && typeof params.push === 'object' ? params.push : null,
    },
    timing,
    frame,
  );
  return {timing, view};
};

// What visualize charts: zoom (base, push or punch) and the view offset in screen px.
export const motionAt = (raw: unknown, frame: number, fps: number): Record<string, number> => {
  const {view} = plateView(raw, frame, fps);
  return {zoom: view.zoom, pan_x: -view.originX, pan_y: -view.originY, visible: 1};
};
