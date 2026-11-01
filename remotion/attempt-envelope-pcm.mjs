import {execFileSync} from 'node:child_process';
import {closeSync, mkdtempSync, openSync, readSync, rmSync, statSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {
  applyAudioEnvelope, audioEnvelopeWav, compileAudioEnvelope,
} from '@banodoco/timeline-composition/evaluation';

// Inactive preparation kernel. The caller supplies the compiled clip's public
// source clock, using compiledAudioSourceClock from the public trim/rate helpers.
// Never pass a previously prepared source or persist these results as authored data.
export const prepareAttemptEnvelopePcm = ({
  clip, fps, source, attemptDirectory, sourceClock,
}) => {
  if (clip.audio?.render_source !== undefined) throw new Error('source already prepared');
  const envelope = compileAudioEnvelope(clip, fps);
  if (!envelope) return undefined;
  if (!sourceClock || !Number.isFinite(sourceClock.source_from)
    || sourceClock.source_from < 0 || !Number.isFinite(sourceClock.playback_rate)
    || sourceClock.playback_rate <= 0) throw new Error('public source clock required');
  if (!path.isAbsolute(source) || !statSync(source).isFile()
    || !path.isAbsolute(attemptDirectory) || !statSync(attemptDirectory).isDirectory()) {
    throw new Error('local source and attempt directory required');
  }
  const run = (program, args, maxBuffer) => execFileSync(program, args, {
    maxBuffer, timeout: 30000, stdio: ['ignore', 'pipe', 'pipe'],
  });
  const probe = JSON.parse(run('ffprobe', [
    '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'a:0',
    '-show_entries', 'format=format_name,start_time:stream=codec_name,sample_rate,channels,time_base,start_pts,start_time', '-of', 'json', source,
  ], 64 * 1024).toString());
  const stream = probe.streams?.[0];
  const sampleRate = Number(stream?.sample_rate), count = stream?.channels;
  if (!Number.isSafeInteger(sampleRate) || sampleRate <= 0
    || !Number.isSafeInteger(count) || count <= 0) {
    throw new Error('source PCM clock cannot be retained');
  }
  if (stream.start_time !== undefined && Number(stream.start_time) !== 0) {
    // MP3 demuxer start includes encoder/decoder priming, already removed by
    // native FFmpeg. Require packet skip + exact presentation ticks, not a
    // codec name or rounded decimal start alone. No sample/clock adjustment.
    let gaplessOrigin = false;
    if (stream.codec_name === 'mp3' && probe.format?.format_name === 'mp3') {
      const packets = JSON.parse(run('ffprobe', [
        '-v', 'error', '-protocol_whitelist', 'file,pipe', '-select_streams', 'a:0',
        '-read_intervals', '%+#1', '-show_packets', '-show_entries',
        'packet=pts,dts:packet_side_data=side_data_type,skip_samples,discard_padding,skip_reason,discard_reason',
        '-of', 'json', source,
      ], 64 * 1024).toString());
      const packet = packets.packets?.[0];
      const skip = packet?.side_data_list?.find(data => data.side_data_type === 'Skip Samples');
      const base = /^(\d+)\/(\d+)$/.exec(stream.time_base ?? '');
      if (packet?.pts === 0 && packet.dts === 0
        && Number.isSafeInteger(skip?.skip_samples) && skip.skip_samples > 0
        && skip.skip_reason === 0 && skip.discard_reason === 0
        && skip.discard_padding === 0
        && Number.isSafeInteger(stream.start_pts) && stream.start_pts > 0
        && base && BigInt(base[1]) > 0n && BigInt(base[2]) > 0n) {
        gaplessOrigin = BigInt(stream.start_pts) * BigInt(base[1]) * BigInt(sampleRate)
          === BigInt(skip.skip_samples) * BigInt(base[2]);
      }
    }
    if (!gaplessOrigin) throw new Error('source PCM clock cannot be retained');
  }
  const directory = mkdtempSync(path.join(attemptDirectory, 'envelope-'));
  try {
    // File-backed subprocess stdout avoids retaining/capping the decoded pipe.
    // Public apply/WAV still need whole-source arrays: this is NOT bounded RAM.
    const decoded = path.join(directory, 'decoded.f32');
    const fd = openSync(decoded, 'wx+');
    let channels;
    try {
      execFileSync('ffmpeg', [
        '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe', '-i', source,
        '-map', '0:a:0', '-threads', '1', '-c:a', 'pcm_f32le', '-f', 'f32le', 'pipe:1',
      ], {maxBuffer: 64 * 1024, timeout: 30000, stdio: ['ignore', fd, 'pipe']});
      const bytes = statSync(decoded).size, frameBytes = count * 4;
      if (!bytes || bytes % frameBytes !== 0) throw new Error('invalid decoded PCM');
      const samples = bytes / frameBytes;
      channels = Array.from({length: count}, () => new Float32Array(samples));
      const block = Buffer.alloc(Math.max(1, Math.floor(64 * 1024 / frameBytes)) * frameBytes);
      for (let position = 0; position < bytes;) {
        const wanted = Math.min(block.length, bytes - position);
        let received = 0;
        while (received < wanted) {
          const length = readSync(fd, block, received, wanted - received, position + received);
          if (!length) throw new Error('incomplete decoded PCM');
          received += length;
        }
        for (let offset = 0; offset < wanted; offset += frameBytes) {
          for (let channel = 0; channel < count; channel++) {
            const value = block.readFloatLE(offset + channel * 4);
            if (!Number.isFinite(value)) throw new Error('non-finite source PCM');
            channels[channel][(position + offset) / frameBytes] = value;
          }
        }
        position += wanted;
      }
    } finally {
      closeSync(fd);
    }
    rmSync(decoded);
    applyAudioEnvelope(channels, sampleRate, envelope,
      sourceClock.source_from, sourceClock.playback_rate);
    const output = path.join(directory, 'source.wav');
    writeFileSync(output, Buffer.from(audioEnvelopeWav(channels, sampleRate)), {flag: 'wx'});
    // The existing consumer parses JSON before comparing its canonical key.
    // Only the later asset-stage owner can attach the attempt's HTTP file URL.
    return {path: output, envelope_key: JSON.stringify(envelope),
      source_from: sourceClock.source_from, playback_rate: sourceClock.playback_rate};
  } catch (error) {
    rmSync(directory, {recursive: true, force: true});
    throw error;
  }
};
