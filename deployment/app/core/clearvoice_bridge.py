"""对 ClearerVoice-Studio 原生推理代码的适配层（不修改原工程源码）。

做三件事：
1. 把模型权重目录从 yaml 里的相对路径 ``checkpoints/<MODEL>`` 重定向到 ``models.root/<MODEL>``；
2. 接管设备选择，避免原工程用 nvidia-smi 选卡与 CUDA_VISIBLE_DEVICES 重映射后下标不一致的问题；
3. 关闭"找不到权重就联网下载"的行为，容器内保持离线可用。
"""

from __future__ import annotations

import os
import threading
from typing import Optional

_PATCH_LOCK = threading.Lock()
_PATCHED = False
_TARGET_DEVICE: Optional[str] = None
_MODEL_ROOT: str = ""
_ALLOW_DOWNLOAD: bool = False


def configure(*, model_root: str, allow_download: bool = False, device: str = "auto") -> None:
    """在导入 clearvoice 之后、实例化模型之前调用"""
    global _TARGET_DEVICE, _MODEL_ROOT, _ALLOW_DOWNLOAD
    _MODEL_ROOT = str(model_root).rstrip("/\\")
    _ALLOW_DOWNLOAD = bool(allow_download)
    _TARGET_DEVICE = device


def _redirect_checkpoint_dir(args, logger) -> None:
    value = getattr(args, "checkpoint_dir", None)
    if not value:
        return
    if os.path.isabs(value) and os.path.isdir(value):
        return
    candidate = os.path.join(_MODEL_ROOT, os.path.basename(str(value).rstrip("/\\")))
    if os.path.isdir(candidate):
        logger.debug("模型目录重定向: %s -> %s", value, candidate)
        args.checkpoint_dir = candidate
    else:
        logger.warning("模型目录 %s 不存在，保持原路径 %s（缺失时推理会失败）", candidate, value)


def install(logger) -> None:
    """给 SpeechModel 打补丁。幂等，可重复调用"""
    global _PATCHED
    with _PATCH_LOCK:
        if _PATCHED:
            return
        # 容器内 PYTHONPATH=/opt/clearvoice，/opt/clearvoice/clearvoice 才是包根，
        # 因此模块路径是 clearvoice.networks（与 demo.py 在 clearvoice/ 目录下运行时的解析一致）
        from clearvoice.networks import SpeechModel

        original_init = SpeechModel.__init__
        original_download = SpeechModel.download_model

        def patched_init(self, args):
            _redirect_checkpoint_dir(args, logger)
            original_init(self, args)
            # 覆盖设备选择：原实现会用 nvidia-smi 挑选"空闲"显卡，
            # 在设置过 CUDA_VISIBLE_DEVICES 的容器里，nvidia-smi 的下标与 CUDA 下标并不一致。
            device = _resolve_device(logger)
            if device is not None:
                import torch

                torch_device = torch.device(device)
                if torch_device.type == "cuda":
                    args.use_cuda = 1
                    torch.cuda.set_device(torch_device)
                else:
                    args.use_cuda = 0
                self.device = torch_device
            logger.debug("模型 %s 使用设备 %s", getattr(args, "network", "?"), self.device)

        def patched_download(self, model_name):
            if not _ALLOW_DOWNLOAD:
                logger.error(
                    "权重 %s 不存在于 %s，且 models.allow_download=false，已跳过联网下载",
                    model_name,
                    _MODEL_ROOT,
                )
                return False
            return original_download(self, model_name)

        SpeechModel.__init__ = patched_init
        SpeechModel.download_model = patched_download
        _silence_progress_bar(logger)
        _block_network_downloads(logger)
        _PATCHED = True
        logger.info("ClearVoice 适配层已安装 (model_root=%s, allow_download=%s)", _MODEL_ROOT, _ALLOW_DOWNLOAD)


def _block_network_downloads(logger) -> None:
    """内网部署兜底：把 huggingface_hub 换成一个"只会报错"的桩模块。

    原工程的 download_model 会 `from huggingface_hub import snapshot_download`。
    虽然我们已经替换了该方法，这里再堵一层，确保任何残留调用都不会尝试联网。
    """
    import sys
    import types

    if not _ALLOW_DOWNLOAD and "huggingface_hub" not in sys.modules:
        stub = types.ModuleType("huggingface_hub")

        def _forbidden(*_args, **_kwargs):
            raise RuntimeError(
                "内网离线模式已禁用模型下载（models.allow_download=false）。"
                f"请把权重放到 {_MODEL_ROOT}/<模型名>/ 并挂载到容器内。"
            )

        stub.snapshot_download = _forbidden
        stub.hf_hub_download = _forbidden
        sys.modules["huggingface_hub"] = stub
        logger.debug("已注入 huggingface_hub 桩模块，禁止任何联网下载")


def _silence_progress_bar(logger) -> None:
    """原推理代码用 tqdm 往 stderr 打进度条，服务端按请求记录进度信息更合适"""
    try:
        from clearvoice import networks

        networks.tqdm = lambda iterable=None, *args, **kwargs: iterable
    except Exception as exc:  # pragma: no cover
        logger.debug("未能替换 tqdm: %s", exc)


def _resolve_device(logger) -> Optional[str]:
    """返回形如 'cpu' / 'cuda:0' 的设备字符串；None 表示沿用原工程逻辑"""
    if not _TARGET_DEVICE or _TARGET_DEVICE == "auto":
        return "cuda:0" if _is_cuda_available() else "cpu" if _TARGET_DEVICE == "auto" else None
    if _TARGET_DEVICE == "cpu":
        return "cpu"
    if _TARGET_DEVICE == "cuda":
        return "cuda:0" if _is_cuda_available() else "cpu"
    return _TARGET_DEVICE


def _is_cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def describe_runtime() -> dict:
    import torch

    info = {
        "torch_version": torch.__version__,
        "cuda_available": bool(torch.cuda.is_available()),
        "cuda_version": getattr(torch.version, "cuda", None),
        "device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
        "selected_device": _TARGET_DEVICE or "auto",
    }
    if torch.cuda.is_available():
        try:
            info["device_name"] = torch.cuda.get_device_name(torch.cuda.current_device())
        except Exception:
            pass
    return info
