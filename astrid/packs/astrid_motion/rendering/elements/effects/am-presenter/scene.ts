import type {ComponentCapabilityContext, PreparedComponentCapability} from '@banodoco/timeline-composition/evaluation';
import type {ElementComponentProps} from '../../../../../rendering/shared/element_contracts';
import {clipFrames, narrowParams} from '../../_shared/am';
import {mosaicBlockAt, type MosaicRamp} from '../../_shared/mosaic';
import {motionAt, mouthStateRect, pixelGeometry, presenterAt} from './motion';

export const presenterMosaicAt = (raw: unknown, clip: ElementComponentProps['clip'], frame: number, fps: number): boolean => {
  const p = narrowParams<{mosaicIn?: MosaicRamp | null; mosaicOut?: MosaicRamp | null}>(raw);
  return mosaicBlockAt(frame, clipFrames(clip, fps), p.mosaicIn, p.mosaicOut) > 6;
};

export const preparePresenterScene = ({clip, fps, width, height}: ComponentCapabilityContext): PreparedComponentCapability => ({
  identity: JSON.stringify([clip.params, clipFrames(clip, fps), fps, width, height]),
  // The public scene supplies the same speed-remapped frame as the component.
  frame: frame => {
    const {view} = presenterAt(clip.params, frame, fps);
    const geometry = pixelGeometry(mouthStateRect(clip.params), view);
    return {
      presence: 'unknown',
      opaqueSupport: 'none',
      // The root clips all drawing to the viewport; this is only an envelope.
      bounds: {status: 'known', rect: {x: 0, y: 0, width, height}},
      diagnostics: {
        motion: motionAt(clip.params, frame, fps),
        // Padded mouth ROI, including closed pose; no plate or alpha claim.
        mouth: presenterMosaicAt(clip.params, clip, frame, fps)
          ? {status: 'unknown', reason: 'Presenter mouth overlay is hidden by mosaic'}
          : {status: 'known', rect: {x: geometry.left, y: geometry.top, width: geometry.width, height: geometry.height}},
      },
    };
  },
});
