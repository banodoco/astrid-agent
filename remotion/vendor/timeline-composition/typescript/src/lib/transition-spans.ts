// Pure transition planning for one visual track. No remotion/react imports so
// the node tests can run this module directly.
//
// Frames are track-absolute unless a field says "draw-local" (relative to the
// start of the clip's Sequence, i.e. drawStart).

export type Align = 'end' | 'start' | 'center';

export type ClipSpan = {start: number; duration: number}; // frames, the clip's own span

export type FadeWindow = {from: number; to: number; kind: 'dissolve' | 'dip'}; // draw-local frames

export type DrawPlan = {
  drawStart: number;
  drawDuration: number; // the Sequence to mount: own span plus any extension
  ownOffset: number; // own frame 0 sits at drawStart + ownOffset
  ownDuration: number;
  fadeIn: FadeWindow | null; // this clip ramps in (incoming side of a transition)
  fadeOut: FadeWindow | null; // this clip ramps out (dip only; a dissolve outgoing clip stays at 1)
};

export type TransitionSpec = {T: number; kind: 'dissolve' | 'dip'; align: Align};

// Placement of the overlap window around a boundary (the outgoing clip's end).
// 'end' is upstream's placement and the default: the window sits fully before
// the boundary, so the incoming clip starts inside it.
export const overlapWindow = (
  boundary: number,
  T: number,
  align: Align,
): {start: number; end: number} => {
  if (align === 'start') {
    return {start: boundary, end: boundary + T};
  }
  if (align === 'center') {
    const start = boundary - Math.floor(T / 2);
    return {start, end: start + T};
  }
  return {start: boundary - T, end: boundary};
};

// transitions[i] is the transition from clip i to clip i+1 (null = a cut).
// `spans` must already be in track order.
export const planTrack = (
  spans: ClipSpan[],
  transitions: Array<TransitionSpec | null>,
): DrawPlan[] => {
  const plans: DrawPlan[] = spans.map((span) => ({
    drawStart: span.start,
    drawDuration: span.duration,
    ownOffset: 0,
    ownDuration: span.duration,
    fadeIn: null,
    fadeOut: null,
  }));

  const applied: Array<{index: number; T: number; kind: 'dissolve' | 'dip'; win: {start: number; end: number}}> = [];

  for (let index = 0; index < spans.length - 1; index += 1) {
    const transition = transitions[index];
    if (!transition) {
      continue;
    }
    const out = spans[index];
    const inc = spans[index + 1];
    const outEnd = out.start + out.duration;
    const T = Math.round(transition.T);
    const adjacent = inc.start >= outEnd - 1 && inc.start <= outEnd;
    const fits = T > 0 && T <= out.duration && T <= inc.duration;
    if (!adjacent || !fits) {
      continue;
    }

    const win = overlapWindow(outEnd, T, transition.align);
    const outPlan = plans[index];
    const incPlan = plans[index + 1];

    // Outgoing clip: extend forward if the window runs past its own end.
    if (win.end > outEnd) {
      outPlan.drawDuration = win.end - outPlan.drawStart;
    }
    // Incoming clip: extend back if the window starts before its own start.
    if (win.start < inc.start) {
      const extra = incPlan.drawStart - win.start;
      incPlan.drawStart = win.start;
      incPlan.drawDuration += extra;
    }

    applied.push({index, T, kind: transition.kind, win});
  }

  // Fade windows are computed after all extensions so draw-local frames are final.
  // dissolve: incoming ramps over the whole window, outgoing untouched (stays 1).
  // dip: outgoing ramps 1->0 over the first half, incoming 0->1 over the second.
  for (const {index, T, kind, win} of applied) {
    const outPlan = plans[index];
    const incPlan = plans[index + 1];
    const half = Math.floor(T / 2);
    if (kind === 'dip') {
      outPlan.fadeOut = {
        from: win.start - outPlan.drawStart,
        to: win.start + half - outPlan.drawStart,
        kind,
      };
      incPlan.fadeIn = {
        from: win.start + half - incPlan.drawStart,
        to: win.end - incPlan.drawStart,
        kind,
      };
    } else {
      incPlan.fadeIn = {
        from: win.start - incPlan.drawStart,
        to: win.end - incPlan.drawStart,
        kind,
      };
    }
  }

  return plans.map((plan, index) => ({
    ...plan,
    ownOffset: spans[index].start - plan.drawStart,
  }));
};

// Clip frame to show at a draw-local frame. Extension frames show the clip
// frozen at its boundary frame, so its own timing never shifts.
export const ownFrame = (plan: DrawPlan, drawLocalFrame: number): number => {
  return Math.min(
    Math.max(drawLocalFrame - plan.ownOffset, 0),
    Math.max(plan.ownDuration - 1, 0),
  );
};

const ramp = (frame: number, from: number, to: number): number => {
  if (to <= from) {
    return frame >= to ? 1 : 0;
  }
  return Math.min(Math.max((frame - from) / (to - from), 0), 1);
};

export const layerOpacity = (plan: DrawPlan, drawLocalFrame: number): number => {
  const incoming = plan.fadeIn ? ramp(drawLocalFrame, plan.fadeIn.from, plan.fadeIn.to) : 1;
  const outgoing = plan.fadeOut && plan.fadeOut.kind === 'dip'
    ? 1 - ramp(drawLocalFrame, plan.fadeOut.from, plan.fadeOut.to)
    : 1;
  return incoming * outgoing;
};
