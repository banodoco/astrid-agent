// Native source coverage for the inactive normal adapter, without HTTP or renderer activation.
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {closeSync, openSync, readSync, statSync, writeSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {after, before, test} from 'node:test';
import {applyAudioEnvelope, audioEnvelopeWav, compileAudioEnvelope} from '@banodoco/timeline-composition/evaluation';
import {prepareNormalAudioSources} from '../normal-audio-sources.mjs';
import {compiledAudioSourceClock} from '../compiled-audio-source-clock.mjs';

const sampleRate = 48000, frames = 86400, count = 2, fps = 30000 / 1001;
const run = (program, args) => execFileSync(program, args, {
  stdio: ['ignore', 'pipe', 'pipe'], timeout: 30000, maxBuffer: 2 * 1024 * 1024,
});
const encode = args => run('ffmpeg', ['-v', 'error', '-nostdin', '-filter_threads', '1',
  ...args.slice(0, -1), '-threads', '1', args.at(-1)]);
const decode = file => run('ffmpeg', ['-v', 'error', '-nostdin', '-i', file,
  '-map', '0:a:0', '-threads', '1', '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1']);
const probe = file => JSON.parse(run('ffprobe', ['-v', 'error', '-select_streams', 'a:0',
  '-show_entries', 'format=format_name,start_time:stream=codec_name,sample_rate,channels,time_base,start_pts,start_time,duration_ts',
  '-of', 'json', file]).toString());
const packets = file => JSON.parse(run('ffprobe', ['-v', 'error', '-select_streams', 'a:0',
  '-show_packets', '-show_entries', 'packet=pts,dts,duration:packet_side_data=side_data_type,skip_samples,discard_padding,skip_reason,discard_reason',
  '-of', 'json', file]).toString()).packets;
const skip = packet => packet.side_data_list?.find(data => data.side_data_type === 'Skip Samples');
const planar = pcm => {
  assert.equal(pcm.length % 8, 0);
  const channels = Array.from({length: count}, () => new Float32Array(pcm.length / 8));
  for (let i = 0; i < channels[0].length; i++) for (let channel = 0; channel < count; channel++) {
    channels[channel][i] = pcm.readFloatLE(i * 8 + channel * 4);
  }
  return channels;
};
const onsets = pcm => planar(pcm).map(channel => channel.findIndex(value => Math.abs(value) > 0.03));
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const referenceWav = (pcm, clip, clock) => {
  const channels = planar(pcm);
  applyAudioEnvelope(channels, sampleRate, compileAudioEnvelope(clip, fps),
    clock.source_from, clock.playback_rate);
  return Buffer.from(audioEnvelopeWav(channels, sampleRate));
};
const wavHeader = wav => {
  assert.equal(wav.toString('ascii', 0, 4), 'RIFF');
  assert.equal(wav.readUInt16LE(20), 3);
  assert.equal(wav.readUInt16LE(22), count);
  assert.equal(wav.readUInt32LE(24), sampleRate);
  assert.equal(wav.readUInt16LE(34), 32);
  assert.equal(wav.readUInt32LE(40), frames * count * 4);
  assert.equal(wav.length, 44 + frames * count * 4);
};
const unityClip = {
  id: 'music', asset: 'music', track: 'A1', at: 5, from: 0, to: 1.8, volume: 7,
  audio: {envelope: {keyframes: [{at: 0, value: 1}]}},
};
let scratch, source, mp3, attemptDirectory, reference, native;
const observed = {};

before(() => {
  scratch = mkdtempSync(path.join(os.tmpdir(), 'gapless-mp3-origin-'));
  attemptDirectory = path.join(scratch, 'attempt');
  mkdirSync(attemptDirectory);
  writeFileSync(path.join(attemptDirectory, 'unrelated'), 'keep');
  source = path.join(scratch, 'source.wav');
  // Independent PCM16 source. Unequal delayed tones expose priming on both
  // channels; the original reference is never trimmed, shifted or aligned.
  const bytes = Buffer.alloc(44 + frames * 4);
  bytes.write('RIFF'); bytes.writeUInt32LE(bytes.length - 8, 4); bytes.write('WAVEfmt ', 8);
  bytes.writeUInt32LE(16, 16); bytes.writeUInt16LE(1, 20); bytes.writeUInt16LE(count, 22);
  bytes.writeUInt32LE(sampleRate, 24); bytes.writeUInt32LE(sampleRate * 4, 28);
  bytes.writeUInt16LE(4, 32); bytes.writeUInt16LE(16, 34);
  bytes.write('data', 36); bytes.writeUInt32LE(frames * 4, 40);
  for (let i = 0; i < frames; i++) {
    bytes.writeInt16LE(i < 9600 ? 0 : Math.round(32768 * 0.1 * Math.sin(2 * Math.PI * 440 * (i - 9600) / sampleRate)), 44 + i * 4);
    bytes.writeInt16LE(i < 14400 ? 0 : Math.round(-32768 * 0.075 * Math.sin(2 * Math.PI * 660 * (i - 14400) / sampleRate)), 46 + i * 4);
  }
  writeFileSync(source, bytes);
  mp3 = path.join(scratch, 'source.mp3');
  encode(['-i', source, '-c:a', 'libmp3lame', '-b:a', '128k', mp3]);
  encode(['-i', source, '-c:a', 'libmp3lame', '-b:a', '128k', '-write_xing', '0', path.join(scratch, 'unknown.mp3')]);
  encode(['-i', source, '-c:a', 'aac', '-b:a', '128k', '-f', 'adts', path.join(scratch, 'unknown.aac')]);
  encode(['-i', source, '-c:a', 'aac', '-b:a', '128k', path.join(scratch, 'source.m4a')]);
  encode(['-itsoffset', '0.125', '-i', path.join(scratch, 'source.m4a'), '-c', 'copy', path.join(scratch, 'offset-aac.mka')]);
  encode(['-i', mp3, '-c', 'copy', '-output_ts_offset', '0.125', path.join(scratch, 'offset-mp3.mka')]);
  reference = decode(source);
  native = decode(mp3);
  for (const name of ['source.mp3', 'unknown.mp3', 'unknown.aac', 'source.m4a', 'offset-aac.mka', 'offset-mp3.mka']) {
    const file = path.join(scratch, name), metadata = probe(file), packetList = packets(file), pcm = decode(file);
    observed[name] = {stream: metadata.streams[0], format: metadata.format,
      first: packetList[0], last: packetList.at(-1),
      frames: pcm.length / 8, onset: onsets(pcm), pcm};
  }
});
after(() => rmSync(scratch, {recursive: true, force: true}));

test('normal adapter gapless MP3 decodes through public apply into a full native-source WAV', () => {
  const metadata = observed['source.mp3'];
  assert.equal(reference.length / 8, frames);
  assert.deepEqual(onsets(reference), [9606, 14405]);
  assert.equal(metadata.stream.start_time, '0.023021');
  assert.equal(metadata.format.start_time, '0.023021');
  assert.equal(metadata.stream.time_base, '1/14112000');
  assert.equal(metadata.stream.start_pts, 324870);
  assert.equal(metadata.first.pts, 0);
  assert.equal(skip(metadata.first).skip_samples, 1105);
  assert.equal(skip(metadata.last).discard_padding, 47);
  assert.equal(metadata.frames, frames);
  const delta = metadata.onset.map((value, channel) => value - onsets(reference)[channel]);
  assert.deepEqual(delta, [0, 1]); // Codec waveform difference, no fitted alignment.
  const [numerator, denominator] = metadata.stream.time_base.split('/').map(Number);
  const packetSamples = packets(mp3).reduce((sum, packet) =>
    sum + packet.duration * numerator * sampleRate / denominator, 0);
  assert.equal(packetSamples - skip(metadata.first).skip_samples - skip(metadata.last).discard_padding, frames);
  const before = digest(readFileSync(mp3)), graph = JSON.stringify(unityClip);
  const sourceClock = compiledAudioSourceClock(unityClip, fps);
  const [prepared] = prepareNormalAudioSources({fps, attemptDirectory, clips: [{clip: unityClip, source: mp3}]});
  const wav = readFileSync(prepared.path);
  wavHeader(wav);
  assert.equal(wav.subarray(44).equals(native), true); // Every raw native decoded sample, both channels.
  assert.equal(wav.equals(referenceWav(native, unityClip, sourceClock)), true);
  assert.equal(decode(prepared.path).equals(native), true);
  assert.equal(prepared.source_from, 0);
  assert.equal(prepared.playback_rate, 1);
  assert.equal(prepared.envelope_key, JSON.stringify(compileAudioEnvelope(unityClip, fps)));
  assert.deepEqual(prepared.envelope, JSON.parse(prepared.envelope_key));
  assert.deepEqual(readdirSync(path.dirname(prepared.path)), ['source.wav']);
  assert.equal(digest(readFileSync(mp3)), before);
  assert.equal(JSON.stringify(unityClip), graph);
  console.log(JSON.stringify({positive: 'gapless MP3', frames, onsetDelta: delta,
    skip: 1105, discard: 47, wavSha256: digest(wav)}));
});

test('rational public trim and child speed retain the full source and expose a wrong decoder origin', () => {
  const clip = {...unityClip, from: 0.051, to: 1.401, speed: 1.5, hold: 0.8,
    params: {fadeIn: 0.2, fadeInCurve: 'equal-power', fadeOut: 0.15}, audio: undefined};
  const graph = JSON.stringify(clip), sourceClock = compiledAudioSourceClock(clip, fps);
  assert.deepEqual(sourceClock, {source_from: 2 / fps, playback_rate: 1.5});
  assert.notEqual(sourceClock.source_from, clip.from);
  const [prepared] = prepareNormalAudioSources({fps, attemptDirectory, clips: [{clip, source: mp3}]});
  const actual = readFileSync(prepared.path), expected = referenceWav(native, clip, sourceClock);
  wavHeader(actual);
  assert.equal(actual.equals(expected), true, 'all full-source public WAV bytes');
  assert.equal(decode(prepared.path).equals(actual.subarray(44)), true, 'native float WAV readback');
  // A priming debt accidentally added to the public origin must fail exact
  // whole-source comparison. Never fit or correct it per clip to get a pass.
  const wrong = referenceWav(native, clip, {...sourceClock,
    source_from: sourceClock.source_from + 1105 / sampleRate});
  assert.notEqual(digest(actual), digest(wrong));
  assert.equal(actual.equals(wrong), false);
  assert.equal(prepared.source_from, sourceClock.source_from);
  assert.equal(prepared.playback_rate, sourceClock.playback_rate);
  assert.equal(JSON.stringify(clip), graph);
  console.log(JSON.stringify({positive: 'rational trim + child speed', sourceClock,
    wavSha256: digest(actual), wrongOriginSha256: digest(wrong)}));
});

test('a source-backed timestamped MP3 without skip metadata cannot use the relaxation', () => {
  const bad = observed['offset-mp3.mka'];
  assert.equal(bad.stream.codec_name, 'mp3');
  assert.notEqual(Number(bad.stream.start_time), 0);
  assert.equal(skip(bad.first), undefined);
  assert.notEqual(bad.frames, frames);
  assert.ok(bad.onset.every((value, channel) => value - onsets(reference)[channel] > 1000));
  const before = readdirSync(attemptDirectory);
  assert.throws(() => prepareNormalAudioSources({fps, attemptDirectory, clips: [
    {clip: unityClip, source: path.join(scratch, 'offset-mp3.mka')},
  ]}), /source PCM clock cannot be retained/);
  assert.deepEqual(readdirSync(attemptDirectory), before);
  assert.equal(readFileSync(path.join(attemptDirectory, 'unrelated'), 'utf8'), 'keep');
});

test('direct unknown-origin evidence retains zero-start MP3 and absent-start/offset AAC debt', () => {
  const unknown = observed['unknown.mp3'], adts = observed['unknown.aac'];
  const m4a = observed['source.m4a'], offset = observed['offset-aac.mka'];
  assert.equal(Number(unknown.stream.start_time), 0);
  assert.equal(skip(unknown.first), undefined);
  assert.notEqual(unknown.frames, frames);
  assert.ok(unknown.onset.every((value, channel) => value - onsets(reference)[channel] > 1000));
  assert.equal(adts.stream.start_time, undefined);
  assert.equal(adts.format.start_time, undefined);
  assert.equal(skip(adts.first), undefined);
  assert.equal(adts.frames, 88064);
  assert.deepEqual(adts.onset.map((value, channel) => value - onsets(reference)[channel]), [1024, 1024]);
  assert.equal(skip(m4a.first).skip_samples, 1024);
  assert.equal(m4a.frames, 87040); // Onset preserved, 640 trailing frames: unresolved count policy.
  assert.deepEqual(m4a.onset, onsets(reference));
  assert.notEqual(Number(offset.stream.start_time), 0);
  assert.equal(skip(offset.first), undefined);
  assert.equal(offset.frames, 88064);
  assert.deepEqual(offset.onset.map((value, channel) => value - onsets(reference)[channel]), [1024, 1024]);
  for (const [name, {pcm, ...data}] of Object.entries(observed)) {
    console.log(JSON.stringify({characterization: name, ...data,
      onsetDelta: data.onset.map((value, channel) => value - onsets(reference)[channel])}));
  }
});

test('normal adapter retains every native stereo frame beyond 64 MiB with the public trim/rate clock', () => {
  const longFrames = 8388609, decodedBytes = longFrames * count * 4;
  const directory = mkdtempSync(path.join(os.tmpdir(), 'normal-long-source-'));
  try {
    const source = path.join(directory, 'music.wav'), attemptDirectory = path.join(directory, 'attempt');
    mkdirSync(attemptDirectory);
    // Independent float WAV, written in small blocks. Tail impulses expose
    // truncation, clipping and channel loss beyond the former stdout cap.
    const header = Buffer.alloc(44);
    header.write('RIFF'); header.writeUInt32LE(decodedBytes + 36, 4); header.write('WAVEfmt ', 8);
    header.writeUInt32LE(16, 16); header.writeUInt16LE(3, 20); header.writeUInt16LE(count, 22);
    header.writeUInt32LE(sampleRate, 24); header.writeUInt32LE(sampleRate * 8, 28);
    header.writeUInt16LE(8, 32); header.writeUInt16LE(32, 34);
    header.write('data', 36); header.writeUInt32LE(decodedBytes, 40);
    const fd = openSync(source, 'wx'), block = Buffer.alloc(64 * 1024);
    try {
      writeSync(fd, header);
      for (let start = 0; start < longFrames; start += block.length / 8) {
        const size = Math.min(block.length / 8, longFrames - start);
        for (let i = 0; i < size; i++) {
          block.writeFloatLE(start + i === longFrames - 1 ? 3 : 0.25, i * 8);
          block.writeFloatLE(start + i === longFrames - 2 ? -2 : -0.125, i * 8 + 4);
        }
        writeSync(fd, block.subarray(0, size * 8));
      }
    } finally { closeSync(fd); }
    const fileDigest = (file, offset = 0) => {
      const hash = createHash('sha256'), fd = openSync(file, 'r'), block = Buffer.alloc(64 * 1024);
      try {
        let length;
        while ((length = readSync(fd, block, 0, block.length, offset))) {
          hash.update(block.subarray(0, length)); offset += length;
        }
        return hash.digest('hex');
      } finally { closeSync(fd); }
    };
    const clip = {...unityClip, from: 0.051, to: longFrames / sampleRate, speed: 1.5,
      audio: {envelope: {keyframes: [
        {at: 0, value: 0, curve: 'equal-power'}, {at: 0.2, value: 1},
        {at: 60, value: 1, curve: 's-curve'}, {at: 90, value: 0.5},
      ]}}};
    const graph = JSON.stringify(clip), before = fileDigest(source);
    const sourceClock = compiledAudioSourceClock(clip, fps);
    assert.deepEqual(sourceClock, {source_from: 2 / fps, playback_rate: 1.5});
    const referenceDigests = () => {
      const channels = [new Float32Array(longFrames).fill(0.25), new Float32Array(longFrames).fill(-0.125)];
      channels[0][longFrames - 1] = 3; channels[1][longFrames - 2] = -2;
      applyAudioEnvelope(channels, sampleRate, compileAudioEnvelope(clip, fps),
        sourceClock.source_from, sourceClock.playback_rate);
      const wav = Buffer.from(audioEnvelopeWav(channels, sampleRate));
      return {wav: digest(wav), pcm: digest(wav.subarray(44))};
    };
    const expected = referenceDigests();
    global.gc?.();
    const [prepared] = prepareNormalAudioSources({fps, attemptDirectory, clips: [{clip, source}]});
    assert.equal(decodedBytes, 64 * 1024 * 1024 + 8);
    assert.equal(statSync(prepared.path).size, decodedBytes + 44);
    assert.equal(fileDigest(prepared.path), expected.wav, 'all full-source public WAV bytes');
    assert.deepEqual(readdirSync(path.dirname(prepared.path)), ['source.wav']);
    const metadata = probe(prepared.path).streams[0];
    assert.equal(metadata.sample_rate, String(sampleRate));
    assert.equal(metadata.channels, count);
    assert.equal(metadata.duration_ts, longFrames);
    const readback = path.join(directory, 'readback.f32'), output = openSync(readback, 'wx');
    try {
      execFileSync('ffmpeg', ['-v', 'error', '-nostdin', '-i', prepared.path,
        '-map', '0:a:0', '-threads', '1', '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1'],
      {stdio: ['ignore', output, 'pipe'], timeout: 30000, maxBuffer: 64 * 1024});
    } finally { closeSync(output); }
    assert.equal(statSync(readback).size, decodedBytes);
    assert.equal(fileDigest(readback), expected.pcm, 'every native readback float byte');
    assert.equal(prepared.source_from, sourceClock.source_from);
    assert.equal(prepared.playback_rate, sourceClock.playback_rate);
    assert.deepEqual(prepared.envelope, clip.audio.envelope);
    assert.equal(JSON.stringify(clip), graph);
    assert.equal(fileDigest(source), before);
    console.log(JSON.stringify({positive: 'normal adapter >64MiB', frames: longFrames,
      sampleRate, channels: count, decodedBytes, sourceClock, wavSha256: expected.wav,
      maxRssKiB: process.resourceUsage().maxRSS}));
  } finally { rmSync(directory, {recursive: true, force: true}); }
});
