import { AUDIO_GAIN_CURVES } from './audio-curves.generated';
import { getClipDurationInFrames } from './duration';
export class AudioEnvelopeError extends Error {
    code;
    constructor(message, duck = false) {
        const code = duck ? 'DUCK_UNRENDERED' : 'ENVELOPE_UNRENDERED';
        super(`${code}: ${message}`);
        this.code = code;
    }
}
export const validateAudioEnvelope = (envelope, host = 'composition-player') => {
    if (!['composition-player', 'ffmpeg'].includes(host)) {
        throw new AudioEnvelopeError(`unsupported sample consumer ${host}`, envelope?.duck);
    }
    if (!envelope || typeof envelope !== 'object' ||
        Object.keys(envelope).some((k) => !['keyframes', 'multiply', 'minimum', 'duck'].includes(k)) ||
        !Array.isArray(envelope.keyframes) || !envelope.keyframes.length) {
        throw new AudioEnvelopeError('invalid compiled envelope');
    }
    let previous = -Infinity;
    for (const point of envelope.keyframes) {
        if (!point || Object.keys(point).some((k) => !['at', 'value', 'curve'].includes(k)) ||
            !Number.isFinite(point.at) || point.at <= previous ||
            !Number.isFinite(point.value) || point.value < 0 ||
            !Object.hasOwn(AUDIO_GAIN_CURVES, point.curve ?? 'linear')) {
            throw new AudioEnvelopeError('invalid keyframe or unknown curve');
        }
        previous = point.at;
    }
    for (const operation of ['multiply', 'minimum']) {
        if (envelope[operation] !== undefined && !Array.isArray(envelope[operation])) {
            throw new AudioEnvelopeError(`invalid ${operation}`);
        }
        for (const child of envelope[operation] ?? [])
            validateAudioEnvelope(child, host);
    }
};
const curveGain = (name, progress) => {
    const u = Math.max(0, Math.min(1, progress));
    const spec = AUDIO_GAIN_CURVES[name];
    switch (spec.kind) {
        case 'linear': return u;
        case 'equal-power': return Math.sin(u * Math.PI / 2);
        case 'exponential': return Math.expm1(u * spec.exponent) / Math.expm1(spec.exponent);
        case 's-curve': return u * u * (3 - 2 * u);
    }
};
/** Validated plan, queried at a sample's clip-local timeline seconds. */
export const audioEnvelopeAt = (envelope, seconds) => {
    const points = envelope.keyframes;
    let value = points[points.length - 1].value;
    if (seconds <= points[0].at)
        value = points[0].value;
    else
        for (let i = 1; i < points.length; i++) {
            const left = points[i - 1], right = points[i];
            if (seconds < right.at) {
                const u = (seconds - left.at) / (right.at - left.at);
                value = right.value >= left.value
                    ? left.value + (right.value - left.value) * curveGain(left.curve ?? 'linear', u)
                    : right.value + (left.value - right.value) * curveGain(left.curve ?? 'linear', 1 - u);
                break;
            }
        }
    for (const child of envelope.multiply ?? [])
        value *= audioEnvelopeAt(child, seconds);
    if (envelope.minimum?.length)
        value *= Math.min(...envelope.minimum.map((child) => audioEnvelopeAt(child, seconds)));
    return value;
};
/** Derived runtime output only: never save this shape back into the document. */
export const compileAudioEnvelope = (clip, fps) => {
    if (clip.audio?.envelope !== undefined) {
        validateAudioEnvelope(clip.audio.envelope);
        return clip.audio.envelope;
    }
    const app = clip.app;
    if ((app?.astrid?.duck ?? app?.duck) !== undefined) {
        throw new AudioEnvelopeError('authored duck requires compiled narration projection', true);
    }
    const params = clip.params ?? {};
    const duration = getClipDurationInFrames(clip, fps) / fps;
    const multiply = [];
    for (const name of ['fadeIn', 'fadeOut']) {
        const raw = params[name];
        if (typeof raw !== 'number' || !Number.isFinite(raw) || raw <= 0)
            continue;
        const length = Math.round(raw * fps) / fps;
        if (!length)
            continue;
        const curve = (params[`${name}Curve`] ?? 'linear');
        multiply.push({ keyframes: name === 'fadeIn'
                ? [{ at: 0, value: 0, curve }, { at: length, value: 1 }]
                : [{ at: duration - length, value: 1, curve }, { at: duration, value: 0 }] });
    }
    if (!multiply.length)
        return undefined;
    const envelope = { keyframes: [{ at: 0, value: 1 }], multiply };
    validateAudioEnvelope(envelope);
    return envelope;
};
/** PCM consumer used by AudioTrack before playback. Keep trim and speed in source coordinates. */
export const applyAudioEnvelope = (channels, sampleRate, envelope, sourceFrom = 0, playbackRate = 1) => {
    validateAudioEnvelope(envelope);
    if (!(sampleRate > 0) || !(playbackRate > 0) || !Number.isFinite(sampleRate + playbackRate + sourceFrom)) {
        throw new AudioEnvelopeError('invalid sample clock');
    }
    const length = channels[0]?.length ?? 0;
    if (channels.some((channel) => channel.length !== length))
        throw new AudioEnvelopeError('mismatched channels');
    for (let sample = 0; sample < length; sample++) {
        const gain = audioEnvelopeAt(envelope, (sample / sampleRate - sourceFrom) / playbackRate);
        for (const channel of channels)
            channel[sample] *= gain;
    }
};
/** Interleaved IEEE float WAV avoids quantizing samples or clipping >unity input. */
export const audioEnvelopeWav = (channels, sampleRate) => {
    const count = channels.length, samples = channels[0]?.length ?? 0;
    if (!count || channels.some((c) => c.length !== samples))
        throw new AudioEnvelopeError('invalid PCM channels');
    const buffer = new ArrayBuffer(44 + samples * count * 4), view = new DataView(buffer);
    const text = (at, value) => [...value].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    text(0, 'RIFF');
    view.setUint32(4, buffer.byteLength - 8, true);
    text(8, 'WAVE');
    text(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 3, true);
    view.setUint16(22, count, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * count * 4, true);
    view.setUint16(32, count * 4, true);
    view.setUint16(34, 32, true);
    text(36, 'data');
    view.setUint32(40, samples * count * 4, true);
    for (let sample = 0; sample < samples; sample++)
        for (let c = 0; c < count; c++) {
            view.setFloat32(44 + (sample * count + c) * 4, channels[c][sample], true);
        }
    return buffer;
};
export const envelopeKey = (value) => {
    if (Array.isArray(value))
        return `[${value.map(envelopeKey).join(',')}]`;
    if (value !== null && typeof value === 'object') {
        const object = value;
        return `{${Object.keys(object).sort().map((k) => `${JSON.stringify(k)}:${envelopeKey(object[k])}`).join(',')}}`;
    }
    return JSON.stringify(value);
};
export const encodedEnvelopeSource = (prepared, envelope, sourceFrom, playbackRate) => {
    validateAudioEnvelope(envelope);
    let key;
    try {
        key = prepared && envelopeKey(JSON.parse(prepared.envelope_key));
    }
    catch { /* malformed preparation falls through to the addressed refusal */ }
    if (!prepared || !/^https?:\/\//.test(prepared.file) ||
        key !== envelopeKey(envelope) ||
        prepared.source_from !== sourceFrom || prepared.playback_rate !== playbackRate) {
        throw new AudioEnvelopeError('external encoder requires matching attempt-local envelope PCM', envelope.duck);
    }
    return prepared.file;
};
