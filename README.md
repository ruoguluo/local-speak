# 本地朗读

把文字读出来，全程离线。语音合成跑在本机的 [Ollama](https://ollama.com) 上，用的是 Gradium 的 [Phonon](https://ollama.com/library/phonon) 语音模型——内容不出机器，没有 API 费用。

Web 界面里粘贴一段文字就能听，也可以拖入 PDF / TXT / Markdown 文件，带播放列表、分段跳转、按时间戳记笔记。另附一个命令行版本，适合批量处理。

## 功能

**Web 界面**（`speak_server.py`）

- 粘贴即读：贴进文本框点一下就开始朗读，⌘ + Enter 也行
- 添加文件：点「＋ 文件」或直接把 PDF / TXT / MD 拖进输入区
- 侧边播放列表：粘贴与文件混排，播完自动接下一段
- 语言模型手动切换：下拉里选哪个就用哪个，新增文档不会自动改动模型
- 分段文本列表：点任意段落从那里开始播
- 暂停 / 继续停在任意位置，不丢进度
- 音色随模型切换：英语 38 个，各语言一套独立音色
- 笔记：一键记录当前播放位置，点时间戳跳回去重听，可复制成纯文本
- 一键导出 MP3：把整篇合成为 64 kbps 单声道 MP3 下载，边合成边传输，生成过程中可随时取消
- 可只监听本机，也可开放给同网络的其他设备（可选访问令牌）

**命令行**（`pdf_speak.py`，用于整份文件）

```bash
python pdf_speak.py book.pdf                  # 直接朗读
python pdf_speak.py notes.txt --voice Hugh    # 换音色
python pdf_speak.py book.pdf --pages 1-20 --out ch1.wav --no-play
python pdf_speak.py book.pdf --dry-run        # 只看断句结果，不发声
```

## 依赖

- Python 3.10+
- [Ollama](https://ollama.com/download) **0.40.11 或更新**，并已拉取模型：

  ```bash
  ollama pull phonon            # 英语（基础模型）
  ollama pull phonon:40m-fr     # 法语
  ollama pull phonon:40m-es     # 西班牙语，其他语言同理（-de / -pt）
  ```

  各语言模型是独立权重，**不装对应语言模型就只能用它读英语**。

- 语音功能目前**只支持 Apple Silicon 的 macOS**（Ollama 的语音能力尚未覆盖 Windows / Linux）

MP3 导出用 [`lameenc`](https://pypi.org/project/lameenc/)，它自带 LAME 编码器，**不需要系统装 ffmpeg**。

安装 Python 依赖（推荐用项目内虚拟环境，启动脚本会自动优先使用它）：

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

如果不想用 venv，也可以装到全局，但启动脚本会按 `$PYTHON` → `./.venv/bin/python` → PATH 里的 `python3` 的顺序，挑第一个真正装了依赖的解释器。

## 使用

```bash
# 只允许本机访问
python3 speak_server.py

# 允许同网络其他设备访问，启动时会打印可用的局域网地址
python3 speak_server.py --lan

# 同上，但要求访问令牌（自动生成，链接里带 ?t=）
python3 speak_server.py --lan --token
```

然后打开 <http://127.0.0.1:8765>，把要读的文字粘进文本框，点「添加并朗读」。macOS 上也可以直接双击 `启动朗读.command` 或 `局域网共享.command`。

首次以 `--lan` 启动时，macOS 会弹窗询问是否允许 Python 接受传入连接，需要选「允许」。

## Chrome 插件（仅本机）

在浏览器里选中文字，右键或点插件图标就能朗读，不必先打开网页再粘贴。插件只是前端壳，**语音合成仍由本机 Python 服务 + Ollama 完成，服务必须先启动**。

### 安装

1. 启动服务：`python3 speak_server.py`（或双击 `启动朗读.command`），确认 <http://127.0.0.1:8765> 能打开
2. 打开 `chrome://extensions`，右上角开启「开发者模式」
3. 点「加载已解压的扩展程序」，选择项目里的 `extension/` 目录
4. 可选：把插件固定到工具栏

插件权限为 `activeTab`、`scripting`、`storage`、`contextMenus`，以及访问 `http://127.0.0.1:8765/*`。

### 使用

| 操作 | 效果 |
|---|---|
| 网页上选中文字 → 右键「用本地朗读播放所选文字」 | 打开阅读器并朗读，文字追加进播放列表 |
| 点插件图标 → 「朗读所选文字」 | 同上 |
| 点插件图标 → 「朗读整页正文」 | 抓取 `article` / `main` / `body` 正文（上限 2 万字符）朗读 |
| 点插件图标 → 「打开完整阅读器」 | 打开完整界面，功能与网页版一致 |

阅读器是常驻标签页：播放列表、段落跳转、笔记、MP3 导出全部保留；再次选中新文字会追加到同一个列表，不会重复开新标签页。popup 顶部会显示服务是否在线。

### 为什么需要服务端配合

扩展页面的 origin 是 `chrome-extension://…`，请求 `127.0.0.1` 属于跨源 + 私有网络访问。服务端通过重写 `end_headers()` 统一注入 CORS / PNA 响应头，并按 Origin 白名单回显——只放行本机、局域网与已安装的扩展，陌生公网 Origin 不回显 `Access-Control-Allow-Origin`。

前端脚本拆成了 `web/app.js`（MV3 禁止内联脚本），`extension/config.js` 注入 `SPEAK_API_BASE` 把 `/api/…` 请求指向本机服务。**改动前端后需同步扩展副本**：

```bash
python3 extension/sync.py
```

### 插件的限制

- 端口固定 `8765`；改端口要同步改 `extension/config.js`、`extension/popup.js` 与 `manifest.json` 的 `host_permissions`
- 不支持 `--token` 模式（跨源请求默认不带 cookie）；本机无令牌模式可正常使用
- 自动打开阅读器时浏览器可能拦截自动播放，若没有声音点一下页面播放按钮即可
- `chrome://`、扩展商店等浏览器内部页面无法注入脚本，提不到选中文字或正文

## 多语言

Phonon 按语言分模型，**每个语言模型还有自己独立的音色库**：

| 语言 | 模型 | 音色数 |
|---|---|---|
| 英语 | `phonon` | 38（Zoey、Hugh、Grub the Ogre…）|
| 法语 | `phonon:40m-fr` | 16（Arnaud、Coralie、Hélène…）|
| 西班牙语 | `phonon:40m-es` | 26（Alba、Lucía-Sol、Nico…）|
| 德语 | `phonon:40m-de` | 独立一套 |
| 葡萄牙语 | `phonon:40m-pt` | 独立一套 |

**用英语模型读法语会明显不对劲**——那是拿英语音系去套法语发音。**当前默认不自动切换模型**：想读法语 / 西语时，在界面顶部的模型下拉里手动选对应模型即可，音色表会随之刷新；新增文档不会改动你的选择。

音色列表是跟着模型走的：英语的 `Zoey` 在法语模型上不存在，强行传会直接 400 `voice not found`。界面上换模型会自动刷新音色下拉。

## 工作原理

**文本处理**（`pdf_speak.py`，Web 版复用同一套逻辑）

PDF 里是一堆定位好的字形，不是句子，所以要先重建文本：

- 跨页重复出现的短行判为页眉页脚——不能只看首末行，pypdf 按内容流顺序抽文字，页眉可能落在正文中间
- 行尾断字连字符要拼回去（`hy-\nphenation` → `hyphenation`），行内连字符要保留（`well-known` 不能被拼成 `wellknown`）。判断只在行拼接阶段做，因为只有那时才知道连字符在不在行尾
- 断句前先保护 `Dr.` `e.g.` `Fig.` 这类缩写，否则会被句号切开
- 分块目标 240 字符，段落结尾停顿 0.45 秒、句末 0.28 秒

**播放**（`web/app.js`）

用 Web Audio API 分块排队播放，预取 3 块、提前 1.2 秒调度：

- **暂停**用 `AudioContext.suspend()`，时钟冻结，恢复时精确接上，不需要手动记录偏移
- **跳转**要同时停掉已调度的音源、作废旧调度循环、重置调度基准，漏一件就会新旧音频叠在一起
- **自动续播**必须复用已有的 `AudioContext`，新建的会被浏览器的自动播放策略挂起
- 判断"播完"不能只看所有块是否已排进队列——那比实际播完早，单块文档会瞬间跳到下一首

## 已知限制

- **只支持英语、法语、德语、西班牙语、葡萄牙语**。中文、日文等会念出乱码，程序里检测到 CJK 字符占比超过 3% 会直接报错退出
- 没有声音克隆，音色只能从固定库里选
- 扫描版 PDF（图片型）抽不到文字，需要先做 OCR
- 笔记存在浏览器 localStorage 里，按来源隔离：`127.0.0.1` 和局域网 IP 是两套，换设备也不同步

### 许可提醒

Phonon 的英语基础模型（`phonon`）是 Apache 2.0，但 40M 多语种模型是 **CC BY-NC 4.0，禁止商用**。要做收费产品，需要换成英语基础版或另谈授权。

## 文件结构

```
speak_server.py        Web 服务（仅标准库，无框架依赖），含 CORS / PNA 响应头
pdf_speak.py           文本处理 + 命令行朗读
make_icon.py           生成 favicon.ico（纯标准库）
web/index.html         前端页面骨架（样式内联）
web/app.js             前端逻辑（独立文件，MV3 CSP 要求）
web/favicon.svg        图标源文件
extension/             Chrome 插件（MV3，见上文）
  manifest.json        权限与入口
  popup.html/js        选中文字 / 整页正文 / 打开阅读器
  background.js        右键菜单、标签页复用、文本传递
  config.js            注入 SPEAK_API_BASE
  reader.html/app.js   完整阅读器（由 sync.py 从 web/ 同步）
  sync.py              改动前端后同步扩展副本
sample.txt             测试用纯文本（含断字、列表、分隔线）
sample.pdf             测试用 PDF（含页眉、页码、硬换行）
启动朗读.command        双击启动，仅本机
局域网共享.command      双击启动，同网络可访问
```

## 许可

代码 MIT。语音模型许可见上文。
