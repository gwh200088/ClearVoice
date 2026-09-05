#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# 【联网机器执行】把运行镜像导出成 tar，拷进内网
#
# 用法（在仓库根目录执行）：
#   ./deployment/tools/export.sh
#   ./deployment/tools/export.sh clearvoice-denoise:1.0.0 /data/transfer
# -----------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")/../.."

IMAGE="${1:-clearvoice-denoise:1.0.0}"
OUT_DIR="${2:-./offline-artifacts}"

NAME="$(echo "${IMAGE}" | tr '/:' '__')"
TAR="${OUT_DIR}/${NAME}.tar"

mkdir -p "${OUT_DIR}"

echo "==> 导出 ${IMAGE} -> ${TAR}"
docker save -o "${TAR}" "${IMAGE}"

echo "==> 计算 SHA256"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "${OUT_DIR}" && sha256sum "$(basename "${TAR}")" > "${TAR}.sha256")
else
  (cd "${OUT_DIR}" && shasum -a 256 "$(basename "${TAR}")" > "${TAR}.sha256")
fi

# 顺带把默认配置文件导出一份，方便内网按需改参（可选，镜像里也有一份）
CONF="${OUT_DIR}/config.yaml"
if [[ -f deployment/config/config.yaml ]]; then
  cp deployment/config/config.yaml "${CONF}"
fi

ls -lh "${TAR}"
echo
echo "==> 完成。请把下面文件拷贝到内网机器："
echo "    ${TAR}"
echo "    ${TAR}.sha256"
echo "    ${CONF}   (可选，内网按需改参后挂载到 /app/config/config.yaml)"
echo
echo "    模型权重不用拷镜像 —— 你自己的模型包直接在内网用 -v 挂载即可。"
