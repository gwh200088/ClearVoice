"""配置加载：内置默认值 < YAML 配置文件 < 环境变量(CV_*)"""

import os
import logging
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from typing import Any, Dict, List, Optional, Tuple, get_args, get_origin

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "/app/config/config.yaml"
ENV_PREFIX = "CV_"
ENV_NESTED_SEP = "__"

# 常用配置的扁平别名，便于 docker run -e CV_MODEL=xxx 直接覆盖
ENV_ALIASES = {
    "CV_HOST": "server.host",
    "CV_PORT": "server.port",
    "CV_API_KEY": "server.api_key",
    "CV_MAX_UPLOAD_MB": "server.max_upload_mb",
    "CV_SYNC_TIMEOUT_S": "server.sync_timeout_s",
    "CV_TASK_TTL_S": "server.task_ttl_s",
    "CV_DEVICE": "runtime.device",
    "CV_GPU_IDS": "runtime.gpu_ids",
    "CV_MODEL": "runtime.model",
    "CV_MODEL_POOL_SIZE": "runtime.model_pool_size",
    "CV_AUTO_SELECT_MODEL": "runtime.auto_select_model",
    "CV_TORCH_THREADS": "runtime.torch_threads",
    "CV_PRELOAD": "runtime.preload",
    "CV_DECODE_WINDOW_S": "runtime.decode_window_s",
    "CV_ONE_TIME_DECODE_LENGTH_S": "runtime.one_time_decode_length_s",
    "CV_PROCESS_MEMORY_RATIO": "runtime.process_memory_ratio",
    "CV_FP16": "runtime.fp16",
    "CV_BATCH_CHUNKS": "runtime.batch_chunks",
    "CV_MAX_CONCURRENCY": "concurrency.max_concurrency",
    "CV_MAX_QUEUE_SIZE": "concurrency.max_queue_size",
    "CV_QUEUE_TIMEOUT_S": "concurrency.queue_timeout_s",
    "CV_MODEL_ROOT": "models.root",
    "CV_ALLOW_DOWNLOAD": "models.allow_download",
    "CV_TEMP_DIR": "audio.temp_dir",
    "CV_AUTO_DETECT": "denoise.auto_detect",
    "CV_SNR_THRESHOLD_DB": "denoise.snr_threshold_db",
    "CV_LOG_DIR": "logging.dir",
    "CV_LOG_LEVEL": "logging.level",
    "CV_LOG_MAX_BYTES": "logging.max_bytes",
    "CV_LOG_BACKUP_COUNT": "logging.backup_count",
    "CV_LOG_CONSOLE": "logging.console",
}

SUPPORTED_MODELS = ("FRCRN_SE_16K", "MossFormerGAN_SE_16K", "MossFormer2_SE_48K")
MODEL_SAMPLE_RATE = {
    "FRCRN_SE_16K": 16000,
    "MossFormerGAN_SE_16K": 16000,
    "MossFormer2_SE_48K": 48000,
}


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    root_path: str = ""
    api_key: str = ""
    max_upload_mb: int = 300
    sync_timeout_s: float = 900.0
    task_ttl_s: int = 1800
    max_tasks: int = 2000


@dataclass
class RuntimeConfig:
    device: str = "auto"
    gpu_ids: str = ""
    model: str = "MossFormerGAN_SE_16K"
    auto_select_model: bool = True
    model_pool_size: int = 2
    torch_threads: int = 4
    allow_tf32: bool = True
    preload: bool = True
    decode_window_s: float = 0.0
    one_time_decode_length_s: float = 0.0
    process_memory_ratio: float = 0.0
    # 长音频分段推理时一次 forward 合并的窗口数（0=关闭），并把模型权重/输入切到 FP16
    fp16: bool = False
    batch_chunks: int = 0


@dataclass
class GpuResourceConfig:
    enabled: bool = True
    reserve_mb: int = 512
    per_task_mb: int = 1024
    max_usage_ratio: float = 0.90
    count_cached_as_free: bool = True
    empty_cache_when_idle: bool = True
    empty_cache_min_interval_s: float = 10.0


@dataclass
class CpuResourceConfig:
    enabled: bool = True
    reserve_mb: int = 1024
    per_task_mb: int = 1024
    max_usage_ratio: float = 0.85


@dataclass
class ResourceConfig:
    poll_interval_ms: int = 200
    gpu: GpuResourceConfig = field(default_factory=GpuResourceConfig)
    cpu: CpuResourceConfig = field(default_factory=CpuResourceConfig)


@dataclass
class ConcurrencyConfig:
    max_concurrency: int = 2
    max_queue_size: int = 64
    queue_timeout_s: float = 600.0


