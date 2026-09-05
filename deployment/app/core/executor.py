"""任务执行器：有界队列 + 线程池，超出容量直接拒绝（返回 429/503）而不是无限堆积"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, TypeVar

T = TypeVar("T")


class CapacityExceeded(Exception):
    """排队任务数超过 max_queue_size"""


class TaskExecutor:
    def __init__(self, max_workers: int, max_queue_size: int, logger, thread_prefix: str = "cv-worker"):
        self._logger = logger
        self._max_workers = max(1, int(max_workers))
        # 信号量容量 = 正在执行 + 允许排队
        self._capacity = self._max_workers + max(0, int(max_queue_size))
        self._slots = threading.BoundedSemaphore(self._capacity)
        self._pool = ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix=thread_prefix
        )
        self._submitted = 0
        self._completed = 0
        self._rejected = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def queued(self) -> int:
        return max(0, self._capacity - self._slots._value)  # noqa: SLF001 - 仅需读取计数值

    def submit(self, fn: Callable[[], T]) -> Future:
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self._rejected += 1
            raise CapacityExceeded("服务繁忙，排队任务数已达上限")
        try:
            future = self._pool.submit(fn)
        except Exception:
            self._slots.release()
            raise
        with self._lock:
            self._submitted += 1

        def _done(_f: "Future") -> None:
            self._slots.release()
            with self._lock:
                self._completed += 1

        future.add_done_callback(_done)
        return future

    def stats(self) -> Dict[str, int]:
        with self._lock:
            submitted, completed, rejected = self._submitted, self._completed, self._rejected
        running = max(0, submitted - completed)
        return {
            "max_workers": self._max_workers,
            "capacity": self._capacity,
            "queued": self.queued,
            "running": min(running, self._max_workers),
            "inflight": running,
            "submitted_total": submitted,
            "completed_total": completed,
            "rejected_total": rejected,
        }

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait)
