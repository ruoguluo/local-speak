/* 扩展页面由 config.js 注入绝对地址；普通网页 / 局域网页面留空，继续走相对路径。 */
const API_BASE = window.SPEAK_API_BASE || '';

const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const AHEAD = 1.2;   // 提前调度多少秒音频
const PREFETCH = 3;  // 提前抓几块
let MODELS = [];     // 已安装的 phonon 变体，用于按语言自动切换
let BASE_MODEL = 'phonon';  // 英语基础模型（不带 40m- 后缀的那个）

const LANG_NAME = { fr: '法语', de: '德语', es: '西班牙语', pt: '葡萄牙语' };
const PASTE_HINT = '⌘ + Enter 也可以直接添加';

const S = {
  doc: null, voice: 'default', model: 'phonon',
  ctx: null, idx: 0, nextStart: 0, prefetch: new Map(),
  running: false, gen: 0, raf: 0, done: false, activeChunk: -1,
  ended: false,           // 真正播完了；done 只代表"所有块都排进队列了"，两者不是一回事
  sources: [],            // 已调度的音源，跳转时要逐个停掉
  durations: {},          // 各块真实时长，用来估算跳转位置
  offset: 0,              // 本次播放起点在整篇音频中的时间偏移
  t0: 0,                  // 重置 offset 时的 ctx.currentTime
  queue: [],              // 播放列表
  cur: -1,                // 当前项下标
  timeline: [],           // 已调度音频的时间区间，用来算"此刻在播哪一块"
  notes: [],              // 当前文档的笔记
};

function fmt(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  return String(Math.floor(sec / 60)).padStart(2, '0') + ':' + String(sec % 60).padStart(2, '0');
}

function showError(msg) {
  const el = $('#alert');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(showError.t);
  showError.t = setTimeout(() => el.classList.remove('show'), 8000);
}

function setIcon(kind) {
  const p = $('#playIcon');
  if (kind === 'pause') p.innerHTML = '<path d="M7 5h4v14H7zM13 5h4v14h-4z"/>';
  else if (kind === 'loading') p.innerHTML = '<path d="M12 4V2A10 10 0 0 0 2 12h2a8 8 0 0 1 8-8z" opacity=".4"/>';
  else p.innerHTML = '<path d="M8 5v14l11-7z"/>';
}

/* ---------- 播放列表 ---------- */

function b64utf8(s) {
  return btoa(String.fromCharCode(...new TextEncoder().encode(s)));
}

/* 语言模型不再自动切换：用户在下拉里选了哪个就用哪个，
   新增文档不会改动当前模型。保留空函数避免改动调用点。 */
async function applyLangModel(_data) {
  /* no-op: 不再根据文档语言自动切换模型 */
}

async function uploadFile(file) {
  const res = await fetch(API_BASE + '/api/open', {
    method: 'POST',
    headers: { 'X-Filename': b64utf8(file.name) },
    body: await file.arrayBuffer(),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || '解析失败');
  return { ...data, done: false };
}

async function addFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  unlockAudio();          // 趁手势还在，先把音频上下文解锁
  const btn = $('#addFileBtn');
  const label = btn.textContent;
  let firstAdded = -1;

  for (let n = 0; n < files.length; n++) {
    btn.textContent = `解析 ${n + 1}/${files.length}…`;
    try {
      const item = await uploadFile(files[n]);
      if (firstAdded < 0) firstAdded = S.queue.length;
      S.queue.push(item);
    } catch (e) {
      showError(`${files[n].name}：${e.message}`);
    }
  }

  btn.textContent = label;
  if (firstAdded < 0) return;
  renderPlaylist();
  await applyLangModel(S.queue[firstAdded]);
  await selectItem(firstAdded, { autoplay: true });
}

function updatePasteState() {
  const n = $('#pasteBox').value.trim().length;
  $('#addText').disabled = n === 0;   // 只要不是空的就能读，别再设字数门槛
  $('#charCount').textContent = n ? `${n} 字符` : '';
}

