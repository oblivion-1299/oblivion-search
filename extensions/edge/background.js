/**
 * OBLIVION Search - Background Service Worker
 * Handles omnibox, context menus, keyboard shortcuts, and privacy counter.
 */

const SEARCH_URL = 'https://oblivionsearch.com/search?q=';
const HOME_URL = 'https://oblivionsearch.com/';

// ── Installation & Context Menu ──────────────────────────────────────────────

chrome.runtime.onInstalled.addListener((details) => {
  // Create context menu
  chrome.contextMenus.create({
    id: 'search-oblivion',
    title: chrome.i18n.getMessage('contextMenuSearch', '%s'),
    contexts: ['selection']
  });

  chrome.contextMenus.create({
    id: 'open-oblivion',
    title: chrome.i18n.getMessage('contextMenuOpen') || 'Open OBLIVION Search',
    contexts: ['page', 'frame']
  });

  // Initialize storage on first install
  if (details.reason === 'install') {
    chrome.storage.local.set({
      searchCount: 0,
      installDate: Date.now(),
      darkMode: true
    });
  }
});

// ── Context Menu Handler ─────────────────────────────────────────────────────

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === 'search-oblivion' && info.selectionText) {
    const query = encodeURIComponent(info.selectionText.trim());
    chrome.tabs.create({ url: `${SEARCH_URL}${query}&src=ctx` });
    incrementSearchCount();
  } else if (info.menuItemId === 'open-oblivion') {
    chrome.tabs.create({ url: HOME_URL });
  }
});

// ── Omnibox Handler ──────────────────────────────────────────────────────────

chrome.omnibox.onInputStarted.addListener(() => {
  chrome.omnibox.setDefaultSuggestion({
    description: 'Search OBLIVION for: %s'
  });
});

chrome.omnibox.onInputChanged.addListener((text, suggest) => {
  if (text.length < 2) return;

  // Provide suggestion hints
  suggest([
    {
      content: text,
      description: `Search OBLIVION for: <match>${escapeXml(text)}</match>`
    },
    {
      content: `${text} site:reddit.com`,
      description: `Search OBLIVION for: <match>${escapeXml(text)}</match> on Reddit`
    }
  ]);
});

chrome.omnibox.onInputEntered.addListener((text, disposition) => {
  const query = encodeURIComponent(text.trim());
  const url = `${SEARCH_URL}${query}&src=omni`;

  switch (disposition) {
    case 'currentTab':
      chrome.tabs.update({ url });
      break;
    case 'newForegroundTab':
      chrome.tabs.create({ url });
      break;
    case 'newBackgroundTab':
      chrome.tabs.create({ url, active: false });
      break;
  }

  incrementSearchCount();
});

// ── Privacy Counter ──────────────────────────────────────────────────────────

async function incrementSearchCount() {
  try {
    const data = await chrome.storage.local.get(['searchCount']);
    const count = (data.searchCount || 0) + 1;
    await chrome.storage.local.set({ searchCount: count });
    updateBadge(count);
  } catch (e) {
    // Silently handle storage errors
  }
}

function updateBadge(count) {
  if (count > 0) {
    const display = count >= 1000 ? `${Math.floor(count / 1000)}k` : String(count);
    chrome.action.setBadgeText({ text: display });
    chrome.action.setBadgeBackgroundColor({ color: '#8b5cf6' });
  }
}

// Restore badge on startup
chrome.runtime.onStartup.addListener(async () => {
  try {
    const data = await chrome.storage.local.get(['searchCount']);
    if (data.searchCount > 0) {
      updateBadge(data.searchCount);
    }
  } catch (e) {
    // Silently handle
  }
});

// ── Message Handler (from popup/newtab) ──────────────────────────────────────

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.type === 'search') {
    incrementSearchCount();
    sendResponse({ ok: true });
  } else if (message.type === 'getStats') {
    chrome.storage.local.get(['searchCount', 'installDate']).then((data) => {
      sendResponse({
        searchCount: data.searchCount || 0,
        installDate: data.installDate || Date.now()
      });
    });
    return true; // Keep channel open for async response
  }
});

// ── Utilities ────────────────────────────────────────────────────────────────

function escapeXml(str) {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
