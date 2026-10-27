#!/bin/zsh
# 双击运行：启动朗读服务并用默认浏览器打开。
# 如果服务已经在跑，就只打开浏览器。

cd "$(dirname "$0")" || exit 1

PORT=8765
URL="http://127.0.0.1:$PORT"

# 找一个可用的 Python 3：优先 $PYTHON，其次从 PATH 里挑
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
  echo "服务已在运行，直接打开浏览器。"
else
  echo "正在启动朗读服务…"
  "$PY" -u speak_server.py --port $PORT &
  SERVER_PID=$!

  # 最多等 10 秒，确认端口真的起来了再开浏览器
  for i in {1..40}; do
    if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
      break
    fi
    sleep 0.25
  done

  if ! lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "启动失败，端口 $PORT 没有监听。上面是错误信息。"
    echo "按任意键关闭。"
    read -r -k 1
    exit 1
  fi
fi

open "$URL"
echo ""
echo "已在浏览器打开 $URL"
echo "关闭这个窗口会停止服务（或按 Ctrl+C）。"
wait