async function addText() {
  const box = $('#pasteBox');
  const text = box.value.trim();
  if (!text) return;
  unlockAudio();          // 趁手势还在，先把音频上下文解锁

  const btn = $('#addText');
  btn.disabled = true;
  btn.textContent = '添加中…';
  $('#pasteHint').textContent = PASTE_HINT;
  try {
    const res = await fetch(API_BASE + '/api/text', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || '添加失败');

    await applyLangModel(data);

    data.done = false;
    S.queue.push(data);
    box.value = '';
    updatePasteState();
    renderPlaylist();
    await selectItem(S.queue.length - 1, { autoplay: true });   // 粘完直接开读
  } catch (e) {
    showError(e.message);
  } finally {
    btn.textContent = '添加并朗读';
    updatePasteState();
  }
}

function renderPlaylist() {
  const box = $('#playlist');
  box.innerHTML = '';
  if (!S.queue.length) {
    const empty = document.createElement('div');
    empty.className = 'pl-empty';
    empty.textContent = '还没有内容。在右边粘贴一段文字。';
    box.append(empty);
    return;
  }

  S.queue.forEach((item, i) => {
    const row = document.createElement('div');
    row.className = 'pl-item' + (i === S.cur ? ' active' : '') + (item.done ? ' done' : '');
    row.title = item.name;

    const idx = document.createElement('span');
    idx.className = 'pl-idx';
    idx.textContent = item.done ? '✓' : String(i + 1);

    const body = document.createElement('div');
    body.className = 'pl-body';
    const name = document.createElement('div');
    name.className = 'pl-name';
    name.textContent = item.name;
    const meta = document.createElement('div');
    meta.className = 'pl-meta';
    meta.textContent = `${item.total} 段 · ${fmt(item.estSec)}`;
    body.append(name, meta);

    const del = document.createElement('button');
    del.className = 'pl-del';
    del.textContent = '×';
    del.title = '移出列表';
    del.addEventListener('click', (e) => {
      e.stopPropagation();
      removeItem(i);
    });

    row.append(idx, body, del);
    row.addEventListener('click', () => selectItem(i, { autoplay: true }));
    box.append(row);
  });
}

function clearDoc() {
  $('#playBtn').disabled = true;
  $('#download').disabled = true;
  $('#chunks').innerHTML = '';
  $('#segText').textContent = '第 0 / 0 段';
  $('#timeText').textContent = '00:00 / 约 00:00';
  $('#barFill').style.width = '0%';
  const det = document.querySelector('details');
  if (det) det.open = false;
  S.notes = [];
  renderNotes();
}

async function selectItem(i, { autoplay = false } = {}) {
  if (i < 0 || i >= S.queue.length) return;
  abortExport();          // 换文档时，上一份的导出没有意义了
  const reuseCtx = autoplay && !!S.ctx;   // 自动续播复用 ctx，否则会被自动播放策略拦住
  resetPlayer({ keepCtx: reuseCtx });
  S.cur = i;
  S.doc = S.queue[i];
  renderDoc();
  renderPlaylist();
  if (autoplay) await startPlayback(0, { reuseCtx });
}

function removeItem(i) {
  const wasCurrent = i === S.cur;
  S.queue.splice(i, 1);

  if (i < S.cur) {
    S.cur--;                       // 删的是前面的项，当前项下标前移
  } else if (wasCurrent) {
    resetPlayer();
    S.doc = null;
    if (S.queue.length) {
      S.cur = Math.min(i, S.queue.length - 1);
      S.doc = S.queue[S.cur];
      renderDoc();
    } else {
      S.cur = -1;
      clearDoc();
    }
  }
  renderPlaylist();
}