@dataclass
class DenoiseConfig:
    # 以下默认值按"执法记录仪"场景标定（长录音 / 人声稀疏 / 环境噪声复杂 / 突发噪声多），
    # 通用场景请参考 README §8.4 重新调整
    auto_detect: bool = True
    snr_threshold_db: float = 22.0
    noise_percentile: float = 8.0
    speech_percentile: float = 85.0
    frame_ms: float = 20.0
    analyze_max_seconds: float = 180.0
    min_speech_ratio: float = 0.005
    min_duration_s: float = 0.2
    bitrate: str = ""
    sample_rate: int = 0
    channels: int = 0


@dataclass
class AudioConfig:
    allowed_formats: List[str] = field(default_factory=lambda: ["mp3", "wav", "m4a"])
    temp_dir: str = "/tmp/clearvoice"
    probe_timeout_s: float = 20.0
    encode_timeout_s: float = 300.0
    decode_timeout_s: float = 120.0


@dataclass
class ModelsConfig:
    root: str = "/opt/models/ClearerVoice-Studio"
    allow_download: bool = False


@dataclass
class LoggingConfig:
    dir: str = "/var/log/clearvoice"
    level: str = "INFO"
    console: bool = True
    max_bytes: int = 50 * 1024 * 1024
    backup_count: int = 10
    encoding: str = "utf-8"
    fmt: str = "%(asctime)s | %(levelname)-8s | rid=%(request_id)s | %(name)-26s | %(message)s"
    datefmt: str = "%Y-%m-%d %H:%M:%S"
    access_log: bool = True
    uvicorn_level: str = "INFO"
    # 把第三方库/uvicorn 打到 stdout、stderr 的内容也收进日志文件
    capture_stdio: bool = True


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    resource: ResourceConfig = field(default_factory=ResourceConfig)
    denoise: DenoiseConfig = field(default_factory=DenoiseConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


_TRUE_SET = {"1", "true", "yes", "on", "y", "t"}
_FALSE_SET = {"0", "false", "no", "off", "n", "f"}


def _parse_scalar(raw: str, target: Any) -> Any:
    """把环境变量字符串转换成与目标默认值一致的类型"""
    if isinstance(target, bool):
        low = raw.strip().lower()
        if low in _TRUE_SET:
            return True
        if low in _FALSE_SET:
            return False
        raise ValueError(f"无法解析为布尔值: {raw!r}")
    if isinstance(target, int) and not isinstance(target, bool):
        return int(str(raw).strip())
    if isinstance(target, float):
        return float(str(raw).strip())
    if isinstance(target, (list, tuple)):
        parsed = yaml.safe_load(raw)
        if parsed is None:
            return []
        if not isinstance(parsed, list):
            raise ValueError(f"无法解析为列表: {raw!r}")
        return [str(x) for x in parsed]
    return raw


def _get_path(data: Dict[str, Any], path: str) -> Tuple[bool, Any]:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False, None
        cur = cur[part]
    return True, cur


def _set_path(data: Dict[str, Any], path: str, value: Any) -> bool:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            return False
        cur = nxt
    if parts[-1] not in cur:
        return False
    cur[parts[-1]] = value
    return True


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: v for k, v in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env(data: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    warnings: List[str] = []
    items = list(os.environ.items())
    # 先处理别名，再处理 CV_SECTION__KEY，保证显式写法优先级更高
    for env_key, raw in items:
        if not env_key.startswith(ENV_PREFIX):
            continue
        if env_key in ("CV_CONFIG_FILE",):  # 保留关键字，不是配置项
            continue
        if env_key in ENV_ALIASES:
            path = ENV_ALIASES[env_key]
        else:
            path = env_key[len(ENV_PREFIX):].lower().replace(ENV_NESTED_SEP, ".")
            if not path:
                continue
        found, current = _get_path(data, path)
        if not found:
            warnings.append(f"未知配置项 {env_key} -> {path}，已忽略")
            continue
        try:
            value = _parse_scalar(raw, current)
        except (TypeError, ValueError) as exc:
            warnings.append(f"配置项 {env_key} 取值 {raw!r} 非法({exc})，沿用原值")
            continue
        _set_path(data, path, value)
    return data, warnings


def _coerce(value: Any, annotation: Any, path: str, warnings: List[str]) -> Any:
    if is_dataclass(annotation) and isinstance(value, dict):
        return _build_dataclass(annotation, value, path, warnings)
    if annotation is Any or annotation is None:
        return value
    origin = get_origin(annotation)
    if origin is list:
        if value is None:
            return []
        if isinstance(value, str):
            value = yaml.safe_load(value)
        if not isinstance(value, list):
            raise ValueError(f"配置项 {path} 需要列表，实际为 {type(value).__name__}")
        item_type = get_args(annotation)[0] if get_args(annotation) else str
        return [item_type(x) if item_type is not str else str(x) for x in value]
    if annotation is bool and isinstance(value, str):
        return value.strip().lower() in _TRUE_SET
    if annotation is int and not isinstance(value, bool):
        return int(value)
    if annotation is float:
        return float(value)
    if annotation is str and value is None:
        return ""
    return value


def _build_dataclass(cls, data: Dict[str, Any], path: str, warnings: List[str]):
    valid = {f.name for f in fields(cls)}
    for key in data:
        if key not in valid:
            warnings.append(f"配置项 {(path + '.' + key) if path else key} 未被识别，已忽略")
    kwargs: Dict[str, Any] = {}
    for f in fields(cls):
        child_path = f"{path}.{f.name}" if path else f.name
        if f.name in data:
            kwargs[f.name] = _coerce(data[f.name], f.type, child_path, warnings)
        elif f.default is not MISSING:
            kwargs[f.name] = f.default
        elif f.default_factory is not MISSING:  # type: ignore[misc]
            kwargs[f.name] = f.default_factory()  # type: ignore[misc]
        else:
            kwargs[f.name] = None
    return cls(**kwargs)


def default_dict() -> Dict[str, Any]:
    return asdict(AppConfig())


def load_config(config_file: Optional[str] = None) -> AppConfig:
    path = config_file or os.environ.get("CV_CONFIG_FILE", DEFAULT_CONFIG_FILE)
    data = default_dict()
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fp:
            loaded = yaml.safe_load(fp) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"配置文件 {path} 顶层必须是映射结构")
        data = _deep_merge(data, loaded)
    elif path:
        logger.warning("配置文件 %s 不存在，使用内置默认配置", path)

    data, warnings = _apply_env(data)
    cfg = _build_dataclass(AppConfig, data, "", warnings)
    for item in warnings:
        logger.warning("配置告警: %s", item)
    _validate(cfg)
    return cfg


def _validate(cfg: AppConfig) -> None:
    if cfg.runtime.model not in SUPPORTED_MODELS:
        raise ValueError(f"runtime.model 必须是 {SUPPORTED_MODELS} 之一，当前为 {cfg.runtime.model!r}")
    if cfg.runtime.model_pool_size < 1:
        raise ValueError("runtime.model_pool_size 必须 >= 1")
    if cfg.concurrency.max_concurrency < 1:
        raise ValueError("concurrency.max_concurrency 必须 >= 1")
    if cfg.concurrency.max_queue_size < 0:
        raise ValueError("concurrency.max_queue_size 必须 >= 0")
    if cfg.runtime.device not in ("auto", "cpu", "cuda") and not cfg.runtime.device.startswith("cuda:"):
        raise ValueError("runtime.device 必须是 auto / cpu / cuda / cuda:N")
    if not 0.0 < cfg.resource.gpu.max_usage_ratio <= 1.0:
        raise ValueError("resource.gpu.max_usage_ratio 必须在 (0, 1] 区间")
    if not 0.0 < cfg.resource.cpu.max_usage_ratio <= 1.0:
        raise ValueError("resource.cpu.max_usage_ratio 必须在 (0, 1] 区间")
    if cfg.logging.max_bytes <= 0:
        raise ValueError("logging.max_bytes 必须 > 0")
    if cfg.logging.backup_count < 0:
        raise ValueError("logging.backup_count 必须 >= 0")
    cfg.audio.allowed_formats = [str(x).lower().lstrip(".") for x in cfg.audio.allowed_formats]


def to_dict(cfg: AppConfig, mask_secrets: bool = True) -> Dict[str, Any]:
    data = asdict(cfg)
    if mask_secrets and data["server"].get("api_key"):
        data["server"]["api_key"] = "***"
    return data


def apply_runtime_env(cfg: AppConfig) -> None:
    """在任何 torch/uvicorn 导入之前调用，设置影响底层运行时的环境变量"""
    if cfg.runtime.device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    elif cfg.runtime.gpu_ids:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(cfg.runtime.gpu_ids).strip()

    threads = str(max(1, int(cfg.runtime.torch_threads)))
    for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(name, threads)
    os.environ.setdefault("CUDA_MODULE_LOADING", "LAZY")


__all__ = [
    "AppConfig",
    "SUPPORTED_MODELS",
    "MODEL_SAMPLE_RATE",
    "load_config",
    "to_dict",
    "apply_runtime_env",
    "default_dict",
]
