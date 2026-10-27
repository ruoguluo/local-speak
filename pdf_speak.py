#!/usr/bin/env python3
"""pdf_speak.py — 用本地 Phonon 把 PDF / TXT / Markdown 读出来。

流水线：抽文本 → 清洗（页眉、断字、Markdown 标记）→ 断句分块 → Phonon 流式合成 → 边生成边播

    python pdf_speak.py book.pdf
    python pdf_speak.py notes.txt --voice Hugh
    python pdf_speak.py README.md --limit 3
    python pdf_speak.py book.pdf --out book.wav --no-play
    python pdf_speak.py notes.txt --dry-run
"""

from __future__ import annotations

import argparse
import queue
import re
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import requests
import sounddevice as sd
from pypdf import PdfReader

ENDPOINT = "http://localhost:11434/v1/audio/speech"
SR = 24000
TARGET = 240
HARD = 340

# 句末标点后切分，但要先保护常见缩写
_PROTECT = [
    r"\bMr\.", r"\bMrs\.", r"\bMs\.", r"\bDr\.", r"\bProf\.", r"\bSt\.",
    r"\bJr\.", r"\bSr\.", r"\bvs\.", r"\betc\.", r"\be\.g\.", r"\bi\.e\.",
    r"\bFig\.", r"\bfig\.", r"\bNo\.", r"\bpp\.", r"\bVol\.", r"\bInc\.",
    r"\bLtd\.", r"\bal\.", r"\bcf\.", r"\bapprox\.",
]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


PDF_SUFFIX = {".pdf"}
TEXT_SUFFIX = {".txt", ".text", ".md", ".markdown"}
ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1")


def extract_pdf(path: Path, first: int | None, last: int | None) -> tuple[list[str], str]:
    reader = PdfReader(str(path))
    total = len(reader.pages)
    lo = 1 if first is None else max(1, first)
    hi = total if last is None else min(total, last)
    pages = []
    for i in range(lo, hi + 1):
        try:
            pages.append(reader.pages[i - 1].extract_text() or "")
        except Exception as exc:  # 单页坏了不该毁掉整本
            log(f"  page {i}: 抽取失败 ({exc})")
            pages.append("")
    return pages, f"{len(pages)}/{total} 页"


def read_text(path: Path) -> tuple[str, str]:
    """按顺序试编码。utf-8 失败多半是 GBK/Big5 的中文文件。"""
    raw = path.read_bytes()
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        return text, f"{path.suffix.lower()} 文件，{enc}"
    return raw.decode("utf-8", errors="replace"), f"{path.suffix.lower()} 文件，含无法解码的字节"