function onDocEnd() {
  if (S.ended) return;          // 幂等：waitThenAdvance 和 tick 兜底可能都会调到这里
  S.ended = true;
  if (S.cur >= 0 && S.queue[S.cur]) S.queue[S.cur].done = true;
  renderPlaylist();
  if (S.cur + 1 < S.queue.length) {
    selectItem(S.cur + 1, { autoplay: true });
  } else {
    setIcon('play');
    paintProgress();
  }
}

/* pump 结束只代表"所有块都排进队列了"，此时音频可能还在播。
   ctx.currentTime 在暂停时会冻结，所以这个循环天然也会等用户恢复。 */
async function waitThenAdvance(gen) {
  while (gen === S.gen && S.ctx && S.ctx.currentTime < S.nextStart - 0.02) {
    await sleep(80);
  }
  if (gen !== S.gen || !S.ctx) return;   // 期间被停止或跳转打断
  onDocEnd();
}

function renderDoc() {
  const d = S.doc;
  if (!d) return clearDoc();
  $('#playBtn').disabled = false;
  $('#download').disabled = false;
  $('#segText').textContent = `第 0 / ${d.total} 段`;
  $('#timeText').textContent = `00:00 / 约 ${fmt(d.estSec)}`;
  $('#barFill').style.width = '0%';
  loadNotes();
  renderNotes();

  const box = $('#chunks');
  box.innerHTML = '';
  d.chunks.forEach((c) => {
    const el = document.createElement('div');
    el.className = 'chunk' + (c.paraEnd ? ' para' : '');
    el.dataset.i = c.i;
    el.title = '点击从这一段开始播放';
    el.addEventListener('click', () => seekTo(c.i));
    const n = document.createElement('span');
    n.className = 'n';
    n.textContent = String(c.i + 1).padStart(2, '0');
    el.append(n, document.createTextNode(c.text));
    box.append(el);
  });
}

/* ---------- 笔记 ---------- */

const NOTES_PREFIX = 'speak.notes.';

/* 文档 id 每次上传都会变，所以用内容指纹做 key，重新拖同一个文件笔记还在 */
function noteKey(d) {
  return `${d.name}|${d.chars}|${d.total}`;
}

function loadNotes() {
  S.notes = [];
  if (!S.doc) return;
  try {
    const raw = localStorage.getItem(NOTES_PREFIX + noteKey(S.doc));
    if (raw) S.notes = JSON.parse(raw);
  } catch (_) {
    S.notes = [];
  }
}

let notesTimer = 0;
function saveNotes() {
  if (!S.doc) return;
  const doc = S.doc;
  clearTimeout(notesTimer);
  notesTimer = setTimeout(() => {
    try {
      localStorage.setItem(NOTES_PREFIX + noteKey(doc), JSON.stringify(S.notes));
    } catch (_) {}
  }, 200);
}

function elapsedNow() {
  if (!S.ctx) return 0;
  return S.offset + (S.ctx.currentTime - S.t0);
}

/* 正在出声的那一块（不是"下一块要调度的"） */
function currentChunk() {
  if (!S.ctx || !S.timeline.length) return Math.max(0, S.idx - 1);
  const t = S.ctx.currentTime;
  for (let k = S.timeline.length - 1; k >= 0; k--) {
    if (t >= S.timeline[k].start) return S.timeline[k].i;
  }
  return Math.max(0, S.idx - 1);
}

function addNote() {
  if (!S.doc) return;
  const note = { i: currentChunk(), t: elapsedNow(), text: '' };
  S.notes.push(note);
  S.notes.sort((a, b) => a.t - b.t);
  renderNotes({ focus: note });
  saveNotes();
}

function formatNotes() {
  const rows = S.notes.filter((n) => n.text.trim());
  if (!rows.length) return '';
  const head = `${S.doc.name} · ${rows.length} 条笔记`;
  return [head, ...rows.map((n) => `${fmt(n.t)}  ${n.text.trim()}`)].join('\n');
}

