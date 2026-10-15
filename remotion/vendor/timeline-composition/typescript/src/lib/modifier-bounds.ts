import type {ModifierBounds} from '../modifier-types';
import type {AssetRegistryEntry, TimelineClip, TrackDefinition} from '../types';
import {computeRenderBounds, computeViewportMediaLayout, getIntrinsicMediaSize, type RenderBounds} from './render-bounds';

const unknown = (reason: string): ModifierBounds => ({status: 'unknown', reason});
const known = (rect: RenderBounds): ModifierBounds => ({status: 'known', rect});

export const mediaModifierBounds = (clip: TimelineClip, track: TrackDefinition, asset: AssetRegistryEntry | undefined,
  width: number, height: number): ModifierBounds => {
  if (!asset?.file) return unknown('Target media is unresolved');
  const fullBounds = {x: clip.x ?? 0, y: clip.y ?? 0, width: clip.width ?? width, height: clip.height ?? height};
  if (![width, height, ...Object.values(fullBounds)].every(Number.isFinite)
    || width <= 0 || height <= 0 || fullBounds.width <= 0 || fullBounds.height <= 0) {
    return unknown('Target media geometry is invalid');
  }
  const positionValues = [clip.x, clip.y, clip.width, clip.height,
    clip.cropTop, clip.cropBottom, clip.cropLeft, clip.cropRight];
  if (positionValues.some((value) => value !== undefined && !Number.isFinite(value))) {
    return unknown('Target media geometry is invalid');
  }
  const positioned = positionValues.some((value) => value !== undefined);
  const manual = track.fit === 'manual' || positioned;
  const scale = positioned ? 1 : track.scale ?? 1;
  if (!Number.isFinite(scale) || scale <= 0) return unknown('Target track scale is invalid');
  const scaled = (rect: RenderBounds): ModifierBounds => known(computeRenderBounds({
    x: width / 2 + (rect.x - width / 2) * scale, y: height / 2 + (rect.y - height / 2) * scale,
    width: rect.width * scale, height: rect.height * scale}, width, height));
  if (manual) {
    const intrinsic = getIntrinsicMediaSize(asset.resolution);
    const layout = computeViewportMediaLayout({fullBounds, cropValues: clip, compositionWidth: width,
      compositionHeight: height, intrinsicWidth: intrinsic.width, intrinsicHeight: intrinsic.height});
    return scaled(layout?.renderBounds ?? {x: 0, y: 0, width: 0, height: 0});
  }
  let rect = {x: 0, y: 0, width, height};
  if ((track.fit ?? 'contain') === 'contain') {
    const intrinsic = getIntrinsicMediaSize(asset.resolution);
    if (!intrinsic.width || !intrinsic.height) return unknown('Contained media has no declared intrinsic size');
    const factor = Math.min(width / intrinsic.width, height / intrinsic.height);
    const mediaWidth = intrinsic.width * factor;
    const mediaHeight = intrinsic.height * factor;
    rect = {x: (width - mediaWidth) / 2, y: (height - mediaHeight) / 2, width: mediaWidth, height: mediaHeight};
  }
  // The existing track scale host is centered and clips to its scaled viewport.
  return scaled(rect);
};
