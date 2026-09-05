"""降噪引擎：模型池 + 资源准入 + 自动噪声检测 + 同格式输出"""

from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np

from . import audio_io, clearvoice_bridge
from .noise_detector import NoiseDetector, NoiseReport

MODEL_TASK = {
    "FRCRN_SE_16K": "speech_enhancement",
    "MossFormerGAN_SE_16K": "speech_enhancement",
    "MossFormer2_SE_48K": "speech_enhancement",
}


class ResourceBusy(Exception):
    """等待 GPU/CPU 资源超时"""


class ModelLoadError(Exception):
    pass


@dataclass
class ModelSlot:
    index: int
    name: str
    sampling_rate: int
    cv: Any
    model: Any
    lock: threading.Lock = field(default_factory=threading.Lock)
    used_count: int = 0
    last_used_ts: float = 0.0
    last_seconds: float = 0.0


class DenoiseEngine:
    def __init__(self, cfg, governor, logger):
        self.cfg = cfg
        self.governor = governor
        self.logger = logger
        self._pools: Dict[str, List[ModelSlot]] = {}
        self._rr: Dict[str, int] = {}
        self._pool_lock = threading.Lock()
        self.detector = NoiseDetector(cfg.denoise, logger)
        self._started = False
        # 保证槽位数不少于并发数，避免"拿到并发名额却等不到模型槽"的死锁
        self._pool_size = max(int(cfg.runtime.model_pool_size), int(cfg.concurrency.max_concurrency))
        if self._pool_size != int(cfg.runtime.model_pool_size):
            logger.warning(
                "model_pool_size(%s) < max_concurrency(%s)，已自动提升到 %s 以保证并发可用（显存占用会随之增加）",
                cfg.runtime.model_pool_size,
                cfg.concurrency.max_concurrency,
                self._pool_size,
            )

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self._started:
            return
        clearvoice_bridge.configure(
            model_root=self.cfg.models.root,
            allow_download=bool(self.cfg.models.allow_download),
            device=self.cfg.runtime.device,
        )
        clearvoice_bridge.install(self.logger)
        self._configure_torch()
        self.log_model_inventory()
        if self.cfg.runtime.preload:
            self.get_pool(self.cfg.runtime.model)
            for name in self._candidate_models():
                self.get_pool(name)
            self.warmup()
        self._started = True

    # ---------------- 模型自检（离线部署必需） ----------------

    def log_model_inventory(self) -> None:
        """启动时扫描模型目录，把"挂载了什么、缺什么"一次性说清楚"""
        root = self.cfg.models.root
        if not os.path.isdir(root):
            self.logger.error(
                "模型根目录不存在: %s —— 请把模型包挂载到该路径（例如 -v <模型包>:%s:ro）",
                root,
                root,
            )
            return
        try:
            entries = sorted(
                d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))
            )
        except OSError as exc:
            self.logger.error("无法读取模型目录 %s: %s", root, exc)
            return

        usable = [d for d in entries if d in MODEL_TASK]
        self.logger.info("模型根目录 %s 下发现 %d 个目录", root, len(entries))
        self.logger.info("本服务可用模型: %s", usable or "无")
        if not usable:
            self.logger.error(
                "没有找到任何可用的降噪模型。可用模型名为 %s，"
                "请确认挂载目录内包含同名子目录（内含 last_best_checkpoint）",
                sorted(MODEL_TASK),
            )
        wanted = self._candidate_models()
        missing = [m for m in wanted if m not in usable]
        if missing:
            self.logger.warning(
                "配置将要使用的模型 %s 中有缺失: %s（缺失模型在被请求时会直接报错，不会降级为随机权重）",
                wanted,
                missing,
            )

    def verify_weights(self, model_name: str) -> None:
        """校验权重完整性。

        原工程 load_model() 在权重缺失时只是 return，模型会带着随机初始化权重继续跑，
        产出无意义音频却不报错。离线场景下必须先拦住。
        """
        root = self.cfg.models.root
        model_dir = os.path.join(root, model_name)
        if not os.path.isdir(model_dir):
            raise ModelLoadError(
                f"模型目录不存在: {model_dir}。"
                f"请确认模型包已挂载到 {root}，且子目录名与模型名一致（可用: {sorted(MODEL_TASK)}）"
            )
        index_file = os.path.join(model_dir, "last_best_checkpoint")
        if not os.path.isfile(index_file):
            raise ModelLoadError(
                f"缺少权重索引文件: {index_file}。"
                f"请确认 {model_name} 的权重已完整下载"
            )
        try:
            with open(index_file, "r", encoding="utf-8") as fp:
                ckpt_name = fp.readline().strip()
        except OSError as exc:
            raise ModelLoadError(f"无法读取权重索引 {index_file}: {exc}") from exc
        if not ckpt_name:
            raise ModelLoadError(f"权重索引文件为空: {index_file}")
        ckpt_path = os.path.join(model_dir, ckpt_name)
        if not os.path.isfile(ckpt_path):
            raise ModelLoadError(
                f"权重文件不存在: {ckpt_path}（索引 {index_file} 指向 {ckpt_name}）"
            )
        size_mb = os.path.getsize(ckpt_path) / (1024.0 * 1024.0)
        if size_mb < 1.0:
            raise ModelLoadError(f"权重文件异常（小于 1MB，疑似下载不完整）: {ckpt_path}")
        self.logger.info("模型 %s 权重校验通过: %s (%.1fMB)", model_name, ckpt_name, size_mb)

    def stop(self) -> None:
        self.logger.info("降噪引擎停止")

    def _configure_torch(self) -> None:
        import torch

        torch.set_num_threads(max(1, int(self.cfg.runtime.torch_threads)))
        try:
            if self.cfg.runtime.allow_tf32 and torch.cuda.is_available():
                torch.backends.cuda.matmul.allow_tf32 = True
                torch.backends.cudnn.allow_tf32 = True
        except Exception as exc:  # pragma: no cover
            self.logger.debug("设置 TF32 失败: %s", exc)

        ratio = float(self.cfg.runtime.process_memory_ratio or 0.0)
        if ratio > 0 and torch.cuda.is_available():
            try:
                torch.cuda.set_per_process_memory_fraction(min(1.0, ratio))
                self.logger.info("已设置单进程显存占比上限: %.2f", ratio)
            except Exception as exc:  # pragma: no cover
                self.logger.warning("set_per_process_memory_fraction 失败: %s", exc)

    def _candidate_models(self) -> List[str]:
        names = {self.cfg.runtime.model}
        if self.cfg.runtime.auto_select_model:
            names.add("MossFormer2_SE_48K")
        return [n for n in names if n in MODEL_TASK]

    # ---------------- 模型池 ----------------

    def get_pool(self, model_name: str) -> List[ModelSlot]:
        with self._pool_lock:
            pool = self._pools.get(model_name)
            if pool:
                return pool
            if model_name not in MODEL_TASK:
                raise ModelLoadError(f"不支持的降噪模型: {model_name}")
            self.verify_weights(model_name)
            self.logger.info("加载模型 %s，实例数 %d", model_name, self._pool_size)
            pool = []
            for i in range(self._pool_size):
                pool.append(self._create_slot(i, model_name))
            self._pools[model_name] = pool
            self._rr[model_name] = 0
            return pool

    def _create_slot(self, index: int, model_name: str) -> ModelSlot:
        from clearvoice import ClearVoice

        started = time.monotonic()
        try:
            cv = ClearVoice(task=MODEL_TASK[model_name], model_names=[model_name])
        except Exception as exc:
            raise ModelLoadError(f"加载模型 {model_name} 失败: {exc}") from exc
        model = cv.models[0]
        if model is None:
            raise ModelLoadError(f"模型 {model_name} 未成功初始化，请检查权重是否挂载到 {self.cfg.models.root}")
        sampling_rate = int(getattr(model.args, "sampling_rate", 16000))

        if self.cfg.runtime.decode_window_s and self.cfg.runtime.decode_window_s > 0:
            model.args.decode_window = float(self.cfg.runtime.decode_window_s)
        if self.cfg.runtime.one_time_decode_length_s and self.cfg.runtime.one_time_decode_length_s > 0:
            model.args.one_time_decode_length = float(self.cfg.runtime.one_time_decode_length_s)

        slot = ModelSlot(
            index=index,
            name=model_name,
            sampling_rate=sampling_rate,
            cv=cv,
            model=model,
        )
        self.logger.info(
            "模型实例就绪 name=%s index=%d device=%s sampling_rate=%d decode_window=%s 耗时=%.2fs",
            model_name,
            index,
            getattr(model, "device", "?"),
            sampling_rate,
            getattr(model.args, "decode_window", "?"),
            time.monotonic() - started,
        )
        return slot

    def _acquire_slot(self, model_name: str, timeout_s: float) -> ModelSlot:
        pool = self.get_pool(model_name)
        deadline = time.monotonic() + max(1.0, timeout_s)
        while True:
            with self._pool_lock:
                start = self._rr.get(model_name, 0)
                self._rr[model_name] = (start + 1) % len(pool)
            for offset in range(len(pool)):
                slot = pool[(start + offset) % len(pool)]
                if slot.lock.acquire(blocking=False):
                    return slot
            if time.monotonic() >= deadline:
                raise ResourceBusy("获取模型实例超时，服务繁忙")
            time.sleep(0.02)

    def warmup(self) -> None:
        """用极短静音音频跑一次推理，提前完成 CUDA 上下文 / cuDNN 初始化"""
        import soundfile as sf

        tmp_wav = os.path.join(self.cfg.audio.temp_dir, "_warmup.wav")
        try:
            os.makedirs(self.cfg.audio.temp_dir, exist_ok=True)
            sr = 16000
            sf.write(tmp_wav, np.zeros(int(sr * 0.5), dtype=np.float32), sr, subtype="PCM_16")
            for name in list(self._pools.keys()):
                slot = self._acquire_slot(name, 60.0)
                try:
                    t0 = time.monotonic()
                    slot.cv(input_path=tmp_wav, online_write=False)
                    self.logger.info("预热完成 model=%s 耗时=%.2fs", name, time.monotonic() - t0)
                except Exception as exc:
                    self.logger.warning("预热失败 model=%s: %s", name, exc)
                finally:
                    slot.lock.release()
        except Exception as exc:
            self.logger.warning("预热跳过: %s", exc)
        finally:
            try:
                if os.path.isfile(tmp_wav):
                    os.remove(tmp_wav)
            except OSError:
                pass

    # ---------------- 主流程 ----------------

    def select_model(self, requested: Optional[str], input_sample_rate: int) -> str:
        if requested:
            name = str(requested)
            if name not in MODEL_TASK:
                raise ModelLoadError(f"不支持的降噪模型: {name}，可选 {sorted(MODEL_TASK)}")
            return name
        if self.cfg.runtime.auto_select_model and input_sample_rate and input_sample_rate >= 32000:
            return "MossFormer2_SE_48K"
        return self.cfg.runtime.model

    def denoise(
        self,
        in_path: str,
        out_path: str,
        ext: str,
        *,
        model: Optional[str] = None,
        auto_detect: Optional[bool] = None,
        bitrate: Optional[str] = None,
        sample_rate: Optional[int] = None,
        channels: Optional[int] = None,
        timeout_s: float = 600.0,
    ) -> Dict[str, Any]:
        started = time.monotonic()
        info = audio_io.probe(in_path, timeout=self.cfg.audio.probe_timeout_s)
        duration = float(info.get("duration") or 0.0)
        input_sr = int(info.get("sample_rate") or 0)
        input_channels = int(info.get("channels") or 1)

        model_name = self.select_model(model, input_sr)

        report: Optional[NoiseReport] = None
        detect_enabled = self.cfg.denoise.auto_detect if auto_detect is None else bool(auto_detect)
        if detect_enabled:
            report = self.detector.analyze_file(in_path, duration)
            self.logger.info("噪声检测: %s", report.to_dict())

        result: Dict[str, Any] = {
            "model": model_name,
            "duration": round(duration, 3),
            "input": info,
            "denoised": False,
            "detect": report.to_dict() if report else None,
        }

        if report is not None and not report.need_denoise:
            # 判定无需降噪：原样返回原始音频（字节级拷贝，格式/码率/元数据完全不变）
            shutil.copyfile(in_path, out_path)
            result.update(
                {
                    "denoised": False,
                    "skip_reason": report.reason,
                    "output": {
                        "encoder": "copy",
                        "sample_rate": input_sr,
                        "channels": input_channels,
                        "bitrate": f"{info.get('bit_rate') // 1000}k" if info.get("bit_rate") else "",
                    },
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return result

        need_mb = self.governor.required_mb(duration)
        granted = self.governor.acquire(need_mb, timeout_s)
        if not granted:
            raise ResourceBusy("等待 GPU/CPU 资源超时，请稍后重试")

        slot = None
        try:
            slot = self._acquire_slot(model_name, timeout_s)
            t0 = time.monotonic()
            with slot.lock:
                wav = slot.cv(input_path=in_path, online_write=False)
            infer_seconds = time.monotonic() - t0
            slot.used_count += 1
            slot.last_used_ts = time.time()
            slot.last_seconds = infer_seconds

            if wav is None:
                raise ModelLoadError(
                    f"模型 {model_name} 推理无输出，请检查权重是否完整挂载于 {self.cfg.models.root}"
                )

            arr = np.asarray(wav, dtype=np.float32)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)

            target_sr = int(sample_rate or self.cfg.denoise.sample_rate or input_sr or slot.sampling_rate)
            target_ch = int(channels or self.cfg.denoise.channels or input_channels or 1)
            target_br = bitrate or self.cfg.denoise.bitrate or (
                f"{int(info.get('bit_rate') or 0) // 1000}k" if info.get("bit_rate") else None
            )
            sample_width = 2  # wav 输出统一使用 16bit PCM；压缩格式由码率控制

            encode_info = audio_io.encode_audio(
                arr,
                slot.sampling_rate,
                out_path,
                ext,
                target_sample_rate=target_sr,
                channels=target_ch,
                bitrate=target_br,
                sample_width=sample_width,
                timeout=self.cfg.audio.encode_timeout_s,
            )
            result.update(
                {
                    "denoised": True,
                    "slot": slot.index,
                    "infer_seconds": round(infer_seconds, 3),
                    "memory_required_mb": round(need_mb, 2),
                    "output": encode_info,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                }
            )
            return result
        finally:
            if slot is not None:
                slot.lock.release()
            self.governor.release()
            self.governor.maybe_release_cache()

    # ---------------- 状态 ----------------

    def status(self) -> Dict[str, Any]:
        pools = []
        for name, pool in self._pools.items():
            pools.append(
                {
                    "name": name,
                    "slots": len(pool),
                    "busy": sum(1 for s in pool if s.lock.locked()),
                    "sampling_rate": pool[0].sampling_rate,
                    "device": str(getattr(pool[0].model, "device", "?")),
                    "used_total": sum(s.used_count for s in pool),
                }
            )
        return {"pool_size": self._pool_size, "models": pools}
