import { jsx as _jsx } from "react/jsx-runtime";
import { Audio, staticFile } from 'remotion';
import { getSanitizedPlaybackRate, getSanitizedVolume, } from './lib/duration';
import { computeMediaTrim } from './lib/trim';
import { useAudioEnvelopeSource } from './lib/use-audio-envelope';
const toRenderableFileUrl = (file) => {
    if (typeof file !== 'string' || file.trim().length === 0) {
        return null;
    }
    if (file.startsWith('http://') || file.startsWith('https://')) {
        return file;
    }
    return staticFile(file);
};
export const AudioTrack = ({ clip, track, assetEntry, fps }) => {
    const fileUrl = toRenderableFileUrl(assetEntry?.file);
    const envelopeUrl = useAudioEnvelopeSource(fileUrl, clip, fps);
    if (!envelopeUrl) {
        return null;
    }
    const baseVolume = track.muted
        ? 0
        : getSanitizedVolume(track.volume ?? 1) * getSanitizedVolume(clip.volume ?? 1);
    return (_jsx(Audio, { src: envelopeUrl, pauseWhenBuffering: true, ...computeMediaTrim(clip, fps), playbackRate: getSanitizedPlaybackRate(clip.speed), volume: baseVolume }));
};
