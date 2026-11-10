import type {ComponentCapabilityContext, ComponentFrameSupport, PreparedComponentCapability} from '@banodoco/timeline-composition/evaluation';
import {elementSource, narrowParams} from '../../_shared/am';
import {footageField, insetOf} from './helper';

type Params = {
  src?: string;
  background?: string;
  inset?: unknown;
  guides?: boolean;
  placeholder?: boolean;
};

const fieldSupport = (background: string, width: number, height: number): ComponentFrameSupport => ({
  presence: 'present',
  opaqueSupport: /^#(?:[\da-f]{3}|[\da-f]{6}|[\da-f]{3}f|[\da-f]{6}ff)$/i.test(background) ? 'rect' : 'unknown',
  bounds: {status: 'known', rect: {x: 0, y: 0, width, height}},
});

export const prepareFootageScene = (context: ComponentCapabilityContext): PreparedComponentCapability => {
  const {clip, width, height, assetEntry} = context;
  const params = narrowParams<Params>(clip.params);
  const {background} = footageField(params.background);
  const inset = insetOf(params.inset, width, height);
  const selectedAsset = typeof clip.asset === 'string' && clip.asset.length > 0;
  const selectedEntryKnown = !selectedAsset || Object.prototype.hasOwnProperty.call(context, 'assetEntry');
  const source = elementSource(assetEntry, params.src);
  const isolatedInset = background === 'transparent' && selectedEntryKnown && Boolean(source) && inset
    && inset.shadow === 'none' && !inset.border && params.guides !== true && params.placeholder !== true;

  return {
    identity: JSON.stringify([clip.params, clip.asset, assetEntry, width, height]),
    frame: () => {
      if (isolatedInset) {
        return {
          presence: 'present',
          opaqueSupport: 'unknown',
          bounds: {status: 'known', rect: {x: inset.x, y: inset.y, width: inset.w, height: inset.h}},
        };
      }
      return fieldSupport(background, width, height);
    },
  };
};