/* http://192.168.x.x 不是安全上下文，navigator.clipboard 根本不存在，必须留降级路径 */
function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;top:-1000px;left:0;opacity:0';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, ta.value.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } finally {
    document.body.removeChild(ta);
  }
  if (!ok) throw new Error('浏览器拒绝了复制请求');
}

/* 边收边显示进度，收完再触发下载——比让浏览器干等一个无 Content-Length 的响应更清楚。
   生成过程中再点一次按钮就是取消：中断请求会让服务端写 socket 失败，从而停止合成。 */
let exportCtl = null;

function abortExport() {
  if (exportCtl) {
    exportCtl.abort();
    exportCtl = null;
  }
}

async function downloadMp3() {
  if (!S.doc) return;
  const btn = $('#download');

  if (exportCtl) {          // 正在生成 → 这次点击是取消
    abortExport();
    return;
  }

  const label = '下载 MP3';
  const ctl = new AbortController();
  exportCtl = ctl;
  btn.classList.add('busy');
  btn.textContent = '取消生成';
  try {
    const q = `voice=${encodeURIComponent(S.voice)}&model=${encodeURIComponent(S.model)}`;
    const res = await fetch(`${API_BASE}/api/export/${S.doc.id}?${q}`, { signal: ctl.signal });
    if (!res.ok) {
      const e = await res.json().catch(() => ({ error: '导出失败' }));
      throw new Error(e.error);
    }
    const est = Number(res.headers.get('X-Est-Bytes') || 0) || S.doc.estSec * 8000;
    const reader = res.body.getReader();
    const parts = [];
    let got = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      parts.push(value);
      got += value.length;
      btn.textContent = `取消生成 ${Math.min(99, Math.round((got / est) * 100))}%`;
    }

    const blob = new Blob(parts, { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (S.doc.name.replace(/\.[^.]+$/, '') || 'audio') + '.mp3';
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    btn.textContent = '已下载';
  } catch (e) {
    if (e.name === 'AbortError') {
      btn.textContent = '已取消';
    } else {
      showError(`导出失败：${e.message}`);
      btn.textContent = label;
    }
  } finally {
    if (exportCtl === ctl) exportCtl = null;
    btn.classList.remove('busy');
    setTimeout(() => {
      btn.textContent = label;
      btn.disabled = !S.doc;
    }, 1400);
  }
}

async function copyNotes() {
  const text = formatNotes();
  if (!text) return showError('还没有可复制的笔记内容。');
  const btn = $('#copyNotes');
  try {
    if (window.isSecureContext && navigator.clipboard) {
      try {
        await navigator.clipboard.writeText(text);
      } catch (_) {
        legacyCopy(text);   // 权限被拒时也降级，别让用户卡住
      }
    } else {
      legacyCopy(text);
    }
    const label = btn.textContent;
    btn.textContent = '已复制';
    setTimeout(() => { btn.textContent = label; }, 1500);
  } catch (e) {
    showError(`复制失败：${e.message}`);
  }
}

function renderNotes(opts = {}) {
  const box = $('#notes');
  box.innerHTML = '';
  $('#noteCount').textContent = S.notes.length ? `(${S.notes.length})` : '';
  $('#addNote').disabled = !S.doc;
  $('#copyNotes').disabled = !S.doc || !S.notes.some((n) => n.text.trim());

  if (!S.notes.length) {
    const empty = document.createElement('div');
    empty.className = 'note-empty';
    empty.textContent = S.doc
      ? '还没有笔记。播放时点「＋ 记笔记」，会记下当前进度。'
      : '先在右边粘贴一段文字。';
    box.append(empty);
    return;
  }

  S.notes.forEach((note) => {
    const row = document.createElement('div');
    row.className = 'note';

    const time = document.createElement('button');
    time.className = 'note-time';
    time.textContent = fmt(note.t);
    time.title = '跳到这个位置';
    time.addEventListener('click', () => seekTo(note.i));

    const input = document.createElement('input');
    input.className = 'note-text';
    input.type = 'text';
    input.value = note.text;
    input.placeholder = '写点什么…';
    input.addEventListener('input', () => {
      note.text = input.value;
      $('#copyNotes').disabled = !S.notes.some((n) => n.text.trim());
      saveNotes();
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === 'Escape') input.blur();
    });

    const del = document.createElement('button');
    del.className = 'note-del';
    del.textContent = '×';
    del.title = '删除这条笔记';
    del.addEventListener('click', () => {
      S.notes = S.notes.filter((n) => n !== note);
      renderNotes();
      saveNotes();
    });

    row.append(time, input, del);
    box.append(row);
    if (opts.focus === note) setTimeout(() => input.focus(), 0);
  });
}

