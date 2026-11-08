#!/bin/zsh
# 双击安装：把「朗读所选文字 / 停止朗读」注册成 macOS 文本服务。
# 想卸载就在终端里执行：python3 install_service.py --uninstall

HERE="$(cd "$(dirname "$0")" && pwd)"
PY="$HERE/../.venv/bin/python"
[ -x "$PY" ] || PY="python3"

"$PY" "$HERE/install_service.py" "$@"
echo ""
echo "按任意键关闭窗口。"
read -r -k 1
