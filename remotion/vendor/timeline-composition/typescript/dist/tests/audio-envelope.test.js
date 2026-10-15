import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AUDIO_GAIN_CURVES } from '../src/lib/audio-curves.generated';
import { audioEnvelopeAt, validateAudioEnvelope, compileAudioEnvelope, applyAudioEnvelope, audioEnvelopeWav } from '../src/lib/audio-envelope';
for (const curve of Object.keys(AUDIO_GAIN_CURVES))
    test(`sample PCM ${curve}: seam, pair, trim, speed and base gain independence`, () => {
        const incoming = { keyframes: [{ at: 0, value: 0, curve }, { at: 1, value: 1 }] };
        const outgoing = { keyframes: [{ at: 0, value: 1, curve }, { at: 1, value: 0 }] };
        const pcm = new Float32Array(96000).fill(.25);
        applyAudioEnvelope([pcm], 48000, incoming, .5, .5);
        for (let i = 0; i < pcm.length; i++) {
            assert.ok(Math.abs(pcm[i] - .25 * audioEnvelopeAt(incoming, (i / 48000 - .5) / .5)) < 2e-8);
        }
        if (curve === 'equal-power')
            for (let i = 0; i <= 48000; i++) {
                assert.ok(Math.abs(audioEnvelopeAt(incoming, i / 48000) ** 2 + audioEnvelopeAt(outgoing, i / 48000) ** 2 - 1) < 1e-12);
            }
    });
test('legacy overlap remains a product; explicit plan is authoritative', () => {
    const clip = { id: 'music', track: 'audio', at: 0, from: 0, to: 1, params: { fadeIn: .73, fadeOut: .83 } };
    const envelope = compileAudioEnvelope(clip, 30);
    for (let frame = 0; frame <= 30; frame++) {
        const expected = Math.min(1, frame / 22) * Math.min(1, (30 - frame) / 25);
        assert.ok(Math.abs(audioEnvelopeAt(envelope, frame / 30) - expected) < 1e-12);
    }
    assert.equal(compileAudioEnvelope({ ...clip, audio: { envelope } }, 30), envelope);
});
test('overlapping ducks choose minimum, fades multiply', () => {
    const weak = { keyframes: [{ at: 0, value: .5 }] }, strong = { keyframes: [{ at: 0, value: .25 }] };
    assert.equal(audioEnvelopeAt({ keyframes: [{ at: 0, value: .8 }], minimum: [weak, strong] }, .4), .2);
});
test('named refusals for unknown curves, invalid order, unsupported hosts and unprojected ducks', () => {
    const tagged = { id: 'bed', track: 'music', at: 0, to: 1, app: { astrid: { duck: { under: 'voice' } } } };
    assert.throws(() => compileAudioEnvelope(tagged, 30), /DUCK_UNRENDERED/);
    assert.throws(() => validateAudioEnvelope({ keyframes: [{ at: 0, value: 1, curve: 'bad' }] }), /ENVELOPE_UNRENDERED/);
    assert.throws(() => validateAudioEnvelope({ keyframes: [{ at: 1, value: 1 }, { at: 0, value: 1 }] }), /ENVELOPE_UNRENDERED/);
    assert.throws(() => validateAudioEnvelope({ keyframes: [{ at: 0, value: 1 }] }, 'frame-only'), /ENVELOPE_UNRENDERED/);
    assert.throws(() => validateAudioEnvelope({ keyframes: [{ at: 0, value: 1 }], duck: true }, 'frame-only'), /DUCK_UNRENDERED/);
});
test('float WAV serializes the processed samples without clipping', () => {
    const channels = [new Float32Array([.25, -.5, 1.5]), new Float32Array([.5, -.25, -1.5])];
    const wav = new DataView(audioEnvelopeWav(channels, 48000));
    assert.equal(wav.getUint16(20, true), 3);
    assert.equal(wav.getUint32(24, true), 48000);
    for (let s = 0; s < 3; s++)
        for (let c = 0; c < 2; c++)
            assert.equal(wav.getFloat32(44 + (s * 2 + c) * 4, true), channels[c][s]);
});
test('external encoding consumes matching prepared PCM once and preserves trim/rate ownership', async () => {
    const { encodedEnvelopeSource } = await import('../src/lib/audio-envelope');
    const envelope = { keyframes: [{ at: 0, value: 1 }], duck: true };
    const prepared = { file: 'http://127.0.0.1:1234/attempt/envelope.wav',
        envelope_key: '{"duck":true,"keyframes":[{"value":1.0,"at":0.0}]}', source_from: .5, playback_rate: 2 };
    // Python numeric serialization is normalized after parsing, with no second gain pass.
    assert.equal(encodedEnvelopeSource(prepared, envelope, .5, 2), prepared.file);
    for (const changed of [undefined, { ...prepared, source_from: 0 }, { ...prepared, playback_rate: 1 },
        { ...prepared, file: 'blob:private' }, { ...prepared, envelope_key: '{}' }, { ...prepared, envelope_key: 'broken' }]) {
        assert.throws(() => encodedEnvelopeSource(changed, envelope, .5, 2), /DUCK_UNRENDERED/);
    }
});
test('actual non-Player AudioTrack uses prepared URL with exactly one trim/rate and no browser bake', async () => {
    const { register } = await import('node:module');
    // Model the non-Player hook environment; execute the real AudioTrack and hook.
    const modules = {
        react: 'export const useMemo=f=>f(); export const useState=()=>[undefined,()=>{}]; export const useEffect=f=>f();',
        remotion: 'export const getRemotionEnvironment=()=>({isPlayer:false}); export const useBufferState=()=>({delayPlayback:()=>{throw Error("browser bake in encoder")}}); export const Audio="Audio"; export const staticFile=x=>x;',
        'react/jsx-runtime': 'export const jsx=(type,props)=>({type,props});',
    };
    const loader = `const modules=${JSON.stringify(modules)}; export async function resolve(s,c,next){if(modules[s])return {url:'data:text/javascript,'+encodeURIComponent(modules[s]),shortCircuit:true};return next(s,c);}`;
    register('data:text/javascript,' + encodeURIComponent(loader), { parentURL: import.meta.url });
    const { AudioTrack } = await import('../src/AudioTrack');
    const envelope = { keyframes: [{ at: 0, value: .5 }] };
    const clip = { id: 'occ:bed', track: 'music', at: 0, from: .5, to: 2.5, speed: 2,
        params: { fadeIn: 1 }, audio: { envelope, render_source: {
                file: 'http://127.0.0.1:1234/attempt/bed.wav', envelope_key: JSON.stringify(envelope), source_from: .5, playback_rate: 2,
            } } };
    const track = { id: 'music', label: 'Music', kind: 'audio', volume: .4 };
    const element = AudioTrack({ clip, track, fps: 30, assetEntry: { file: 'http://127.0.0.1:1234/source.wav' } });
    assert.equal(element.props.src, clip.audio.render_source.file);
    assert.equal(element.props.trimBefore, 15);
    assert.equal(element.props.trimAfter, 75);
    assert.equal(element.props.playbackRate, 2);
    assert.equal(element.props.volume, .4); // envelope was already baked; legacy fade stays single-applied
    assert.throws(() => AudioTrack({ clip: { ...clip, audio: { envelope } }, track, fps: 30 }), /ENVELOPE_UNRENDERED/);
});
