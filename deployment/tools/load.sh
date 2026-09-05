#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 【内网机器执行】导入运行镜像
#
# 用法：
#   ./deployment/tools/load.sh /data/transfer/clearvoice-denoise_1.0.0.tar
#   ./deployment/tools/load.sh ./offline-artifacts/clearvoice-denoise_1.0.0.tar --verify
# -----------------------------------------------------------------------------
set -euo pipefail

TAR="${1:?请指定 tar 包路径，例如 ./deployment/tools/load.sh ./offline-artifacts/xxx.tar}"
VERIFY="${2:-}"

if [[ ! -f "${TAR}" ]]; then
  echo "文件不存在: ${TAR}" >&2
  exit 1
fi

if [[ "${VERIFY}" == "--verify" ]]; then
  SHA="${TAR}.sha256"
  if [[ -f "${SHA}" ]]; then
    echo "==> 校验完整性"
    if command -v sha256sum >/dev/null 2>&1; then
      (cd "$(dirname "${TAR}")" && sha256sum -c "$(basename "${SHA}")")
    else
      (cd "$(dirname "${TAR}")" && shasum -a 256 -c "$(basename "${SHA}")")
    fi
  else
    echo "未找到 ${SHA}，跳过校验" >&2
  fi
fi

echo "==> 导入镜像: ${TAR}"
docker load -i "${TAR}"

echo
echo "==> 已导入的镜像："
docker images --format "{{.Repository}}:{{.Tag}}\t{{.Size}}" | grep clearvoice || true
echo
echo "==> 启动示例（Docker 18.09）："
echo "    docker run -d --name clearvoice --runtime=nvidia -p 8000:8000 \\"
echo "      -v /data/models/ClearerVoice-Studio:/opt/models/ClearerVoice-Studio:ro \\"
echo "      -v /data/clearvoice/logs:/var/log/clearvoice \\"
echo "      -v /data/clearvoice/tmp:/tmp/clearvoice \\"
echo "      -e NVIDIA_VISIBLE_DEVICES=0 \\"
echo "      clearvoice-denoise:1.0.0"