def strip_markdown(text: str) -> str:
    """把 Markdown 标记去掉，别让合成器把 # 和 ** 念出来。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    # 标题单独成句，否则会跟后面正文连读成一串
    text = re.sub(
        r"^\s{0,3}#{1,6}\s*(.+?)\s*$",
        lambda m: m.group(1) if m.group(1)[-1:] in ".!?:" else m.group(1) + ".",
        text,
        flags=re.M,
    )
    text = re.sub(r"^\s{0,3}>\s?", "", text, flags=re.M)
    # 列表项补句号，否则几条 bullet 会被连读成一串
    text = re.sub(
        r"^\s*[-*+]\s+(.+?)\s*$",
        lambda m: m.group(1) if m.group(1)[-1:] in ".!?:;" else m.group(1) + ".",
        text,
        flags=re.M,
    )
    text = re.sub(r"^\s*\|.*\|\s*$", "", text, flags=re.M)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"<?https?://\S+>?", "", text)
    return text


def load(path: Path, first: int | None, last: int | None) -> tuple[list[str], str, bool]:
    """返回 (文本块列表, 描述, 是否 PDF)。"""
    suffix = path.suffix.lower()
    if suffix in PDF_SUFFIX:
        pages, desc = extract_pdf(path, first, last)
        return pages, desc, True
    if suffix in TEXT_SUFFIX:
        text, desc = read_text(path)
        if suffix in {".md", ".markdown"}:
            text = strip_markdown(text)
        return [text], desc, False
    raise ValueError(f"不支持的文件类型：{suffix or '(无后缀)'}，只认 .pdf / .txt / .md")


def running_heads(pages: list[str]) -> set[str]:
    """跨页重复出现的短行，多半是页眉页脚。

    不能只看首末行：pypdf 按内容流顺序抽文字，页眉可能落在正文中间。
    所以统计每一页的所有短行，重复率够高的才判为页眉。
    """
    seen: dict[str, int] = {}
    live = 0
    for text in pages:
        lines = {l.strip() for l in text.splitlines() if l.strip()}
        if not lines:
            continue
        live += 1
        for cand in lines:
            if len(cand) < 80:
                seen[cand] = seen.get(cand, 0) + 1
    threshold = max(3, int(live * 0.3))
    return {line for line, n in seen.items() if n >= threshold}


def keep(line: str, junk: set[str], strict: bool) -> bool:
    """strict=True 用于 PDF：丢弃页码、图表行、符号密集行。
    txt 里这些可能是用户真写的内容，所以宽松模式只丢分隔线和纯符号行。"""
    if not line or line in junk:
        return False
    if re.fullmatch(r"[-=*_~#\s]{3,}", line):
        return False
    if not strict:
        return True
    if re.fullmatch(r"[-–—\s]*\d{1,4}[-–—\s]*", line):
        return False
    if len(line) > 4:
        alpha = sum(c.isalpha() or c.isspace() for c in line) / len(line)
        if alpha < 0.5:
            return False
    return True


def to_paragraphs(blocks: list[str], junk: set[str], strict: bool) -> list[str]:
    """把硬换行的行拼回段落。

    断字连字符必须在这里处理：只有在这一步才知道连字符位于行尾。
    早先版本先把换行变成空格再统一去连字符，会把 "well- known" 误拼成
    "wellknown"——那是两个词，不是一个词被拆开。
    """
    paras: list[str] = []
    for text in blocks:
        cur = ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                if cur:
                    paras.append(cur)
                    cur = ""
                continue
            if not keep(line, junk, strict):
                continue
            # 列表符号在 PDF 和 txt 里都会出现，不剥掉会被念成"横杠"
            line = re.sub(r"^[-*•·–—]\s+", "", line)
            if not line:
                continue
            if not cur:
                cur = line
            elif cur.endswith("-") and line[:1].islower():
                cur = cur[:-1] + line
            else:
                cur = f"{cur} {line}"
        if cur:
            paras.append(cur)
    return [re.sub(r"\s{2,}", " ", p).strip() for p in paras if p.strip()]


def sentences(para: str) -> list[str]:
    text = para
    for i, pat in enumerate(_PROTECT):
        text = re.sub(pat, pat.replace(r"\.", f"\x00{i}\x00"), text)
    parts = re.split(r"(?<=[.!?])\s+", text)
    out = []
    for part in parts:
        part = re.sub(r"\x00\d+\x00", ".", part).strip()
        if part:
            out.append(part)
    return out


def to_chunks(paras: list[str]) -> list[tuple[str, bool]]:
    """返回 (文本, 是否段落结尾)。段落结尾要停顿更久，否则整段像一口气念完。"""
    chunks: list[tuple[str, bool]] = []
    buf = ""
    buf_end = False
    for para in paras:
        # 跨段合并时，上一段若没有终止标点就补一个，否则标题会跟正文连读
        if buf and buf[-1:] not in ".!?:;":
            buf += "."
        sents = sentences(para)
        for idx, sent in enumerate(sents):
            last_of_para = idx == len(sents) - 1
            while len(sent) > HARD:
                cut = max(
                    sent.rfind(", ", 0, HARD),
                    sent.rfind("; ", 0, HARD),
                    sent.rfind(" — ", 0, HARD),
                    sent.rfind(" ", 0, HARD),
                )
                if cut < TARGET // 3:
                    cut = HARD
                if buf:
                    chunks.append((buf, buf_end))
                    buf = ""
                chunks.append((sent[: cut + 1].strip(), False))
                sent = sent[cut + 1 :].strip()
                if not sent:
                    break
            if not sent:
                continue
            if not buf:
                buf, buf_end = sent, last_of_para
            elif len(buf) + 1 + len(sent) <= TARGET:
                buf, buf_end = f"{buf} {sent}", last_of_para
            else:
                chunks.append((buf, buf_end))
                buf, buf_end = sent, last_of_para
    if buf:
        chunks.append((buf, buf_end))
    return chunks


def pause_for(chunk: str, para_end: bool) -> float:
    terminal = chunk.rstrip()[-1:] in ".!?"
    if para_end and terminal:
        return 0.45
    if terminal:
        return 0.28
    return 0.14


def synth(session: requests.Session, text: str, model: str, voice: str | None, tries: int = 3):
    payload = {"model": model, "input": text, "response_format": "pcm"}
    if voice:
        payload["voice"] = voice
    for attempt in range(tries):
        try:
            resp = session.post(ENDPOINT, json=payload, timeout=180)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
            return np.frombuffer(resp.content, dtype="<i2").astype(np.float32) / 32768.0
        except Exception:
            if attempt == tries - 1:
                raise
            time.sleep(0.7 * (attempt + 1))
    raise RuntimeError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser(description="用本地 Phonon 朗读 PDF")
    ap.add_argument("source", type=Path, help="要朗读的文件：.pdf / .txt / .md")
    ap.add_argument("--voice", default=None, help="音色，如 Zoey / Easton / Freya / Hugh")
    ap.add_argument("--model", default="phonon")
    ap.add_argument("--pages", default=None, help="页码范围，如 1-12 或 5（仅 PDF）")
    ap.add_argument("--out", type=Path, default=None, help="另存为 WAV")
    ap.add_argument("--no-play", action="store_true", help="不播放，只合成")
    ap.add_argument("--dry-run", action="store_true", help="只打印分块结果，不合成")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 块，用于试听音色")
    ap.add_argument("--prefetch", type=int, default=4, help="预取块数")
    args = ap.parse_args()

    if not args.source.exists():
        log(f"找不到文件：{args.source}")
        return 2

    first = last = None
    if args.pages:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", args.pages.strip())
        if not m:
            log("--pages 格式应为 1-12 或 5")
            return 2
        first = int(m.group(1))
        last = int(m.group(2)) if m.group(2) else first
        if args.source.suffix.lower() not in PDF_SUFFIX:
            log("--pages 只对 PDF 有效，已忽略。")
            first = last = None

    t0 = time.time()
    try:
        blocks, desc, is_pdf = load(args.source, first, last)
    except ValueError as exc:
        log(str(exc))
        return 2
    raw = "\n".join(blocks)
    log(f"读取 {desc}，{len(raw)} 字符，用时 {time.time() - t0:.1f}s")

    # 语言检查必须在长度检查之前：短的中文文件否则会被误报成"没有文字"
    cjk = sum(1 for c in raw if "\u4e00" <= c <= "\u9fff")
    if cjk and cjk / max(len(raw.strip()), 1) > 0.03:
        log(f"检测到中文内容（占比 {cjk / len(raw):.0%}），但 Phonon 只支持英/法/德/西/葡，读不了中文。")
        return 4

    if len(raw.strip()) < 40:
        if is_pdf:
            log("几乎没有抽到文字 —— 这大概是扫描版 PDF，需要先 OCR（视觉模型转文字）再喂给 Phonon。")
        else:
            log("文件里没有可用文字。")
        return 3

    # 页眉页脚检测只对 PDF 有意义；txt 里重复的行可能是用户真写的
    junk = running_heads(blocks) if is_pdf else set()
    paras = to_paragraphs(blocks, junk, strict=is_pdf)
    chunks = to_chunks(paras)
    if not chunks:
        log("清洗后没有可用文本。")
        return 3
    chars = sum(len(c[0]) for c in chunks)
    secs = chars / 15
    span = f"{secs / 60:.0f} 分钟" if secs >= 90 else f"{secs:.0f} 秒"
    log(f"{len(paras)} 段 → {len(chunks)} 块，约 {chars} 字符，预计 {span}音频")
    if args.limit and args.limit < len(chunks):
        chunks = chunks[: args.limit]
        log(f"  --limit：只处理前 {len(chunks)} 块")

    if args.dry_run:
        for i, (text, para_end) in enumerate(chunks, 1):
            mark = "¶" if para_end else " "
            print(f"[{i:3d}]{mark}({len(text):3d}) {text}")
        return 0

    wav = None
    if args.out:
        wav = wave.open(str(args.out), "wb")
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SR)

    audio_q: queue.Queue = queue.Queue(maxsize=max(1, args.prefetch))
    stop = threading.Event()
    errors: list[str] = []

    def producer() -> None:
        session = requests.Session()
        try:
            for i, (chunk, para_end) in enumerate(chunks, 1):
                if stop.is_set():
                    break
                t = time.time()
                audio = synth(session, chunk, args.model, args.voice)
                dur = len(audio) / SR
                log(f"  [{i}/{len(chunks)}] 合成 {time.time() - t:.2f}s → 音频 {dur:.1f}s")
                audio_q.put((audio, pause_for(chunk, para_end)))
        except Exception as exc:
            errors.append(str(exc))
            log(f"合成失败：{exc}")
        finally:
            audio_q.put(None)

    thread = threading.Thread(target=producer, daemon=True)
    thread.start()

    played = 0.0
    try:
        stream = None
        if not args.no_play:
            stream = sd.OutputStream(samplerate=SR, channels=1, dtype="float32")
            stream.start()
        while True:
            item = audio_q.get()
            if item is None:
                break
            audio, pause = item
            if wav is not None:
                wav.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
                if pause:
                    wav.writeframes(np.zeros(int(SR * pause), dtype="<i2").tobytes())
            if stream is not None:
                for i in range(0, len(audio), 2400):
                    stream.write(audio[i : i + 2400])
                if pause:
                    stream.write(np.zeros(int(SR * pause), dtype=np.float32))
            played += len(audio) / SR
        if stream is not None:
            stream.stop()
            stream.close()
    except KeyboardInterrupt:
        log("\n中断。")
        stop.set()
    finally:
        if wav is not None:
            wav.close()
            log(f"已写入 {args.out}（约 {played:.0f}s 音频）")

    if errors:
        return 5
    log(f"完成，共 {played:.0f}s 音频。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
