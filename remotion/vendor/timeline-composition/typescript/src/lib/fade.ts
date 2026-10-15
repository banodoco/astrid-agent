import {useCurrentFrame} from 'remotion';
import type {TimelineClip} from '../types';
import {fadeMultiplier} from './clip-look';

export const useFadeOpacity = (clip: TimelineClip, fps: number): number => {
  return fadeMultiplier(clip, useCurrentFrame(), fps);
};
