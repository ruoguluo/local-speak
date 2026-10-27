#!/bin/zsh
# 双击运行：让同网络的其他设备也能访问朗读服务。
#
# 默认不启用访问令牌，同网络里知道地址的人都能用。
# 想加令牌就在下面命令末尾加上 --token。

cd "$(dirname "$0")" || exit 1

PORT=8765

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
