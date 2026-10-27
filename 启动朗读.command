#!/bin/zsh
# 双击运行：启动朗读服务并用默认浏览器打开。
# 如果服务已经在跑，就只打开浏览器。

cd "$(dirname "$0")" || exit 1

PORT=8765
URL="http://127.0.0.1:$PORT"

# 挑一个真正装了依赖的 Python：$PYTHON > 项目内 .venv > PATH 里的 python3
pick_python() {
  local cands=()
  [ -n "$PYTHON" ] && cands+=("$PYTHON")
  [ -x "./.venv/bin/python" ] && cands+=("./.venv/bin/python")
  cands+=(python3 python3.13 python3.12 python)
  for c in $cands; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "import numpy, requests, sounddevice, pypdf" >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
  echo "没找到装好依赖的 Python。请在项目目录执行："
  echo "  python3 -m venv .venv"
  echo "  ./.venv/bin/pip install -r requirements.txt"
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
