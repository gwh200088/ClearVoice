"""ClearerVoice 降噪服务入口。

注意：必须先加载配置并设置底层运行时环境变量，再导入任何会初始化 torch 的模块，
因此本文件顶部的导入顺序是有意为之。
"""

import os

from app.settings import apply_runtime_env, load_config

CONFIG = load_config()
# 必须在导入 torch / uvicorn 之前生效（CUDA_VISIBLE_DEVICES、线程数等）
apply_runtime_env(CONFIG)

import logging  # noqa: E402

import uvicorn  # noqa: E402

from app.api import build_app  # noqa: E402
from app.logging_setup import capture_stdio, setup_logging  # noqa: E402

setup_logging(CONFIG.logging)
capture_stdio(CONFIG.logging)

logger = logging.getLogger("clearvoice")
logger.info("=" * 72)
logger.info("ClearerVoice 降噪服务启动中")
logger.info("配置: 模型=%s 设备=%s 并发=%s 队列=%s 模型实例=%s",
            CONFIG.runtime.model, CONFIG.runtime.device,
            CONFIG.concurrency.max_concurrency, CONFIG.concurrency.max_queue_size,
            CONFIG.runtime.model_pool_size)
logger.info("模型目录=%s 日志目录=%s 临时目录=%s",
            CONFIG.models.root, CONFIG.logging.dir, CONFIG.audio.temp_dir)
logger.info("=" * 72)

os.makedirs(CONFIG.audio.temp_dir, exist_ok=True)

app = build_app(CONFIG, logger)


def main() -> None:
    uvicorn.run(
        app,
        host=CONFIG.server.host,
        port=int(CONFIG.server.port),
        # 日志完全由 app.logging_setup 统一接管：
        # log_config=None 阻止 uvicorn 用 dictConfig 覆盖根/uvicorn 的 handler
        log_config=None,
        log_level=None,
        access_log=False,
        timeout_keep_alive=120,
        server_header=False,
    )


if __name__ == "__main__":
    main()
