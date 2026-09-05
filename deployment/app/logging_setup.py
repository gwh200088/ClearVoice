"""日志系统：按大小滚动落盘到指定目录，目录/单文件大小/备份数/级别全部可配"""

import logging
import sys
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from typing import Optional

_REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="-")


def bind_request_id(request_id: str):
    """绑定请求 ID，返回可用于 ContextVar.reset 的 token"""
    return _REQUEST_ID.set(request_id or "-")


def get_request_id() -> str:
    return _REQUEST_ID.get()


def reset_request_id(token=None) -> None:
    if token is not None:
        _REQUEST_ID.reset(token)
    else:
        _REQUEST_ID.set("-")


class RequestIdFilter(logging.Filter):
    """为每条日志注入请求 ID；格式串里没有 %(request_id)s 也不会报错"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _REQUEST_ID.get()
        return True


class _SafeFormatter(logging.Formatter):
    """允许用户自定义格式串，缺失字段时回退到默认格式"""

    def __init__(self, fmt: str, datefmt: str, default_fmt: str):
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._default_fmt = default_fmt
        self._default = logging.Formatter(fmt=default_fmt, datefmt=datefmt)

    def format(self, record: logging.LogRecord) -> str:
        try:
            return super().format(record)
        except (KeyError, ValueError):
            return self._default.format(record)


def _ensure_request_id_fields(fmt: str) -> str:
    return fmt


def build_handlers(cfg) -> list:
    """cfg: LoggingConfig"""
    handlers: list = []
    request_filter = RequestIdFilter()

    log_dir = cfg.dir.rstrip("/\\")
    try:
        import os

        os.makedirs(log_dir, exist_ok=True)
    except OSError as exc:  # pragma: no cover - 仅在运行环境异常时触发
        print(f"[logging] 无法创建日志目录 {log_dir}: {exc}", file=sys.stderr)
        log_dir = None

    default_fmt = "%(asctime)s | %(levelname)-8s | rid=%(request_id)s | %(name)-26s | %(message)s"
    formatter = _SafeFormatter(cfg.fmt or default_fmt, cfg.datefmt, default_fmt)

    if log_dir:
        import os

        main_file = os.path.join(log_dir, "clearvoice.log")
        error_file = os.path.join(log_dir, "clearvoice-error.log")
        try:
            file_handler = RotatingFileHandler(
                main_file,
                mode="a",
                maxBytes=int(cfg.max_bytes),
                backupCount=int(cfg.backup_count),
                encoding=cfg.encoding,
            )
            file_handler.setLevel(logging.DEBUG if cfg.level.upper() == "DEBUG" else logging.INFO)
            file_handler.setFormatter(formatter)
            file_handler.addFilter(request_filter)
            handlers.append(file_handler)

            error_handler = RotatingFileHandler(
                error_file,
                mode="a",
                maxBytes=int(cfg.max_bytes),
                backupCount=int(cfg.backup_count),
                encoding=cfg.encoding,
            )
            error_handler.setLevel(logging.ERROR)
            error_handler.setFormatter(formatter)
            error_handler.addFilter(request_filter)
            handlers.append(error_handler)
        except OSError as exc:  # pragma: no cover
            print(f"[logging] 无法创建日志文件: {exc}", file=sys.stderr)

    if cfg.console:
        console = logging.StreamHandler(stream=sys.stdout)
        console.setFormatter(formatter)
        console.addFilter(request_filter)
        handlers.append(console)

    return handlers


def setup_logging(cfg) -> None:
    """cfg: LoggingConfig"""
    level = getattr(logging, str(cfg.level).upper(), logging.INFO)
    handlers = build_handlers(cfg)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:  # pragma: no cover
            pass
    root.setLevel(level)
    for h in handlers:
        root.addHandler(h)

    # uvicorn / fastapi 的日志统一收敛到本服务配置
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.setLevel(getattr(logging, str(cfg.uvicorn_level).upper(), logging.INFO))
        lg.propagate = True

    # 抑制第三方库的噪声日志
    for name in ("matplotlib", "numba", "torchaudio"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "日志初始化完成 dir=%s level=%s max_bytes=%s backup_count=%s console=%s",
        cfg.dir,
        cfg.level,
        cfg.max_bytes,
        cfg.backup_count,
        cfg.console,
    )


def get_logger(name: Optional[str] = None) -> logging.Logger:
    return logging.getLogger(name)


class _StreamToLogger:
    """把 print / 第三方库写到 stdout、stderr 的内容也收进日志文件"""

    def __init__(self, logger: logging.Logger, level: int, origin):
        self._logger = logger
        self._level = level
        self._origin = origin
        self._buffer = ""

    def write(self, buf: str) -> int:
        if not buf:
            return 0
        self._buffer += buf
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                self._logger.log(self._level, "%s", line)
        return len(buf)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, "%s", self._buffer.rstrip())
        self._buffer = ""

    def isatty(self) -> bool:
        try:
            return bool(self._origin.isatty())
        except Exception:
            return False

    def fileno(self) -> int:
        # 部分第三方库会直接向 fd 写内容，这里保留对原始流的引用
        return self._origin.fileno()

    def __getattr__(self, item):
        return getattr(self._origin, item)


def capture_stdio(cfg) -> None:
    """cfg: LoggingConfig。必须在 setup_logging 之后调用"""
    if not getattr(cfg, "capture_stdio", False):
        return
    sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO, sys.stdout)  # type: ignore[assignment]
    sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.WARNING, sys.stderr)  # type: ignore[assignment]
