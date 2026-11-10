import {Composition} from 'remotion';
import {FontProvider} from '../../../../../remotion/src/fonts';
import {Preview} from './Preview';

// FontProvider is the real remotion/src/fonts.ts, so previews load the same
// shipped faces (Gelasio, Departure Mono, Inter) as the render.
export const Root = () => (
  <>
    <FontProvider />
    <Composition
      id="Scene"
      component={Preview}
      durationInFrames={90}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={{layers: [], assets: {}}}
      calculateMetadata={({props}) => ({
        // Long enough for the latest layer end (min 90 frames).
        durationInFrames: Math.max(90, ...props.layers.map((l) => Math.ceil((l.at + l.hold) * 30))),
      })}
    />
  </>
);
