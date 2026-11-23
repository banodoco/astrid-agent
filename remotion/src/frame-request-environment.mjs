// A warm frame owner must not carry project selectors into the next lease.
export const applyFrameRequestEnvironment = (target, environment) => {
  delete target.ASTRID_RENDER_PACK_ROOTS;
  delete target.ASTRID_RENDER_REGISTRY_ROOT;
  delete target.ASTRID_PACKS_PATH;
  Object.assign(target, environment ?? {});
};