/* ---------- 播放 ---------- */

async function loadChunk(i, gen) {
  const q = `voice=${encodeURIComponent(S.voice)}&model=${encodeURIComponent(S.model)}`;
  const res = await fetch(`${API_BASE}/api/audio/${S.doc.id}/${i}?${q}`);
  if (!res.ok) {
    const e = await res.json().catch(() => ({ error: '音频加载失败' }));
    throw new Error(e.error);
  }
  if (gen !== S.gen) throw new Error('cancelled');
  return await S.ctx.decodeAudioData(await res.arrayBuffer());
}

function prefetchAhead(gen) {
  const end = Math.min(S.idx + PREFETCH, S.doc.total);
  for (let k = S.idx; k < end; k++) {
    if (!S.prefetch.has(k)) S.prefetch.set(k, loadChunk(k, gen));
  }
}

async function pump(gen) {
  while (S.running && gen === S.gen) {
    if (S.idx >= S.doc.total) {
      S.done = true;
      S.running = false;
      waitThenAdvance(gen);   // 等音频真正播完，不能只看"调度完"
      return;
    }
    if (S.nextStart > S.ctx.currentTime + AHEAD) {
      await sleep(60);
      continue;
    }
    prefetchAhead(gen);
    const pending = S.prefetch.get(S.idx);
    if (!pending) { await sleep(20); continue; }

    let buf;
    try {
      buf = await pending;
    } catch (e) {
      if (gen !== S.gen || e.message === 'cancelled') return;
      showError(`第 ${S.idx + 1} 段合成失败：${e.message}`);
      S.prefetch.delete(S.idx);
      S.idx++;
      continue;
    }
    if (gen !== S.gen) return;

    S.prefetch.delete(S.idx);
    const src = S.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(S.ctx.destination);
    const at = Math.max(S.nextStart, S.ctx.currentTime + 0.04);
    src.start(at);
    S.durations[S.idx] = buf.duration;
    S.sources.push(src);
    S.timeline.push({ start: at, end: at + buf.duration, i: S.idx });
    src.onended = () => {
      S.sources = S.sources.filter((x) => x !== src);
    };
    S.nextStart = at + buf.duration;
    S.idx++;
  }
}

/* 还没合成过的块用字数估时长，够用来摆进度条 */
function estimateStart(i) {
  let t = 0;
  for (let k = 0; k < i; k++) {
    t += S.durations[k] ?? (S.doc.chunks[k].text.length / 15 + 0.3);
  }
  return t;
}

function paintActive(i) {
  document.querySelectorAll('.chunk.active').forEach((el) => el.classList.remove('active'));
  const el = document.querySelector(`.chunk[data-i="${i}"]`);
  if (el) {
    el.classList.add('active');
    el.scrollIntoView({ block: 'nearest' });
  }
  S.activeChunk = i;
}

