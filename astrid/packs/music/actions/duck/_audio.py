"""PCM I/O and BS.1770 measurements; numpy and the pack's ffmpeg dependency only."""
from __future__ import annotations

import json
import math
import re
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from astrid.pack_api import AstridError

TAG_KIND = "astrid.music.duck.v1"


def ffmpeg(args: list[str], data: bytes | None = None) -> bytes:
    proc = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", *args], input=data,
                          capture_output=True, check=False)
    if proc.returncode:
        raise AstridError(f"music.duck ffmpeg failed: {proc.stderr.decode(errors='replace')[-1200:]}")
    return proc.stdout


@dataclass(frozen=True)
class Audio:
    samples: np.ndarray
    rate: int
    bits: int
    floating: bool
    fmt: bytes | None = None


def riff_chunks(path: Path):
    with path.open("rb") as stream:
        header = stream.read(12)
        if header[:4] != b"RIFF" or header[8:] != b"WAVE":
            return
        while chunk := stream.read(8):
            if len(chunk) != 8:
                raise AstridError("truncated WAV chunk")
            kind, length = struct.unpack("<4sI", chunk)
            position = stream.tell()
            # Only metadata needs materializing (a source can be very large).
            yield kind, stream.read(length) if kind in (b"fmt ", b"LIST") else b""
            stream.seek(position + length + (length & 1))


def read_duck_tag(path: Path) -> dict | None:
    found = None
    for kind, value in riff_chunks(Path(path)):
        if kind != b"LIST" or value[:4] != b"INFO":
            continue
        at = 4
        while at + 8 <= len(value):
            key, size = struct.unpack_from("<4sI", value, at)
            body = value[at + 8:at + 8 + size].rstrip(b"\0")
            at += 8 + size + (size & 1)
            if key == b"ICMT" and TAG_KIND.encode() in body:
                try:
                    found = json.loads(body)
                except (ValueError, UnicodeError) as exc:
                    raise AstridError("invalid music.duck source tag; restore the original source") from exc
                if not isinstance(found, dict) or found.get("kind") != TAG_KIND:
                    raise AstridError("invalid music.duck source tag")
    return found


