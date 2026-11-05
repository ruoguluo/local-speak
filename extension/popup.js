const API = 'http://127.0.0.1:8765';

const statusEl = document.getElementById('status');
const previewEl = document.getElementById('preview');
const selBtn = document.getElementById('sel');
const pageBtn = document.getElementById('page');
const openBtn = document.getElementById('open');

function setStatus(text, kind) {
  statusEl.textContent = text;
  statusEl.className = 'status ' + kind;
}

async function serviceOnline() {
  try {
    const res = await fetch(API + '/api/voices', { signal: AbortSignal.timeout(3000) });
    return res.ok;
  } catch (_) {
    return false;
  }
}

async function activeTabId() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab && tab.id;
}

async function getSelection() {
  const id = await activeTabId();
  if (!id) return '';
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: id },
      func: () => (window.getSelection() || '').toString().trim(),
    });
    return (res && res.result) || '';
  } catch (_) {
    return '';
  }
}

async function getPageText() {
  const id = await activeTabId();
  if (!id) return '';
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: id },
      func: () => {
        const root = document.querySelector('article, main, [role="main"]') || document.body;
        const seen = new Set();
        const lines = [];
        for (const raw of (root.innerText || '').split('\n')) {
          const line = raw.trim();
          if (line.length < 2 || seen.has(line)) continue;
          seen.add(line);
          lines.push(line);
        }
        return lines.join('\n\n').slice(0, 20000);
      },
    });
    return (res && res.result) || '';
  } catch (_) {
    return '';
  }
}

async function speak(text) {
  if (!text || !text.trim()) {
    setStatus('没有可朗读的文字，请先在网页上选中内容。', 'bad');
    return;
  }
  try {
    await chrome.runtime.sendMessage({ type: 'speak', text: text.trim() });
    window.close();
  } catch (e) {
    setStatus('发送失败：' + e.message, 'bad');
  }
}

selBtn.addEventListener('click', async () => {
  selBtn.disabled = true;
  await speak(await getSelection());
  selBtn.disabled = false;
});

pageBtn.addEventListener('click', async () => {
  pageBtn.disabled = true;
  const text = await getPageText();
  pageBtn.disabled = false;
  if (!text) {
    setStatus('这个页面没能提取到正文（可能是浏览器内部页面）。', 'bad');
    return;
  }
  await speak(text);
});

openBtn.addEventListener('click', async () => {
  try {
    await chrome.runtime.sendMessage({ type: 'open-reader' });
    window.close();
  } catch (e) {
    setStatus('打开失败：' + e.message, 'bad');
  }
});

(async () => {
  const online = await serviceOnline();
  if (!online) {
    setStatus('朗读服务未启动。请先运行 启动朗读.command 或 speak_server.py。', 'bad');
    return;
  }
  setStatus('服务已连接 · 可以朗读', 'ok');
  selBtn.disabled = false;
  pageBtn.disabled = false;

  const sel = await getSelection();
  if (sel) {
    previewEl.textContent = sel.length > 300 ? sel.slice(0, 300) + '…' : sel;
    previewEl.classList.remove('empty');
  } else {
    previewEl.textContent = '（未选中文字，可以改用「朗读整页正文」）';
  }
})();
