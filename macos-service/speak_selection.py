#!/usr/bin/env python3
"""macOS 文本 Service 的播放后端：从 stdin 读选中文字，用本地 Phonon 直接朗读。

调用链：预览.app 选中文字 → 右键「服务」→ Automator「运行 Shell 脚本」
（传递输入：到 stdin）→ 本脚本 → Ollama /v1/audio/speech → sounddevice 输出。

与 Chrome 插件不同，这条链路不经过 speak_server.py，也不打开浏览器：
不抢焦点、不受浏览器自动播放策略限制；代价是没有播放列表 / 笔记 UI。

多个 Service 调用之间用一个 Unix socket 做控制通道：新选择或「停止朗读」
连上正在播放的进程发一个 stop，旧进程停下后再播新的。这样既不会两段音频
叠在一起，也不用 PID 文件 / ps（沙箱里常常不可用），更不会有 PID 复用误杀。

    echo "Hello world." | python3 speak_selection.py --dry-run   # 只看分块，不发声
    echo "Hello world." | python3 speak_selection.py             # 实际朗读
    python3 speak_selection.py --stop                           # 停止当前朗读

环境变量（改 install_service.py 生成的 COMMAND_STRING 即可长期生效）：
    SPEAK_MODEL   模型名，留空则按语种自动挑；对应语种模型没装时退回 phonon
    SPEAK_VOICE   音色名，留空用模型默认音色
"""

from __future__ import annotations

import argparse
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # 让 import pdf_speak 找到项目根目录

import numpy as np
import requests
import sounddevice as sd

from pdf_speak import SR, pause_for, synth, to_chunks, to_paragraphs

SOCK = Path("/tmp") / f"localread-speak-{os.getuid()}.sock"
LANG_MODEL = {
    "fr": "phonon:40m-fr",
    "de": "phonon:40m-de",
    "es": "phonon:40m-es",
    "pt": "phonon:40m-pt",
}
BASE_MODEL = "phonon"
PREFETCH = 3
FRAME = 2400  # 24000 Hz × 0.1 s，写块足够短，收到停止信号能迅速退出


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def notify(title: str, msg: str) -> None:
    """Service 没有终端，失败时必须用系统通知告诉用户，否则会静默无声。"""
    script = f'display notification "{_esc(msg)}" with title "{_esc(title)}"'
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=5,
                       capture_output=True)
    except Exception:
        pass  # 通知权限被关掉时不影响主流程，错误仍会写进日志


def stop_running(wait: bool = False) -> bool:
    """通知正在播放的进程停下；返回是否确实有进程在播。"""
    if not SOCK.exists():
        return False
    hit = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1.0)
            client.connect(str(SOCK))
            client.sendall(b"stop")
            hit = True
    except OSError:
        # 残留的 socket 文件（进程被强杀过）：清掉，别让后面的 bind 一直失败
        try:
            SOCK.unlink(missing_ok=True)
        except OSError:
            pass
    if hit and wait:
        # 等旧进程在 finally 里 unlink socket，避免新旧音频短暂重叠
        for _ in range(40):
            if not SOCK.exists():
                break
            time.sleep(0.05)
    return hit


def bind_control() -> socket.socket | None:
    try:
        SOCK.unlink(missing_ok=True)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCK))
        srv.listen(4)
        srv.settimeout(0.3)
        return srv
    except OSError:
        return None  # 拿不到控制通道也照常朗读，只是不能被中途停下


