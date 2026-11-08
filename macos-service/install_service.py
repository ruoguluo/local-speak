#!/usr/bin/env python3
"""生成并安装两个 macOS 文本 Service（Automator Quick Action）。

    python3 install_service.py                        # 安装（默认 ⌃⌥⌘S 开始朗读）
    python3 install_service.py --shortcut '~@r'       # 改成 ⌥⌘R
    python3 install_service.py --shortcut ''          # 不设快捷键
    python3 install_service.py --shortcut-stop '^~@x' # 顺带给「停止朗读」设快捷键
    python3 install_service.py --dry-run              # 只打印将要写入的内容，不落盘
    python3 install_service.py --force                # 覆盖已有的同名工作流
    python3 install_service.py --uninstall            # 移除本脚本安装的全部工作流

原理：~/Library/Services/*.workflow 是一个 bundle，Info.plist 里的 NSServices
声明「接收 public.utf8-plain-text」，pbs 把它注册进所有 App 的「服务」菜单；
Contents/document.wflow 里只有一个「运行 Shell 脚本」动作，inputMethod=0
表示把选中的文字通过 stdin 传给脚本。安装后必须 pbs -flush / -update 刷新缓存。

快捷键写在 NSServices 的 NSKeyEquivalent 里（与系统自带服务同一套声明），
由 pbs 读取，因此会跟着仓库走、每台机器安装后自动生效，无需手动去系统设置里点。
修饰键编码：@ = ⌘，^ = ⌃，~ = ⌥，$ = ⇧；字母用小写避免被当成隐含 Shift。
用户之后在「系统设置 → 键盘 → 键盘快捷键 → 服务」里改过的值会覆盖这里的默认值。
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TEMPLATE = HERE / "RunShellScript.wflow"
SERVICES = Path.home() / "Library" / "Services"
PBS = "/System/Library/CoreServices/pbs"
MARKER = "LocalReadServiceVersion"
PLAYER = HERE / "speak_selection.py"
LOG = '"$HOME/Library/Logs/localread-service.log"'  # 写进 zsh 脚本里，运行时展开
LOG_PATH = Path.home() / "Library" / "Logs" / "localread-service.log"

WORKFLOWS = ("朗读所选文字", "停止朗读")

# 默认开始朗读：⌃⌥⌘S。三个修饰键基本不会和系统 / 常用 App 撞车。
# 停止朗读默认不设：文本型 Service 只有在「当前有选中文字」时才可用，
# 读完点掉选区后再按会失效，做成全局快捷键反而容易让人误以为坏了。
DEFAULT_SHORTCUT = "^~@s"
DEFAULT_STOP_SHORTCUT = ""
MODIFIERS = {"^": "⌃", "~": "⌥", "$": "⇧", "@": "⌘"}


def describe_shortcut(value: str) -> str:
    """^~@s → ⌃⌥⌘S，用于安装输出，便于核对按什么键。"""
    if not value:
        return "（不设置）"
    mods = "".join(MODIFIERS[c] for c in value if c in MODIFIERS)
    key = "".join(c for c in value if c not in MODIFIERS)
    return mods + key.upper()


def check_shortcut(value: str, label: str) -> None:
    if not value:
        return
    key = "".join(c for c in value if c not in MODIFIERS)
    if len(key) != 1 or not key.isprintable():
        raise SystemExit(
            f"{label} 格式不对：{value!r}。应是修饰键 + 单个按键，"
            "例如 ^~@s（⌃⌥⌘S）、~@r（⌥⌘R）；传空字符串表示不设置。"
        )


def shquote(s: str) -> str:
    """生成 zsh 双引号字符串；路径里有空格是常态，必须逐个转义特殊字符。"""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') \
                   .replace("$", "\\$").replace("`", "\\`") + '"'


def pick_python() -> str:
    """Automator 的 PATH 很干净，必须写解释器的绝对路径。"""
    candidates = [ROOT / ".venv" / "bin" / "python", Path(sys.executable)]
    candidates += [Path(p) for p in ("/usr/bin/python3", "/usr/local/bin/python3")]
    for exe in candidates:
        if not exe.exists():
            continue
        try:
            ok = subprocess.run(
                [str(exe), "-c", "import numpy, requests, sounddevice, pypdf"],
                capture_output=True, timeout=10,
            ).returncode == 0
        except Exception:
            continue
        if ok:
            return str(exe)
    raise SystemExit(
        "找不到装好依赖的 Python。请先在项目目录执行：\n"
        "  python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
    )


def make_workflow(command: str) -> dict:
    wf = plistlib.loads(TEMPLATE.read_bytes())
    action = wf["actions"][0]["action"]
    action["ActionParameters"]["COMMAND_STRING"] = command
    # 每个 workflow 用独立 UUID，避免与别的 Automator 文档撞 key
    action["UUID"] = str(uuid.uuid4()).upper()
    action["InputUUID"] = str(uuid.uuid4()).upper()
    action["OutputUUID"] = str(uuid.uuid4()).upper()
    return wf


def make_info(title: str, shortcut: str = "") -> dict:
    service = {
        "NSBackgroundColorName": "background",
        "NSIconName": "NSActionTemplate",
        "NSMenuItem": {"default": title},
        "NSMessage": "runWorkflowAsService",
        "NSSendTypes": ["public.utf8-plain-text"],
    }
    if shortcut:
        service["NSKeyEquivalent"] = {"default": shortcut}
    return {
        MARKER: 1,  # 卸载时用来确认这个 bundle 确实是本脚本生成的
        "NSServices": [service],
    }


def is_ours(bundle: Path) -> bool:
    try:
        with (bundle / "Contents" / "Info.plist").open("rb") as f:
            return MARKER in plistlib.load(f)
    except Exception:
        return False


def refresh_services() -> None:
    for flag in ("-flush", "-update"):
        try:
            subprocess.run([PBS, flag], capture_output=True, timeout=20)
        except Exception as exc:
            print(f"  警告：pbs {flag} 执行失败：{exc}")


def install(dry_run: bool, force: bool, shortcuts: dict[str, str]) -> int:
    py = pick_python()
    qpy, qplayer = shquote(py), shquote(str(PLAYER))
    commands = {
        # 不 exec、末尾吞掉退出码：避免 Automator 在出错时弹错误框；
        # 真正的问题由脚本写进日志 + 系统通知告诉用户。
        WORKFLOWS[0]: (
            f'{qpy} -u {qplayer} >> {LOG} 2>&1 || true'
        ),
        # 停止动作同样会收到 stdin 上的文字，先排空，避免管道写满卡住
        WORKFLOWS[1]: (
            f'cat > /dev/null\n{qpy} -u {qplayer} --stop >> {LOG} 2>&1 || true'
        ),
    }

    print(f"Python：{py}")
    print(f"播放脚本：{PLAYER}")
    print(f"安装位置：{SERVICES}\n")

    for name in WORKFLOWS:
        bundle = SERVICES / f"{name}.workflow"
        cmd = commands[name]
        print(f"[{name}]  快捷键 {describe_shortcut(shortcuts[name])}")
        print(f"  {cmd.splitlines()[0]}")
        if dry_run:
            continue
        if bundle.exists():
            if not force and not is_ours(bundle):
                print(f"  跳过：{bundle} 已存在且不是本脚本生成的（加 --force 才覆盖）")
                continue
            shutil.rmtree(bundle)
        contents = bundle / "Contents"
        contents.mkdir(parents=True, exist_ok=True)
        with (contents / "Info.plist").open("wb") as f:
            plistlib.dump(make_info(name, shortcuts[name]), f,
                          fmt=plistlib.FMT_XML, sort_keys=True)
        with (contents / "document.wflow").open("wb") as f:
            plistlib.dump(make_workflow(cmd), f, fmt=plistlib.FMT_XML, sort_keys=True)
        subprocess.run(["/usr/bin/plutil", "-lint", str(contents / "Info.plist")],
                       capture_output=True)
        subprocess.run(["/usr/bin/plutil", "-lint", str(contents / "document.wflow")],
                       capture_output=True)
        print(f"  已写入 {bundle}")

    if not dry_run:
        refresh_services()
        print("\n已刷新系统服务缓存。")
        print("在「预览」里选中文字，按 " + describe_shortcut(shortcuts[WORKFLOWS[0]])
              + " 即可朗读；也可以右键 →「服务」。")
        print("查看 / 修改快捷键：系统设置 → 键盘 → 键盘快捷键… → 服务 → 文本。")
        print("没生效时先在那里确认服务已勾选，再注销后重新登录。")
        print(f"日志：{LOG_PATH}")
    return 0


def uninstall(dry_run: bool) -> int:
    for name in WORKFLOWS:
        bundle = SERVICES / f"{name}.workflow"
        if not bundle.exists():
            print(f"[{name}] 不存在，跳过")
            continue
        if not is_ours(bundle):
            print(f"[{name}] 不是本脚本生成的，保留不动：{bundle}")
            continue
        print(f"[{name}] 移除 {bundle}")
        if not dry_run:
            shutil.rmtree(bundle)
    if not dry_run:
        refresh_services()
        print("已刷新系统服务缓存。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="安装 macOS 本地朗读 Service")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不写文件")
    ap.add_argument("--force", action="store_true", help="覆盖同名工作流")
    ap.add_argument("--uninstall", action="store_true", help="移除本脚本安装的工作流")
    ap.add_argument("--shortcut", default=DEFAULT_SHORTCUT,
                    help=f"开始朗读的快捷键，默认 {DEFAULT_SHORTCUT}（{describe_shortcut(DEFAULT_SHORTCUT)}）；"
                         "传空字符串为不设置")
    ap.add_argument("--shortcut-stop", default=DEFAULT_STOP_SHORTCUT,
                    help="「停止朗读」的快捷键，默认不设置")
    args = ap.parse_args()
    if not TEMPLATE.exists():
        raise SystemExit(f"缺少模板文件：{TEMPLATE}")
    if args.uninstall:
        return uninstall(args.dry_run)
    check_shortcut(args.shortcut, "--shortcut")
    check_shortcut(args.shortcut_stop, "--shortcut-stop")
    shortcuts = {WORKFLOWS[0]: args.shortcut, WORKFLOWS[1]: args.shortcut_stop}
    return install(args.dry_run, args.force, shortcuts)


if __name__ == "__main__":
    raise SystemExit(main())
