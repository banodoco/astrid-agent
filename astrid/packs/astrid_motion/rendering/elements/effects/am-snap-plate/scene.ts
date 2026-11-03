import type {ComponentCapabilityContext, PreparedComponentCapability} from '@banodoco/timeline-composition/evaluation';
import {plateSurface} from './surface';

export const preparePlateScene = ({clip, assetEntry, width, height}: ComponentCapabilityContext): PreparedComponentCapability => {
  const {url, fill, background} = plateSurface(clip.params, assetEntry);
  return {
    identity: JSON.stringify([clip.params, assetEntry, width, height]),
    frame: () => {
      if (!url && !fill) {
        return {presence: 'unknown', opaqueSupport: 'unknown',
          bounds: {status: 'unknown', reason: 'Plate has no resolved source or fill'}};
      }
      return {
        presence: 'present',
        // The AbsoluteFill backing covers the viewport even when the image has
        // alpha, pans, mosaics or wipes. Other CSS colours remain unproven.
        opaqueSupport: /^#(?:[\da-f]{3}|[\da-f]{6})$/i.test(background) ? 'rect' : 'unknown',
        bounds: {status: 'known', rect: {x: 0, y: 0, width, height}},
      };
    },
  };
};
