#!/usr/bin/env bash
# 启动本仓库 MediaMTX（Linux）
# 一般无需手动跑：python main.py 会自动拉起
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
BIN="$DIR/mediamtx"
CFG="$DIR/mediamtx.local.yml"

if [[ ! -x "$BIN" ]]; then
  echo "未找到 mediamtx，正在下载..."
  bash "$DIR/download.sh"
fi

if [[ ! -x "$BIN" ]]; then
  echo "mediamtx 仍不存在，请检查网络后运行: ./deploy/mediamtx/download.sh" >&2
  exit 1
fi

if [[ ! -f "$CFG" ]]; then
  echo "缺少配置: $CFG" >&2
  exit 1
fi

echo "启动 MediaMTX..."
echo "  WHEP: http://127.0.0.1:8889/cam1/whep"
echo "  API:  http://127.0.0.1:9997/v3/paths/list"
echo "配置: $CFG"
echo "按 Ctrl+C 停止"
cd "$DIR"
exec ./mediamtx "$CFG"
