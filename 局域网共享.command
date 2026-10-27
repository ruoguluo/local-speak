#!/bin/zsh
# 双击运行：让同网络的其他设备也能访问朗读服务。
#
# 默认不启用访问令牌，同网络里知道地址的人都能用。
# 想加令牌就在下面命令末尾加上 --token。

cd "$(dirname "$0")" || exit 1

PORT=8765

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for cand in python3 python3.13 python3.12 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      PY="$cand"
      break
    fi
  done
fi

if [ -z "$PY" ]; then
  echo "没找到 Python 3，先安装：https://www.python.org/downloads/"
  echo "按任意键关闭。"
  read -r -k 1
  exit 1
fi

if ! "$PY" -c "import numpy, requests, sounddevice, pypdf" >/dev/null 2>&1; then
  echo "缺少依赖，先运行："
  echo "  $PY -m pip install -r requirements.txt"
  echo "按任意键关闭。"
  read -r -k 1
  exit 1
fi

if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "端口 $PORT 已被占用（可能是之前启动的服务）。"
  echo "如果那是本机专用模式，先关掉它再运行本脚本，否则其他设备连不上。"
  echo ""
  lsof -nP -iTCP:$PORT -sTCP:LISTEN | tail -n +2
  echo ""
  echo "按任意键关闭。"
  read -r -k 1
  exit 1
fi

echo "正在以局域网模式启动…"
echo ""
"$PY" -u speak_server.py --port $PORT --lan
