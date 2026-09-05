"""资源探测与准入控制。

核心设计（区分"显示占用"与"真实占用"）：

GPU 侧，一张卡的显存可以拆成四部分：

    total = 其它进程占用 + torch 真实在用(allocated) + torch 缓存池空闲(reserved-allocated) + 完全空闲

* ``nvidia-smi`` 看到的 used = 其它进程占用 + allocated + 缓存池空闲  -> **显示占用**
* ``torch.cuda.memory_allocated()``                                  -> **真实在用**
* ``torch.cuda.memory_reserved() - memory_allocated()``              -> **假占用**（PyTorch 缓存分配器
  用完不还驱动的那部分，随时可被本进程复用，也可以在 idle 时 empty_cache 还给驱动）

本模块据此计算"新任务真正还能拿到的显存"，不够就让任务排队等待，而不是直接 OOM。

CPU 侧同理：以操作系统 ``available`` 为真实可用量，同时给出进程 RSS 作为"显示占用"参考。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

MB = 1024.0 * 1024.0


def _mb(n_bytes: float) -> float:
    return round(float(n_bytes) / MB, 2)


class GpuProbe:
    """封装 GPU 显存查询，torch 优先，pynvml / nvidia-smi 作为补充信息"""

    def __init__(self, logger):
        self._logger = logger
        self._torch = None
        self._nvml = None
        self._nvml_handle = None
        self._init_torch()
        self._init_nvml()

    def _init_torch(self) -> None:
        try:
            import torch

            self._torch = torch
        except Exception as exc:  # pragma: no cover
            self._logger.warning("无法导入 torch: %s", exc)

    def _init_nvml(self) -> None:
        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml = pynvml
        except Exception:
            self._nvml = None  # pynvml 是可选依赖，缺失不影响主流程

    @property
    def available(self) -> bool:
        torch = self._torch
        if torch is None:
            return False
        try:
            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def device_name(self) -> str:
        torch = self._torch
        if torch is None or not self.available:
            return "cpu"
        try:
            return torch.cuda.get_device_name(torch.cuda.current_device())
        except Exception:
            return "cuda"

    def memory(self) -> Optional[Dict[str, float]]:
        """返回显存分布，单位为 MB"""
        torch = self._torch
        if torch is None or not self.available:
            return None
        try:
            free_bytes, total_bytes = torch.cuda.mem_get_info()
        except Exception as exc:
            self._logger.debug("torch.cuda.mem_get_info 失败: %s", exc)
            return None
        try:
            reserved = float(torch.cuda.memory_reserved())
            allocated = float(torch.cuda.memory_allocated())
        except Exception:
            reserved = 0.0
            allocated = 0.0

        total = float(total_bytes)
        # nvidia-smi 视角的已用显存（含本进程缓存池）
        used_reported = total - float(free_bytes)
        cached_idle = max(0.0, reserved - allocated)
        other_used = max(0.0, used_reported - reserved)
        true_used = other_used + allocated

        info = {
            "total_mb": _mb(total),
            "used_reported_mb": _mb(used_reported),
            "free_reported_mb": _mb(free_bytes),
            "torch_reserved_mb": _mb(reserved),
            "torch_allocated_mb": _mb(allocated),
            "torch_cached_idle_mb": _mb(cached_idle),
            "other_process_used_mb": _mb(other_used),
            "true_used_mb": _mb(true_used),
        }
        util = self._utilization()
        if util is not None:
            info["gpu_util_percent"] = util
        return info

    def _utilization(self) -> Optional[float]:
        if self._nvml is None:
            return None
        try:
            torch = self._torch
            idx = torch.cuda.current_device() if torch is not None else 0
            handle = self._nvml.nvmlDeviceGetHandleByIndex(idx)
            rates = self._nvml.nvmlDeviceGetUtilizationRates(handle)
            return float(rates.gpu)
        except Exception:
            return None

    def empty_cache(self) -> None:
        torch = self._torch
        if torch is None or not self.available:
            return
        try:
            torch.cuda.empty_cache()
        except Exception as exc:  # pragma: no cover
            self._logger.debug("empty_cache 失败: %s", exc)

    def synchronize(self) -> None:
        torch = self._torch
        if torch is None or not self.available:
            return
        try:
            torch.cuda.synchronize()
        except Exception:
            pass


class CpuProbe:
    def __init__(self, logger):
        self._logger = logger
        self._psutil = None
        try:
            import psutil

            self._psutil = psutil
            self._proc = psutil.Process()
        except Exception as exc:  # pragma: no cover
            self._logger.warning("psutil 不可用，CPU 资源控制将退化: %s", exc)
            self._proc = None

    def memory(self) -> Optional[Dict[str, float]]:
        if self._psutil is None:
            return None
        try:
            vm = self._psutil.virtual_memory()
        except Exception:
            return None
        rss = 0.0
        if self._proc is not None:
            try:
                rss = float(self._proc.memory_info().rss)
            except Exception:
                rss = 0.0
        return {
            "total_mb": _mb(vm.total),
            "available_mb": _mb(vm.available),
            "used_reported_mb": _mb(vm.used),
            "process_rss_mb": _mb(rss),
            "cpu_count": self._psutil.cpu_count(logical=True) or 1,
        }


class ResourceGovernor:
    """并发准入控制器：并发数上限 + 资源(显存/内存)实时检测，不足则排队等待"""

    def __init__(self, cfg, device_kind: str, max_concurrency: int, logger):
        """
        cfg: ResourceConfig
        device_kind: 'cuda' | 'cpu'
        max_concurrency: 允许同时推理的最大任务数
        """
        self._cfg = cfg
        self._device_kind = device_kind
        self._max_concurrency = max(1, int(max_concurrency))
        self._logger = logger
        self._gpu = GpuProbe(logger)
        self._cpu = CpuProbe(logger)
        self._cond = threading.Condition()
        self._running = 0
        self._stopped = False
        self._last_empty_cache_ts = 0.0
        self._waited_total = 0
        self._rejected_total = 0
        self._served_total = 0
        self._wait_seconds_total = 0.0

    # ---------------- 探测 ----------------

    @property
    def device_kind(self) -> str:
        if self._device_kind == "cuda" and not self._gpu.available:
            return "cpu"
        return self._device_kind

    def gpu_info(self) -> Optional[Dict[str, float]]:
        return self._gpu.memory() if self._device_kind == "cuda" else None

    def cpu_info(self) -> Optional[Dict[str, float]]:
        return self._cpu.memory()

    def _available_mb(self) -> Dict[str, Any]:
        """返回当前可用于新任务的资源量及明细"""
        detail: Dict[str, Any] = {"kind": self.device_kind}

        if self.device_kind == "cuda" and self._cfg.gpu.enabled:
            info = self._gpu.memory()
            if info is None:
                detail.update({"available_mb": None, "reason": "gpu_unavailable"})
                return detail
            g = self._cfg.gpu
            # 真实占用：其它进程 + 本进程真正在用的显存
            true_used = info["other_process_used_mb"] + info["torch_allocated_mb"]
            if not g.count_cached_as_free:
                # 保守模式：把缓存池也当成占用（显示占用口径）
                true_used += info["torch_cached_idle_mb"]
            raw_available = info["total_mb"] - true_used
            # 本进程可使用的总量上限（配额）
            budget_left = info["total_mb"] * g.max_usage_ratio - info["torch_allocated_mb"]
            usable = max(0.0, min(raw_available, budget_left))
            available = usable - g.reserve_mb
            detail.update(
                {
                    "available_mb": round(available, 2),
                    # 参与可用量计算的"真实占用"，受 count_cached_as_free 影响
                    "true_used_mb": round(true_used, 2),
                    # 不含缓存池的严格真实占用（其它进程 + allocated）
                    "strict_true_used_mb": info["true_used_mb"],
                    "used_reported_mb": info["used_reported_mb"],
                    "torch_allocated_mb": info["torch_allocated_mb"],
                    "torch_cached_idle_mb": info["torch_cached_idle_mb"],
                    "other_process_used_mb": info["other_process_used_mb"],
                    "total_mb": info["total_mb"],
                    "reserve_mb": g.reserve_mb,
                    "count_cached_as_free": g.count_cached_as_free,
                }
            )
            if "gpu_util_percent" in info:
                detail["gpu_util_percent"] = info["gpu_util_percent"]
            return detail

        info = self._cpu.memory()
        if info is None:
            detail.update({"available_mb": None, "reason": "cpu_probe_unavailable"})
            return detail
        c = self._cfg.cpu
        raw_available = info["available_mb"]
        budget_left = info["total_mb"] * c.max_usage_ratio - info["process_rss_mb"]
        usable = max(0.0, min(raw_available, budget_left))
        available = usable - c.reserve_mb
        detail.update(
            {
                "available_mb": round(available, 2),
                "system_available_mb": info["available_mb"],
                "used_reported_mb": info["used_reported_mb"],
                "process_rss_mb": info["process_rss_mb"],
                "total_mb": info["total_mb"],
                "reserve_mb": c.reserve_mb,
            }
        )
        return detail

    def snapshot(self) -> Dict[str, Any]:
        with self._cond:
            running = self._running
        detail = self._available_mb()
        return {
            "kind": detail["kind"],
            "available_mb": detail.get("available_mb"),
            "detail": detail,
            "running": running,
            "served_total": self._served_total,
            "waited_total": self._waited_total,
            "rejected_total": self._rejected_total,
            "wait_seconds_total": round(self._wait_seconds_total, 3),
            "gpu": self.gpu_info(),
            "cpu": self.cpu_info(),
        }

    # ---------------- 准入 ----------------

    def required_mb(self, audio_seconds: float = 0.0) -> float:
        """粗估单个任务需要的额外资源（MB）"""
        if self.device_kind == "cuda":
            base = float(self._cfg.gpu.per_task_mb)
            # 长音频会多占一些中间结果，按每 60 秒增加 25% 的基准量，最多翻倍
            factor = 1.0 + min(1.0, max(0.0, audio_seconds) / 60.0 * 0.25)
            return base * factor
        base = float(self._cfg.cpu.per_task_mb)
        factor = 1.0 + min(1.0, max(0.0, audio_seconds) / 60.0 * 0.5)
        return base * factor

    def _maybe_reclaim_cache(self) -> None:
        """资源不足时，尝试把 PyTorch 缓存池里的空闲显存还给驱动"""
        if self.device_kind != "cuda":
            return
        g = self._cfg.gpu
        if not g.empty_cache_when_idle:
            return
        now = time.monotonic()
        if now - self._last_empty_cache_ts < g.empty_cache_min_interval_s:
            return
        info = self._gpu.memory()
        if not info:
            return
        if info["torch_cached_idle_mb"] < 32:
            return
        self._last_empty_cache_ts = now
        self._logger.info(
            "显存不足触发缓存回收: 缓存空闲 %.2fMB -> empty_cache()",
            info["torch_cached_idle_mb"],
        )
        self._gpu.empty_cache()

    def acquire(self, need_mb: float, timeout_s: float) -> bool:
        """申请执行一个任务，资源不足时在条件变量上等待"""
        deadline = time.monotonic() + max(0.0, timeout_s)
        poll = max(0.01, self._cfg.poll_interval_ms / 1000.0)
        started = time.monotonic()
        waited = False
        last_log = 0.0

        with self._cond:
            while True:
                if self._stopped:
                    return False
                if self._running < self._max_concurrency:
                    detail = self._available_mb()
                    avail = detail.get("available_mb")
                    if avail is None or avail >= need_mb:
                        self._running += 1
                        self._served_total += 1
                        if waited:
                            self._wait_seconds_total += time.monotonic() - started
                        return True
                    reason = (
                        "可用显存 %.2fMB < 需要 %.2fMB (真实占用 %.2fMB / 显示占用 %.2fMB / 缓存空闲 %.2fMB)"
                        % (
                            avail,
                            need_mb,
                            detail.get("true_used_mb", 0.0),
                            detail.get("used_reported_mb", 0.0),
                            detail.get("torch_cached_idle_mb", 0.0),
                        )
                        if detail["kind"] == "cuda"
                        else "可用内存 %.2fMB < 需要 %.2fMB" % (avail, need_mb)
                    )
                else:
                    reason = "并发已满 (%d 个任务正在推理)" % self._running

                if not waited:
                    waited = True
                    self._waited_total += 1
                    self._logger.info("任务等待资源: %s", reason)
                elif time.monotonic() - last_log > 5.0:
                    last_log = time.monotonic()
                    self._logger.info("任务仍在等待资源: %s", reason)

                # 让出锁再尝试回收缓存，避免长时间持锁
                self._cond.release()
                try:
                    self._maybe_reclaim_cache()
                finally:
                    self._cond.acquire()

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._rejected_total += 1
                    self._logger.warning("等待资源超时(%.1fs): %s", timeout_s, reason)
                    return False
                self._cond.wait(timeout=min(poll, remaining))

    def release(self) -> None:
        with self._cond:
            self._running = max(0, self._running - 1)
            self._cond.notify_all()

    def shutdown(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def maybe_release_cache(self) -> None:
        """任务结束且队列空闲时，把缓存显存还给驱动，降低"显示占用" """
        if self.device_kind != "cuda":
            return
        g = self._cfg.gpu
        if not g.empty_cache_when_idle:
            return
        with self._cond:
            idle = self._running == 0
        if not idle:
            return
        info = self._gpu.memory()
        if info and info["torch_cached_idle_mb"] > 32:
            self._gpu.empty_cache()
