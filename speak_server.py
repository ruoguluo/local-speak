#!/usr/bin/env python3
"""speak_server.py — 本地朗读 Web 应用。

    python speak_server.py                    # 只有本机能访问 127.0.0.1:8765
    python speak_server.py --lan              # 同网络设备可访问（无令牌）
    python speak_server.py --lan --token      # 同网络可访问，带自动生成的令牌
    python speak_server.py --port 9000

后端只用标准库，文本处理复用 pdf_speak.py，语音合成走 Ollama 的
/v1/audio/speech。前端把分块音频排队播放，所以暂停能停在任意位置。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import uuid
import wave
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests

try:
    import lameenc  # 自带 LAME，不依赖系统 ffmpeg
except ImportError:
    lameenc = None

from pdf_speak import ENDPOINT, SR, load, pause_for, running_heads, to_chunks, to_paragraphs

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
MP3_BITRATE = 64
CACHE_PER_DOC = 48  # 每份文档最多缓存多少块音频（约 24MB）
CACHE_TOTAL = 240   # 所有文档合计上限，约 120MB。播放列表会让文档数变多，必须封顶
LOCK = threading.Lock()
DOCS: dict[str, "Doc"] = {}
AUDIO_LRU: OrderedDict[tuple, None] = OrderedDict()
TOKEN: str | None = None  # None 表示不校验


class Doc:
    def __init__(self, name: str, desc: str, chunks: list[tuple[str, bool]], chars: int, lang: str = "en"):
        self.id = uuid.uuid4().hex[:12]
        self.name = name
        self.desc = desc
        self.chunks = chunks
        self.chars = chars
        self.lang = lang
        self.audio: OrderedDict[tuple, bytes] = OrderedDict()

    def meta(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "desc": self.desc,
            "chars": self.chars,
            "estSec": self.chars / 15,
            "lang": self.lang,
            "langModel": _LANG_MODEL.get(self.lang, ""),
            "total": len(self.chunks),
            "chunks": [{"i": i, "text": t, "paraEnd": p} for i, (t, p) in enumerate(self.chunks)],
        }

    def remember(self, key: tuple, data: bytes) -> None:
        # key 必须带 voice/model，否则换音色后会命中旧音频
        with LOCK:
            self.audio[key] = data
            self.audio.move_to_end(key)
            AUDIO_LRU[(self.id, key)] = None
            AUDIO_LRU.move_to_end((self.id, key))

            while len(self.audio) > CACHE_PER_DOC:
                old, _ = self.audio.popitem(last=False)
                AUDIO_LRU.pop((self.id, old), None)

            # 全局封顶：超了就从最久没用的块开始丢，跨文档回收
            while len(AUDIO_LRU) > CACHE_TOTAL:
                (doc_id, old), _ = AUDIO_LRU.popitem(last=False)
                target = DOCS.get(doc_id)
                if target is not None:
                    target.audio.pop(old, None)


def synth_pcm(text: str, model: str, voice: str | None) -> bytes:
    payload = {"model": model, "input": text, "response_format": "pcm"}
    if voice and voice != "default":
        payload["voice"] = voice
    resp = requests.post(ENDPOINT, json=payload, timeout=180)
    # 每个语言模型的音色库是独立的，英语音色在法语模型上不存在。
    # 前端会同步音色表，这里再兜一层：真撞上了就退回模型默认音色。
    if resp.status_code == 400 and "voice" in resp.text and "voice" in payload:
        payload.pop("voice")
        resp = requests.post(ENDPOINT, json=payload, timeout=180)
    if resp.status_code != 200:
        raise RuntimeError(f"合成失败 HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.content


def chunk_pcm(doc: Doc, i: int, model: str, voice: str | None) -> bytes:
    """裸 PCM（含停顿填充）。缓存裸流而不是 WAV，导出 MP3 时可直接复用。"""
    key = (voice or "default", model, i)
    if key in doc.audio:
        return doc.audio[key]
    text, para_end = doc.chunks[i]
    pcm = synth_pcm(text, model, voice)
    pcm += b"\x00\x00" * int(SR * pause_for(text, para_end))
    doc.remember(key, pcm)
    return pcm


def wav_bytes(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return buf.getvalue()


def mp3_stream(doc: Doc, model: str, voice: str | None):
    """边合成边编码，客户端能立刻开始接收，不必等整篇生成完。"""
    enc = lameenc.Encoder()
    enc.set_bit_rate(MP3_BITRATE)
    enc.set_in_sample_rate(SR)
    enc.set_channels(1)
    enc.set_quality(2)
    for i in range(len(doc.chunks)):
        block = enc.encode(chunk_pcm(doc, i, model, voice))
        if block:
            yield block
    tail = enc.flush()
    if tail:
        yield tail


def export_filename(doc: Doc) -> str:
    stem = Path(doc.name).stem or "audio"
    safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", stem).strip(" ._") or "audio"
    return f"{safe}.mp3"


def list_voices(model: str = "phonon") -> list[str]:
    """音色库按模型独立：英语模型 38 个，法语模型只有 16 个法语名字。"""
    try:
        out = subprocess.run(
            ["ollama", "show", model], capture_output=True, text=True, timeout=20
        ).stdout
    except Exception:
        return ["default"]
    lines = out.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip().lower() == "voices")
    except StopIteration:
        return ["default"]
    voices = []
    for line in lines[start + 1 :]:
        name = line.strip()
        if not name or name.lower().startswith("license"):
            break
        voices.append(name)
    return voices or ["default"]


def list_models() -> list[str]:
    try:
        out = subprocess.run(["ollama", "list"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ["phonon"]
    models = [
        line.split()[0]
        for line in out.splitlines()[1:]
        if line.strip() and line.split()[0].startswith("phonon")
    ]
    return models or ["phonon"]


# 语言识别：变音符号密度 + 高频词占比。词表刻意只收各语言独有或强特征的词，
# 像 as / no 这类英语里也常见的词不能放进去，否则英文会被误判。
_LANG_DIACRITICS = {
    "fr": "àâçéèêëîïôûùüÿœæ",
    "de": "äöüß",
    "es": "ñáíóú¿¡",
    "pt": "ãõáâêç",
}
_LANG_WORDS = {
    "fr": {"le", "la", "les", "des", "une", "est", "dans", "pour", "que", "qui", "pas",
           "vous", "nous", "avec", "sur", "du", "ce", "cette", "sont", "il", "au", "aux", "et", "par"},
    "de": {"der", "die", "das", "und", "ist", "nicht", "mit", "sich", "auf", "für", "ein",
           "eine", "dem", "den", "werden", "auch", "wir", "sie", "von", "zu", "im", "als"},
    "es": {"el", "los", "las", "que", "en", "una", "es", "por", "con", "para", "se", "su",
           "del", "como", "pero", "más", "este", "hay", "muy", "son", "está", "también", "cuando",
           "al", "lo", "porque", "hoy", "tiene", "hacer", "desde", "entre", "sin", "sobre"},
    "pt": {"do", "da", "dos", "das", "com", "não", "uma", "pelo", "pela", "mais", "você", "são",
           "está", "também", "muito", "quando", "porque", "mas", "já", "só", "na", "nas", "nos",
           "ao", "aos", "os", "em", "por", "para", "hoje", "tem", "fazer", "desde", "entre", "sem", "sobre"},
}
_LANG_MODEL = {
    "fr": "phonon:40m-fr",
    "de": "phonon:40m-de",
    "es": "phonon:40m-es",
    "pt": "phonon:40m-pt",
}


def detect_lang(text: str) -> str:
    sample = text[:4000].lower()
    words = re.findall(r"[a-zà-ÿœæ]+", sample)
    if not words:
        return "en"
    best, best_score = "en", 0.0
    for code, diacritics in _LANG_DIACRITICS.items():
        density = sum(1 for c in sample if c in diacritics) / len(sample) * 100
        hits = sum(1 for w in words if w in _LANG_WORDS[code])
        score = density * 0.5 + (hits / len(words)) * 12
        if score > best_score:
            best, best_score = code, score
    return best if best_score >= 0.9 else "en"


def lan_ips() -> list[str]:
    """本机的局域网地址。UDP 连接只用来选路由，不会真的发包。"""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        for m in re.finditer(r"inet (\d+\.\d+\.\d+\.\d+)", out):
            ip = m.group(1)
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def finalize(blocks: list[str], desc: str, name: str, is_pdf: bool) -> tuple[Doc | None, str]:
    """文件与粘贴文本共用的清洗、校验与分块。"""
    text = "\n".join(blocks)
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    if cjk and cjk / max(len(text.strip()), 1) > 0.03:
        return None, f"内容里有 {cjk / len(text):.0%} 的中文，但 Phonon 只支持英/法/德/西/葡，读不了中文。"
    if len(text.strip()) < 40:
        if is_pdf:
            return None, "几乎没有抽到文字。这大概是扫描版 PDF，需要先 OCR 才能朗读。"
        return None, "内容太短了，至少需要 40 个字符。"

    junk = running_heads(blocks) if is_pdf else set()
    paras = to_paragraphs(blocks, junk, strict=is_pdf)
    chunks = to_chunks(paras)
    if not chunks:
        return None, "清洗后没有可用文本。"
    return Doc(name, desc, chunks, sum(len(c[0]) for c in chunks), detect_lang(text)), ""


def build_doc(raw: bytes, filename: str) -> tuple[Doc | None, str]:
    suffix = Path(filename).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(raw)
        tmp_path = Path(tmp.name)
    try:
        blocks, desc, is_pdf = load(tmp_path, None, None)
    finally:
        tmp_path.unlink(missing_ok=True)
    return finalize(blocks, desc, filename, is_pdf)


def derive_name(text: str) -> str:
    """用第一行非空文字给粘贴内容起名，播放列表里好认。"""
    for line in text.splitlines():
        s = re.sub(r"\s+", " ", line.strip())
        if s:
            return (s[:40] + "…") if len(s) > 40 else s
    return "粘贴文本"


def build_doc_from_text(text: str, name: str | None = None) -> tuple[Doc | None, str]:
    if not text.strip():
        return None, "没有收到文字。"
    return finalize([text], "粘贴文本", name or derive_name(text), False)


class Handler(BaseHTTPRequestHandler):
    server_version = "SpeakServer/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # 必须先格式化再判断：send_error 会把 HTTPStatus 枚举塞进 args[0]，
        # 直接对它做 in 判断会抛 TypeError，把处理线程搞崩。
        try:
            line = fmt % args
        except Exception:
            line = str(fmt)
        if "/api/audio/" in line:
            return  # 分块请求太密，不刷屏
        print(f"  {self.address_string()} {line}")

    def _authorized(self) -> bool:
        """带 ?t= 打开一次就会种下 cookie，之后前端的相对请求自动带上。"""
        if not TOKEN:
            return True
        if parse_qs(urlparse(self.path).query).get("t", [None])[0] == TOKEN:
            self._grant_cookie = True
            return True
        return f"speak_token={TOKEN}" in (self.headers.get("Cookie") or "")

    def _maybe_cookie(self) -> None:
        if getattr(self, "_grant_cookie", False):
            self.send_header(
                "Set-Cookie", f"speak_token={TOKEN}; Path=/; SameSite=Lax; Max-Age=86400"
            )

    def send_json(self, obj: dict, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._maybe_cookie()
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, data: bytes, ctype: str, code: int = 200, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if cache:
            self.send_header("Cache-Control", "private, max-age=3600")
        self._maybe_cookie()
        self.end_headers()
        self.wfile.write(data)

    def send_login_hint(self) -> None:
        page = (
            "<!DOCTYPE html><meta charset='utf-8'>"
            "<title>需要访问令牌</title>"
            "<body style=\"font:15px/1.7 -apple-system,sans-serif;max-width:520px;"
            "margin:80px auto;padding:0 20px;color:#2C2C2A\">"
            "<h2 style='font-weight:500'>需要访问令牌</h2>"
            "<p style='color:#5F5E5A'>这个朗读服务设了访问令牌。请使用启动时打印的完整链接"
            "（带 <code>?t=</code> 参数）打开，或者向服务提供者索取令牌。</p>"
            "</body>"
        ).encode()
        self.send_bytes(page, "text/html; charset=utf-8", code=401)

    def do_HEAD(self) -> None:
        """浏览器和可达性检测会发 HEAD，不实现会落到 send_error。"""
        path = urlparse(self.path).path
        if path in ("/", "/index.html") and (WEB / "index.html").exists():
            size = (WEB / "index.html").stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        # 图标放在鉴权之前：标签页图标不该因为没令牌就加载失败
        if path in ("/favicon.ico", "/favicon.svg"):
            name = path.lstrip("/")
            icon = WEB / name
            if icon.exists():
                ctype = "image/x-icon" if name.endswith(".ico") else "image/svg+xml"
                return self.send_bytes(icon.read_bytes(), ctype, cache=True)

        if not self._authorized():
            if path in ("/", "/index.html"):
                return self.send_login_hint()
            return self.send_json({"error": "需要访问令牌"}, 401)

        if path in ("/", "/index.html"):
            page = WEB / "index.html"
            if not page.exists():
                return self.send_json({"error": "缺少 web/index.html"}, 500)
            return self.send_bytes(page.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/voices":
            q = parse_qs(urlparse(self.path).query)
            model = q.get("model", ["phonon"])[0]
            return self.send_json({"voices": list_voices(model), "models": list_models()})

        if path == "/api/preview":
            q = parse_qs(urlparse(self.path).query)
            voice = q.get("voice", ["default"])[0]
            model = q.get("model", ["phonon"])[0]
            try:
                pcm = synth_pcm(
                    "This is how I sound. Pick the voice that fits the document.", model, voice
                )
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 502)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(pcm)
            return self.send_bytes(buf.getvalue(), "audio/wav")

        m = re.fullmatch(r"/api/audio/([0-9a-f]+)/(\d+)", path)
        if m:
            doc_id, idx = m.group(1), int(m.group(2))
            q = parse_qs(urlparse(self.path).query)
            voice = q.get("voice", [None])[0]
            model = q.get("model", ["phonon"])[0]
            doc = DOCS.get(doc_id)
            if not doc:
                return self.send_json({"error": "文档已失效，请重新选择文件"}, 404)
            if not 0 <= idx < len(doc.chunks):
                return self.send_json({"error": "块号越界"}, 404)
            try:
                data = wav_bytes(chunk_pcm(doc, idx, model, voice))
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 502)
            return self.send_bytes(data, "audio/wav", cache=True)

        m = re.fullmatch(r"/api/export/([0-9a-f]+)", path)
        if m:
            q = parse_qs(urlparse(self.path).query)
            voice = q.get("voice", [None])[0]
            model = q.get("model", ["phonon"])[0]
            doc = DOCS.get(m.group(1))
            if not doc:
                return self.send_json({"error": "文档已失效，请重新选择文件"}, 404)
            if lameenc is None:
                return self.send_json(
                    {"error": "服务端缺少 lameenc，请执行 pip install lameenc"}, 503
                )
            return self.stream_mp3(doc, model, voice)

        self.send_json({"error": "not found"}, 404)

    def stream_mp3(self, doc: "Doc", model: str, voice: str | None) -> None:
        """HTTP/1.0 下不发 Content-Length，靠关闭连接表示结束，客户端可边收边处理。"""
        name = export_filename(doc)
        # 64kbps = 8000 字节/秒。用 17 字符/秒估时长，比 estSec 的 15 更贴近实测，
        # 进度条才不会早早卡在 99%。
        est = int(doc.chars / 17 * MP3_BITRATE * 1000 / 8)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header(
            "Content-Disposition",
            f"attachment; filename=\"audio.mp3\"; filename*=UTF-8''{quote(name)}",
        )
        self.send_header("X-Est-Bytes", str(est))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = 0
        try:
            for block in mp3_stream(doc, model, voice):
                self.wfile.write(block)
                sent += len(block)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端点了取消：连接断开，写 socket 立刻失败，生成器随之停止
            print(f"  导出被取消：{name}（已发送 {sent} 字节）")
        except Exception as exc:
            print(f"  导出失败：{exc}")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        if not self._authorized():
            return self.send_json({"error": "需要访问令牌"}, 401)

        # 文件上传接口：Web 界面已改为只接受粘贴文本，此接口保留给 curl / 脚本调用
        if path == "/api/open":
            raw = self.rfile.read(length) if length else b""
            try:
                filename = base64.b64decode(self.headers.get("X-Filename", "")).decode()
            except Exception:
                filename = "upload.txt"
            if not raw:
                return self.send_json({"error": "空文件"}, 400)
            try:
                doc, err = build_doc(raw, filename)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                return self.send_json({"error": f"解析失败：{exc}"}, 400)
            if not doc:
                return self.send_json({"error": err}, 400)
            with LOCK:
                DOCS[doc.id] = doc
            return self.send_json(doc.meta())

        if path == "/api/text":
            body = self.rfile.read(length) if length else b""
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return self.send_json({"error": "请求格式错误"}, 400)
            text = payload.get("text") or ""
            name = (payload.get("name") or "").strip() or None
            if not isinstance(text, str):
                return self.send_json({"error": "text 必须是字符串"}, 400)
            try:
                doc, err = build_doc_from_text(text, name)
            except Exception as exc:
                return self.send_json({"error": f"处理失败：{exc}"}, 400)
            if not doc:
                return self.send_json({"error": err}, 400)
            with LOCK:
                DOCS[doc.id] = doc
            return self.send_json(doc.meta())

        self.send_json({"error": "not found"}, 404)


def main() -> int:
    global TOKEN

    ap = argparse.ArgumentParser(description="本地朗读 Web 应用")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default=None, help="绑定地址，默认 127.0.0.1")
    ap.add_argument("--lan", action="store_true", help="允许同网络其他设备访问（绑定 0.0.0.0）")
    ap.add_argument(
        "--token",
        nargs="?",
        const="auto",
        default=None,
        help="访问令牌；不给值则自动生成",
    )
    args = ap.parse_args()

    host = args.host or ("0.0.0.0" if args.lan else "127.0.0.1")
    if args.token == "auto":
        TOKEN = secrets.token_urlsafe(9)
    elif args.token:
        TOKEN = args.token

    server = ThreadingHTTPServer((host, args.port), Handler)
    suffix = f"?t={TOKEN}" if TOKEN else ""
    print("朗读服务已启动。")
    if host == "0.0.0.0":
        print("\n本机：")
        print(f"  http://127.0.0.1:{args.port}{suffix}")
        ips = lan_ips()
        if ips:
            print("\n同网络的其他设备用这些地址：")
            for ip in ips:
                print(f"  http://{ip}:{args.port}{suffix}")
        else:
            print("\n没找到局域网地址，检查一下网络连接。")
        if not TOKEN:
            print(
                "\n注意：当前没有访问令牌，同网络里任何人只要知道地址就能用这个服务、"
                "\n      也能占用你的显卡做语音合成。加 --token 可以只把带令牌的链接分享出去。"
            )
        else:
            print("\n访问令牌已启用，上面链接里的 ?t= 就是令牌，只把完整链接给信任的人。")
        print("\n首次运行时 macOS 可能会弹窗问是否允许 Python 接受传入连接，要选「允许」。")
    else:
        print(f"  http://{host}:{args.port}{suffix}")
        print("\n只有本机能访问。要让同网络其他设备也能用，加 --lan。")
    print("\n按 Ctrl+C 停止。\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