function paintProgress() {
  if (!S.ctx || !S.doc) return;
  const denom = Math.max(S.doc.estSec, S.offset + S.nextStart, 1);
  // ctx.currentTime 播完后还在继续走，不夹住的话时间会一直往前跑
  const raw = S.offset + (S.ctx.currentTime - S.t0);
  const elapsed = S.ended ? denom : Math.min(raw, denom);
  $('#barFill').style.width = Math.min(100, (elapsed / denom) * 100) + '%';
  $('#timeText').textContent = `${fmt(elapsed)} / 约 ${fmt(denom)}`;
  $('#segText').textContent = `第 ${Math.min(S.idx, S.doc.total)} / ${S.doc.total} 段`;
}

function tick() {
  if (!S.ctx) return;
  paintProgress();

  const cur = Math.min(S.idx - 1, S.doc.total - 1);
  if (cur !== S.activeChunk) paintActive(cur);

  // 兜底：调度早已完成、音频也确实播到了末尾，但收尾没跑到（waitThenAdvance 可能
  // 因为别的原因提前退出）。在这里补一次，保证按钮不会停在"暂停"。
  if (S.done && !S.ended && S.ctx.state === 'running' && S.ctx.currentTime >= S.nextStart) {
    onDocEnd();
  }

  S.raf = requestAnimationFrame(tick);
}

/* 必须在点击的同步阶段调用：Safari 要求 AudioContext 的创建与 resume 发生在
   用户手势内，等 await 完再建就已经脱离手势，会被挂起甚至让 resume 一直不 resolve。 */
function unlockAudio() {
  if (!S.ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    try { S.ctx = new AC(); } catch (_) { return; }
  }
  if (S.ctx.state === 'suspended') S.ctx.resume().catch(() => {});
}

async function startPlayback(from = 0, { reuseCtx = false } = {}) {
  // 自动续播时必须复用已有的 AudioContext，新建的会被自动播放策略挂起
  if (S.ctx && !reuseCtx) { S.ctx.close().catch(() => {}); S.ctx = null; }
  if (!S.ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    S.ctx = new AC();
  }
  if (S.ctx.state === 'suspended') {
    // resume 有可能永远不 resolve（没有用户手势时），加超时兜底，别把播放卡死
    try { await Promise.race([S.ctx.resume(), sleep(1500)]); } catch (_) {}
  }
  S.sources = [];
  S.timeline = [];
  S.idx = from;
  S.nextStart = 0;
  S.prefetch.clear();
  S.running = true;
  S.done = false;
  S.ended = false;
  S.activeChunk = -1;
  S.offset = estimateStart(from);
  S.t0 = S.ctx.currentTime;
  const gen = ++S.gen;
  setIcon('pause');
  cancelAnimationFrame(S.raf);
  tick();
  pump(gen);

  // 没有用户手势时浏览器会把 AudioContext 挂起，表现为"图标是暂停但没声音"
  if (S.ctx.state !== 'running') {
    setIcon('play');
    showError('浏览器拦截了自动播放，点一下播放按钮即可开始。');
  }
}

/* 跳转到第 i 段：先掐掉已排队的音源，再重置调度基准 */
function seekTo(i) {
  if (!S.doc) return;
  i = Math.max(0, Math.min(i, S.doc.total - 1));
  const playing = S.ctx && S.ctx.state === 'running';

  S.sources.forEach((s) => { try { s.stop(); } catch (_) {} });
  S.sources = [];
  S.timeline = [];
  S.gen++;                 // 让旧的 pump 循环退出
  S.prefetch.clear();
  S.running = false;
  S.done = false;
  S.ended = false;

  if (!S.ctx) {            // 还没开始播，就从这一段开始
    startPlayback(i);
    return;
  }

  S.idx = i;
  S.nextStart = 0;
  S.offset = estimateStart(i);
  S.t0 = S.ctx.currentTime;
  paintActive(i);
  paintProgress();

  if (playing) {
    const gen = ++S.gen;
    S.running = true;
    pump(gen);
  } else {
    setIcon('play');       // 保持暂停，下次播放从新位置开始
  }
}

