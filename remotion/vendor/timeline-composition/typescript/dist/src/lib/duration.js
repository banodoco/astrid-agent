export const secondsToFrames = (seconds, fps) => {
    return Math.round(seconds * fps);
};
export const getClipSourceDuration = (clip) => {
    if (typeof clip.hold === 'number') {
        return clip.hold;
    }
    return (clip.to ?? 0) - (clip.from ?? 0);
};
export const getClipTimelineDuration = (clip) => {
    const speed = clip.speed ?? 1;
    return getClipSourceDuration(clip) / speed;
};
export const getSanitizedPlaybackRate = (speed) => {
    return typeof speed === 'number' && Number.isFinite(speed) && speed > 0 ? speed : 1;
};
export const getSanitizedVolume = (volume, fallback = 1) => {
    return typeof volume === 'number' && Number.isFinite(volume)
        ? Math.max(0, volume)
        : fallback;
};
export const getClipDurationInFrames = (clip, fps) => {
    return Math.max(1, secondsToFrames(getClipTimelineDuration(clip), fps));
};
export const getTimelineDurationInFrames = (timeline, fps) => {
    return Math.max(1, ...timeline.clips.map((clip) => {
        return secondsToFrames(clip.at, fps) + getClipDurationInFrames(clip, fps);
    }));
};
