#!/bin/sh
# 容器启动脚本：创建日志/临时目录后启动服务。
# 所有可调参数都来自挂载的 config.yaml 或 CV_* 环境变量，无需重新构建镜像。
set -e

LOG_DIR="${CV_LOG_DIR:-/var/log/clearvoice}"
TEMP_DIR="${CV_TEMP_DIR:-/tmp/clearvoice}"

mkdir -p "$LOG_DIR" "$TEMP_DIR"

echo "[entrypoint] log_dir=$LOG_DIR temp_dir=$TEMP_DIR"
echo "[entrypoint] config=${CV_CONFIG_FILE:-/app/config/config.yaml}"

exec python -m app.main
