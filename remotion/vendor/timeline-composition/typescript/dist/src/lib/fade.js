import { useCurrentFrame } from 'remotion';
import { fadeMultiplier } from './clip-look';
export const useFadeOpacity = (clip, fps) => {
    return fadeMultiplier(clip, useCurrentFrame(), fps);
};
