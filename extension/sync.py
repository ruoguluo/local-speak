"""把 web/ 的前端同步到扩展目录。

改动 web/index.html 或 web/app.js 之后，运行一次：
    python3 extension/sync.py

扩展页面受 MV3 CSP 约束不能执行内联脚本，所以 reader.html 在 app.js 之前
额外加载 config.js 注入 SPEAK_API_BASE；favicon 路径也从站点根改成扩展内相对路径。
"""

from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
EXT = ROOT / "extension"

html = (WEB / "index.html").read_text()
html = html.replace(
    '<script src="app.js"></script>',
    '<script src="config.js"></script>\n<script src="app.js"></script>',
)
html = html.replace('href="/favicon.svg"', 'href="favicon.svg"')
html = html.replace('href="/favicon.ico"', 'href="favicon.ico"')
(EXT / "reader.html").write_text(html)

for name in ("app.js", "favicon.svg", "favicon.ico"):
    shutil.copy2(WEB / name, EXT / name)

print("已同步：reader.html / app.js / favicon.svg / favicon.ico")