def installed_models() -> set[str]:
    """ollama 不在 Automator 的 PATH 里，按常见安装位置逐个试。"""
    out = ""
    for exe in ("/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "ollama"):
        try:
            out = subprocess.run(
                [exe, "list"], capture_output=True, text=True, timeout=5
            ).stdout
        except Exception:
            continue
        if out:
            break
    names = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.add(parts[0].removesuffix(":latest"))
    return names


def pick_model(text: str) -> str:
    override = os.environ.get("SPEAK_MODEL", "").strip()
    if override:
        return override
    try:
        from speak_server import detect_lang  # 复用 Web 版的语种判定，规则保持一致
        lang = detect_lang(text)
    except Exception:
        lang = "en"
    want = LANG_MODEL.get(lang, BASE_MODEL)
    if want == BASE_MODEL:
        return want
    # 多语种模型是独立权重；没装就退回英语模型，总比直接合成失败好
    return want if want in installed_models() else BASE_MODEL


def ollama_ready() -> bool:
    try:
        return requests.get("http://localhost:11434/api/tags", timeout=3).ok
    except Exception:
        return False


def prepare(text: str) -> list[tuple[str, bool]]:
    """与 speak_server.build_doc_from_text 同一套清洗规则（非 PDF 的宽松模式）。"""
    text = text.strip()
    if not text:
        raise ValueError("没有收到选中的文字。")
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if cjk and cjk / len(text) > 0.03:
        raise ValueError("检测到中文，但 Phonon 只支持英/法/德/西/葡，读不了中文。")
    paras = to_paragraphs([text], set(), strict=False)
    chunks = to_chunks(paras)
    if not chunks:
        raise ValueError("清洗后没有可朗读的文字。")
    return chunks


def play(chunks: list[tuple[str, bool]], model: str, voice: str | None) -> bool:
    stop = threading.Event()
    interrupted = threading.Event()
    errors: list[str] = []
    q: queue.Queue = queue.Queue(maxsize=PREFETCH)
    srv = bind_control()

    def on_signal(signum, _frame):
        interrupted.set()
        stop.set()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    def wake() -> None:
        """往队列塞个哨兵，让阻塞在 q.get() 的主线程立刻醒来。"""
        try:
            q.put_nowait(None)
        except queue.Full:
            pass

    def control() -> None:
        if srv is None:
            return
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                try:
                    if conn.recv(64).startswith(b"stop"):
                        interrupted.set()
                        stop.set()
                        wake()
                except OSError:
                    pass

    def producer() -> None:
        session = requests.Session()
        try:
            for text, para_end in chunks:
                if stop.is_set():
                    break
                q.put((synth(session, text, model, voice), pause_for(text, para_end)))
        except Exception as exc:
            errors.append(str(exc))
        finally:
            try:
                q.put(None, timeout=1)
            except queue.Full:
                pass

    threading.Thread(target=control, daemon=True).start()
    threading.Thread(target=producer, daemon=True).start()

    try:
        with sd.OutputStream(samplerate=SR, channels=1, dtype="float32") as stream:
            while not stop.is_set():
                item = q.get()
                if item is None:
                    break
                audio, pause = item
                for i in range(0, len(audio), FRAME):
                    if stop.is_set():
                        break
                    stream.write(audio[i:i + FRAME])
                if pause and not stop.is_set():
                    stream.write(np.zeros(int(SR * pause), dtype=np.float32))
    finally:
        stop.set()
        wake()
        if srv is not None:
            try:
                srv.close()
            except OSError:
                pass
        try:
            SOCK.unlink(missing_ok=True)
        except OSError:
            pass

    if errors and not interrupted.is_set():
        raise RuntimeError(errors[0][:200])
    return interrupted.is_set()


def main() -> int:
    ap = argparse.ArgumentParser(description="朗读 macOS Service 送来的选中文字")
    ap.add_argument("--dry-run", action="store_true", help="只打印分块，不合成也不播放")
    ap.add_argument("--stop", action="store_true", help="停止当前正在进行的朗读")
    args = ap.parse_args()

    if args.stop:
        print("已发送停止信号" if stop_running() else "当前没有正在进行的朗读")
        return 0

    text = sys.stdin.read()
    try:
        chunks = prepare(text)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        notify("本地朗读", str(exc))
        return 2

    model = pick_model(text)
    voice = os.environ.get("SPEAK_VOICE", "").strip() or None
    chars = sum(len(c[0]) for c in chunks)
    print(f"[{uuid.uuid4().hex[:8]}] {len(chunks)} 块 / {chars} 字符，model={model}, voice={voice or 'default'}",
          flush=True)

    if args.dry_run:
        for i, (t, para_end) in enumerate(chunks, 1):
            print(f"[{i:3d}]{'¶' if para_end else ' '}({len(t):3d}) {t}")
        return 0

    if not ollama_ready():
        msg = "Ollama 没在运行（或没拉取 phonon 模型），无法合成语音。"
        print(msg, file=sys.stderr)
        notify("本地朗读失败", msg)
        return 3

    stop_running(wait=True)
    try:
        was_stopped = play(chunks, model, voice)
    except SystemExit:
        print("朗读已中断", flush=True)
        return 0
    except Exception as exc:
        print(f"朗读失败：{exc}", file=sys.stderr)
        notify("本地朗读失败", str(exc)[:180])
        return 4
    print("朗读已中断" if was_stopped else "朗读完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
