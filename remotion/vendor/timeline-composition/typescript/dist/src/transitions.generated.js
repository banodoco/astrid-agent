import CrossFade from '@workspace-transitions/cross-fade/component';
import Fade from '@workspace-transitions/fade/component';
export const ACTIVE_THEME_ID = null;
export const TRANSITION_IDS = ['cross-fade', 'fade'];
export const TRANSITION_REGISTRY = {
    'cross-fade': CrossFade,
    'fade': Fade,
};
export const TRANSITION_DEFAULTS = {
    'cross-fade': { "durationFrames": 12, "easing": "linear" },
    'fade': { "durationFrames": 12, "easing": "linear" },
};
export const TRANSITION_META = {
    'cross-fade': { "defaultDurationFrames": 12, "id": "cross-fade", "name": "Cross Fade" },
    'fade': { "aliases": ["cross-fade"], "defaultDurationFrames": 12, "id": "fade", "name": "Fade Transition" },
};
