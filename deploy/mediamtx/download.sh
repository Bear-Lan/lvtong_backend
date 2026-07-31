#!/usr/bin/env bash
# 下载 MediaMTX Linux amd64（不覆盖 mediamtx.local.yml）
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION="v1.19.3"
ARCH="linux_amd64"
URL="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/mediamtx_${VERSION}_${ARCH}.tar.gz"

mkdir -p "$DIR"
TMP="$(mktemp -d)"
ARCHIVE="$TMP/mediamtx.tar.gz"

cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

echo "下载: $URL"
if command -v curl >/dev/null 2>&1; then
  curl -fL --retry 5 --retry-delay 2 -o "$ARCHIVE" "$URL"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$ARCHIVE" "$URL"
else
  echo "需要 curl 或 wget" >&2
  exit 1
fi

tar -xzf "$ARCHIVE" -C "$TMP"
BIN="$(find "$TMP" -type f -name mediamtx | head -n 1)"
if [[ -z "$BIN" ]]; then
  echo "压缩包内未找到 mediamtx" >&2
  exit 1
fi

cp -f "$BIN" "$DIR/mediamtx"
chmod +x "$DIR/mediamtx"

echo "OK: $DIR/mediamtx"
