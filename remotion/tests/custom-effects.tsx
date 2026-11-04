import {createElement} from 'react';
export default Object.assign((props) => createElement('b', {'data-params': JSON.stringify(props.params)}, 'custom element'), {
  prepareSceneCapability: ({clip}) => ({
    identity: JSON.stringify(clip.params),
    frame: () => ({presence: 'present', opaqueSupport: 'rect',
      bounds: {status: 'known', rect: {x: 0, y: 0, width: 1920, height: 1080}}}),
  }),
});
