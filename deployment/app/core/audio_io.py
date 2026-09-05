"""音频探测 / 解码 / 编码，全部基于 ffmpeg(ffprobe) + soundfile，保证 mp3|wav|m4a 进出同格式"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

# 输出容器 -> (ffmpeg 复用器, 音频编码器, 默认码率)
LOSSY_PROFILE = {
    "mp3": ("mp3", "libmp3lame", "128k"),
    "m4a": ("mp4", "aac", "128k"),
    "aac": ("adts", "aac", "128k"),
    "ogg": ("ogg", "libvorbis", "128k"),
    "opus": ("opus", "libopus", "64k"),
    "flac": ("flac", "flac", None),
}
WAV_SUBTYPE = {1: "PCM_U8", 2: "PCM_16", 3: "PCM_24", 4: "PCM_32"}

_BITRATE_RE = re.compile(r"^\d{1,4}\s*[kKmM]?$")


class AudioError(Exception):
    pass


def _ffprobe() -> str:
    exe = shutil.which("ffprobe")
    if not exe:
        raise AudioError("未找到 ffprobe，无法解析音频信息")
    return exe


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise AudioError("未找到 ffmpeg，无法处理 mp3/m4a 等压缩格式")
    return exe


def normalize_extension(name: str) -> str:
    """同时支持传入文件名(a.mp3)或裸扩展名(mp3)"""
    base, ext = os.path.splitext(str(name))
    ext = ext.lower().lstrip(".")
    if not ext:
        ext = base.lower().lstrip(".")
    if ext == "wave":
        ext = "wav"
    if ext in ("mp4", "m4a"):
        ext = "m4a"
    return ext


def content_type_of(ext: str) -> str:
    return {
        "wav": "audio/wav",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "flac": "audio/flac",
        "ogg": "audio/ogg",
    }.get(ext, "application/octet-stream")


def sanitize_bitrate(bitrate: Optional[str]) -> Optional[str]:
    if not bitrate:
        return None
    value = str(bitrate).strip()
    if not _BITRATE_RE.match(value):
        return None
    return value.replace(" ", "").lower()


def probe(path: str, timeout: float = 20.0) -> Dict[str, Any]:
    """读取音频元信息：时长、采样率、声道、码率、编码"""
    info: Dict[str, Any] = {
        "duration": 0.0,
        "sample_rate": 0,
        "channels": 0,
        "bit_rate": 0,
        "codec_name": "",
        "format_name": "",
        "probe_backend": "none",
    }
    try:
        cmd = [
            _ffprobe(),
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            "-select_streams", "a:0",
            path,
        ]
        out = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if out.returncode == 0 and out.stdout:
            data = json.loads(out.stdout.decode("utf-8", "ignore"))
            stream = (data.get("streams") or [{}])[0]
            fmt = data.get("format") or {}
            info.update(
                {
                    "duration": _to_float(stream.get("duration") or fmt.get("duration")),
                    "sample_rate": _to_int(stream.get("sample_rate")),
                    "channels": _to_int(stream.get("channels")),
                    "bit_rate": _to_int(stream.get("bit_rate") or fmt.get("bit_rate")),
                    "codec_name": stream.get("codec_name") or "",
                    "format_name": fmt.get("format_name") or "",
                    "probe_backend": "ffprobe",
                }
            )
            return info
    except Exception:
        pass

    # 回退：soundfile（wav/flac/ogg）
    try:
        meta = sf.info(path)
        info.update(
            {
                "duration": float(meta.duration),
                "sample_rate": int(meta.samplerate),
                "channels": int(meta.channels),
                "bit_rate": 0,
                "codec_name": meta.subtype or "",
                "format_name": meta.format or "",
                "probe_backend": "soundfile",
            }
        )
    except Exception as exc:
        raise AudioError(f"无法解析音频文件 {path}: {exc}") from exc
    return info


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def decode_pcm(
    path: str,
    sample_rate: int,
    channels: int = 1,
    start: Optional[float] = None,
    duration: Optional[float] = None,
    timeout: float = 120.0,
) -> np.ndarray:
    """用 ffmpeg 解码为 float32 PCM，返回形状为 [channels, samples]"""
    cmd = [_ffmpeg(), "-v", "quiet", "-nostdin"]
    if start and start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", path]
    if duration and duration > 0:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-ac", str(int(channels)), "-ar", str(int(sample_rate)), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise AudioError(f"ffmpeg 解码失败: {proc.stderr.decode('utf-8', 'ignore')[:500]}")
    data = np.frombuffer(proc.stdout, dtype="<f4")
    if channels > 1 and data.size >= channels:
        data = data[: data.size // channels * channels].reshape(-1, channels).T
    else:
        data = data.reshape(1, -1)
    return np.ascontiguousarray(data, dtype=np.float32)


def encode_audio(
    pcm: np.ndarray,
    src_sample_rate: int,
    out_path: str,
    ext: str,
    target_sample_rate: int = 0,
    channels: int = 0,
    bitrate: Optional[str] = None,
    sample_width: int = 2,
    timeout: float = 300.0,
) -> Dict[str, Any]:
    """
    把 float32 PCM（取值 [-1, 1]，形状 [channels, samples]）写出为目标格式的音频文件。

    wav 用 soundfile 直写以保留位深；mp3/m4a 等用 ffmpeg 编码，可精确控制码率，
    全程只做一次有损编码。
    """
    ext = normalize_extension(ext)
    if pcm.ndim == 1:
        pcm = pcm.reshape(1, -1)
    pcm = np.nan_to_num(pcm, nan=0.0, posinf=0.0, neginf=0.0)
    pcm = np.clip(pcm, -1.0, 1.0)

    out_sr = int(target_sample_rate or src_sample_rate or 16000)
    out_channels = int(channels or pcm.shape[0] or 1)

    if pcm.shape[0] != out_channels:
        if out_channels == 1:
            pcm = pcm.mean(axis=0, keepdims=True)
        elif out_channels >= 2 and pcm.shape[0] == 1:
            pcm = np.repeat(pcm, out_channels, axis=0)
        else:
            pcm = pcm[:out_channels]

    if out_sr != int(src_sample_rate):
        pcm = _resample(pcm, int(src_sample_rate), out_sr)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    if ext == "wav":
        subtype = WAV_SUBTYPE.get(int(sample_width), "PCM_16")
        pcm_int = _to_pcm_int(pcm, sample_width)
        sf.write(out_path, pcm_int.T, out_sr, subtype=subtype)
        return {"encoder": "soundfile", "subtype": subtype, "sample_rate": out_sr, "channels": out_channels}

    if ext not in LOSSY_PROFILE:
        raise AudioError(f"不支持的输出格式: {ext}")

    muxer, codec, default_br = LOSSY_PROFILE[ext]
    br = sanitize_bitrate(bitrate) or default_br
    raw_fmt, pcm_bytes = _to_raw_bytes(pcm, sample_width=2)

    cmd = [
        _ffmpeg(), "-v", "error", "-nostdin", "-y",
        "-f", raw_fmt,
        "-ar", str(out_sr),
        "-ac", str(out_channels),
        "-i", "-",
        "-vn",
        "-c:a", codec,
    ]
    if br:
        cmd += ["-b:a", br]
    if ext == "m4a":
        cmd += ["-movflags", "+faststart"]
    cmd += ["-f", muxer, out_path]

    proc = subprocess.run(cmd, input=pcm_bytes, capture_output=True, timeout=timeout)
    if proc.returncode != 0 or not os.path.isfile(out_path):
        raise AudioError(f"ffmpeg 编码失败: {proc.stderr.decode('utf-8', 'ignore')[:500]}")
    return {
        "encoder": "ffmpeg",
        "codec": codec,
        "bitrate": br or "",
        "sample_rate": out_sr,
        "channels": out_channels,
    }


def _resample(pcm: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """重采样，按 soxr > librosa > scipy 的优先级选择实现"""
    if src_sr == dst_sr:
        return pcm
    errors = []

    try:
        import soxr

        out = soxr.resample(pcm.astype(np.float32), src_sr, dst_sr)
        return np.ascontiguousarray(out, dtype=np.float32)
    except Exception as exc:
        errors.append(f"soxr: {exc}")

    try:
        import librosa

        out = np.empty(
            (pcm.shape[0], int(round(pcm.shape[1] * dst_sr / float(src_sr)))), dtype=np.float32
        )
        for i in range(pcm.shape[0]):
            out[i] = librosa.resample(pcm[i], orig_sr=src_sr, target_sr=dst_sr).astype(np.float32)
        return out
    except Exception as exc:
        errors.append(f"librosa: {exc}")

    try:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(int(src_sr), int(dst_sr))
        up, down = int(dst_sr) // g, int(src_sr) // g
        out = resample_poly(pcm.astype(np.float64), up, down, axis=1)
        return np.ascontiguousarray(out, dtype=np.float32)
    except Exception as exc:
        errors.append(f"scipy: {exc}")

    raise AudioError(f"重采样失败 {src_sr}->{dst_sr} ({'; '.join(errors)})")


def _to_pcm_int(pcm: np.ndarray, sample_width: int) -> np.ndarray:
    bits = {1: 8, 2: 16, 3: 24, 4: 32}.get(int(sample_width), 16)
    scale = float(2 ** (bits - 1)) - 1.0
    arr = np.clip(pcm * scale, -(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
    if bits <= 8:
        return arr.astype(np.uint8)
    if bits <= 16:
        return arr.astype(np.int16)
    return arr.astype(np.int32)


def _to_raw_bytes(pcm: np.ndarray, sample_width: int) -> Tuple[str, bytes]:
    arr = _to_pcm_int(pcm, sample_width)
    if arr.dtype == np.int16:
        return "s16le", arr.T.astype("<i2").tobytes()
    if arr.dtype == np.int32:
        return "s32le", arr.T.astype("<i4").tobytes()
    return "u8", arr.T.astype("<u1").tobytes()


def list_segments(duration: float, max_seconds: float) -> List[Tuple[Optional[float], Optional[float]]]:
    """从音频的头/中/尾各取一段用于分析，返回 [(start, duration), ...]"""
    if not duration or duration <= 0 or not max_seconds or max_seconds <= 0 or duration <= max_seconds:
        return [(None, None)]
    part = max_seconds / 3.0
    segs: List[Tuple[Optional[float], Optional[float]]] = [(0.0, part)]
    middle = max(0.0, duration / 2.0 - part / 2.0)
    segs.append((middle, part))
    tail = max(0.0, duration - part)
    segs.append((tail, part))
    return segs
