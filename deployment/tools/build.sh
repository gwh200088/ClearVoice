#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 【联网机器执行】构建运行镜像
#
# 镜像内已含 Python/ffmpeg/torch/全部依赖/服务代码，不含模型权重（运行时 -v 挂载）。
# 构建完用 ./tools/export.sh 导出 tar 拷进内网。
#
# 用法（在仓库根目录执行）：
#   ./deployment/tools/build.sh
#   ./deployment/tools/build.sh --cpu
#   ./deployment/tools/build.sh --pip-mirror https://pypi.tuna.tsinghua.edu.cn/simple
#   ./deployment/tools/build.sh --apt-mirror mirrors.aliyun.com
#   ./deployment/tools/build.sh --tag clearvoice-denoise:2.0.0
# -----------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/../.."   # 回到仓库根目录

IMAGE="clearvoice-denoise:1.0.0"
CUDA_IMAGE="nvidia/cuda:11.8.0-cudnn8-runtime-ubuntu22.04"
TORCH_VERSION="2.4.1"
TORCH_CUDA=""        # 留空则按基础镜像自动推导（12.4.x -> cu124）；--cpu 时为 CPU 版
TORCH_INDEX_URL=""   # 留空则按 TORCH_CUDA 自动推导
PIP_INDEX_URL=""
APT_MIRROR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cpu)         CUDA_IMAGE="ubuntu:22.04"; TORCH_CUDA="" ;;
    --tag)         IMAGE="$2"; shift ;;
    --cuda-image)  CUDA_IMAGE="$2"; shift ;;
    --torch)       TORCH_VERSION="$2"; shift ;;
    --torch-cuda)  TORCH_CUDA="$2"; shift ;;
    --torch-index) TORCH_INDEX_URL="$2"; shift ;;
    --pip-mirror)  PIP_INDEX_URL="$2"; shift ;;
    --apt-mirror)  APT_MIRROR="$2"; shift ;;
    -h|--help)     sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
  shift
done

# 按基础镜像自动推导 torch 的 CUDA 变体与下载源
if [[ -z "${TORCH_CUDA}" ]]; then
  if [[ "${CUDA_IMAGE}" == "ubuntu:22.04" ]]; then
    TORCH_CUDA=""                                  # CPU 版
  else
    CUDA_NUM="$(printf '%s' "${CUDA_IMAGE}" | grep -oE '[0-9]+\.[0-9]+' | head -n1 || true)"
    if [[ -n "${CUDA_NUM}" ]]; then
      TORCH_CUDA="cu$(printf '%s' "${CUDA_NUM}" | tr -d '.')"   # 12.4.x -> cu124
    else
      TORCH_CUDA="cu118"                           # 兜底
    fi
  fi
fi
if [[ -z "${TORCH_INDEX_URL}" ]]; then
  if [[ -n "${TORCH_CUDA}" ]]; then
    TORCH_INDEX_URL="https://download.pytorch.org/whl/${TORCH_CUDA}"
  else
    TORCH_INDEX_URL="https://download.pytorch.org/whl/cpu"
  fi
fi

echo "==> 构建 ${IMAGE}"
echo "    基础镜像  : ${CUDA_IMAGE}"
echo "    torch     : ${TORCH_VERSION} (${TORCH_CUDA:-cpu})"
echo "    pip 源    : ${PIP_INDEX_URL:-<官方源>}"
echo "    apt 源    : ${APT_MIRROR:-<官方源>}"
echo "    模型权重  : 不打包进镜像，运行时 -v 挂载"

docker build \
  -f deployment/Dockerfile \
  --build-arg CUDA_IMAGE="${CUDA_IMAGE}" \
  --build-arg TORCH_VERSION="${TORCH_VERSION}" \
  --build-arg TORCH_CUDA="${TORCH_CUDA}" \
  --build-arg TORCH_INDEX_URL="${TORCH_INDEX_URL}" \
  --build-arg PIP_INDEX_URL="${PIP_INDEX_URL}" \
  --build-arg APT_MIRROR="${APT_MIRROR}" \
  -t "${IMAGE}" \
  .

echo "==> 完成: ${IMAGE}"
docker images --format "{{.Repository}}:{{.Tag}}\t{{.Size}}" | grep clearvoice || true
echo "==> 下一步: ./deployment/tools/export.sh ${IMAGE}"
