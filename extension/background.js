const READER_URL = chrome.runtime.getURL('reader.html');

async function getReaderTabId() {
  const { readerTabId } = await chrome.storage.local.get('readerTabId');
  if (!readerTabId) return null;
  try {
    await chrome.tabs.get(readerTabId);   // 标签页可能已被手动关闭
    return readerTabId;
  } catch (_) {
    await chrome.storage.local.remove('readerTabId');
    return null;
  }
}

async function createReader(pendingText) {
  if (pendingText) await chrome.storage.local.set({ pendingText });
  const tab = await chrome.tabs.create({ url: READER_URL });
  await chrome.storage.local.set({ readerTabId: tab.id });
}

async function focusTab(id) {
  await chrome.tabs.update(id, { active: true });
  const tab = await chrome.tabs.get(id);
  if (tab.windowId) await chrome.windows.update(tab.windowId, { focused: true });
}

async function openReader() {
  const id = await getReaderTabId();
  if (id) await focusTab(id);
  else await createReader(null);
}

async function deliverText(text) {
  if (!text || !text.trim()) return;
  const clean = text.trim();
  const id = await getReaderTabId();
  if (id) {
    // 阅读器已打开：文字直接送到该标签页，追加进播放列表
    try {
      await chrome.tabs.sendMessage(id, { type: 'speak:text', text: clean });
    } catch (_) {
      // 页面可能正在加载，退回 pendingText 由初始化逻辑读取
      await chrome.storage.local.set({ pendingText: clean });
    }
    await focusTab(id);
  } else {
    // 首次打开：先存好文本，reader.html 初始化时自动添加并朗读
    await createReader(clean);
  }
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: 'speak-selection',
    title: '用本地朗读播放所选文字',
    contexts: ['selection'],
  });
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId === 'speak-selection' && info.selectionText) {
    deliverText(info.selectionText);
  }
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    if (msg && msg.type === 'speak') await deliverText(msg.text);
    else if (msg && msg.type === 'open-reader') await openReader();
    sendResponse({ ok: true });
  })();
  return true;   // 保持消息通道，等异步处理完再回
});
