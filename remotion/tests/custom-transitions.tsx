export default (props) => {
  globalThis.transitionProbe = props;
  return {presentation: null, timing: null};
};
