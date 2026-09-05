"""噪声自动检测：基于分位数噪声底估计的 SNR 评估，判断音频是否需要降噪。

思路：
1. 把音频切成短帧(默认 20ms)并计算每帧功率；
2. 取功率最低的 noise_percentile% 帧的平均功率作为**噪声底**（语音间隙/本底噪声）；
3. 取功率最高的 speech_percentile% 帧的平均功率作为**语音电平**；
4. SNR = 10 * log10(语音电平 / 噪声底)，低于阈值才认为需要降噪；
5. 语音占比过低（近乎静音）或时长过短时直接判定为不需要降噪。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple

import numpy as np

from .audio_io import decode_pcm, list_segments

EPS = 1e-12


@dataclass
class NoiseReport:
    need_denoise: bool
    reason: str
    snr_db: Optional[float]
    noise_floor_db: Optional[float]
    speech_level_db: Optional[float]
    speech_ratio: float
    duration: float
    analyzed_seconds: float

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in ("snr_db", "noise_floor_db", "speech_level_db"):
            if data[key] is not None:
                data[key] = round(float(data[key]), 2)
        data["speech_ratio"] = round(float(self.speech_ratio), 4)
        data["duration"] = round(float(self.duration), 3)
        data["analyzed_seconds"] = round(float(self.analyzed_seconds), 3)
        return data


class NoiseDetector:
    def __init__(self, cfg, logger):
        """
        cfg: DenoiseConfig
        """
        self._cfg = cfg
        self._logger = logger

    def analyze_file(self, path: str, duration: float) -> NoiseReport:
        cfg = self._cfg
        if duration > 0 and 0 < duration < cfg.min_duration_s:
            return NoiseReport(False, "too_short", None, None, None, 0.0, duration, 0.0)

        segments = list_segments(duration, cfg.analyze_max_seconds)
        chunks: List[np.ndarray] = []
        analyzed = 0.0
        for start, seg_dur in segments:
            try:
                pcm = decode_pcm(path, 16000, channels=1, start=start, duration=seg_dur)
            except Exception as exc:
                self._logger.warning("噪声检测解码失败(%.2fs~%.2fs): %s", start or 0.0, seg_dur or 0.0, exc)
                continue
            if pcm.size:
                chunks.append(pcm[0])
                analyzed += pcm.shape[1] / 16000.0

        if not chunks:
            # 探测不到内容时保守处理：交给模型降噪
            return NoiseReport(True, "probe_failed", None, None, None, 0.0, duration or 0.0, 0.0)

        mono = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        return self.analyze(mono, 16000, duration=duration or analyzed, analyzed_seconds=analyzed)

    def analyze(
        self,
        mono: np.ndarray,
        sample_rate: int,
        duration: float = 0.0,
        analyzed_seconds: float = 0.0,
    ) -> NoiseReport:
        cfg = self._cfg
        mono = np.asarray(mono, dtype=np.float32).reshape(-1)
        if analyzed_seconds <= 0:
            analyzed_seconds = mono.shape[0] / float(sample_rate or 16000)
        if duration <= 0:
            duration = analyzed_seconds

        if mono.size < int(sample_rate * 0.05):
            return NoiseReport(False, "too_short", None, None, None, 0.0, duration, analyzed_seconds)

        frame_len = max(64, int(sample_rate * max(5.0, cfg.frame_ms) / 1000.0))
        hop = max(1, frame_len // 2)
        if mono.shape[0] < frame_len:
            frame_len = mono.shape[0]
            hop = frame_len

        frames = _frame_view(mono, frame_len, hop)
        power = np.mean(frames.astype(np.float64) ** 2, axis=1) + EPS
        power = power[np.isfinite(power) & (power > EPS)]
        if power.size < 5:
            return NoiseReport(False, "silent", None, None, None, 0.0, duration, analyzed_seconds)

        noise_p = float(np.percentile(power, float(cfg.noise_percentile)))
        speech_p = float(np.percentile(power, float(cfg.speech_percentile)))

        # 噪声底：所有低于噪声分位点的帧功率均值（比单点分位数更稳健）
        noise_frames = power[power <= noise_p]
        noise_power = float(np.mean(noise_frames)) if noise_frames.size else noise_p
        noise_power = max(noise_power, EPS)

        # 语音电平：高于噪声分位点的帧功率均值
        speech_frames = power[power >= speech_p]
        speech_power = float(np.mean(speech_frames)) if speech_frames.size else speech_p

        # 全部转回 Python 原生类型，避免 numpy 标量泄漏到 JSON 序列化环节
        snr_db = float(10.0 * np.log10(max(speech_power - noise_power, EPS) / noise_power))
        noise_floor_db = float(10.0 * np.log10(noise_power))
        speech_level_db = float(10.0 * np.log10(max(speech_power, EPS)))

        # 语音活跃帧占比（高于噪声底 6dB 视为语音）
        active = power > noise_power * (10 ** (6.0 / 10.0))
        speech_ratio = float(np.mean(active)) if active.size else 0.0

        if speech_ratio < float(cfg.min_speech_ratio):
            return NoiseReport(
                False, "no_speech", snr_db, noise_floor_db, speech_level_db, speech_ratio, duration, analyzed_seconds
            )

        need = bool(snr_db < float(cfg.snr_threshold_db))
        reason = "snr_below_threshold" if need else "snr_above_threshold"
        return NoiseReport(
            need, reason, snr_db, noise_floor_db, speech_level_db, speech_ratio, duration, analyzed_seconds
        )


def _frame_view(signal: np.ndarray, frame_len: int, hop: int) -> np.ndarray:
    if signal.shape[0] <= frame_len:
        return signal[:frame_len].reshape(1, -1)
    num_frames = 1 + (signal.shape[0] - frame_len) // hop
    idx = np.arange(frame_len)[None, :] + hop * np.arange(num_frames)[:, None]
    return signal[idx]
