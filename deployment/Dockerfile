# -----------------------------------------------------------------------------
# ClearerVoice 语音降噪服务 —— 唯一的运行镜像
#
# 镜像内已包含：Python 环境、ffmpeg、torch/torchaudio、全部 Python 依赖、服务代码。
# 镜像内【不含】模型权重，运行时通过 -v 挂载模型包。
#
# 工作流：
#   联网机器：./deployment/tools/build.sh  →  ./deployment/tools/export.sh
#   内网机器：./deployment/tools/load.sh  →  docker run -v <模型包>:/opt/models/...
#
# 兼容性：
#   * 只使用经典构建器语法（无 COPY --chmod、RUN --mount），Docker 18.09 可直接构建。
#   * 默认 CUDA 11.8（驱动 >= 520.06，覆盖 T4(sm_75) 及更新显卡）。
#
# 注意：这里刻意不声明 VOLUME。若声明 VOLUME /opt/models/ClearerVoice-Studio，
#       Docker 会在容器启动时把该路径替换成匿名空卷，即使你用 -v 挂载了它或它的父目录，
#       也会导致"模型明明挂进去了却读不到"。挂载点完全交给 -v 决定更安全。
# -----------------------------------------------------------------------------
ARG CUDA_IMAGE=nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04
FROM ${CUDA_IMAGE}

ARG TORCH_VERSION=2.4.1
ARG TORCH_CUDA=cu118
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu118
# 可选：构建机访问官方源慢时指定镜像源
#   --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
#   --build-arg APT_MIRROR=mirrors.aliyun.com
ARG PIP_INDEX_URL=""
ARG APT_MIRROR=""

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    APP_HOME=/app \
    MODEL_ROOT=/opt/models/ClearerVoice-Studio \
    LOG_DIR=/var/log/clearvoice \
    PYTHONPATH=/app:/opt/clearvoice \
    # 运行期兜底：容器内禁止任何 pip / HuggingFace 联网行为
    PIP_NO_INDEX=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1

WORKDIR /app

# ---------------------------------------------------------------- 系统依赖
RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|http://archive.ubuntu.com/ubuntu|http://${APT_MIRROR}/ubuntu|g; \
                s|http://security.ubuntu.com/ubuntu|http://${APT_MIRROR}/ubuntu|g" \
            /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-dev \
        ffmpeg ca-certificates curl; \
    ln -sf /usr/bin/python3 /usr/bin/python; \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- Python 依赖
COPY deployment/requirements.txt /tmp/requirements.txt

# requests 仅用于容器内跑 smoke_test 自测，运行时不依赖
RUN set -eux; \
    PIP_IDX=""; \
    if [ -n "${PIP_INDEX_URL}" ]; then PIP_IDX="-i ${PIP_INDEX_URL}"; fi; \
    python -m pip install --upgrade ${PIP_IDX} pip setuptools wheel; \
    if [ -n "${TORCH_CUDA}" ]; then \
        python -m pip install ${PIP_IDX} --index-url "${TORCH_INDEX_URL}" \
            "torch==${TORCH_VERSION}+${TORCH_CUDA}" \
            "torchaudio==${TORCH_VERSION}+${TORCH_CUDA}"; \
    else \
        python -m pip install ${PIP_IDX} --index-url "${TORCH_INDEX_URL}" \
            "torch==${TORCH_VERSION}" \
            "torchaudio==${TORCH_VERSION}"; \
    fi; \
    python -m pip install ${PIP_IDX} -r /tmp/requirements.txt; \
    python -m pip install ${PIP_IDX} requests; \
    python -c "import torch, torchaudio, fastapi, uvicorn, soundfile, librosa, yaml, psutil; \
               print('deps ok: torch', torch.__version__, '| cuda', torch.version.cuda)"

# ---------------------------------------------------------------- 源码
# ClearerVoice-Studio 推理包（不修改原工程任何文件）
COPY clearvoice/clearvoice /opt/clearvoice/clearvoice
# 服务代码与默认配置
COPY deployment/app /app/app
COPY deployment/config /app/config
COPY deployment/entrypoint.sh /app/entrypoint.sh

# 兜底：即使脚本在拷贝过程中被转成 CRLF，也要保证能正常执行
RUN set -eux; \
    sed -i 's/\r$//' /app/entrypoint.sh; \
    chmod +x /app/entrypoint.sh; \
    mkdir -p /var/log/clearvoice /tmp/clearvoice /opt/models; \
    PYTHONPATH=/opt/clearvoice python -c "import clearvoice; print('clearvoice package ok')"; \
    ffmpeg -version | head -n 1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
