import assert from 'node:assert/strict';
import {mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {test} from 'node:test';
import {applyAudioEnvelope, audioEnvelopeWav, compileAudioEnvelope} from '@banodoco/timeline-composition/evaluation';
import {prepareNormalAudioSources} from '../normal-audio-sources.mjs';
import {compiledAudioSourceClock} from '../compiled-audio-source-clock.mjs';

const sampleRate = 30000, frames = 60000;
const wav = (left, right) => {
  const data = Buffer.alloc(44 + frames * 4);
  data.write('RIFF'); data.writeUInt32LE(data.length - 8, 4); data.write('WAVEfmt ', 8);
  data.writeUInt32LE(16, 16); data.writeUInt16LE(1, 20); data.writeUInt16LE(2, 22);
  data.writeUInt32LE(sampleRate, 24); data.writeUInt32LE(sampleRate * 4, 28);
  data.writeUInt16LE(4, 32); data.writeUInt16LE(16, 34);
  data.write('data', 36); data.writeUInt32LE(frames * 4, 40);
  for (let i = 0; i < frames; i++) {
    data.writeInt16LE(left, 44 + i * 4); data.writeInt16LE(right, 46 + i * 4);
  }
  return data;
};
const fixture = run => {
  const scratch = mkdtempSync(path.join(os.tmpdir(), 'normal-audio-test-'));
  try {
    const source = path.join(scratch, 'original.wav'), attemptDirectory = path.join(scratch, 'attempt');
    mkdirSync(attemptDirectory); writeFileSync(source, wav(8192, -4096));
    run({scratch, source, attemptDirectory});
  } finally { rmSync(scratch, {recursive: true, force: true}); }
};
const clip = {id: 'sound', track: 'A1', asset: 'sound', at: 0, from: 0, to: 2,
  volume: 3, params: {fadeIn: 0.5, fadeOut: 0.5}};

for (const [fps, from, speed, curve] of [
  [30, 0, 1, 'linear'], [30, 0, 1, 'equal-power'],
  [30, 0.05, 1.5, 'equal-power'], [30000 / 1001, 0.05, 1.5, 's-curve'],
]) test(`owner-supplied original phase: fps=${fps}, from=${from}, speed=${speed}, curve=${curve}`, () => fixture(args => {
  const originalClip = {...clip, from, speed,
    params: {fadeIn: 0.5, fadeOut: 0.5, fadeInCurve: curve, fadeOutCurve: curve}};
  const sliced = {...originalClip, id: 'slice', from: from + 0.5 * speed, to: Math.min(2, from + 1.5 * speed),
    app: {astrid_slice_origin: {from: 99, to: 99}}};
  const before = JSON.stringify({originalClip, sliced});
  const [whole, part] = prepareNormalAudioSources({...args, fps, clips: [
    {clip: originalClip, source: args.source}, {clip: sliced, originalClip, source: args.source},
  ]});
  const wholePcm = readFileSync(whole.path), partPcm = readFileSync(part.path);
  assert.equal(wholePcm.length, 44 + frames * 8);
  assert.equal(partPcm.length, wholePcm.length);
  for (let at = 44; at < wholePcm.length; at += 4) {
    assert.ok(Math.abs(partPcm.readFloatLE(at) - wholePcm.readFloatLE(at)) < 1e-7, `sample byte ${at}`);
  }
  if (from === 0 && speed === 1) assert.equal(partPcm.readFloatLE(44 + 15000 * 8), 0.25);
  assert.deepEqual(part.envelope, JSON.parse(part.envelope_key));
  assert.equal(JSON.stringify({originalClip, sliced}), before);
  const reference = [new Float32Array(frames).fill(0.25), new Float32Array(frames).fill(-0.125)];
  const clock = compiledAudioSourceClock(originalClip, fps);
  applyAudioEnvelope(reference, sampleRate, compileAudioEnvelope(originalClip, fps), clock.source_from, clock.playback_rate);
  assert.deepEqual(wholePcm, Buffer.from(audioEnvelopeWav(reference, sampleRate)));
}));

test('same envelope and clock retain distinct actual source bytes, not a swapped WAV', () => fixture(args => {
  const other = path.join(args.scratch, 'other.wav'); writeFileSync(other, wav(-16384, 2048));
  const [a, b] = prepareNormalAudioSources({...args, fps: 30, clips: [
    {clip, source: args.source}, {clip: {...clip, asset: 'other'}, source: other},
  ]});
  assert.equal(a.envelope_key, b.envelope_key);
  assert.equal(a.source_from, b.source_from); assert.equal(a.playback_rate, b.playback_rate);
  assert.notEqual(a.path, b.path);
  assert.equal(readFileSync(a.path).readFloatLE(44 + 15000 * 8), 0.25);
  assert.equal(readFileSync(b.path).readFloatLE(44 + 15000 * 8), -0.5);
}));

test('recursive multiply/minimum phase translation preserves envelope fields', () => fixture(args => {
  const originalClip = {...clip, audio: {envelope: {keyframes: [{at: 0, value: 2}],
    multiply: [{keyframes: [{at: 0, value: 0, curve: 's-curve'}, {at: 0.5, value: 1}]}],
    minimum: [{keyframes: [{at: 0, value: 0.5}]}]}}};
  const [row] = prepareNormalAudioSources({...args, fps: 30, clips: [
    {clip: {...originalClip, from: 0.5}, originalClip, source: args.source},
  ]});
  assert.equal(row.envelope.keyframes[0].at, -0.5);
  assert.equal(row.envelope.multiply[0].keyframes[0].at, -0.5);
  assert.equal(row.envelope.multiply[0].keyframes[0].curve, 's-curve');
  assert.equal(row.envelope.minimum[0].keyframes[0].at, -0.5);
  assert.equal(readFileSync(row.path).readFloatLE(44 + 15000 * 8), 0.25);
}));

test('forged prepared WAVs and double apply fail before preparing any source', () => fixture(args => {
  const [row] = prepareNormalAudioSources({...args, fps: 30, clips: [{clip, source: args.source}]});
  for (const file of [row.path, '/wrong.wav']) {
    assert.throws(() => prepareNormalAudioSources({...args, fps: 30, clips: [
      {clip: {...clip, audio: {render_source: {file, envelope_key: row.envelope_key,
        source_from: row.source_from, playback_rate: row.playback_rate}}}, source: args.source},
    ]}), /already prepared/);
  }
}));

test('failed batches clean prepared predecessors and preserve unrelated attempt files', () => fixture(args => {
  writeFileSync(path.join(args.attemptDirectory, 'unrelated'), 'keep');
  assert.throws(() => prepareNormalAudioSources({...args, fps: 30, clips: [
    {clip, source: args.source}, {clip: {...clip, params: {fadeIn: 0.5, fadeInCurve: 'unknown'}}, source: args.source},
  ]}), /ENVELOPE_UNRENDERED/);
  assert.deepEqual(readdirSync(args.attemptDirectory), ['unrelated']);
}));
