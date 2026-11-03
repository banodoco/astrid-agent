import type {CSSProperties} from 'react';
import {clamp, finiteNumber} from '../../_shared/am';

export type Inset = {
  x: number;
  y: number;
  w: number;
  h: number;
  radius?: number;
  shadow?: boolean | string;
  border?: string;
};

export const FOOTAGE_CARD_BACKGROUND = '#1F1F1F';

// Keep the declared inset in one place for the renderer and its scene answer.
// The scene contract must describe the same clipped box the component draws.
export const insetOf = (value: unknown, width: number, height: number): (Required<Pick<Inset, 'x' | 'y' | 'w' | 'h' | 'radius'>> & {shadow: string; border: string | null}) | null => {
  if (!value || typeof value !== 'object') return null;
  const v = value as Inset;
  const w = Math.round(clamp(finiteNumber(v.w, 0), 0, width));
  const h = Math.round(clamp(finiteNumber(v.h, 0), 0, height));
  if (w < 2 || h < 2) return null;
  const shadow = v.shadow === false ? 'none' : typeof v.shadow === 'string' ? v.shadow : '0 28px 64px rgba(21, 19, 17, 0.18), 0 3px 8px rgba(21, 19, 17, 0.10)';
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

export const footageField = (background?: string): {background: string; style: CSSProperties} => {
  const value = background ?? FOOTAGE_CARD_BACKGROUND;
  return {background: value, style: {background: value, overflow: 'hidden'}};
};
