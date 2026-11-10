import type {ReactElement} from 'react';
import {AbsoluteFill, Img, Sequence, useVideoConfig} from 'remotion';
import AmSnapPlate from '../../rendering/elements/effects/am-snap-plate/component';
import AmSprite from '../../rendering/elements/effects/am-sprite/component';
import AmType from '../../rendering/elements/effects/am-type/component';
import AmCallout from '../../rendering/elements/effects/am-callout/component';
import AmPixelWipe from '../../rendering/elements/effects/am-pixel-wipe/component';
import AmPixelShape from '../../rendering/elements/effects/am-pixel-shape/component';
import AmPresenter from '../../rendering/elements/effects/am-presenter/component';
import AmDiscord from '../../rendering/elements/effects/am-discord/component';
import AmFlap from '../../rendering/elements/effects/am-flap/component';
import AmUiSketch from '../../rendering/elements/effects/am-ui-sketch/component';
import AmBurst from '../../rendering/elements/effects/am-burst/component';
import AmFootage from '../../rendering/elements/effects/am-footage/component';
import AmTweet from '../../rendering/elements/effects/am-tweet/component';
import AmQuote from '../../rendering/elements/effects/am-quote/component';
import AmTerminal from '../../rendering/elements/effects/am-terminal/component';
import AmDroste from '../../rendering/elements/effects/am-droste/component';
import AmSeasons from '../../rendering/elements/effects/am-seasons/component';
import AmOrbit from '../../rendering/elements/effects/am-orbit/component';
import AmOrgchart from '../../rendering/elements/effects/am-orgchart/component';
import {elementSource, type ElementComponentProps} from '../../rendering/elements/_shared/am';

// Control: the same plate through a plain <Img> with no pixelated style. It shows
// the renderer's default smoothing, which is the Q2 comparison.
const ControlPlain = (props: ElementComponentProps): ReactElement | null => {
  const url = elementSource(props.assetEntry, undefined);
  if (!url) return null;
  return (
    <AbsoluteFill>
      <Img src={url} crossOrigin="anonymous" style={{position: 'absolute', left: 0, top: 0, width: 1920, height: 1080}} />
    </AbsoluteFill>
  );
};

type Comp = (props: ElementComponentProps) => ReactElement | null;
const REGISTRY: Record<string, Comp> = {
  'am-snap-plate': AmSnapPlate,
  'am-sprite': AmSprite,
  'am-type': AmType,
  'am-callout': AmCallout,
  'am-pixel-wipe': AmPixelWipe,
  'am-pixel-shape': AmPixelShape,
  'am-presenter': AmPresenter,
  'am-discord': AmDiscord,
  'am-flap': AmFlap,
  'am-ui-sketch': AmUiSketch,
  'am-burst': AmBurst,
  'am-footage': AmFootage,
  'am-tweet': AmTweet,
  'am-quote': AmQuote,
  'am-terminal': AmTerminal,
  'am-droste': AmDroste,
  'am-seasons': AmSeasons,
  'am-orbit': AmOrbit,
  'am-orgchart': AmOrgchart,
  'control-plain': ControlPlain,
};

export type Layer = {element: string; at: number; hold: number; asset?: string; params?: Record<string, unknown>};
export type AssetDef = {file?: string; type?: string; resolution?: string};
export type SceneProps = {layers: Layer[]; assets: Record<string, AssetDef>};

// Mounts each layer the way the composition does: clip-relative frames,
// clip.params as params, and clip.asset resolved to assetEntry.
export const Preview = ({layers, assets}: SceneProps): ReactElement => {
  const {fps} = useVideoConfig();
  return (
    <AbsoluteFill style={{backgroundColor: '#F7F4ED'}}>
      {layers.map((layer, index) => {
        const Component = REGISTRY[layer.element];
        if (!Component) return null;
        const params = layer.params ?? {};
        const clip = {id: 'l' + index, at: layer.at, track: 'fx', clipType: layer.element, hold: layer.hold, asset: layer.asset, params};
        return (
          <Sequence key={index} from={Math.round(layer.at * fps)} durationInFrames={Math.max(1, Math.round(layer.hold * fps))}>
            <Component
              clip={clip as never}
              params={params}
              theme={{} as never}
              fps={fps}
              assetEntry={layer.asset ? assets[layer.asset] : undefined}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
