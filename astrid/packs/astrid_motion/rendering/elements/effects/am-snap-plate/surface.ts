import {COLOR, elementSource, narrowParams, type ElementComponentProps} from '../../_shared/am';

// Brand tokens accepted by params.fill; anything else is used as a CSS colour.
const FILL_TOKENS: Record<string, string> = {
  paper: COLOR.paper,
  panel: COLOR.panel,
  ink: COLOR.ink,
  muted: COLOR.muted,
  rust: COLOR.rust,
  orange: COLOR.orange,
  rule: COLOR.rule,
  charcoal: COLOR.charcoal,
};

// The component and scene capability resolve the same painted backing field.
export const plateSurface = (raw: unknown, assetEntry: ElementComponentProps['assetEntry']) => {
  const params = narrowParams<{src?: string; fill?: string; background?: string}>(raw);
  const url = elementSource(assetEntry, params.src);
  const fill = typeof params.fill === 'string' && params.fill.trim() !== ''
    ? (FILL_TOKENS[params.fill.trim()] ?? params.fill.trim())
    : null;
  return {url, fill, background: params.background ?? fill ?? COLOR.paper};
};