def load_audio(path: Path) -> Audio:
    if not path.is_file():
        raise AstridError(f"audio file does not exist: {path}")
    fmt = next((value for kind, value in riff_chunks(path) if kind == b"fmt "), None)
    if fmt is not None:
        code, channels, rate, _, _, bits = struct.unpack_from("<HHIIHH", fmt)
        if code == 65534 and len(fmt) >= 40:
            code = struct.unpack_from("<H", fmt, 24)[0]
            valid_bits = struct.unpack_from("<H", fmt, 18)[0]
            if valid_bits not in (0, bits):
                raise AstridError("WAV packed valid-bit formats are not supported; use full-width PCM")
        if code not in (1, 3) or bits not in ((32, 64) if code == 3 else (8, 16, 24, 32)):
            raise AstridError("music.duck requires integer PCM or IEEE float WAV source audio")
        floating = code == 3
    else:
        # Compressed inputs have a decoded sample format. Preserve that format in WAV.
        proc = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0",
                               "-show_streams", "-of", "json", str(path)],
                              capture_output=True, check=False)
        if proc.returncode:
            raise AstridError(f"could not inspect audio: {path.name}")
        streams = json.loads(proc.stdout).get("streams") or []
        if not streams:
            raise AstridError("source contains no audio stream")
        info = streams[0]
        channels, rate = int(info["channels"]), int(info["sample_rate"])
        sample_fmt = info.get("sample_fmt", "").removesuffix("p")
        floating = sample_fmt in ("flt", "dbl")
        bits = {"u8": 8, "s16": 16, "s32": 32, "flt": 32, "dbl": 64}.get(sample_fmt)
        if bits is None:
            raise AstridError(f"unsupported decoded sample format: {sample_fmt}")
    raw = ffmpeg(["-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "f64le", "-"])
    samples = np.frombuffer(raw, "<f8").reshape(-1, channels)
    if not len(samples) or not np.isfinite(samples).all():
        raise AstridError("audio must contain finite samples")
    return Audio(samples, rate, bits, floating, fmt)


def weighted_power(samples: np.ndarray, rate: int) -> np.ndarray:
    """BS.1770 K weighting, with filter coefficients evaluated at the actual rate."""
    channels = samples.shape[1]
    if channels not in (1, 2, 6, 8):
        raise AstridError("EBU R128 analysis supports mono, stereo, 5.1 or 7.1 sources")
    k = math.tan(math.pi * 1681.974450955533 / rate)
    q, vh = 0.7071752369554196, 10 ** (3.999843853973347 / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    b = [(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0,
         (vh - vb * k / q + k * k) / a0]
    a = [1, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]
    k = math.tan(math.pi * 38.13547087602444 / rate)
    a0 = 1 + k / 0.5003270373238773 + k * k
    a2 = [1, 2 * (k * k - 1) / a0, (1 - k / 0.5003270373238773 + k * k) / a0]
    def biquad(bb, aa):
        return "biquad=" + ":".join(f"{name}{i}={v:.17g}" for name, vals in (("b", bb), ("a", aa))
                                     for i, v in enumerate(vals)) + ":precision=f64"
    filtered = ffmpeg(["-v", "error", "-f", "f64le", "-ar", str(rate), "-ac", str(channels),
                       "-i", "-", "-af", biquad(b, a) + "," + biquad([1, -2, 1], a2),
                       "-f", "f64le", "-"], np.asarray(samples, dtype="<f8").tobytes())
    x = np.frombuffer(filtered, "<f8").reshape(-1, channels)
    weights = np.ones(channels)
    if channels >= 6:
        weights[3] = 0  # LFE, ffmpeg native channel order
        weights[4:] = 1.41
    return (x * x) @ weights


def lufs(power):
    return -0.691 + 10 * np.log10(np.maximum(power, 1e-30))


def moments(power: np.ndarray, rate: int, times: np.ndarray) -> np.ndarray:
    """400 ms windows centred on each requested time (edge windows clipped)."""
    centers = np.rint(times * rate).astype(np.int64)
    half = round(.2 * rate)
    lo, hi = np.clip(centers - half, 0, len(power)), np.clip(centers + half, 0, len(power))
    c = np.concatenate(([0.0], np.cumsum(power)))
    return lufs(np.maximum(0, c[hi] - c[lo]) / np.maximum(1, hi - lo))


def integrated(power: np.ndarray, rate: int, *, gate: bool = True) -> float | None:
    """EBU R128 400 ms blocks, 100 ms hop, -70 LUFS absolute / -10 LU relative gates."""
    win, hop = round(.4 * rate), round(.1 * rate)
    if len(power) < win:
        return None
    c = np.concatenate(([0.0], np.cumsum(power)))
    starts = np.arange(0, len(power) - win + 1, hop)
    p = np.maximum(0, c[starts + win] - c[starts]) / win
    mask = lufs(p) > -70
    if not mask.any():
        return None
    if gate:
        mask &= lufs(p) > float(lufs(p[mask].mean())) - 10
    return float(lufs(p[mask].mean()))


def _chunk(kind: bytes, body: bytes) -> bytes:
    return kind + struct.pack("<I", len(body)) + body + b"\0" * (len(body) & 1)


def write_duck_wav(path: Path, audio: Audio, gain_db: np.ndarray, tag: dict) -> None:
    """Deterministic TPDF dither at the original precision; unity sections stay bit-exact."""
    channels = audio.samples.shape[1]
    fmt = audio.fmt or struct.pack("<HHIIHH", 3 if audio.floating else 1, channels, audio.rate,
                                  audio.rate * channels * audio.bits // 8,
                                  channels * audio.bits // 8, audio.bits)
    comment = json.dumps(tag, sort_keys=True, separators=(",", ":")).encode() + b"\0"
    meta = _chunk(b"LIST", b"INFO" + _chunk(b"ICMT", comment))
    data_size = len(audio.samples) * channels * audio.bits // 8
    fact = _chunk(b"fact", struct.pack("<I", len(audio.samples))) if audio.floating else b""
    size = 4 + len(_chunk(b"fmt ", fmt)) + len(fact) + 8 + data_size + (data_size & 1) + len(meta)
    if size > 0xffffffff:
        raise AstridError("ducked WAV exceeds RIFF's 4 GiB limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    with path.open("wb") as out:
        out.write(b"RIFF" + struct.pack("<I", size) + b"WAVE" + _chunk(b"fmt ", fmt) + fact)
        out.write(b"data" + struct.pack("<I", data_size))
        for start in range(0, len(audio.samples), 65536):
            x = audio.samples[start:start + 65536]
            g = gain_db[start:start + len(x)]
            y = x * np.power(10., g[:, None] / 20)
            precision = (24 if audio.bits == 32 else 53) if audio.floating else audio.bits - 1
            dither = (rng.random(y.shape) - rng.random(y.shape)) / 2 ** precision
            y += dither * (g != 0)[:, None]
            if audio.floating:
                encoded = y.astype("<f4" if audio.bits == 32 else "<f8").tobytes()
            else:
                scale = 2 ** (audio.bits - 1)
                pcm = np.clip(np.rint(y * scale), -scale, scale - 1).astype(np.int64)
                if audio.bits == 8:
                    encoded = (pcm + 128).astype("u1").tobytes()
                elif audio.bits == 24:
                    encoded = ((pcm[..., None] >> np.array([0, 8, 16])) & 255).astype("u1").tobytes()
                else:
                    encoded = pcm.astype("<i2" if audio.bits == 16 else "<i4").tobytes()
            out.write(encoded)
        out.write(b"\0" * (data_size & 1) + meta)


def true_peak(path: Path) -> float:
    proc = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-v", "info", "-i", str(path),
                           "-af", "ebur128=peak=true", "-f", "null", "-"],
                          capture_output=True, text=True, check=False)
    values = re.findall(r"Peak:\s*([+-]?(?:\d+(?:\.\d+)?|inf))\s*dBFS", proc.stderr)
    if proc.returncode or not values:
        raise AstridError("ffmpeg did not measure the output true peak")
    return float(values[-1])
