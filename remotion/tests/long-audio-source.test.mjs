import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {closeSync, mkdirSync, mkdtempSync, openSync, readSync, readdirSync, rmSync, statSync, writeFileSync, writeSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {test} from 'node:test';
import {applyAudioEnvelope, audioEnvelopeWav} from '@banodoco/timeline-composition/evaluation';
import {prepareAttemptEnvelopePcm} from '../attempt-envelope-pcm.mjs';
import {compiledAudioSourceClock} from '../compiled-audio-source-clock.mjs';

const sampleRate = 48000, channels = 2, frames = 8388609;
const decodedBytes = frames * channels * 4; // 64 MiB plus one stereo frame.
const fps = 30000 / 1001;
const clip = {
  id: 'long-music', track: 'A1', asset: 'music', at: 0, from: 0.051,
  to: frames / sampleRate, speed: 1.5, volume: 7,
  audio: {envelope: {keyframes: [
    {at: 0, value: 0, curve: 'equal-power'}, {at: 0.2, value: 1},
    {at: 60, value: 1, curve: 's-curve'}, {at: 90, value: 0.5},
  ]}},
};
const digest = file => {
  const hash = createHash('sha256'), fd = openSync(file, 'r'), block = Buffer.alloc(64 * 1024);
  try {
    let length;
    while ((length = readSync(fd, block))) hash.update(block.subarray(0, length));
    return hash.digest('hex');
  } finally { closeSync(fd); }
};
const read = (file, at, length) => {
  const fd = openSync(file, 'r'), result = Buffer.alloc(length);
  try { assert.equal(readSync(fd, result, 0, length, at), length); return result; }
  finally { closeSync(fd); }
};
// Independent native-rate stereo float WAV, written in bounded blocks. Unequal
// channels and edge impulses expose truncation, clipping and interleaving loss.
const sourceWav = (file, count = frames, nonfinite = false) => {
  const header = Buffer.alloc(44), bytes = count * 8;
  header.write('RIFF'); header.writeUInt32LE(bytes + 36, 4); header.write('WAVEfmt ', 8);
  header.writeUInt32LE(16, 16); header.writeUInt16LE(3, 20); header.writeUInt16LE(2, 22);
  header.writeUInt32LE(sampleRate, 24); header.writeUInt32LE(sampleRate * 8, 28);
  header.writeUInt16LE(8, 32); header.writeUInt16LE(32, 34);
  header.write('data', 36); header.writeUInt32LE(bytes, 40);
  const fd = openSync(file, 'wx'), block = Buffer.alloc(64 * 1024);
  try {
    writeSync(fd, header);
    for (let start = 0; start < count; start += block.length / 8) {
      const size = Math.min(block.length / 8, count - start);
      for (let i = 0; i < size; i++) {
        block.writeFloatLE(nonfinite ? Infinity : start + i === count - 1 ? 3 : 0.25, i * 8);
        block.writeFloatLE(start + i === count - 2 ? -2 : -0.125, i * 8 + 4);
      }
      writeSync(fd, block.subarray(0, size * 8));
    }
  } finally { closeSync(fd); }
};

test('ordinary long source exceeds the old stdout cap and retains full-source public WAV bytes', () => {
  assert.equal(process.env.TIMELINE_HEAVY, '1', 'run with the explicitly authorized heavy flag');
  const scratch = mkdtempSync(path.join(os.tmpdir(), 'long-envelope-'));
  try {
    const source = path.join(scratch, 'music.wav'), attemptDirectory = path.join(scratch, 'attempt');
    mkdirSync(attemptDirectory);
    sourceWav(source);
    const before = digest(source), graph = JSON.stringify(clip);
    const sourceClock = compiledAudioSourceClock(clip, fps);
    assert.deepEqual(sourceClock, {source_from: 2 / fps, playback_rate: 1.5});
    const args = {clip, fps, source, attemptDirectory, sourceClock};
    assert.equal(decodedBytes, 64 * 1024 * 1024 + 8);
    // Independent whole-source channels and unchanged public apply/WAV are the
    // oracle. No chunk-origin arithmetic or replacement gain policy is used.
    const referenceDigest = () => {
      const reference = [new Float32Array(frames).fill(0.25), new Float32Array(frames).fill(-0.125)];
      reference[0][frames - 1] = 3;
      reference[1][frames - 2] = -2;
      applyAudioEnvelope(reference, sampleRate, clip.audio.envelope,
        sourceClock.source_from, sourceClock.playback_rate);
      return createHash('sha256').update(Buffer.from(audioEnvelopeWav(reference, sampleRate))).digest('hex');
    };
    const expected = referenceDigest();
    global.gc?.();
    const prepared = prepareAttemptEnvelopePcm(args);
    assert.equal(digest(prepared.path), expected);
    assert.equal(statSync(prepared.path).size, decodedBytes + 44);
    assert.deepEqual(readdirSync(path.dirname(prepared.path)), ['source.wav']);
    const header = read(prepared.path, 0, 44);
    assert.equal(header.toString('ascii', 0, 4), 'RIFF');
    assert.equal(header.readUInt16LE(20), 3);
    assert.equal(header.readUInt16LE(22), channels);
    assert.equal(header.readUInt32LE(24), sampleRate);
    assert.equal(header.readUInt32LE(40), decodedBytes);
    assert.equal(read(prepared.path, 44, 8).readFloatLE(0), 0);
    // Far beyond the former limit: no truncation, gain baking or clipping.
    assert.equal(read(prepared.path, 44 + (frames - 1) * 8, 8).readFloatLE(0), 1.5);
    assert.equal(read(prepared.path, 44 + (frames - 2) * 8, 8).readFloatLE(4), -1);
    assert.equal(prepared.source_from, sourceClock.source_from);
    assert.equal(prepared.playback_rate, sourceClock.playback_rate);
    assert.equal(prepared.envelope_key, JSON.stringify(clip.audio.envelope));
    const decoded = path.join(scratch, 'readback.f32'), fd = openSync(decoded, 'wx');
    try {
      execFileSync('ffmpeg', ['-v', 'error', '-nostdin', '-i', prepared.path,
        '-map', '0:a:0', '-threads', '1', '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1'],
      {stdio: ['ignore', fd, 'pipe'], timeout: 30000, maxBuffer: 64 * 1024});
    } finally { closeSync(fd); }
    assert.equal(statSync(decoded).size, decodedBytes);
    assert.equal(read(decoded, (frames - 1) * 8, 8).readFloatLE(0), 1.5);
    assert.equal(digest(source), before);
    assert.equal(JSON.stringify(clip), graph);
    console.log(JSON.stringify({frames, sampleRate, channels, decodedBytes, seconds: frames / sampleRate,
      sourceClock, wavSha256: expected, maxRssKiB: process.resourceUsage().maxRSS}));
  } finally { rmSync(scratch, {recursive: true, force: true}); }
});

test('file-backed proposal cleans its own scratch on nonfinite PCM and decoder failure', () => {
  const scratch = mkdtempSync(path.join(os.tmpdir(), 'long-envelope-errors-'));
  try {
    const source = path.join(scratch, 'bad.wav'), attemptDirectory = path.join(scratch, 'attempt');
    mkdirSync(attemptDirectory);
    const sentinel = path.join(attemptDirectory, 'unrelated');
    writeFileSync(sentinel, 'keep');
    sourceWav(source, 100, true);
    const args = {clip, fps, source, attemptDirectory, sourceClock: compiledAudioSourceClock(clip, fps)};
    assert.throws(() => prepareAttemptEnvelopePcm(args), /non-finite source PCM/);
    assert.deepEqual(readdirSync(attemptDirectory), ['unrelated']);
    // Valid WAV header but no sample frames: ffprobe succeeds, ffmpeg produces
    // no PCM (or fails), and preparation never publishes a partial source.wav.
    writeFileSync(source, read(source, 0, 44));
    assert.throws(() => prepareAttemptEnvelopePcm(args));
    assert.deepEqual(readdirSync(attemptDirectory), ['unrelated']);
    assert.equal(digest(sentinel), createHash('sha256').update('keep').digest('hex'));
  } finally { rmSync(scratch, {recursive: true, force: true}); }
});