function resetPlayer({ keepCtx = false } = {}) {
  S.running = false;
  S.gen++;
  S.sources.forEach((s) => { try { s.stop(); } catch (_) {} });
  S.sources = [];
  S.timeline = [];
  if (S.ctx && !keepCtx) {
    S.ctx.close().catch(() => {});
    S.ctx = null;
  }
  cancelAnimationFrame(S.raf);
  S.idx = 0;
  S.nextStart = 0;
  S.prefetch.clear();
  S.done = false;
  S.ended = false;
  S.activeChunk = -1;
  S.offset = 0;
  S.t0 = 0;
  setIcon('play');
  $('#barFill').style.width = '0%';
  document.querySelectorAll('.chunk.active').forEach((el) => el.classList.remove('active'));
  if (S.doc) {
    $('#timeText').textContent = `00:00 / 约 ${fmt(S.doc.estSec)}`;
    $('#segText').textContent = `第 0 / ${S.doc.total} 段`;
  }
}

async function onToggle() {
  if (!S.doc) return;
  if (!S.ctx) {
    await startPlayback(S.idx);
    return;
  }
  if (S.ended) {              // 真播完了才重播；done 时音频可能还在放尾巴
    await startPlayback(0);
    return;
  }
  if (S.ctx.state === 'running') {
    await S.ctx.suspend();   // 时钟冻结，恢复时精确接上
    setIcon('play');
  } else {
    await S.ctx.resume();
    setIcon('pause');
    if (!S.running && !S.done) {   // done 时没有可调度的块了，别再起泵
      const gen = ++S.gen;
      S.running = true;
      pump(gen);
    }
  }
}

/* ---------- 事件 ---------- */

$('#playBtn').addEventListener('click', onToggle);
$('#addNote').addEventListener('click', addNote);
$('#copyNotes').addEventListener('click', copyNotes);
$('#download').addEventListener('click', downloadMp3);

$('#addText').addEventListener('click', addText);
$('#addBtn').addEventListener('click', () => $('#pasteBox').focus());
$('#addFileBtn').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', (e) => {
  addFiles(e.target.files);
  e.target.value = '';
});

const inputCard = $('#inputCard');
['dragenter', 'dragover'].forEach((ev) =>
  inputCard.addEventListener(ev, (e) => {
    e.preventDefault();
    inputCard.classList.add('over');
  })
);
['dragleave', 'drop'].forEach((ev) =>
  inputCard.addEventListener(ev, (e) => {
    e.preventDefault();
    inputCard.classList.remove('over');
  })
);
inputCard.addEventListener('drop', (e) => {
  if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
});
$('#pasteBox').addEventListener('input', updatePasteState);
$('#pasteBox').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    addText();
  }
});

/* 自绘下拉：不依赖原生 select 弹层，WebView 里也能正常用 */
function makeDropdown(rootId, onPick) {
  const root = document.getElementById(rootId);
  const btn = root.querySelector('.dd-btn');
  const valueEl = root.querySelector('.dd-value');
  const menu = root.querySelector('.dd-menu');
  let options = [];
  let current = '';

  function render() {
    menu.innerHTML = '';
    options.forEach((opt) => {
      const item = document.createElement('div');
      item.className = 'dd-item' + (opt === current ? ' sel' : '');
      item.textContent = opt;
      item.setAttribute('role', 'option');
      item.addEventListener('click', () => pick(opt));
      menu.appendChild(item);
    });
  }

  function setOpen(open) {
    menu.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    if (open) {
      render();
      // 下方空间不够就向上翻，别让菜单跑出视口
      const rect = btn.getBoundingClientRect();
      const below = window.innerHeight - rect.bottom;
      root.classList.toggle('up', below < 280 && rect.top > below);
      const sel = menu.querySelector('.sel');
      if (sel) sel.scrollIntoView({ block: 'nearest' });
    }
  }

  function pick(opt) {
    current = opt;
    valueEl.textContent = opt;
    setOpen(false);
    onPick(opt);
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    setOpen(menu.hidden);
  });
  document.addEventListener('click', () => { if (!menu.hidden) setOpen(false); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !menu.hidden) setOpen(false); });

  return {
    setOptions(list, selected) {
      options = list;
      current = selected;
      valueEl.textContent = selected;
      render();
    },
  };
}

