/** Key-time zero, in seconds from the clip's timeline start. Media trim and
 * playback speed never enter this clock. Keys keep their existing units.
 * Absent origin: App keys use timeline seconds; sprite keys use local frames.
 */
export type KeyframeClock = {at: number; keyframeOrigin?: number};

export const keyframeOrigin = (clip: Pick<KeyframeClock, 'keyframeOrigin'>, legacy = 0): number => {
  const origin = clip.keyframeOrigin;
  if (origin === undefined) return legacy;
  if (typeof origin !== 'number' || !Number.isFinite(origin)) {
    throw new Error('keyframeOrigin must be finite seconds relative to clip start');
  }
  return origin;
};

/** App timeline-seconds adapter. Add offsets before subtraction so declaring
 * legacy origin -at is exactly the identity, including non-frame-aligned at.
 */
export const timelineKeyframeTime = (clip: KeyframeClock, timelineSeconds: number): number =>
  timelineSeconds - (clip.at + keyframeOrigin(clip, -clip.at));

/** Donor sprite adapter. Its input is already the composition's own local frame,
 * including any transition clamp; do not add source trim or rescale by speed.
 */
export const spriteKeyframeFrame = (clip: Pick<KeyframeClock, 'keyframeOrigin'>, localFrame: number, fps: number): number => {
  if (!Number.isFinite(fps) || fps <= 0) throw new Error('fps must be positive and finite');
  return localFrame - keyframeOrigin(clip) * fps;
};

/** Declare an existing key domain without moving or rounding any key. */
export const declareKeyframeOrigin = <T extends KeyframeClock>(clip: T, source: 'app-seconds' | 'sprite-frames'): T & {keyframeOrigin: number} => ({
  ...clip,
  keyframeOrigin: keyframeOrigin(clip, source === 'app-seconds' ? -clip.at : 0),
});
