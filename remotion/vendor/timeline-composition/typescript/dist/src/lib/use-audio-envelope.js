import { useEffect, useMemo, useState } from 'react';
import { getRemotionEnvironment, useBufferState } from 'remotion';
import { getSanitizedPlaybackRate } from './duration';
import { computeMediaTrim } from './trim';
import { applyAudioEnvelope, audioEnvelopeWav, AudioEnvelopeError, compileAudioEnvelope, encodedEnvelopeSource } from './audio-envelope';
/** Sample gain is baked once: browser PCM for Player, attempt-served PCM for encoding. */
export const useAudioEnvelopeSource = (src, clip, fps) => {
    const envelope = useMemo(() => compileAudioEnvelope(clip, fps), [clip, fps]);
    const prepared = clip.audio?.render_source;
    const key = JSON.stringify([src, envelope, clip.from ?? 0, clip.speed ?? 1]);
    const [state, setState] = useState();
    const player = getRemotionEnvironment().isPlayer;
    const buffer = useBufferState();
    useEffect(() => {
        if (!src || !envelope || !player || prepared)
            return;
        const pending = buffer.delayPlayback();
        let cancelled = false;
        let url;
        const abort = new AbortController();
        const render = async () => {
            if (typeof AudioContext === 'undefined')
                throw new AudioEnvelopeError('Web Audio decoding is unavailable', envelope.duck);
            const response = await fetch(src, { signal: abort.signal });
            if (!response.ok)
                throw new Error(`audio fetch ${response.status}`);
            const bytes = await response.arrayBuffer();
            if (cancelled)
                return;
            const context = new AudioContext();
            let decoded;
            try {
                decoded = await context.decodeAudioData(bytes);
            }
            finally {
                await context.close();
            }
            if (cancelled)
                return;
            const channels = Array.from({ length: decoded.numberOfChannels }, (_, i) => decoded.getChannelData(i));
            applyAudioEnvelope(channels, decoded.sampleRate, envelope, computeMediaTrim(clip, fps).trimBefore / fps, getSanitizedPlaybackRate(clip.speed));
            url = URL.createObjectURL(new Blob([audioEnvelopeWav(channels, decoded.sampleRate)], { type: 'audio/wav' }));
            setState({ key, url });
        };
        void render().catch((error) => {
            if (!cancelled)
                setState({ key, error: new AudioEnvelopeError(String(error), envelope.duck) });
        }).finally(pending.unblock);
        return () => { pending.unblock(); cancelled = true; abort.abort(); if (url)
            URL.revokeObjectURL(url); };
    }, [src, envelope, key, player, clip.from, clip.speed, fps, buffer, prepared]);
    if (!envelope)
        return src;
    if (!player || prepared)
        return encodedEnvelopeSource(prepared, envelope, computeMediaTrim(clip, fps).trimBefore / fps, getSanitizedPlaybackRate(clip.speed));
    if (state?.key === key && state.error)
        throw state.error;
    return state?.key === key ? state.url ?? null : null;
};