const voiceDD = makeDropdown('voiceDD', (v) => {
  S.voice = v;
  if (S.ctx) { resetPlayer(); showError('已切换音色，请重新播放。'); }
});

/* 音色库跟着模型走：英语模型 38 个音色，法语模型只有 16 个法语名字，
   沿用旧音色会直接 400（voice not found） */
async function loadVoices(model, preferred) {
  try {
    const res = await fetch(`${API_BASE}/api/voices?model=${encodeURIComponent(model)}`);
    const { voices } = await res.json();
    const pick = voices.includes(preferred)
      ? preferred
      : voices.includes('default') ? 'default' : voices[0];
    voiceDD.setOptions(voices, pick);
    S.voice = pick;

    const extras = voices.filter((v) => /Grub|Snow Queen|Ogre|Godmother/i.test(v));
    $('#voiceHint').textContent =
      `${voices.length} 个音色` +
      (extras.length ? `，其中有 ${extras.slice(0, 2).join('、')} 这类角色音色，读故事时挺有意思` : '') +
      '。';
    return voices;
  } catch (_) {
    return [];
  }
}

const modelDD = makeDropdown('modelDD', async (m) => {
  S.model = m;
  await loadVoices(m, S.voice);
  if (S.ctx) { resetPlayer(); showError('已切换模型，请重新播放。'); }
});

$('#preview').addEventListener('click', async () => {
  const btn = $('#preview');
  btn.textContent = '合成中…';
  try {
    const q = `voice=${encodeURIComponent(S.voice)}&model=${encodeURIComponent(S.model)}`;
    const res = await fetch(`${API_BASE}/api/preview?${q}`);
    if (!res.ok) {
      const e = await res.json().catch(() => ({ error: '试听失败' }));
      throw new Error(e.error);
    }
    const url = URL.createObjectURL(await res.blob());
    const audio = new Audio(url);
    audio.onended = () => URL.revokeObjectURL(url);
    await audio.play();
  } catch (e) {
    showError(e.message);
  } finally {
    btn.textContent = '试听';
  }
});

/* ---------- 初始化 ---------- */

(async () => {
  try {
    const res = await fetch(API_BASE + '/api/voices');
    const { models } = await res.json();
    MODELS = models;
    BASE_MODEL = models.find((m) => !m.includes('40m-')) || models[0];

    S.model = BASE_MODEL;
    modelDD.setOptions(models, BASE_MODEL);

    const voices = await loadVoices(S.model, 'default');
  } catch (e) {
    showError('连不上后端，确认 speak_server.py 还在运行。');
  }
  renderPlaylist();
  renderNotes();

  /* ---------- Chrome 扩展接入（普通网页里 chrome 不存在，自动跳过） ---------- */
  if (typeof chrome !== 'undefined' && chrome.storage && chrome.runtime) {
    const speakFromExtension = async (text) => {
      if (!text || !text.trim()) return;
      $('#pasteBox').value = text;
      updatePasteState();
      await addText();
    };

    // 首次打开阅读器：popup / 右键菜单把文本先存进 storage
    try {
      const { pendingText } = await chrome.storage.local.get('pendingText');
      if (pendingText) {
        await chrome.storage.local.remove('pendingText');
        await speakFromExtension(pendingText);
      }
    } catch (_) { /* storage 不可用时忽略，不影响手工粘贴 */ }

    // 阅读器已开着时，后续文字直接送到本标签页，追加进播放列表
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg && msg.type === 'speak:text' && msg.text) {
        speakFromExtension(msg.text).catch((e) => showError(e.message));
      }
    });
  }
})();